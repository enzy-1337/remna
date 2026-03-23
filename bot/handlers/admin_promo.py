"""Админ-управление промокодами (создание/редактирование/просмотр)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.states.admin_promo import AdminPromoStates
from bot.utils.screen_photo import answer_callback_with_photo_screen, delete_message_safe
from shared.config import get_settings
from shared.md2 import bold, code, esc, join_lines, plain, strip_for_popup_alert
from shared.models.promo import PromoCode
from shared.models.user import User

from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin

logger = logging.getLogger(__name__)

router = Router(name="admin_promo")

PAGE_SIZE = 8


async def _send_and_track(
    state: FSMContext,
    target: Message,
    text: str,
    reply_markup=None,
) -> Message:
    sent = await target.answer(esc(text), reply_markup=reply_markup)
    await state.update_data(prompt_mid=sent.message_id)
    return sent


def _is_admin(tg_id: int | None) -> bool:
    if tg_id is None:
        return False
    return tg_id in get_settings().admin_telegram_ids


def _parse_date_any(raw: str) -> datetime | None:
    t = (raw or "").strip()
    if not t or t == "-":
        return None
    # Разрешаем YYYY-MM-DD и DD.MM.YYYY
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(t, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError("Неверный формат даты. Используйте YYYY-MM-DD или DD.MM.YYYY.")


def _format_expires(promo: PromoCode) -> str:
    if promo.expires_at is None:
        return "∞"
    # Ожидаем, что expires_at выставляется как 00:00 UTC (см. админ-панель)
    return promo.expires_at.strftime("%d.%m.%Y")


def _promo_reward_caption(promo: PromoCode) -> str:
    v = promo.value
    if promo.type in ("balance_rub", "bonus_rub"):
        return f"+{v} ₽"
    if promo.type == "subscription_days":
        return f"+{v} дн. (если есть подписка)"
    if promo.type == "topup_bonus_percent":
        return f"+{v}% к первому пополнению"
    return f"+{v}"


def _promo_details_lines(promo: PromoCode, created_by: User | None) -> list[str]:
    base: list[str] = [
        plain("Промокод: ") + code(promo.code),
        plain("Тип: ") + code(promo.type),
        plain("Награда: ") + bold(_promo_reward_caption(promo)),
        plain("Активен: ") + bold("да" if promo.is_active else "нет"),
        plain("Срок (до): ") + bold(_format_expires(promo)),
        plain("Лимит активаций: ")
        + bold("∞" if promo.max_uses is None else str(promo.max_uses)),
        plain("Активирован: ") + bold(str(promo.used_count)),
        plain("Создан: ") + bold(promo.created_at.strftime("%d.%m.%Y %H:%M UTC")),
    ]
    if created_by is not None:
        base.append(
            plain("Создал админ: ")
            + bold(f"#{created_by.id}")
            + plain(" · tg ")
            + code(str(created_by.telegram_id))
        )
    else:
        base.append(plain("Создал админ: —"))

    if promo.type == "subscription_days":
        fb = promo.fallback_value_rub
        base.append(
            plain("Фолбэк (деньги при отсутствии подписки): ")
            + bold(str(fb or Decimal("0")))
            + plain(" ₽")
        )

    return base


def _list_button_label(promo: PromoCode) -> str:
    status = "✅" if promo.is_active else "🚫"
    exp = _format_expires(promo)
    limit = "∞" if promo.max_uses is None else str(promo.max_uses)
    v = promo.value
    if promo.type == "subscription_days":
        r = f"{v}дн"
    elif promo.type == "topup_bonus_percent":
        r = f"{v}%"
    else:
        r = f"{v}₽"
    s = f"{status} {promo.code} · {r} · до {exp} ·/{limit}"
    return s[:64]


def _cancel_keyboard(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="❌ Отмена", callback_data=f"{prefix}:cancel"
        )
    )
    return b.as_markup()


def _type_select_keyboard(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="📆 Дни подписки + фолбэк деньгами",
            callback_data=f"{prefix}:type:subscription_days",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="💰 Деньги на баланс",
            callback_data=f"{prefix}:type:balance_rub",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="📈 % к первому пополнению (1 раз)",
            callback_data=f"{prefix}:type:topup_bonus_percent",
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:promos:page:0"))
    return b.as_markup()


def _active_select_keyboard(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🟢 Активен", callback_data=f"{prefix}:active:true"),
        InlineKeyboardButton(text="⚪️ Неактивен", callback_data=f"{prefix}:active:false"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:promos:page:0"))
    return b.as_markup()


async def _try_delete_by_id(bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def _render_promos_list(
    cq: CallbackQuery,
    session: AsyncSession,
    *,
    page: int,
) -> None:
    settings = get_settings()
    total = (await session.execute(select(func.count()).select_from(PromoCode))).scalar_one()
    offset = page * PAGE_SIZE
    res = await session.execute(
        select(PromoCode).order_by(desc(PromoCode.id)).offset(offset).limit(PAGE_SIZE)
    )
    promos = list(res.scalars().all())

    lines = [
        "🎁 " + bold("Промокоды (все)"),
        plain(f"Стр. {page + 1} · всего: ") + bold(str(total)),
        "",
    ]

    b = InlineKeyboardBuilder()
    for p in promos:
        b.row(InlineKeyboardButton(text=_list_button_label(p), callback_data=f"admin:promos:view:{p.id}"))

    total_pages = max(1, (int(total) + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
    left = InlineKeyboardButton(text="⬅️", callback_data="admin:promos:page:0")
    right = InlineKeyboardButton(text="➡️", callback_data=f"admin:promos:page:{total_pages-1}")
    if page > 0:
        left = InlineKeyboardButton(text="⬅️", callback_data=f"admin:promos:page:{page-1}")
    if page + 1 < total_pages:
        right = InlineKeyboardButton(text="➡️", callback_data=f"admin:promos:page:{page+1}")
    b.row(left, right)

    b.row(InlineKeyboardButton(text="➕ Создать", callback_data="admin:promos:create"))
    b.row(InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:panel"))

    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(*lines),
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "admin:promos:create")
async def cb_promos_create_start(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not (is_bot_admin or _is_admin(cq.from_user.id)):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminPromoStates.create_waiting_code)

    kb = _cancel_keyboard("admin:promos")
    await cq.answer()
    if cq.message is None:
        return
    # Удаляем экран списка промокодов, чтобы не оставался "висячим" сообщением.
    await delete_message_safe(cq.message)
    await _send_and_track(
        state,
        cq.message,
        "Введите код промокода. Примеры: PROMO10, SUMMER2026. Длина до 64.",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("admin:promos:page:"))
async def cb_promos_page(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not (is_bot_admin or _is_admin(cq.from_user.id)):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    parts = cq.data.split(":")
    try:
        page = int(parts[-1])
    except Exception:
        page = 0
    await _render_promos_list(cq, session, page=page)


@router.callback_query(F.data == "admin:promos:cancel")
async def cb_promos_cancel(
    cq: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not (is_bot_admin or _is_admin(cq.from_user.id)):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await state.clear()
    await cq.answer("Отменено.")
    await _render_promos_list(cq, session, page=0)


@router.message(AdminPromoStates.create_waiting_code, F.text)
async def msg_promos_create_code(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if message.from_user is None or db_user is None:
        await state.clear()
        return
    if not _is_admin(message.from_user.id):
        await state.clear()
        return

    raw_code = (message.text or "").strip().upper()
    if not raw_code:
        await _send_and_track(state, message, "Код пустой. Введите снова.")
        return
    if len(raw_code) > 64:
        await _send_and_track(state, message, "Слишком длинный код (до 64).")
        return

    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await _try_delete_by_id(message.bot, message.chat.id, prompt_mid if isinstance(prompt_mid, int) else None)
    await delete_message_safe(message)

    await state.update_data(create_code=raw_code)
    await state.set_state(AdminPromoStates.create_waiting_type)
    await _send_and_track(
        state,
        message,
        "Выберите тип промокода:",
        reply_markup=_type_select_keyboard("admin:promos:create"),
    )


@router.callback_query(F.data.startswith("admin:promos:create:type:"))
async def cb_promos_create_type(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    if cq.from_user is None:
        return
    if not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    promo_type = cq.data.split(":")[-1]
    if promo_type not in {"subscription_days", "balance_rub", "topup_bonus_percent"}:
        await cq.answer("Неверный тип.", show_alert=True)
        return
    await state.update_data(create_type=promo_type)
    await state.set_state(AdminPromoStates.create_waiting_value)
    await cq.answer()
    if cq.message:
        # Удаляем сообщение "Выберите тип промокода", чтобы шаги были чистыми.
        await delete_message_safe(cq.message)
        cancel_kb = _cancel_keyboard("admin:promos")
        if promo_type == "subscription_days":
            await _send_and_track(
                state,
                cq.message,
                "Введите количество дней (целое число, например 5).",
                reply_markup=cancel_kb,
            )
        elif promo_type == "balance_rub":
            await _send_and_track(
                state,
                cq.message,
                "Введите сумму в ₽ (например 100 или 10.5).",
                reply_markup=cancel_kb,
            )
        else:
            await _send_and_track(
                state,
                cq.message,
                "Введите процент к первому пополнению (например 10 или 15.5).",
                reply_markup=cancel_kb,
            )


@router.message(AdminPromoStates.create_waiting_value, F.text)
async def msg_promos_create_value(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    promo_type = data.get("create_type")
    if not isinstance(promo_type, str):
        await state.clear()
        return

    raw = (message.text or "").strip().replace(",", ".")
    prompt_mid = data.get("prompt_mid")
    await _try_delete_by_id(message.bot, message.chat.id, prompt_mid if isinstance(prompt_mid, int) else None)
    await delete_message_safe(message)

    if promo_type == "subscription_days":
        if not raw.isdigit():
            await _send_and_track(state, message, "Нужно целое число дней.")
            return
        days = int(raw)
        if days <= 0:
            await _send_and_track(state, message, "Дни должны быть > 0.")
            return
        await state.update_data(create_value=Decimal(days))
        await state.set_state(AdminPromoStates.create_waiting_fallback)
        cancel_kb = _cancel_keyboard("admin:promos")
        await _send_and_track(
            state,
            message,
            "Если подписки не будет: введите фолбэк-сумму на баланс в ₽ (например 100).",
            reply_markup=cancel_kb,
        )
        return

    try:
        val = Decimal(raw)
    except InvalidOperation:
        await _send_and_track(state, message, "Введите число (например 100 или 10.5).")
        return
    if val <= 0:
        await _send_and_track(state, message, "Значение должно быть > 0.")
        return

    await state.update_data(create_value=val)
    await state.set_state(AdminPromoStates.create_waiting_expires_at)
    cancel_kb = _cancel_keyboard("admin:promos")
    await _send_and_track(
        state,
        message,
        "Введите срок действия до (YYYY-MM-DD или DD.MM.YYYY), или '-' чтобы без срока.",
        reply_markup=cancel_kb,
    )


@router.message(AdminPromoStates.create_waiting_fallback, F.text)
async def msg_promos_create_fallback(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    promo_type = data.get("create_type")
    if promo_type != "subscription_days":
        await state.clear()
        return

    raw = (message.text or "").strip().replace(",", ".")
    prompt_mid = data.get("prompt_mid")
    await _try_delete_by_id(message.bot, message.chat.id, prompt_mid if isinstance(prompt_mid, int) else None)
    await delete_message_safe(message)

    try:
        fb = Decimal(raw)
    except InvalidOperation:
        await _send_and_track(state, message, "Введите число (например 100).")
        return
    if fb <= 0:
        await _send_and_track(state, message, "Фолбэк должен быть > 0.")
        return

    await state.update_data(create_fallback=fb)
    await state.set_state(AdminPromoStates.create_waiting_expires_at)
    cancel_kb = _cancel_keyboard("admin:promos")
    await _send_and_track(
        state,
        message,
        "Введите срок действия до (YYYY-MM-DD или DD.MM.YYYY), или '-' чтобы без срока.",
        reply_markup=cancel_kb,
    )


@router.message(AdminPromoStates.create_waiting_expires_at, F.text)
async def msg_promos_create_expires(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    prompt_mid = data.get("prompt_mid")
    await _try_delete_by_id(message.bot, message.chat.id, prompt_mid if isinstance(prompt_mid, int) else None)
    await delete_message_safe(message)

    try:
        expires_at = _parse_date_any(raw)
    except ValueError as e:
        await _send_and_track(state, message, str(e))
        return

    await state.update_data(create_expires_at=expires_at)
    await state.set_state(AdminPromoStates.create_waiting_max_uses)
    cancel_kb = _cancel_keyboard("admin:promos")
    await _send_and_track(
        state,
        message,
        "Введите лимит активаций (целое число) или '-' чтобы без лимита.",
        reply_markup=cancel_kb,
    )


@router.message(AdminPromoStates.create_waiting_max_uses, F.text)
async def msg_promos_create_max_uses(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    prompt_mid = data.get("prompt_mid")
    await _try_delete_by_id(message.bot, message.chat.id, prompt_mid if isinstance(prompt_mid, int) else None)
    await delete_message_safe(message)

    max_uses: int | None = None
    if raw != "-":
        if not raw.isdigit():
            await _send_and_track(state, message, "Нужно целое число или '-' .")
            return
        max_uses = int(raw)
        if max_uses <= 0:
            await _send_and_track(state, message, "Лимит должен быть > 0.")
            return

    await state.update_data(create_max_uses=max_uses)
    await state.set_state(AdminPromoStates.create_waiting_active)
    await _send_and_track(
        state,
        message,
        "Промокод: включить или выключить?",
        reply_markup=_active_select_keyboard("admin:promos:create"),
    )


@router.callback_query(F.data.startswith("admin:promos:create:active:"))
async def cb_promos_create_active(
    cq: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or db_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    data = await state.get_data()
    promo_code = data.get("create_code")
    promo_type = data.get("create_type")
    promo_value = data.get("create_value")
    promo_fallback = data.get("create_fallback")
    promo_expires_at = data.get("create_expires_at")
    promo_max_uses = data.get("create_max_uses")

    if not isinstance(promo_code, str) or not isinstance(promo_type, str) or not isinstance(promo_value, Decimal):
        await state.clear()
        await cq.answer("Ошибка данных.", show_alert=True)
        return

    flag_raw = cq.data.split(":")[-1]
    is_active = flag_raw == "true"

    created = PromoCode(
        code=promo_code,
        type=promo_type,
        value=promo_value,
        fallback_value_rub=(promo_fallback if promo_type == "subscription_days" else None),
        expires_at=promo_expires_at,
        max_uses=promo_max_uses,
        is_active=is_active,
        created_by_user_id=db_user.id,
    )
    try:
        session.add(created)
        await session.flush()
    except Exception as e:
        msg = strip_for_popup_alert(str(e))
        await cq.answer(msg[:200], show_alert=True)
        return

    await state.clear()
    await cq.answer("Создано.")
    await notify_admin(
        get_settings(),
        title="🎁 " + bold("Промокод создан"),
        lines=[
            plain("Код: ") + code(created.code),
            plain("Тип: ") + code(created.type),
            plain("Награда: ") + bold(_promo_reward_caption(created)),
            plain("Срок (до): ") + bold(_format_expires(created)),
            plain("Лимит: ") + bold("∞" if created.max_uses is None else str(created.max_uses)),
            plain("Создал: ") + bold(f"#{db_user.id}"),
        ],
        event_type="promo_create",
        topic=AdminLogTopic.PROMO,
        subject_user=db_user,
        session=session,
    )
    await _render_promos_view(cq, session, promo_id=created.id)


async def _render_promos_view(
    cq: CallbackQuery,
    session: AsyncSession,
    *,
    promo_id: int,
    page_back: str = "admin:promos:page:0",
) -> None:
    settings = get_settings()
    promo = await session.get(PromoCode, promo_id)
    if promo is None:
        await cq.answer("Промокод не найден.", show_alert=True)
        return
    created_by = None
    if promo.created_by_user_id is not None:
        created_by = await session.get(User, promo.created_by_user_id)

    lines = _promo_details_lines(promo, created_by)
    cap = join_lines(*lines)

    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"admin:promos:edit:{promo.id}"))
    b.row(
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=f"admin:promos:delete:{promo.id}",
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ Список", callback_data=page_back))
    b.row(InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:panel"))

    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("admin:promos:delete:"))
async def cb_promos_delete_ask(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    if not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return

    try:
        promo_id = int(cq.data.split(":")[-1])
    except Exception:
        await cq.answer("Неверный id", show_alert=True)
        return

    promo = await session.get(PromoCode, promo_id)
    if promo is None:
        await cq.answer("Промокод не найден.", show_alert=True)
        return

    cap = join_lines(
        "⚠️ " + bold("Удаление промокода"),
        "",
        plain("Промокод ") + code(promo.code) + plain(" будет удалён из системы."),
        plain("После удаления его нельзя будет активировать."),
        "",
        plain("Продолжить?"),
    )
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="✅ Да, удалить",
            callback_data=f"admin:promos:dodelete:{promo.id}",
        ),
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin:promos:view:" + str(promo.id)),
    )
    await cq.answer()
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=get_settings(),
    )


@router.callback_query(F.data.startswith("admin:promos:dodelete:"))
async def cb_promos_delete_do(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    if not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return

    try:
        promo_id = int(cq.data.split(":")[-1])
    except Exception:
        await cq.answer("Неверный id", show_alert=True)
        return

    promo = await session.get(PromoCode, promo_id)
    if promo is None:
        await cq.answer("Промокод не найден.", show_alert=True)
        return

    deleted_code = promo.code
    await session.delete(promo)
    await session.flush()
    # Удаление подтверждается коммитом middleware DbSessionMiddleware.

    await cq.answer("Удалено.")
    settings = get_settings()
    await notify_admin(
        settings,
        title="🗑 " + bold("Промокод удалён"),
        lines=[
            f"Код: {code(deleted_code)}",
            f"Удалил: {bold(f'#{db_user.id}')}",
        ],
        event_type="promo_delete",
        topic=AdminLogTopic.PROMO,
        subject_user=db_user,
        session=session,
    )

    await _render_promos_list(cq, session, page=0)


@router.callback_query(F.data.startswith("admin:promos:view:"))
async def cb_promos_view(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not (is_bot_admin or _is_admin(cq.from_user.id)):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    try:
        promo_id = int(cq.data.split(":")[-1])
    except Exception:
        await cq.answer("Неверный id", show_alert=True)
        return
    await _render_promos_view(cq, session, promo_id=promo_id)


@router.callback_query(F.data.startswith("admin:promos:edit:"))
async def cb_promos_edit_start(
    cq: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not (is_bot_admin or _is_admin(cq.from_user.id)):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    try:
        promo_id = int(cq.data.split(":")[-1])
    except Exception:
        await cq.answer("Неверный id", show_alert=True)
        return

    promo = await session.get(PromoCode, promo_id)
    if promo is None:
        await cq.answer("Промокод не найден.", show_alert=True)
        return

    await state.clear()
    await state.update_data(edit_promo_id=promo_id, edit_type=promo.type)
    await state.set_state(AdminPromoStates.edit_waiting_type)
    await cq.answer()
    if cq.message:
        await _send_and_track(
            state,
            cq.message,
            "Выберите новый тип промокода:",
            reply_markup=_type_select_keyboard("admin:promos:edit"),
        )


@router.callback_query(F.data.startswith("admin:promos:edit:type:"))
async def cb_promos_edit_type(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    promo_type = cq.data.split(":")[-1]
    if promo_type not in {"subscription_days", "balance_rub", "topup_bonus_percent"}:
        await cq.answer("Неверный тип.", show_alert=True)
        return
    await state.update_data(edit_type=promo_type)
    await state.set_state(AdminPromoStates.edit_waiting_value)
    await cq.answer()
    if cq.message:
        await delete_message_safe(cq.message)
        cancel_kb = _cancel_keyboard("admin:promos")
        if promo_type == "subscription_days":
            await _send_and_track(state, cq.message, "Введите дни подписки (целое число).", reply_markup=cancel_kb)
        elif promo_type == "balance_rub":
            await _send_and_track(
                state,
                cq.message,
                "Введите сумму в ₽ (например 100 или 10.5).",
                reply_markup=cancel_kb,
            )
        else:
            await _send_and_track(
                state,
                cq.message,
                "Введите процент к первому пополнению (например 10).",
                reply_markup=cancel_kb,
            )


@router.message(AdminPromoStates.edit_waiting_value, F.text)
async def msg_promos_edit_value(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    promo_type = data.get("edit_type")
    promo_id = data.get("edit_promo_id")
    if not isinstance(promo_type, str) or not isinstance(promo_id, int):
        await state.clear()
        return

    raw = (message.text or "").strip().replace(",", ".")
    prompt_mid = data.get("prompt_mid")
    await _try_delete_by_id(message.bot, message.chat.id, prompt_mid if isinstance(prompt_mid, int) else None)
    await delete_message_safe(message)

    if promo_type == "subscription_days":
        if not raw.isdigit():
            await _send_and_track(state, message, "Нужно целое число дней.")
            return
        days_i = int(raw)
        if days_i <= 0:
            await _send_and_track(state, message, "Дни должны быть > 0.")
            return
        await state.update_data(edit_value=Decimal(days_i))
        await state.set_state(AdminPromoStates.edit_waiting_fallback)
        cancel_kb = _cancel_keyboard("admin:promos")
        await _send_and_track(
            state,
            message,
            "Фолбэк (деньги при отсутствии подписки): введите сумму ₽.",
            reply_markup=cancel_kb,
        )
        return

    try:
        val = Decimal(raw)
    except InvalidOperation:
        await _send_and_track(state, message, "Введите число (например 100 или 10.5).")
        return
    if val <= 0:
        await _send_and_track(state, message, "Значение должно быть > 0.")
        return

    await state.update_data(edit_value=val)
    await state.set_state(AdminPromoStates.edit_waiting_expires_at)
    cancel_kb = _cancel_keyboard("admin:promos")
    await _send_and_track(
        state,
        message,
        "Срок до (YYYY-MM-DD или DD.MM.YYYY), или '-' для без срока.",
        reply_markup=cancel_kb,
    )


@router.message(AdminPromoStates.edit_waiting_fallback, F.text)
async def msg_promos_edit_fallback(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    promo_type = data.get("edit_type")
    if promo_type != "subscription_days":
        await state.clear()
        return
    raw = (message.text or "").strip().replace(",", ".")
    prompt_mid = data.get("prompt_mid")
    await _try_delete_by_id(message.bot, message.chat.id, prompt_mid if isinstance(prompt_mid, int) else None)
    await delete_message_safe(message)

    try:
        fb = Decimal(raw)
    except InvalidOperation:
        await _send_and_track(state, message, "Введите число (например 100).")
        return
    if fb <= 0:
        await _send_and_track(state, message, "Фолбэк должен быть > 0.")
        return
    await state.update_data(edit_fallback=fb)
    await state.set_state(AdminPromoStates.edit_waiting_expires_at)
    cancel_kb = _cancel_keyboard("admin:promos")
    await _send_and_track(
        state,
        message,
        "Срок до (YYYY-MM-DD или DD.MM.YYYY), или '-' для без срока.",
        reply_markup=cancel_kb,
    )


@router.message(AdminPromoStates.edit_waiting_expires_at, F.text)
async def msg_promos_edit_expires(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    prompt_mid = data.get("prompt_mid")
    await _try_delete_by_id(message.bot, message.chat.id, prompt_mid if isinstance(prompt_mid, int) else None)
    await delete_message_safe(message)

    try:
        expires_at = _parse_date_any(raw)
    except ValueError as e:
        await _send_and_track(state, message, str(e))
        return

    await state.update_data(edit_expires_at=expires_at)
    await state.set_state(AdminPromoStates.edit_waiting_max_uses)
    cancel_kb = _cancel_keyboard("admin:promos")
    await _send_and_track(
        state,
        message,
        "Лимит активаций (целое число) или '-' для без лимита.",
        reply_markup=cancel_kb,
    )


@router.message(AdminPromoStates.edit_waiting_max_uses, F.text)
async def msg_promos_edit_max_uses(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    prompt_mid = data.get("prompt_mid")
    await _try_delete_by_id(message.bot, message.chat.id, prompt_mid if isinstance(prompt_mid, int) else None)
    await delete_message_safe(message)

    max_uses: int | None = None
    if raw != "-":
        if not raw.isdigit():
            await _send_and_track(state, message, "Нужно целое число или '-' .")
            return
        max_uses = int(raw)
        if max_uses <= 0:
            await _send_and_track(state, message, "Лимит должен быть > 0.")
            return

    await state.update_data(edit_max_uses=max_uses)
    await state.set_state(AdminPromoStates.edit_waiting_active)
    await _send_and_track(
        state,
        message,
        "Промокод: включить или выключить?",
        reply_markup=_active_select_keyboard("admin:promos:edit"),
    )


@router.callback_query(F.data.startswith("admin:promos:edit:active:"))
async def cb_promos_edit_active(
    cq: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if cq.from_user is None or db_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    data = await state.get_data()
    promo_id = data.get("edit_promo_id")
    promo_type = data.get("edit_type")
    promo_value = data.get("edit_value")
    promo_fallback = data.get("edit_fallback")
    promo_expires_at = data.get("edit_expires_at")
    promo_max_uses = data.get("edit_max_uses")

    if not isinstance(promo_id, int) or not isinstance(promo_type, str) or not isinstance(promo_value, Decimal):
        await state.clear()
        await cq.answer("Ошибка данных.", show_alert=True)
        return

    flag_raw = cq.data.split(":")[-1]
    is_active = flag_raw == "true"

    promo = await session.get(PromoCode, promo_id)
    if promo is None:
        await state.clear()
        await cq.answer("Промокод не найден.", show_alert=True)
        return

    promo.type = promo_type
    promo.value = promo_value
    promo.fallback_value_rub = promo_fallback if promo_type == "subscription_days" else None
    promo.expires_at = promo_expires_at
    promo.max_uses = promo_max_uses
    promo.is_active = is_active
    try:
        await session.flush()
    except Exception as e:
        msg = strip_for_popup_alert(str(e))
        await cq.answer(msg[:200], show_alert=True)
        return

    await state.clear()
    await cq.answer("Изменено.")
    await notify_admin(
        get_settings(),
        title="✏️ " + bold("Промокод изменён"),
        lines=[
            plain("Код: ") + code(promo.code),
            plain("Тип: ") + code(promo.type),
            plain("Награда: ") + bold(_promo_reward_caption(promo)),
            plain("Срок (до): ") + bold(_format_expires(promo)),
            plain("Лимит: ") + bold("∞" if promo.max_uses is None else str(promo.max_uses)),
            plain("Изменил: ") + bold(f"#{db_user.id}"),
        ],
        event_type="promo_edit",
        topic=AdminLogTopic.PROMO,
        subject_user=db_user,
        session=session,
    )
    await _render_promos_view(cq, session, promo_id=promo_id)



