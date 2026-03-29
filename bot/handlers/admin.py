"""Админ-панель: пользователи, поиск, рефералы, подписка (вкл/выкл, +дни)."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.states.admin import (
    AdminBroadcastStates,
    AdminFactoryResetStates,
    AdminFindUserStates,
    AdminSubscriptionStates,
)
from bot.utils.screen_photo import answer_callback_with_photo_screen, send_profile_screen
from shared.config import get_settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.md2 import bold, code, esc, italic, join_lines, plain, strip_for_popup_alert
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.admin_user_delete import delete_user_from_app
from shared.services.factory_reset_service import wipe_all_application_data

_MSK_TZ = ZoneInfo("Europe/Moscow")
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin
from shared.services.broadcast_service import (
    MAX_MESSAGE_LEN,
    broadcast_to_users,
    collect_recipient_telegram_ids,
)
from shared.database import get_session_factory
from shared.services.referral_service import count_invited_users
from shared.services.subscription_service import (
    get_base_subscription_plan,
    update_rw_user_respecting_hwid_limit,
)

logger = logging.getLogger(__name__)

router = Router(name="admin")

PAGE_SIZE = 8


def _is_admin(tg_id: int | None) -> bool:
    if tg_id is None:
        return False
    return tg_id in get_settings().admin_telegram_ids


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📋 Пользователи", callback_data="admin:users:0"))
    b.row(InlineKeyboardButton(text="🔎 Найти по Telegram ID", callback_data="admin:find"))
    b.row(InlineKeyboardButton(text="📢 Рассылка всем", callback_data="admin:broadcast"))
    b.row(InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin:promos:page:0"))
    b.row(InlineKeyboardButton(text="⛔ Полный сброс БД", callback_data="admin:reset:start"))
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="menu:main"))
    return b.as_markup()


def _admin_reset_cancel_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Отмена сброса", callback_data="admin:reset:cancel"))
    return kb.as_markup()


def _norm_display_name(s: str) -> str:
    return (s or "").strip().casefold()


def _norm_username_typed(raw: str) -> str:
    t = (raw or "").strip()
    if t.startswith("@"):
        t = t[1:]
    return t.casefold()


def _list_button_label(u: User) -> str:
    status = "🚫" if u.is_blocked else "✅"
    name = (u.first_name or u.username or "?").strip()
    if len(name) > 18:
        name = name[:17] + "…"
    label = f"{status} #{u.id} {name}"
    return label[:64]


async def _try_delete_message(bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        logger.debug("admin delete_message failed chat=%s id=%s", chat_id, message_id, exc_info=True)


async def _admin_pick_subscription(
    session: AsyncSession, user_id: int
) -> tuple[Subscription | None, object | None]:
    """Активная подписка или последняя отключённая (cancelled) для действий в админке."""
    now = datetime.now(timezone.utc)
    r = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(("active", "trial")),
            Subscription.expires_at > now,
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    sub = r.scalar_one_or_none()
    if sub:
        return sub, sub.plan
    r2 = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(Subscription.user_id == user_id, Subscription.status == "cancelled")
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    sub2 = r2.scalar_one_or_none()
    if sub2:
        return sub2, sub2.plan
    return None, None


def _subscription_caption_lines(sub: Subscription, plan) -> list[str]:
    now = datetime.now(timezone.utc)
    pname = plan.name if plan is not None else "—"
    # Тип «триал» только по статусу записи; имя плана «Триал» при active — рассинхрон БД, не смешиваем с триалом
    is_trial = sub.status == "trial"
    kind = plain("триал") if is_trial else plain("платная")
    if sub.status == "cancelled":
        st = plain("отключена админом")
        left = plain("—")
    else:
        st = plain("активна")
        delta = sub.expires_at - now
        if delta.total_seconds() <= 0:
            left = plain("истекла")
        else:
            days, rem = divmod(int(delta.total_seconds()), 86400)
            hours = rem // 3600
            left = plain(f"~{days}д {hours}ч")
    exp_naive = sub.expires_at
    if exp_naive.tzinfo is None:
        exp_naive = exp_naive.replace(tzinfo=timezone.utc)
    exp_s = exp_naive.astimezone(_MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")
    return [
        plain("Тариф: ") + bold(pname) + plain(" · ") + kind + plain(" · ") + st,
        plain("До: ") + bold(exp_s),
        plain("Остаток: ") + left,
    ]


async def _render_admin_users_list_screen(
    cq: CallbackQuery,
    session: AsyncSession,
    *,
    page: int,
) -> None:
    settings = get_settings()
    total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    offset = page * PAGE_SIZE
    res = await session.execute(
        select(User).order_by(desc(User.id)).offset(offset).limit(PAGE_SIZE)
    )
    rows = list(res.scalars().all())

    lines = [
        "📋 " + bold("Пользователи"),
        plain(f"Стр. {page + 1} · всего записей: ") + bold(str(total)),
        "",
    ]
    b = InlineKeyboardBuilder()
    for u in rows:
        b.row(InlineKeyboardButton(text=_list_button_label(u), callback_data=f"admin:u:{u.id}"))
    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
    cur_page = page + 1
    page_label = f"{cur_page}/{total_pages}"
    placeholder = InlineKeyboardButton(text="·", callback_data="admin:users:noop")
    left_btn = (
        InlineKeyboardButton(text="⬅️", callback_data=f"admin:users:{page - 1}")
        if page > 0
        else placeholder
    )
    mid_btn = InlineKeyboardButton(text=page_label, callback_data="admin:users:noop")
    right_btn = (
        InlineKeyboardButton(text="➡️", callback_data=f"admin:users:{page + 1}")
        if offset + len(rows) < total
        else placeholder
    )
    b.row(left_btn, mid_btn, right_btn)
    b.row(InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:panel"))

    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(*lines),
        reply_markup=b.as_markup(),
        settings=settings,
    )


async def _build_user_card(
    session: AsyncSession,
    *,
    user_id: int,
    viewer_telegram_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup] | None:
    res = await session.execute(select(User).where(User.id == user_id))
    u = res.scalar_one_or_none()
    if u is None:
        return None

    bal = f"{u.balance:.2f}"
    reason = esc(u.block_reason or "—")
    full_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "—"
    invited = await count_invited_users(session, u.id)
    sub, plan = await _admin_pick_subscription(session, u.id)

    phone_s = esc((u.phone or "").strip()) if (u.phone or "").strip() else plain("—")
    rw_line = (
        plain("UUID: ") + code(str(u.remnawave_uuid))
        if u.remnawave_uuid is not None
        else plain("Панель VPN: ") + italic("не привязана")
    )
    sep = plain("────────────────────────")
    lines: list[str] = [
        "👤 " + bold(f"Карточка пользователя · #{u.id}"),
        sep,
        "📱 " + bold("Telegram"),
        plain("ID: ") + code(str(u.telegram_id)),
        plain("Username: ") + esc(u.username or "—"),
        plain("Имя: ") + esc(full_name),
        plain("Телефон: ") + phone_s,
        plain("Язык: ") + esc(u.language_code or "—"),
        sep,
        "🖥 " + bold("Remnawave"),
        rw_line,
        sep,
        "💳 " + bold("Баланс и рефералы"),
        plain("Баланс: ") + bold(bal) + plain(" ₽"),
        plain("Приглашено по ссылке: ") + bold(str(invited)),
        plain("Триал использован: ") + bold("да" if u.trial_used else "нет"),
        sep,
        "⚡ " + bold("Статус в боте"),
        plain("Аккаунт: ") + bold("заблокирован 🚫" if u.is_blocked else "активен ✅"),
        plain("Причина блока: ") + reason,
        sep,
    ]
    if sub is None:
        lines.append("📋 " + bold("Подписка"))
        lines.append(plain("Нет активной или отключённой записи для действий."))
    else:
        lines.append("📋 " + bold("Подписка"))
        lines.extend(_subscription_caption_lines(sub, plan))

    b = InlineKeyboardBuilder()
    if u.is_blocked:
        b.row(InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"admin:unblock:{u.id}"))
    else:
        b.row(InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"admin:block:{u.id}"))

    now = datetime.now(timezone.utc)
    if sub is not None:
        if sub.status in ("active", "trial") and sub.expires_at > now:
            b.row(
                InlineKeyboardButton(
                    text="⏹ Отключить подписку",
                    callback_data=f"admin:sd:{u.id}:{sub.id}",
                )
            )
        elif sub.status == "cancelled":
            b.row(
                InlineKeyboardButton(
                    text="▶️ Включить подписку",
                    callback_data=f"admin:se:{u.id}:{sub.id}",
                )
            )
        b.row(
            InlineKeyboardButton(
                text="➕ Добавить дни",
                callback_data=f"admin:ad:{u.id}:{sub.id}",
            )
        )

    b.row(
        InlineKeyboardButton(
            text="💳 Добавить баланс",
            callback_data=f"admin:ab:{u.id}",
        )
    )

    if viewer_telegram_id is not None and viewer_telegram_id != u.telegram_id:
        b.row(
            InlineKeyboardButton(
                text="🗑 Удалить пользователя",
                callback_data=f"admin:dask:{u.id}",
            )
        )

    b.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="admin:users:0"))
    return join_lines(*lines), b.as_markup()


async def _render_user_card(
    cq: CallbackQuery,
    session: AsyncSession,
    *,
    user_id: int,
) -> None:
    settings = get_settings()
    viewer = cq.from_user.id if cq.from_user else None
    built = await _build_user_card(session, user_id=user_id, viewer_telegram_id=viewer)
    if built is None:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    cap, kb = built
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=kb,
        settings=settings,
    )


@router.callback_query(F.data == "admin:panel")
async def cb_admin_panel(cq: CallbackQuery, db_user: User | None) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    settings = get_settings()
    text = join_lines(
        "🛠 " + bold("Админ-панель"),
        "",
        plain("Выберите действие."),
        "",
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=admin_panel_keyboard(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("admin:users:"))
async def cb_admin_users_page(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    parts = cq.data.split(":")
    if len(parts) >= 3 and parts[2] == "noop":
        await cq.answer()
        return
    try:
        page = int(parts[2])
    except (IndexError, ValueError):
        page = 0
    await _render_admin_users_list_screen(cq, session, page=page)


@router.callback_query(F.data.startswith("admin:u:"))
async def cb_admin_user_card(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    try:
        uid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Неверный id", show_alert=True)
        return
    await _render_user_card(cq, session, user_id=uid)


@router.callback_query(F.data.startswith("admin:dask:"))
async def cb_admin_delete_user_ask(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    try:
        uid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Неверный id", show_alert=True)
        return
    target = await session.get(User, uid)
    if target is None:
        await cq.answer("Не найден", show_alert=True)
        return
    if target.telegram_id == cq.from_user.id:
        await cq.answer("Нельзя удалить самого себя.", show_alert=True)
        return
    settings = get_settings()
    cap = join_lines(
        "⚠️ " + bold("Удаление пользователя"),
        "",
        plain("Учётная запись ")
        + bold(f"#{uid}")
        + plain(" будет удалена из бота: подписки, баланс, история."),
        plain("Если в профиле указан Remnawave, пользователь будет удалён и в панели."),
        "",
        plain("Продолжить?"),
    )
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin:dyes:{uid}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:u:{uid}"),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("admin:dyes:"))
async def cb_admin_delete_user_confirm(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    try:
        uid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Неверный id", show_alert=True)
        return
    target = await session.get(User, uid)
    if target is None:
        await cq.answer("Уже удалён или не найден.", show_alert=True)
        await _render_admin_users_list_screen(cq, session, page=0)
        return
    if target.telegram_id == cq.from_user.id:
        await cq.answer("Нельзя удалить самого себя.", show_alert=True)
        return
    settings = get_settings()
    ok, msg = await delete_user_from_app(session, user_id=uid, settings=settings)
    if not ok:
        plain_msg = strip_for_popup_alert(msg)
        await cq.answer(plain_msg[:200] + ("…" if len(plain_msg) > 200 else ""), show_alert=True)
        return
    await cq.answer("Удалено")
    await _render_admin_users_list_screen(cq, session, page=0)


@router.callback_query(F.data.startswith("admin:block:"))
async def cb_admin_block(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    try:
        uid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Неверный id", show_alert=True)
        return
    res = await session.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if u is None:
        await cq.answer("Не найден", show_alert=True)
        return
    u.is_blocked = True
    u.block_reason = u.block_reason or "Админ-панель"
    await session.commit()
    await _render_user_card(cq, session, user_id=uid)


@router.callback_query(F.data.startswith("admin:unblock:"))
async def cb_admin_unblock(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    try:
        uid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Неверный id", show_alert=True)
        return
    res = await session.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if u is None:
        await cq.answer("Не найден", show_alert=True)
        return
    u.is_blocked = False
    u.block_reason = None
    await session.commit()
    await _render_user_card(cq, session, user_id=uid)


def _parse_user_sub(callback_data: str) -> tuple[int, int] | None:
    parts = callback_data.split(":")
    if len(parts) < 4:
        return None
    try:
        return int(parts[2]), int(parts[3])
    except ValueError:
        return None


@router.callback_query(F.data.startswith("admin:sd:"))
async def cb_admin_sub_disable(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    parsed = _parse_user_sub(cq.data)
    if parsed is None:
        await cq.answer("Неверные данные", show_alert=True)
        return
    user_id, sub_id = parsed
    sub = await session.get(Subscription, sub_id)
    if sub is None or sub.user_id != user_id:
        await cq.answer("Подписка не найдена", show_alert=True)
        return
    now = datetime.now(timezone.utc)
    if sub.status not in ("active", "trial") or sub.expires_at <= now:
        await cq.answer("Нет активной подписки", show_alert=True)
        return
    sub.status = "cancelled"
    u = await session.get(User, user_id)
    settings = get_settings()
    if u is not None and u.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            await rw.update_user(str(u.remnawave_uuid), status="DISABLED")
        except RemnaWaveError as e:
            logger.warning("admin sub disable RW failed: %s", e)
    await session.commit()
    await cq.answer("Подписка отключена")
    await _render_user_card(cq, session, user_id=user_id)


@router.callback_query(F.data.startswith("admin:se:"))
async def cb_admin_sub_enable(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    parsed = _parse_user_sub(cq.data)
    if parsed is None:
        await cq.answer("Неверные данные", show_alert=True)
        return
    user_id, sub_id = parsed
    sub = (
        await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.id == sub_id)
        )
    ).scalar_one_or_none()
    if sub is None or sub.user_id != user_id:
        await cq.answer("Подписка не найдена", show_alert=True)
        return
    if sub.status != "cancelled":
        await cq.answer("Эта подписка не в статусе отключения", show_alert=True)
        return
    plan = sub.plan
    is_trial = plan is not None and plan.name == "Триал"
    sub.status = "trial" if is_trial else "active"
    if not is_trial:
        bp = await get_base_subscription_plan(session)
        if bp is not None:
            sub.plan_id = bp.id
    u = await session.get(User, user_id)
    settings = get_settings()
    if u is not None and u.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            await update_rw_user_respecting_hwid_limit(
                rw,
                str(u.remnawave_uuid),
                devices_limit_for_panel=sub.devices_count,
                expire_at=sub.expires_at,
                status="ACTIVE",
            )
        except RemnaWaveError as e:
            logger.warning("admin sub enable RW failed: %s", e)
    await session.commit()
    await cq.answer("Подписка включена")
    await _render_user_card(cq, session, user_id=user_id)


@router.callback_query(F.data.startswith("admin:ad:"))
async def cb_admin_add_days_start(
    cq: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    parsed = _parse_user_sub(cq.data)
    if parsed is None:
        await cq.answer("Неверные данные", show_alert=True)
        return
    user_id, sub_id = parsed
    sub = await session.get(Subscription, sub_id)
    if sub is None or sub.user_id != user_id:
        await cq.answer("Подписка не найдена", show_alert=True)
        return
    await state.set_state(AdminSubscriptionStates.waiting_add_days)
    await cq.answer()
    if cq.message and cq.bot:
        chat_id = cq.message.chat.id
        await _try_delete_message(cq.bot, chat_id, cq.message.message_id)
        sent = await cq.bot.send_message(
            chat_id,
            esc("Введите целое число дней для продления подписки (1-3650)."),
        )
        await state.update_data(
            admin_add_days_sub_id=sub_id,
            admin_add_days_user_id=user_id,
            admin_add_days_prompt_mid=sent.message_id,
        )
    else:
        await state.update_data(admin_add_days_sub_id=sub_id, admin_add_days_user_id=user_id)


@router.message(StateFilter(AdminSubscriptionStates.waiting_add_days), F.text)
async def msg_admin_add_days(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    if db_user is None:
        await state.clear()
        return
    data = await state.get_data()
    sub_id = data.get("admin_add_days_sub_id")
    user_id = data.get("admin_add_days_user_id")
    prompt_mid = data.get("admin_add_days_prompt_mid")

    async def _del_admin_input() -> None:
        if message.bot:
            await _try_delete_message(message.bot, message.chat.id, message.message_id)

    if not isinstance(sub_id, int) or not isinstance(user_id, int):
        await _del_admin_input()
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await _del_admin_input()
        await message.answer("Нужно целое число дней.")
        return
    days = int(raw)
    if days < 1 or days > 3650:
        await _del_admin_input()
        await message.answer("Допустимо от 1 до 3650 дней.")
        return

    sub = (
        await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.id == sub_id, Subscription.user_id == user_id)
        )
    ).scalar_one_or_none()
    if sub is None:
        await _del_admin_input()
        if message.bot and prompt_mid is not None:
            await _try_delete_message(message.bot, message.chat.id, int(prompt_mid))
        await state.clear()
        await message.answer("Подписка не найдена.")
        return

    await _del_admin_input()
    if message.bot and prompt_mid is not None:
        await _try_delete_message(message.bot, message.chat.id, int(prompt_mid))
    await state.clear()

    sub.expires_at = sub.expires_at + timedelta(days=days)
    pl = sub.plan
    if not (sub.status == "trial" and pl is not None and pl.name == "Триал"):
        bp = await get_base_subscription_plan(session)
        if bp is not None:
            sub.plan_id = bp.id
    if sub.status == "cancelled":
        pass
    u = await session.get(User, user_id)
    settings = get_settings()
    if u is not None and u.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            await update_rw_user_respecting_hwid_limit(
                rw,
                str(u.remnawave_uuid),
                devices_limit_for_panel=sub.devices_count,
                expire_at=sub.expires_at,
                status="ACTIVE",
            )
        except RemnaWaveError as e:
            logger.warning("admin add days RW failed: %s", e)

    await session.commit()
    viewer = message.from_user.id if message.from_user else None
    built = await _build_user_card(session, user_id=user_id, viewer_telegram_id=viewer)
    if built is None or message.bot is None:
        await message.answer(f"Добавлено дней: {days}")
        return
    cap, kb = built
    await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=join_lines(plain(f"✅ +{days} дн. к подписке"), "", cap),
        reply_markup=kb,
        settings=settings,
        delete_message=None,
    )


@router.callback_query(F.data.startswith("admin:ab:"))
async def cb_admin_add_balance_start(
    cq: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    try:
        uid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Неверный id", show_alert=True)
        return

    await state.set_state(AdminSubscriptionStates.waiting_add_balance)
    await cq.answer()
    if cq.message and cq.bot:
        chat_id = cq.message.chat.id
        await _try_delete_message(cq.bot, chat_id, cq.message.message_id)
        sent = await cq.bot.send_message(
            chat_id,
            esc("Введите сумму для добавления баланса (например 10 или 10.5)."),
        )
        await state.update_data(
            admin_add_balance_user_id=uid,
            admin_add_balance_prompt_mid=sent.message_id,
        )
    else:
        await state.update_data(admin_add_balance_user_id=uid)


@router.message(StateFilter(AdminSubscriptionStates.waiting_add_balance), F.text)
async def msg_admin_add_balance(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    if db_user is None:
        await state.clear()
        return

    data = await state.get_data()
    user_id = data.get("admin_add_balance_user_id")
    prompt_mid = data.get("admin_add_balance_prompt_mid")

    async def _del_admin_input() -> None:
        if message.bot:
            await _try_delete_message(message.bot, message.chat.id, message.message_id)

    if not isinstance(user_id, int):
        await _del_admin_input()
        await state.clear()
        return

    raw = (message.text or "").strip().replace(",", ".")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        await _del_admin_input()
        await message.answer("Нужно число, например 10 или 10.5.")
        return
    if amount <= 0:
        await _del_admin_input()
        await message.answer("Сумма должна быть > 0.")
        return

    u = await session.get(User, user_id)
    if u is None:
        await _del_admin_input()
        if message.bot and prompt_mid is not None:
            await _try_delete_message(message.bot, message.chat.id, int(prompt_mid))
        await state.clear()
        await message.answer("Пользователь не найден.")
        return

    await _del_admin_input()
    if message.bot and prompt_mid is not None:
        await _try_delete_message(message.bot, message.chat.id, int(prompt_mid))
    await state.clear()

    u.balance += amount
    session.add(
        Transaction(
            user_id=u.id,
            type="admin_balance_add",
            amount=amount,
            currency="RUB",
            payment_provider="admin",
            payment_id=None,
            status="completed",
            description=f"Админ добавил баланс: +{amount} ₽ (admin #{db_user.id})",
            meta={"admin_id": db_user.id},
        )
    )

    await session.commit()

    viewer = message.from_user.id if message.from_user else None
    built = await _build_user_card(session, user_id=user_id, viewer_telegram_id=viewer)
    settings = get_settings()
    await notify_admin(
        settings,
        title="💳 " + bold("Админ: пополнение баланса"),
        lines=[
            plain("Пользователь: ") + bold(f"#{u.id}") + plain(" tg ") + code(str(u.telegram_id)),
            plain("Сумма: ") + bold(str(amount)) + plain(" ₽"),
            plain("Админ: ") + bold(f"#{db_user.id}"),
        ],
        event_type="admin_balance_add",
        topic=AdminLogTopic.PAYMENTS,
        subject_user=u,
        session=None,
    )

    if built is None or message.bot is None:
        await message.answer(f"Баланс добавлен: +{amount} ₽")
        return

    cap, kb = built
    await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=join_lines(plain(f"✅ Баланс пополнен на +{amount} ₽"), "", cap),
        reply_markup=kb,
        settings=settings,
        delete_message=None,
    )


@router.callback_query(F.data == "admin:find")
async def cb_admin_find_start(
    cq: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    prev = await state.get_data()
    old_prompt = prev.get("find_prompt_mid")
    if cq.bot and cq.message and old_prompt:
        await _try_delete_message(cq.bot, cq.message.chat.id, int(old_prompt))

    await state.set_state(AdminFindUserStates.waiting_telegram_id)
    settings = get_settings()
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:find_cancel"))
    sent = await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "🔎 " + bold("Поиск по Telegram ID"),
            "",
            plain("Отправьте числом Telegram ID пользователя."),
        ),
        reply_markup=b.as_markup(),
        settings=settings,
    )
    new_mid = sent.message_id if sent else None
    await state.update_data(
        find_prompt_mid=new_mid,
        find_last_result_mid=prev.get("find_last_result_mid"),
    )


@router.callback_query(F.data == "admin:find_cancel")
async def cb_admin_find_cancel(
    cq: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    data = await state.get_data()
    await state.clear()
    if cq.bot and cq.message:
        pm = data.get("find_prompt_mid")
        await _try_delete_message(cq.bot, cq.message.chat.id, int(pm) if pm is not None else None)
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await cb_admin_panel(cq, db_user)


@router.message(StateFilter(AdminFindUserStates.waiting_telegram_id), F.text)
async def msg_admin_find_telegram_id(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    if db_user is None:
        await state.clear()
        return
    data = await state.get_data()
    prompt_mid = data.get("find_prompt_mid")
    last_res = data.get("find_last_result_mid")

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(esc("Нужно целое число (Telegram ID)."))
        return

    if message.bot:
        await _try_delete_message(message.bot, message.chat.id, message.message_id)
        await _try_delete_message(
            message.bot, message.chat.id, int(prompt_mid) if prompt_mid is not None else None
        )
        await _try_delete_message(
            message.bot, message.chat.id, int(last_res) if last_res is not None else None
        )

    tg_id = int(raw)
    res = await session.execute(select(User).where(User.telegram_id == tg_id))
    u = res.scalar_one_or_none()
    settings = get_settings()

    if u is None:
        sent = await message.answer(
            join_lines(plain("Не найден пользователь с ") + code(str(tg_id)), "", plain("/admin"))
        )
        await state.update_data(find_last_result_mid=sent.message_id, find_prompt_mid=None)
        await state.set_state(None)
        return

    un = esc(u.username or "—")
    line_user = plain(f"#{u.id} · tg ") + code(str(u.telegram_id))
    if u.username:
        line_user += plain(" · @") + un
    invited = await count_invited_users(session, u.id)
    sub, plan = await _admin_pick_subscription(session, u.id)
    extra: list[str] = [plain("Пригласил: ") + bold(str(invited)), ""]
    if sub is None:
        extra.append(plain("Подписка: ") + bold("нет"))
    else:
        extra.extend(_subscription_caption_lines(sub, plan))

    full_nm = f"{u.first_name or ''} {u.last_name or ''}".strip() or "—"
    ph_ln = (
        plain("Телефон: ") + esc((u.phone or "").strip())
        if (u.phone or "").strip()
        else plain("Телефон: —")
    )
    rw_ln = (
        plain("RemnaWave: ") + code(str(u.remnawave_uuid))
        if u.remnawave_uuid is not None
        else plain("RemnaWave: —")
    )
    lines = [
        "🔎 " + bold("Найден"),
        line_user,
        plain("Имя: ") + esc(full_nm),
        ph_ln,
        rw_ln,
        plain("Баланс: ") + bold(f"{u.balance:.2f}") + plain(" ₽"),
        "",
        *extra,
    ]
    adm = InlineKeyboardBuilder()
    adm.row(InlineKeyboardButton(text="🛠 Карточка", callback_data=f"admin:u:{u.id}"))
    adm.row(InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:panel"))
    if message.bot is None:
        return
    sent2 = await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=join_lines(*lines),
        reply_markup=adm.as_markup(),
        settings=settings,
        delete_message=None,
    )
    await state.update_data(find_last_result_mid=sent2.message_id, find_prompt_mid=None)
    await state.set_state(None)


@router.message(F.text == "/admin")
async def cmd_admin(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    if db_user is None:
        await message.answer(esc("Сначала /start"))
        return
    settings = get_settings()
    text = join_lines(
        "🛠 " + bold("Админ-панель"),
        "",
        plain("Выберите действие или используйте кнопку в профиле."),
        "",
    )
    await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=text,
        reply_markup=admin_panel_keyboard(),
        settings=settings,
        delete_message=None,
    )


@router.callback_query(F.data == "admin:reset:start")
async def cb_admin_reset_start(
    cq: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await state.clear()
    settings = get_settings()
    warn = join_lines(
        "⛔ " + bold("Полный сброс базы данных"),
        "",
        plain("Будут удалены все пользователи, подписки, устройства, транзакции, промокоды, планы и прочие данные бота."),
        plain("Настройки в .env и аккаунт RemnaWave не трогаются."),
        "",
        italic("Дальше нужно трижды подтвердить личность: имя в Telegram, username без @ и числовой Telegram ID."),
        "",
        plain("Если вы нажали случайно — «Отмена»."),
    )
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Продолжить к проверкам", callback_data="admin:reset:proceed"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:reset:cancel"))
    await cq.answer()
    await answer_callback_with_photo_screen(
        cq,
        caption=warn,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "admin:reset:proceed")
async def cb_admin_reset_proceed(
    cq: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    fu = cq.from_user
    fn_cf = _norm_display_name(fu.first_name or "")
    un_cf = _norm_username_typed(fu.username or "")
    await state.set_state(AdminFactoryResetStates.waiting_first_name)
    await state.update_data(
        reset_fn_cf=fn_cf,
        reset_un_cf=un_cf,
        reset_tid=fu.id,
    )
    await cq.answer()
    if cq.bot is None or cq.message is None:
        return
    hint_un = (
        plain("У вас в Telegram не задан username. На следующем шаге отправьте ")
        + code("-")
        + plain(".")
        if not un_cf
        else plain("")
    )
    step1 = join_lines(
        "1/3 " + bold("Имя в Telegram"),
        "",
        plain("Отправьте одним сообщением имя так, как оно указано в вашем профиле Telegram (поле «Имя»)."),
        plain("Пример: если в профиле написано «Enzy» — отправьте именно это, без фамилии."),
        hint_un,
        "",
        plain("Если имени в профиле нет, отправьте ")
        + code("-")
        + plain("."),
    )
    await cq.bot.send_message(
        cq.message.chat.id,
        step1,
        reply_markup=_admin_reset_cancel_markup(),
    )


@router.callback_query(F.data == "admin:reset:cancel")
async def cb_admin_reset_cancel(
    cq: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    await state.clear()
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Отменено.", show_alert=True)
        return
    await cq.answer("Сброс отменён.")
    if db_user is None:
        return
    settings = get_settings()
    text = join_lines(
        "🛠 " + bold("Админ-панель"),
        "",
        plain("Выберите действие."),
        "",
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=admin_panel_keyboard(),
        settings=settings,
    )


def _reset_first_name_ok(expected_cf: str, typed: str) -> bool:
    t = _norm_display_name(typed)
    if expected_cf == "":
        return t in ("-", "—", "нет", "пусто")
    return t == expected_cf


def _reset_username_ok(expected_cf: str, typed: str) -> bool:
    t = _norm_username_typed(typed)
    if expected_cf == "":
        return t in ("-", "—", "нет", "пусто")
    return t == expected_cf


@router.message(StateFilter(AdminFactoryResetStates.waiting_first_name), F.text)
async def msg_admin_reset_step_first_name(
    message: Message,
    state: FSMContext,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    exp = data.get("reset_fn_cf")
    if not isinstance(exp, str):
        await state.clear()
        return
    raw = message.text or ""
    if not _reset_first_name_ok(exp, raw):
        await message.answer(
            esc("Имя не совпадает. Отправьте имя из профиля Telegram (как в настройках «Имя»)."),
            reply_markup=_admin_reset_cancel_markup(),
        )
        return
    await state.set_state(AdminFactoryResetStates.waiting_username)
    un_hint = (
        join_lines(
            "2/3 " + bold("Username в Telegram"),
            "",
            plain("Отправьте username без символа @ — только латиница, цифры и подчёркивание."),
            plain("Пример: для @enzy_dmitriev отправьте enzy_dmitriev"),
            "",
            plain("Если username не задан, отправьте ")
            + code("-")
            + plain("."),
        )
        if data.get("reset_un_cf")
        else join_lines(
            "2/3 " + bold("Username в Telegram"),
            "",
            plain("У вас не задан username. Отправьте ")
            + code("-")
            + plain("."),
        )
    )
    await message.answer(un_hint, reply_markup=_admin_reset_cancel_markup())


@router.message(StateFilter(AdminFactoryResetStates.waiting_username), F.text)
async def msg_admin_reset_step_username(
    message: Message,
    state: FSMContext,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    exp = data.get("reset_un_cf")
    if not isinstance(exp, str):
        await state.clear()
        return
    raw = message.text or ""
    if not _reset_username_ok(exp, raw):
        await message.answer(
            esc("Username не совпадает. Без @, в нижнем регистре не обязательно — регистр игнорируется."),
            reply_markup=_admin_reset_cancel_markup(),
        )
        return
    await state.set_state(AdminFactoryResetStates.waiting_telegram_numeric_id)
    await message.answer(
        join_lines(
            "3/3 " + bold("Числовой Telegram ID"),
            "",
            plain("Отправьте только цифры вашего Telegram ID, без пробелов."),
            plain("Пример: 883400626"),
        ),
        reply_markup=_admin_reset_cancel_markup(),
    )


@router.message(StateFilter(AdminFactoryResetStates.waiting_telegram_numeric_id), F.text)
async def msg_admin_reset_step_telegram_id(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    exp_id = data.get("reset_tid")
    if not isinstance(exp_id, int):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) != exp_id:
        await message.answer(
            esc("ID не совпадает. Нужен ваш числовой Telegram ID (можно узнать у @userinfobot и др.)."),
            reply_markup=_admin_reset_cancel_markup(),
        )
        return
    await state.clear()
    try:
        await wipe_all_application_data(session)
        await session.commit()
    except Exception:
        logger.exception("factory reset failed")
        await session.rollback()
        await message.answer(esc("Ошибка при очистке БД. Данные не тронуты."))
        return
    logger.warning("factory reset completed by telegram_id=%s", exp_id)
    await message.answer(
        join_lines(
            "✅ " + bold("База данных очищена."),
            "",
            plain("Все записи приложения удалены. Ваш пользователь в боте тоже удалён."),
            plain("Отправьте /start, чтобы зарегистрироваться заново."),
        ),
    )


def _broadcast_confirm_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✅ Отправить всем", callback_data="admin:broadcast_go"))
    kb.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:broadcast_cancel"))
    return kb.as_markup()


@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast_start(
    cq: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await state.set_state(AdminBroadcastStates.waiting_text)
    await cq.answer()
    if cq.bot is None or cq.message is None:
        return
    await cq.bot.send_message(
        cq.message.chat.id,
        join_lines(
            "📢 " + bold("Рассылка всем пользователям"),
            "",
            plain("Отправьте одним сообщением текст. Поддерживается форматирование "),
            plain("как в Telegram "),
            plain("(жирный, курсив, ссылки через меню сообщения) "),
            plain("и HTML-теги вручную."),
            "",
            plain("Примеры HTML: ")
            + code("<b>жирный</b>")
            + plain(", ")
            + code("<i>курсив</i>")
            + plain(", ")
            + code("<u>подчёркнутый</u>")
            + plain(", "),
            plain("ссылка: ")
            + code('<a href="https://example.com">текст</a>')
            + plain(", код: ")
            + code("<code>фрагмент</code>")
            + plain("."),
            "",
            plain("Смайлики можно вставлять как обычно. До ")
            + code(str(MAX_MESSAGE_LEN))
            + plain(" символов в итоговом сообщении."),
            "",
            italic("Не получат пользователи, отмеченные в боте как заблокированные."),
            "",
            plain("Отмена: ") + code("/cancel_broadcast"),
        ),
    )


@router.message(Command("cancel_broadcast"), StateFilter(AdminBroadcastStates))
async def cmd_cancel_broadcast(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(esc("Рассылка отменена."))


@router.callback_query(F.data == "admin:broadcast_cancel")
async def cb_broadcast_cancel(
    cq: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    await state.clear()
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    await cq.answer("Отменено.")
    if db_user is None:
        return
    settings = get_settings()
    text = join_lines(
        "🛠 " + bold("Админ-панель"),
        "",
        plain("Выберите действие."),
        "",
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=admin_panel_keyboard(),
        settings=settings,
    )


@router.message(StateFilter(AdminBroadcastStates.waiting_text), F.text)
async def msg_broadcast_receive_text(
    message: Message,
    state: FSMContext,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    if (message.text or "").strip().startswith("/"):
        await message.answer(
            esc("Пришлите текст рассылки без команд или отмените: /cancel_broadcast")
        )
        return

    # Сохраняем форматирование из клиента Telegram (жирный и т.д.) → HTML
    raw = (getattr(message, "html_text", None) or message.text or "").strip()
    if not raw:
        await message.answer(esc("Текст пустой. Отправьте непустое сообщение."))
        return
    if len(raw) > MAX_MESSAGE_LEN:
        await message.answer(
            esc(f"Слишком длинно. Максимум {MAX_MESSAGE_LEN} символов. Сократите и отправьте снова.")
        )
        return

    factory = get_session_factory()
    async with factory() as session:
        n = len(await collect_recipient_telegram_ids(session, skip_blocked=True))

    preview = raw if len(raw) <= 800 else raw[:797] + "..."
    footer = (
        f"\n\n➖➖➖➖➖\n"
        f"Получателей (не в блок-листе бота): <b>{n}</b>\n"
        f"<i>Подтвердите отправку кнопками ниже.</i>"
    )
    preview_html = f"<b>Предпросмотр рассылки</b>\n\n{preview}{footer}"
    try:
        await message.answer(
            preview_html,
            parse_mode=ParseMode.HTML,
            reply_markup=_broadcast_confirm_markup(),
        )
    except TelegramBadRequest:
        await message.answer(
            esc(
                "Telegram не принял разметку: проверьте парные теги "
                "(<b>, <i>, <a>), кавычки в href и спецсимволы < и & в тексте "
                "(замените на &lt; и &amp;). Отправьте исправленный текст."
            )
        )
        return

    await state.update_data(broadcast_text=raw)
    await state.set_state(AdminBroadcastStates.waiting_confirm)


@router.callback_query(F.data == "admin:broadcast_go", StateFilter(AdminBroadcastStates.waiting_confirm))
async def cb_broadcast_go(
    cq: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    await state.clear()
    if not isinstance(text, str) or not text.strip():
        await cq.answer("Нет текста. Начните снова.", show_alert=True)
        return
    if cq.bot is None:
        await cq.answer("Ошибка бота.", show_alert=True)
        return

    await cq.answer("Идёт рассылка…")
    if cq.message:
        try:
            await cq.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    ok, fail = await broadcast_to_users(cq.bot, text)

    settings = get_settings()
    summary = join_lines(
        "✅ " + bold("Рассылка завершена"),
        "",
        plain("Доставлено: ") + bold(str(ok)),
        plain("Не доставлено: ") + bold(str(fail)),
        "",
        italic("(Не доставлено: бот заблокирован, аккаунт удалён, лимиты Telegram и т.п.)"),
    )
    if cq.message:
        await cq.message.answer(summary)
    if db_user is not None:
        await notify_admin(
            settings,
            title="📢 " + bold("Массовая рассылка"),
            lines=[
                plain("Доставлено: ") + bold(str(ok)) + plain(", ошибок: ") + bold(str(fail)),
            ],
            event_type="broadcast",
            topic=AdminLogTopic.GENERAL,
            subject_user=db_user,
            session=None,
        )
