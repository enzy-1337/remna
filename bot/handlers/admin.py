"""Админ-панель: пользователи, поиск, рефералы, управление подпиской."""

from __future__ import annotations

import logging
import math
import pyotp
from uuid import UUID
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import String, desc, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.states.admin import (
    AdminBroadcastStates,
    AdminFactoryResetStates,
    AdminFindUserStates,
    AdminSecurityStates,
    AdminSubscriptionStates,
)
from bot.utils.screen_photo import answer_callback_with_photo_screen, send_profile_screen
from shared.config import get_settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.md2 import bold, code, esc, italic, join_lines, link, plain, strip_for_popup_alert
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.models.billing_ledger_entry import BillingLedgerEntry
from shared.models.billing_usage_event import BillingUsageEvent
from shared.models.device import Device
from shared.models.remnawave_webhook_event import RemnawaveWebhookEvent
from shared.services.admin_user_delete import delete_user_from_app
from shared.services.factory_reset_service import wipe_all_application_data
from shared.services.billing_v2.traffic_meter_poll_service import baseline_meter_at_hybrid_transition
from shared.services.topup_service import apply_balance_credit_followups

_MSK_TZ = ZoneInfo("Europe/Moscow")
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin
from shared.services.broadcast_service import (
    MAX_MESSAGE_LEN,
    broadcast_to_users,
    collect_recipient_telegram_ids,
)
from shared.database import get_session_factory
from shared.services.billing_calculator import transition_credit_for_remaining_legacy_rub
from shared.services.referral_service import count_invited_users
from shared.services.remnawave_user_panel_sync import update_rw_user_respecting_hwid_limit
from shared.services.feature_flags import set_tariff_purchases_enabled, tariff_purchases_enabled
from shared.services.subscription_service import (
    admin_convert_monthly_subscriptions_to_payg_balance,
    get_base_subscription_plan,
    resolve_legacy_transition_base_month_rub,
)

logger = logging.getLogger(__name__)

router = Router(name="admin")

PAGE_SIZE = 8
_INT32_MAX = 2_147_483_647
_INT64_MAX = 9_223_372_036_854_775_807


def _add_calendar_months(dt: datetime, months: int) -> datetime:
    shifted = (dt.year * 12 + (dt.month - 1)) + months
    year = shifted // 12
    month = shifted % 12 + 1
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _is_admin(tg_id: int | None) -> bool:
    if tg_id is None:
        return False
    return tg_id in get_settings().admin_telegram_ids


async def admin_panel_keyboard() -> InlineKeyboardMarkup:
    settings = get_settings()
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="👤 Раздел пользователей",
            callback_data="admin:section:users",
        ),
        InlineKeyboardButton(
            text="📊 Раздел аналитики",
            callback_data="admin:section:analytics",
        ),
    )
    b.row(
        InlineKeyboardButton(
            text="👨‍💼 Админ-профиль", callback_data="admin:section:profile"
        )
    )
    b.row(
        InlineKeyboardButton(
            text="📋 Продажа тарифов в боте",
            callback_data="admin:tariffs_shop",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="⛔ Factory reset", callback_data="admin:reset:start", style="danger"
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="menu:main", style="danger"))
    return b.as_markup()


def _admin_users_section_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:0"),
        InlineKeyboardButton(text="⏱ Подписки", callback_data="admin:subs:0"),
    )
    b.row(InlineKeyboardButton(text="🔎 Поиск", callback_data="admin:find"))
    b.row(
        InlineKeyboardButton(
            text="⬅️ Назад в админ-панель", callback_data="admin:panel", style="danger"
        )
    )
    return b.as_markup()


def _admin_analytics_section_keyboard() -> InlineKeyboardMarkup:
    s = get_settings()
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="📈 Метрики (24ч)", callback_data="admin:metrics"),
        InlineKeyboardButton(text="🌐 Web-Admin", callback_data="admin:web"),
    )
    row_legacy_payg: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            text="🧮 Калькулятор перехода legacy",
            callback_data="admin:transition_calc",
        ),
    ]
    if s.billing_v2_enabled:
        row_legacy_payg.append(
            InlineKeyboardButton(
                text="📊 Калькулятор PAYG", callback_data="admin:calc_payg"
            ),
        )
    b.row(*row_legacy_payg)
    if s.billing_v2_enabled:
        b.row(
            InlineKeyboardButton(
                text="🔁 Конвертировать подписки в PAYG",
                callback_data="admin:mass_convert_payg",
            )
        )
    b.row(
        InlineKeyboardButton(
            text="🎁 Промокоды", callback_data="admin:promos:page:0"
        ),
        InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast"),
    )
    if not (s.public_site_url or "").strip():
        b.row(
            InlineKeyboardButton(
                text="ℹ️ Web-Admin не настроен", callback_data="admin:noop"
            )
        )
    b.row(
        InlineKeyboardButton(
            text="⬅️ Назад в админ-панель", callback_data="admin:panel", style="danger"
        )
    )
    return b.as_markup()


def _has_active_web_admin_session(user: User) -> bool:
    exp = user.web_admin_session_expires_at
    if not user.web_admin_session_token or exp is None:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


def _admin_profile_section_keyboard(user: User) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔗 GitHub", callback_data="menu:github", style="primary"))
    enabled = bool(user.web_admin_totp_enabled and (user.web_admin_totp_secret or "").strip())
    b.row(
        InlineKeyboardButton(
            text="🔐 Отключить Google Auth" if enabled else "🔐 Подключить Google Auth",
            callback_data="admin:profile:totp:disable" if enabled else "admin:profile:totp:enable",
            style="danger" if enabled else "success",
        )
    )
    if _has_active_web_admin_session(user):
        b.row(
            InlineKeyboardButton(
                text="🚪 Отключить web-admin сессию",
                callback_data="admin:profile:web_session:disable",
                style="danger",
            )
        )
    b.row(
        InlineKeyboardButton(
            text="⬅️ Назад в админ-панель", callback_data="admin:panel", style="danger"
        )
    )
    return b.as_markup()


def _admin_web_keyboard() -> InlineKeyboardMarkup:
    s = get_settings()
    root = (s.public_site_url or "").rstrip("/")
    admin_url = f"{root}/admin" if root else ""
    b = InlineKeyboardBuilder()
    if admin_url:
        b.row(
            InlineKeyboardButton(text="🏠 Главная", url=admin_url),
            InlineKeyboardButton(text="💳 Пополнения", url=f"{admin_url}/topups"),
        )
        b.row(
            InlineKeyboardButton(
                text="👥 Пользователи", url=f"{admin_url}/users"
            ),
            InlineKeyboardButton(
                text="⏱ Подписки", url=f"{admin_url}/subscriptions"
            ),
        )
        b.row(
            InlineKeyboardButton(text="🎁 Промокоды", url=f"{admin_url}/promos"),
            InlineKeyboardButton(text="📋 Тарифы", url=f"{admin_url}/tariffs"),
        )
        b.row(
            InlineKeyboardButton(
                text="📢 Рассылка", url=f"{admin_url}/broadcast"
            )
        )
        b.row(
            InlineKeyboardButton(text="🎫 Тикеты", url=f"{admin_url}/tickets"),
            InlineKeyboardButton(text="⚙️ Настройки", url=f"{admin_url}/settings"),
        )
    b.row(
        InlineKeyboardButton(
            text="⬅️ Аналитика", callback_data="admin:section:analytics", style="danger"
        )
    )
    return b.as_markup()


def _sub_status_emoji(status: str) -> str:
    st = (status or "").strip().lower()
    if st == "active":
        return "🟢"
    if st == "trial":
        return "🎁"
    if st == "cancelled":
        return "⏹"
    if st == "expired":
        return "⚪"
    return "⚪"


def _parse_subs_filters(parts: list[str]) -> tuple[int, str, str]:
    # admin:subs:<page>:<scope>:<ar>
    # scope: all | exp24 | exp3 | trial
    # ar: all | on | off
    page = 0
    scope = "all"
    ar = "all"
    try:
        if len(parts) >= 3:
            page = int(parts[2])
    except Exception:
        page = 0
    if len(parts) >= 4 and parts[3] in ("all", "exp24", "exp3", "trial"):
        scope = parts[3]
    if len(parts) >= 5 and parts[4] in ("all", "on", "off"):
        ar = parts[4]
    return page, scope, ar


def _subs_cb(page: int, scope: str, ar: str) -> str:
    return f"admin:subs:{page}:{scope}:{ar}"


async def _render_admin_subs_screen(
    cq: CallbackQuery,
    session: AsyncSession,
    *,
    page: int,
    scope: str,
    ar: str,
) -> None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_to = None
    statuses = ("active", "trial")
    if scope == "trial":
        statuses = ("trial",)
    elif scope == "exp24":
        expires_to = now + timedelta(hours=24)
    elif scope == "exp3":
        expires_to = now + timedelta(hours=3)

    ar_cond = None
    if ar == "on":
        ar_cond = True
    elif ar == "off":
        ar_cond = False

    base_q = (
        select(Subscription, User)
        .join(User, User.id == Subscription.user_id)
        .where(
            Subscription.status.in_(statuses),
            Subscription.expires_at > now,
        )
    )
    if expires_to is not None:
        base_q = base_q.where(Subscription.expires_at <= expires_to)
    if ar_cond is not None:
        base_q = base_q.where(Subscription.auto_renew.is_(ar_cond))

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(Subscription).where(
                    Subscription.status.in_(statuses),
                    Subscription.expires_at > now,
                    *( [Subscription.expires_at <= expires_to] if expires_to is not None else [] ),
                    *( [Subscription.auto_renew.is_(ar_cond)] if ar_cond is not None else [] ),
                )
            )
        ).scalar_one()
        or 0
    )
    offset = max(0, int(page)) * PAGE_SIZE
    rows = (
        await session.execute(
            base_q.order_by(Subscription.expires_at.asc()).offset(offset).limit(PAGE_SIZE)
        )
    ).all()
    scope_label = {
        "all": "все активные/триал",
        "trial": "только trial",
        "exp24": "истекают < 24ч",
        "exp3": "истекают < 3ч",
    }.get(scope, "все")
    ar_label = {"all": "любой", "on": "вкл", "off": "выкл"}.get(ar, "любой")
    lines: list[str] = [
        "⏱ " + bold("Подписки"),
        plain("Фильтр: ") + bold(scope_label) + plain(" · авто: ") + bold(ar_label),
        plain(f"Стр. {int(page) + 1} · записей: ") + bold(str(total)),
        "",
        plain("Сортировка: ближайшее окончание сверху."),
        "",
    ]
    b = InlineKeyboardBuilder()
    # Фильтры
    b.row(
        InlineKeyboardButton(text="Все", callback_data=_subs_cb(0, "all", ar)),
        InlineKeyboardButton(
            text="<24ч", callback_data=_subs_cb(0, "exp24", ar)
        ),
        InlineKeyboardButton(text="<3ч", callback_data=_subs_cb(0, "exp3", ar)),
        InlineKeyboardButton(
            text="Trial", callback_data=_subs_cb(0, "trial", ar)
        ),
    )
    b.row(
        InlineKeyboardButton(
            text="Авто: любой", callback_data=_subs_cb(0, scope, "all")
        ),
        InlineKeyboardButton(
            text="Авто: вкл", callback_data=_subs_cb(0, scope, "on")
        ),
        InlineKeyboardButton(
            text="Авто: выкл", callback_data=_subs_cb(0, scope, "off")
        ),
    )
    for sub, u in rows:
        exp = sub.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        exp_s = exp.astimezone(_MSK_TZ).strftime("%d.%m %H:%M")
        nm = (u.first_name or u.username or "?").strip()
        if len(nm) > 16:
            nm = nm[:15] + "…"
        ar_emoji = "🔄" if sub.auto_renew else "⏸"
        btn_text = f"{_sub_status_emoji(sub.status)}{ar_emoji} #{sub.id} до {exp_s} · {nm}"
        b.row(
            InlineKeyboardButton(
                text=btn_text[:64], callback_data=f"admin:u:{u.id}"
            )
        )

    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
    cur_page = int(page) + 1
    page_label = f"{cur_page}/{total_pages}"
    placeholder = InlineKeyboardButton(text="·", callback_data="admin:subs:noop")
    left_btn = (
        InlineKeyboardButton(
            text="⬅️", callback_data=_subs_cb(int(page) - 1, scope, ar)
        )
        if int(page) > 0
        else placeholder
    )
    mid_btn = InlineKeyboardButton(text=page_label, callback_data="admin:subs:noop")
    right_btn = (
        InlineKeyboardButton(
            text="➡️", callback_data=_subs_cb(int(page) + 1, scope, ar)
        )
        if offset + len(rows) < total
        else placeholder
    )
    b.row(left_btn, mid_btn, right_btn)
    b.row(
        InlineKeyboardButton(
            text="⬅️ Пользователи", callback_data="admin:section:users", style="danger"
        )
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(*lines),
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("admin:subs:"))
async def cb_admin_subs(
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
    page, scope, ar = _parse_subs_filters(parts)
    await _render_admin_subs_screen(cq, session, page=page, scope=scope, ar=ar)


@router.callback_query(F.data == "admin:noop")
async def cb_admin_noop(cq: CallbackQuery) -> None:
    await cq.answer()


@router.callback_query(F.data == "admin:section:users")
async def cb_admin_section_users(cq: CallbackQuery, db_user: User | None) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "👤 " + bold("Раздел пользователей"),
            "",
            plain("Выберите действие."),
        ),
        reply_markup=_admin_users_section_keyboard(),
        settings=get_settings(),
    )


@router.callback_query(F.data == "admin:section:profile")
async def cb_admin_section_profile(cq: CallbackQuery, db_user: User | None) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "👨‍💼 " + bold("Админ-профиль"),
            "",
            plain("Управление GitHub-входом, Google Auth и web-admin сессией."),
        ),
        reply_markup=_admin_profile_section_keyboard(db_user),
        settings=get_settings(),
    )


@router.callback_query(F.data == "admin:profile:totp:enable")
async def cb_admin_profile_totp_enable_start(
    cq: CallbackQuery, db_user: User | None, state: FSMContext
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    if db_user.web_admin_totp_enabled and (db_user.web_admin_totp_secret or "").strip():
        await cq.answer("Google Auth уже подключен.", show_alert=True)
        return
    secret = pyotp.random_base32()
    await state.set_state(AdminSecurityStates.waiting_totp_enable_code)
    await state.update_data(admin_totp_secret=secret)
    await cq.answer()
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "👨‍💼 " + bold("Админ-профиль"),
            "",
            plain("Добавьте ключ в Google Authenticator и отправьте код из приложения следующим сообщением."),
            plain("Ключ: ") + code(secret),
        ),
        reply_markup=_admin_profile_section_keyboard(db_user),
        settings=get_settings(),
    )


@router.callback_query(F.data == "admin:profile:totp:disable")
async def cb_admin_profile_totp_disable_start(
    cq: CallbackQuery, db_user: User | None, state: FSMContext
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    if not db_user.web_admin_totp_enabled or not (db_user.web_admin_totp_secret or "").strip():
        await cq.answer("Google Auth не подключен.", show_alert=True)
        return
    await state.set_state(AdminSecurityStates.waiting_totp_disable_code)
    await cq.answer()
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "👨‍💼 " + bold("Админ-профиль"),
            "",
            plain("Отправьте текущий код из Google Authenticator, чтобы отключить защиту."),
        ),
        reply_markup=_admin_profile_section_keyboard(db_user),
        settings=get_settings(),
    )


@router.callback_query(F.data == "admin:profile:web_session:disable")
async def cb_admin_profile_disable_web_session(
    cq: CallbackQuery, session: AsyncSession, db_user: User | None
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    if not _has_active_web_admin_session(db_user):
        await cq.answer("Активной web-admin сессии нет.", show_alert=True)
        return
    db_user.web_admin_session_token = None
    db_user.web_admin_session_expires_at = None
    await session.commit()
    await cq.answer("Сессия отключена.")
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "👨‍💼 " + bold("Админ-профиль"),
            "",
            plain("Активная web-admin сессия отключена. Для входа потребуется новая авторизация."),
        ),
        reply_markup=_admin_profile_section_keyboard(db_user),
        settings=get_settings(),
    )


@router.callback_query(F.data == "admin:section:analytics")
async def cb_admin_section_analytics(cq: CallbackQuery, db_user: User | None) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "📊 " + bold("Раздел аналитики"),
            "",
            plain("Выберите действие."),
        ),
        reply_markup=_admin_analytics_section_keyboard(),
        settings=get_settings(),
    )


@router.message(StateFilter(AdminSecurityStates.waiting_totp_enable_code), F.text)
async def msg_admin_profile_totp_enable_code(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    if db_user is None:
        await state.clear()
        await message.answer("Сначала выполните /start.")
        return
    secret = str((await state.get_data()).get("admin_totp_secret") or "").strip()
    if not secret:
        await state.clear()
        await message.answer("Настройка истекла. Нажмите кнопку подключения Google Auth заново.")
        return
    otp = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if not pyotp.TOTP(secret).verify(otp, valid_window=1):
        await message.answer("Неверный код. Отправьте актуальный код из Google Authenticator.")
        return
    db_user.web_admin_totp_secret = secret
    db_user.web_admin_totp_enabled = True
    await session.commit()
    await state.clear()
    await message.answer("Google Auth подключен для web-admin.")


@router.message(StateFilter(AdminSecurityStates.waiting_totp_disable_code), F.text)
async def msg_admin_profile_totp_disable_code(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    if db_user is None:
        await state.clear()
        await message.answer("Сначала выполните /start.")
        return
    secret = (db_user.web_admin_totp_secret or "").strip()
    if not db_user.web_admin_totp_enabled or not secret:
        await state.clear()
        await message.answer("Google Auth уже отключен.")
        return
    otp = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if not pyotp.TOTP(secret).verify(otp, valid_window=1):
        await message.answer("Неверный код. Отправьте актуальный код из Google Authenticator.")
        return
    db_user.web_admin_totp_enabled = False
    db_user.web_admin_totp_secret = None
    await session.commit()
    await state.clear()
    await message.answer("Google Auth отключен для web-admin.")


def _admin_reset_cancel_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Отмена сброса", callback_data="admin:reset:cancel", style="danger"
        )
    )
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
        b.row(
            InlineKeyboardButton(
                text=_list_button_label(u), callback_data=f"admin:u:{u.id}"
            )
        )
    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
    cur_page = page + 1
    page_label = f"{cur_page}/{total_pages}"
    placeholder = InlineKeyboardButton(text="·", callback_data="admin:users:noop")
    left_btn = (
        InlineKeyboardButton(
            text="⬅️", callback_data=f"admin:users:{page - 1}"
        )
        if page > 0
        else placeholder
    )
    mid_btn = InlineKeyboardButton(text=page_label, callback_data="admin:users:noop")
    right_btn = (
        InlineKeyboardButton(
            text="➡️", callback_data=f"admin:users:{page + 1}"
        )
        if offset + len(rows) < total
        else placeholder
    )
    b.row(left_btn, mid_btn, right_btn)
    b.row(
        InlineKeyboardButton(
            text="⬅️ Пользователи", callback_data="admin:section:users", style="danger"
        )
    )

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
        plain("Billing mode: ") + bold(u.billing_mode),
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
        b.row(
            InlineKeyboardButton(
                text="✅ Разблокировать",
                callback_data=f"admin:unblock:{u.id}",
            )
        )
    else:
        b.row(
            InlineKeyboardButton(
                text="🚫 Заблокировать", callback_data=f"admin:block:{u.id}"
            )
        )

    root = (get_settings().public_site_url or "").strip().rstrip("/")
    if root:
        b.row(
            InlineKeyboardButton(
                text="🌐 Профиль в Web-Admin",
                url=f"{root}/admin/users/{u.id}",
            )
        )

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
                text="⏳ Продлить подписку",
                callback_data=f"admin:ad:{u.id}:{sub.id}",
            )
        )
        b.row(
            InlineKeyboardButton(
                text="📆 Добавить дни",
                callback_data=f"admin:days:{u.id}:{sub.id}",
            )
        )

    b.row(
        InlineKeyboardButton(
            text="💳 Добавить баланс",
            callback_data=f"admin:ab:{u.id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="🔎 Проверить подписку в Remnawave",
            callback_data=f"admin:rwcheck:{u.id}",
        ),
        InlineKeyboardButton(
            text="🧷 Привязать подписку вручную",
            callback_data=f"admin:mlink:{u.id}",
        ),
    )
    next_mode = "hybrid" if u.billing_mode == "legacy" else "legacy"
    b.row(
        InlineKeyboardButton(
            text=f"🧾 Billing: переключить в {next_mode}",
            callback_data=f"admin:bm:{u.id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="🧹 Обнулить баланс",
            callback_data=f"admin:rb:{u.id}",
        )
    )

    if viewer_telegram_id is not None and viewer_telegram_id != u.telegram_id:
        b.row(
            InlineKeyboardButton(
                text="🗑 Удалить пользователя",
                callback_data=f"admin:dask:{u.id}",
            )
        )

    b.row(
        InlineKeyboardButton(text="⬅️ К списку", callback_data="admin:users:0", style="danger")
    )
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
        reply_markup=await admin_panel_keyboard(),
        settings=settings,
    )


async def _render_admin_tariffs_shop_screen(cq: CallbackQuery, db_user: User) -> None:
    settings = get_settings()
    en = await tariff_purchases_enabled(settings)
    cap = join_lines(
        "📋 " + bold("Продажа тарифов в боте"),
        "",
        plain("Сейчас: ")
        + bold("включена — кнопки «Тарифы» и покупка с баланса доступны.")
        if en
        else plain("Сейчас: ")
        + bold("выключена — кнопки тарифов скрыты, покупка недоступна."),
        "",
        plain("Также можно переключить на странице «Тарифы» в web-admin."),
    )
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=("⏸ Выключить продажу тарифов" if en else "▶️ Включить продажу тарифов"),
            callback_data="admin:tariffs_toggle_do",
            style="success" if en else "danger",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="⬅️ Админ-панель", callback_data="admin:panel", style="danger"
        )
    )
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=b.as_markup(), settings=settings)


@router.callback_query(F.data == "admin:tariffs_shop")
async def cb_admin_tariffs_shop(cq: CallbackQuery, db_user: User | None) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await _render_admin_tariffs_shop_screen(cq, db_user)


@router.callback_query(F.data == "admin:tariffs_toggle_do")
async def cb_admin_tariffs_toggle_do(cq: CallbackQuery, db_user: User | None) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    settings = get_settings()
    cur = await tariff_purchases_enabled(settings)
    new_val = not cur
    await set_tariff_purchases_enabled(settings, new_val)
    await cq.answer("Готово")
    assert db_user is not None
    await _render_admin_tariffs_shop_screen(cq, db_user)


@router.callback_query(F.data == "admin:transition_calc")
async def cb_admin_transition_calc(
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
    s = get_settings()
    base = await resolve_legacy_transition_base_month_rub(session, s)
    fee = s.billing_transition_fee_percent
    sample_days = (7, 15, 30, 45)
    lines: list[str | object] = [
        "🧮 " + bold("Калькулятор перехода с legacy"),
        "",
        plain("Сумма на баланс (ориентир): (остаток_срока / 30) × ")
        + bold(str(base))
        + plain(" ₽ × (1 − ")
        + bold(str(fee))
        + plain(
            "%). База месяца — минимальный активный тариф ~30 дней из БД; если таких нет — "
        )
        + code("BILLING_TRANSITION_BASE_MONTH_RUB")
        + plain(". Автоначисления нет."),
        "",
    ]
    for d in sample_days:
        c = transition_credit_for_remaining_legacy_rub(
            s, remaining_days=d, base_month_rub=base
        )
        lines.append(plain(f"{d} → ") + bold(str(c)) + plain(" ₽"))
    root = (s.public_site_url or "").strip().rstrip("/")
    if root:
        lines.append(
            plain("Любое значение: раздел ")
            + link("Тарифы", f"{root}/admin/tariffs")
            + plain(" в web-admin.")
        )
    else:
        lines.append(
            plain("Задайте PUBLIC_SITE_URL — там же калькулятор с полем «остаток срока».")
        )
    await cq.answer()
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Аналитика", callback_data="admin:section:analytics", style="danger"
        )
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(*lines),
        reply_markup=kb.as_markup(),
        settings=s,
    )


@router.callback_query(F.data == "admin:web")
async def cb_admin_web_links(cq: CallbackQuery, db_user: User | None) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    s = get_settings()
    if not (s.public_site_url or "").strip():
        await cq.answer("Не задан PUBLIC_SITE_URL", show_alert=True)
        return
    cap = join_lines(
        "🌐 " + bold("Web-admin"),
        "",
        plain("Быстрые ссылки на разделы веб-админки."),
    )
    await cq.answer()
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=_admin_web_keyboard(),
        settings=s,
    )


@router.callback_query(F.data == "admin:mass_convert_payg")
async def cb_admin_mass_convert_payg(
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
    s = get_settings()
    changed, rw_changed, total_credit = await admin_convert_monthly_subscriptions_to_payg_balance(
        session,
        settings=s,
    )
    await session.commit()
    await cq.answer("Готово")
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "🔁 " + bold("Конвертация legacy → PAYG"),
            "",
            plain("Обновлено пользователей: ") + bold(str(changed)),
            plain("Синхронизировано в панели: ") + bold(str(rw_changed)),
            plain("Начислено в баланс: ") + bold(str(total_credit)) + plain(" ₽"),
        ),
        reply_markup=_admin_analytics_section_keyboard(),
        settings=s,
    )


@router.callback_query(F.data == "admin:metrics")
async def cb_admin_metrics(
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
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    webhook_ok_24h = int(
        (
            await session.execute(
                select(func.count()).select_from(RemnawaveWebhookEvent).where(
                    RemnawaveWebhookEvent.received_at >= day_ago,
                    RemnawaveWebhookEvent.signature_valid.is_(True),
                )
            )
        ).scalar_one()
        or 0
    )
    webhook_dup_24h = int(
        (
            await session.execute(
                select(func.count()).select_from(RemnawaveWebhookEvent).where(
                    RemnawaveWebhookEvent.received_at >= day_ago,
                    RemnawaveWebhookEvent.status == "duplicate",
                )
            )
        ).scalar_one()
        or 0
    )
    webhook_invalid_24h = int(
        (
            await session.execute(
                select(func.count()).select_from(RemnawaveWebhookEvent).where(
                    RemnawaveWebhookEvent.received_at >= day_ago,
                    RemnawaveWebhookEvent.signature_valid.is_(False),
                )
            )
        ).scalar_one()
        or 0
    )
    rating_events_24h = int(
        (
            await session.execute(
                select(func.count()).select_from(BillingUsageEvent).where(BillingUsageEvent.created_at >= day_ago)
            )
        ).scalar_one()
        or 0
    )
    ledger_rejects_24h = int(
        (
            await session.execute(
                select(func.count()).select_from(BillingLedgerEntry).where(
                    BillingLedgerEntry.created_at >= day_ago,
                    BillingLedgerEntry.entry_type == "reject",
                )
            )
        ).scalar_one()
        or 0
    )
    transitions_24h = int(
        (
            await session.execute(
                select(func.count()).select_from(Transaction).where(
                    Transaction.created_at >= day_ago,
                    Transaction.type == "billing_transition",
                )
            )
        ).scalar_one()
        or 0
    )
    risk_1h_users = int(
        (
            await session.execute(select(func.count()).select_from(User).where(User.risk_notified_1h_at.is_not(None)))
        ).scalar_one()
        or 0
    )
    risk_24h_users = int(
        (
            await session.execute(select(func.count()).select_from(User).where(User.risk_notified_24h_at.is_not(None)))
        ).scalar_one()
        or 0
    )
    active_sub_users = int(
        (
            await session.execute(
                select(func.count(distinct(Subscription.user_id))).where(
                    Subscription.status.in_(("active", "trial")),
                    Subscription.expires_at > now,
                )
            )
        ).scalar_one()
        or 0
    )
    topups_24h = int(
        (
            await session.execute(
                select(func.count()).select_from(Transaction).where(
                    Transaction.type == "topup",
                    Transaction.status == "completed",
                    Transaction.created_at >= day_ago,
                )
            )
        ).scalar_one()
        or 0
    )
    cap = join_lines(
        "📈 " + bold("Метрики (24 часа)"),
        "",
        "🌐 " + bold("Remnawave webhooks"),
        plain("ok: ") + bold(str(webhook_ok_24h)),
        plain("duplicate: ") + bold(str(webhook_dup_24h)),
        plain("invalid: ") + bold(str(webhook_invalid_24h)),
        "",
        "💸 " + bold("Billing"),
        plain("rating events: ") + bold(str(rating_events_24h)),
        plain("ledger rejects: ") + bold(str(ledger_rejects_24h)),
        plain("legacy→hybrid transitions: ") + bold(str(transitions_24h)),
        "",
        "👥 " + bold("Пользователи"),
        plain("active subs (users): ") + bold(str(active_sub_users)),
        plain("risk notified 24h: ") + bold(str(risk_24h_users)),
        plain("risk notified 1h: ") + bold(str(risk_1h_users)),
        "",
        "💳 " + bold("Платежи"),
        plain("topups completed: ") + bold(str(topups_24h)),
    )
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:metrics"))
    b.row(
        InlineKeyboardButton(
            text="⬅️ Аналитика", callback_data="admin:section:analytics", style="danger"
        )
    )
    await cq.answer()
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=get_settings(),
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
        InlineKeyboardButton(
            text="✅ Да, удалить", callback_data=f"admin:dyes:{uid}", style="danger"
        ),
        InlineKeyboardButton(
            text="❌ Отмена", callback_data=f"admin:u:{uid}", style="danger"
        ),
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


@router.callback_query(F.data.startswith("admin:bm:"))
async def cb_admin_toggle_billing_mode(
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
    u = await session.get(User, uid)
    if u is None:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    u.billing_mode = "hybrid" if u.billing_mode == "legacy" else "legacy"
    await session.commit()
    await cq.answer(f"Billing mode: {u.billing_mode}")
    await _render_user_card(cq, session, user_id=uid)


def _parse_user_sub(callback_data: str) -> tuple[int, int] | None:
    parts = callback_data.split(":")
    if len(parts) < 4:
        return None
    try:
        return int(parts[2]), int(parts[3])
    except ValueError:
        return None


def _parse_admin_user_id(callback_data: str) -> int | None:
    parts = callback_data.split(":")
    if len(parts) < 3:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


async def _find_rw_user_by_tg_or_username(
    rw: RemnaWaveClient,
    *,
    query: str,
) -> tuple[dict | None, str]:
    typed = (query or "").strip()
    if not typed:
        return None, "Пустой запрос."
    if typed.isdigit():
        hit = await rw.find_user_by_telegram_id(int(typed))
        return hit, "telegram_id"
    hit = await rw.find_user_by_username(typed)
    return hit, "username"


def _admin_months_quick_markup(user_id: int, sub_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="1 мес.", callback_data=f"admin:am:{user_id}:{sub_id}:1"
        ),
        InlineKeyboardButton(
            text="3 мес.", callback_data=f"admin:am:{user_id}:{sub_id}:3"
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="6 мес.", callback_data=f"admin:am:{user_id}:{sub_id}:6"
        ),
        InlineKeyboardButton(
            text="12 мес.", callback_data=f"admin:am:{user_id}:{sub_id}:12"
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="⌨️ Ввести вручную", callback_data="admin:noop"
        )
    )
    return kb.as_markup()


def _admin_days_quick_markup(user_id: int, sub_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for chunk in ((7, 14), (30, 90), (180, 365)):
        kb.row(
            *[
                InlineKeyboardButton(
                    text=f"+{d} дн.",
                    callback_data=f"admin:dayq:{user_id}:{sub_id}:{d}",
                )
                for d in chunk
            ]
        )
    kb.row(InlineKeyboardButton(text="⌨️ Ввести вручную", callback_data="admin:noop"))
    return kb.as_markup()


async def _apply_subscription_add_days(
    session: AsyncSession,
    *,
    user_id: int,
    sub_id: int,
    days: int,
    settings,
) -> tuple[bool, str]:
    if days < 1 or days > 3650:
        return False, "Допустимо от 1 до 3650 дней."
    sub = (
        await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.id == sub_id, Subscription.user_id == user_id)
        )
    ).scalar_one_or_none()
    if sub is None:
        return False, "Подписка не найдена."
    exp = sub.expires_at
    if exp is None:
        return False, "Нет даты окончания подписки."
    now = datetime.now(timezone.utc)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    base = max(now, exp) if exp < now else exp
    sub.expires_at = base + timedelta(days=days)
    pl = sub.plan
    if not (sub.status == "trial" and pl is not None and pl.name == "Триал"):
        bp = await get_base_subscription_plan(session)
        if bp is not None:
            sub.plan_id = bp.id
    u = await session.get(User, user_id)
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
    return True, ""


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
async def cb_admin_add_months_start(
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
    await state.set_state(AdminSubscriptionStates.waiting_add_months)
    await cq.answer()
    if cq.message and cq.bot:
        chat_id = cq.message.chat.id
        await _try_delete_message(cq.bot, chat_id, cq.message.message_id)
        sent = await cq.bot.send_message(
            chat_id,
            esc("Введите целое число месяцев для продления подписки (1-120)."),
            reply_markup=_admin_months_quick_markup(user_id, sub_id),
        )
        await state.update_data(
            admin_add_months_sub_id=sub_id,
            admin_add_months_user_id=user_id,
            admin_add_months_prompt_mid=sent.message_id,
        )
    else:
        await state.update_data(admin_add_months_sub_id=sub_id, admin_add_months_user_id=user_id)


@router.callback_query(F.data.startswith("admin:am:"))
async def cb_admin_add_months_quick(
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
    parts = (cq.data or "").split(":")
    if len(parts) < 5:
        await cq.answer("Неверные данные", show_alert=True)
        return
    try:
        user_id = int(parts[2])
        sub_id = int(parts[3])
        months = int(parts[4])
    except ValueError:
        await cq.answer("Неверные данные", show_alert=True)
        return
    if months < 1 or months > 120:
        await cq.answer("Допустимо от 1 до 120.", show_alert=True)
        return

    sub = (
        await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.id == sub_id, Subscription.user_id == user_id)
        )
    ).scalar_one_or_none()
    if sub is None:
        await cq.answer("Подписка не найдена", show_alert=True)
        return

    sub.expires_at = _add_calendar_months(sub.expires_at, months)
    pl = sub.plan
    if not (sub.status == "trial" and pl is not None and pl.name == "Триал"):
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
            logger.warning("admin quick add months RW failed: %s", e)
    await session.commit()
    await cq.answer(f"Продлено на {months} мес.")
    await _render_user_card(cq, session, user_id=user_id)


@router.message(StateFilter(AdminSubscriptionStates.waiting_add_months), F.text)
async def msg_admin_add_months(
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
    sub_id = data.get("admin_add_months_sub_id")
    user_id = data.get("admin_add_months_user_id")
    prompt_mid = data.get("admin_add_months_prompt_mid")

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
        await message.answer("Нужно целое число.")
        return
    months = int(raw)
    if months < 1 or months > 120:
        await _del_admin_input()
        await message.answer("Допустимо от 1 до 120.")
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

    sub.expires_at = _add_calendar_months(sub.expires_at, months)
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
            logger.warning("admin add months RW failed: %s", e)

    await session.commit()
    viewer = message.from_user.id if message.from_user else None
    built = await _build_user_card(session, user_id=user_id, viewer_telegram_id=viewer)
    if built is None or message.bot is None:
        await message.answer(f"Подписка продлена на: {months} мес.")
        return
    cap, kb = built
    await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=join_lines(plain(f"✅ Подписка продлена на {months} мес."), "", cap),
        reply_markup=kb,
        settings=settings,
        delete_message=None,
        photo_key="admin:users:card",
    )


@router.callback_query(F.data.startswith("admin:days:"))
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
    parts = (cq.data or "").split(":")
    if len(parts) != 4:
        await cq.answer("Неверные данные", show_alert=True)
        return
    try:
        user_id = int(parts[2])
        sub_id = int(parts[3])
    except ValueError:
        await cq.answer("Неверные данные", show_alert=True)
        return
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
            esc("Введите целое число дней для добавления к сроку подписки (1–3650)."),
            reply_markup=_admin_days_quick_markup(user_id, sub_id),
        )
        await state.update_data(
            admin_add_days_sub_id=sub_id,
            admin_add_days_user_id=user_id,
            admin_add_days_prompt_mid=sent.message_id,
        )
    else:
        await state.update_data(admin_add_days_sub_id=sub_id, admin_add_days_user_id=user_id)


@router.callback_query(F.data.startswith("admin:dayq:"))
async def cb_admin_add_days_quick(
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
    parts = (cq.data or "").split(":")
    if len(parts) != 5:
        await cq.answer("Неверные данные", show_alert=True)
        return
    try:
        user_id = int(parts[2])
        sub_id = int(parts[3])
        days = int(parts[4])
    except ValueError:
        await cq.answer("Неверные данные", show_alert=True)
        return
    settings = get_settings()
    ok, err = await _apply_subscription_add_days(
        session, user_id=user_id, sub_id=sub_id, days=days, settings=settings
    )
    if not ok:
        await cq.answer(err[:200], show_alert=True)
        return
    await session.commit()
    await cq.answer(f"+{days} дн.")
    await _render_user_card(cq, session, user_id=user_id)


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
        await message.answer("Нужно целое число.")
        return
    days = int(raw)
    if days < 1 or days > 3650:
        await _del_admin_input()
        await message.answer("Допустимо от 1 до 3650 дней.")
        return

    settings = get_settings()
    ok, err = await _apply_subscription_add_days(
        session, user_id=user_id, sub_id=sub_id, days=days, settings=settings
    )
    if not ok:
        await _del_admin_input()
        await message.answer(err)
        return

    await _del_admin_input()
    if message.bot and prompt_mid is not None:
        await _try_delete_message(message.bot, message.chat.id, int(prompt_mid))
    await state.clear()
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
        caption=join_lines(plain(f"✅ К сроку подписки добавлено {days} дн."), "", cap),
        reply_markup=kb,
        settings=settings,
        delete_message=None,
        photo_key="admin:users:card",
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


@router.callback_query(F.data.startswith("admin:rb:"))
async def cb_admin_reset_balance(
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
    u = await session.get(User, uid)
    if u is None:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    before = u.balance
    u.balance = Decimal("0")
    session.add(
        Transaction(
            user_id=u.id,
            type="admin_balance_reset",
            amount=Decimal("0"),
            currency="RUB",
            payment_provider="admin",
            payment_id=None,
            status="completed",
            description=f"Админ обнулил баланс: {before} ₽ -> 0 ₽ (admin #{db_user.id})",
            meta={"admin_id": db_user.id, "balance_before": str(before), "balance_after": "0"},
        )
    )
    await session.flush()
    settings = get_settings()
    if settings.billing_v2_enabled and u.billing_mode == "hybrid":
        try:
            await baseline_meter_at_hybrid_transition(session, user=u, settings=settings)
        except Exception:
            logger.exception("baseline_meter after admin balance reset failed user_id=%s", u.id)
    await session.commit()
    await cq.answer("Баланс обнулён")
    await _render_user_card(cq, session, user_id=uid)


@router.callback_query(F.data.startswith("admin:rwcheck:"))
async def cb_admin_rwcheck_start(
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
    uid = _parse_admin_user_id(cq.data or "")
    if uid is None:
        await cq.answer("Неверный id", show_alert=True)
        return
    await state.set_state(AdminSubscriptionStates.waiting_rw_lookup_query)
    await state.update_data(admin_rw_lookup_user_id=uid)
    await cq.answer()
    if cq.message and cq.bot:
        chat_id = cq.message.chat.id
        await _try_delete_message(cq.bot, chat_id, cq.message.message_id)
        sent = await cq.bot.send_message(
            chat_id,
            esc("Введите Telegram ID или @username для поиска подписки в Remnawave."),
        )
        await state.update_data(admin_rw_lookup_prompt_mid=sent.message_id)


@router.callback_query(F.data.startswith("admin:mlink:"))
async def cb_admin_manual_link_start(
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
    uid = _parse_admin_user_id(cq.data or "")
    if uid is None:
        await cq.answer("Неверный id", show_alert=True)
        return
    await state.set_state(AdminSubscriptionStates.waiting_manual_bind_subscription_id)
    await state.update_data(admin_manual_bind_user_id=uid)
    await cq.answer()
    if cq.message and cq.bot:
        chat_id = cq.message.chat.id
        await _try_delete_message(cq.bot, chat_id, cq.message.message_id)
        sent = await cq.bot.send_message(
            chat_id,
            esc("Введите ID подписки в базе (например 104), чтобы привязать её к этому пользователю."),
        )
        await state.update_data(admin_manual_bind_prompt_mid=sent.message_id)


@router.message(StateFilter(AdminSubscriptionStates.waiting_rw_lookup_query), F.text)
async def msg_admin_rw_lookup(
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
    user_id = data.get("admin_rw_lookup_user_id")
    prompt_mid = data.get("admin_rw_lookup_prompt_mid")
    if not isinstance(user_id, int):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if message.bot:
        await _try_delete_message(message.bot, message.chat.id, message.message_id)
        if prompt_mid is not None:
            await _try_delete_message(message.bot, message.chat.id, int(prompt_mid))
    if not raw:
        await message.answer("Введите Telegram ID или @username.")
        return
    settings = get_settings()
    if settings.remnawave_stub:
        await state.clear()
        await message.answer("REMNAWAVE_STUB включён: проверка в реальной панели недоступна.")
        return
    rw = RemnaWaveClient(settings)
    try:
        hit, mode = await _find_rw_user_by_tg_or_username(rw, query=raw)
    except RemnaWaveError as e:
        await state.clear()
        await message.answer(f"Ошибка обращения к Remnawave: {e}")
        return
    await state.clear()
    if hit is None:
        await message.answer("В панели Remnawave ничего не найдено.")
        return
    rw_uuid = str(hit.get("uuid") or "—")
    rw_un = str(hit.get("username") or hit.get("tag") or "—")
    rw_tid = str(hit.get("telegramId") or hit.get("telegram_id") or "—")
    rw_status = str(hit.get("status") or "—")
    rw_exp = str(hit.get("expireAt") or "—")
    await message.answer(
        join_lines(
            "✅ " + bold("Найдена запись в Remnawave"),
            plain("Поиск: ") + bold("Telegram ID") if mode == "telegram_id" else plain("Поиск: ") + bold("Username"),
            plain("UUID: ") + code(rw_uuid),
            plain("Username/tag: ") + code(rw_un),
            plain("Telegram ID: ") + code(rw_tid),
            plain("Статус: ") + bold(rw_status),
            plain("Истекает: ") + code(rw_exp),
            "",
            plain("Открываю карточку пользователя…"),
        )
    )
    if message.bot:
        cap_kb = await _build_user_card(session, user_id=user_id, viewer_telegram_id=message.from_user.id)
        if cap_kb is not None:
            cap, kb = cap_kb
            await send_profile_screen(
                message.bot,
                chat_id=message.chat.id,
                caption=cap,
                reply_markup=kb,
                settings=settings,
                delete_message=None,
                photo_key="admin:find",
            )


@router.message(StateFilter(AdminSubscriptionStates.waiting_manual_bind_subscription_id), F.text)
async def msg_admin_manual_bind_subscription(
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
    user_id = data.get("admin_manual_bind_user_id")
    prompt_mid = data.get("admin_manual_bind_prompt_mid")
    if not isinstance(user_id, int):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if message.bot:
        await _try_delete_message(message.bot, message.chat.id, message.message_id)
        if prompt_mid is not None:
            await _try_delete_message(message.bot, message.chat.id, int(prompt_mid))
    if not raw.isdigit():
        await message.answer("Нужен числовой ID: локальной подписки или пользователя Remnawave (например 105).")
        return
    typed_id = int(raw)
    target_user = await session.get(User, user_id)
    if target_user is None:
        await state.clear()
        await message.answer("Пользователь не найден.")
        return
    sub = await session.get(Subscription, typed_id)
    used_panel_id = False
    if sub is None:
        settings = get_settings()
        if settings.remnawave_stub:
            await state.clear()
            await message.answer("Подписка не найдена в локальной БД.")
            return
        rw = RemnaWaveClient(settings)
        try:
            rw_user = await rw.find_user_by_panel_id(typed_id)
        except RemnaWaveError as e:
            await state.clear()
            await message.answer(f"Ошибка Remnawave: {e}")
            return
        if rw_user is None:
            await state.clear()
            await message.answer("Не найдено: ни локальная подписка, ни пользователь Remnawave с таким ID.")
            return
        rw_uuid_raw = str(rw_user.get("uuid") or "").strip()
        if not rw_uuid_raw:
            await state.clear()
            await message.answer("У пользователя Remnawave нет UUID.")
            return
        try:
            rw_uuid = UUID(rw_uuid_raw)
        except ValueError:
            await state.clear()
            await message.answer("UUID пользователя Remnawave некорректный.")
            return
        sub = (
            await session.execute(
                select(Subscription)
                .where(Subscription.remnawave_sub_uuid == rw_uuid)
                .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if sub is None:
            await state.clear()
            await message.answer("В панели пользователь найден, но локальная подписка с его UUID не найдена.")
            return
        used_panel_id = True
    prev_user_id = sub.user_id
    sub.user_id = target_user.id
    if sub.remnawave_sub_uuid is not None:
        target_user.remnawave_uuid = sub.remnawave_sub_uuid
    dev_rows = await session.execute(select(Device).where(Device.subscription_id == sub.id))
    for d in dev_rows.scalars().all():
        d.user_id = target_user.id
    await session.commit()
    await state.clear()
    await message.answer(
        join_lines(
            "✅ " + bold("Подписка привязана"),
            plain("Подписка #") + bold(str(sub.id)) + plain(" теперь у пользователя #") + bold(str(target_user.id)),
            plain("Ранее была у пользователя #") + bold(str(prev_user_id)),
            plain("Введённый ID: ") + code(str(typed_id)),
            plain("Режим: ") + bold("ID пользователя Remnawave" if used_panel_id else "локальный ID подписки"),
        )
    )
    if message.bot:
        cap_kb = await _build_user_card(session, user_id=target_user.id, viewer_telegram_id=message.from_user.id)
        if cap_kb is not None:
            cap, kb = cap_kb
            await send_profile_screen(
                message.bot,
                chat_id=message.chat.id,
                caption=cap,
                reply_markup=kb,
                settings=get_settings(),
                delete_message=None,
                photo_key="admin:find",
            )


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
    txn_bal = Transaction(
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
    session.add(txn_bal)
    await session.flush()
    settings = get_settings()
    await apply_balance_credit_followups(
        session,
        user=u,
        credited=amount,
        settings=settings,
        triggering_txn=txn_bal,
        grant_referrer_reward=True,
        try_smart_cart=True,
    )

    await session.commit()

    viewer = message.from_user.id if message.from_user else None
    built = await _build_user_card(session, user_id=user_id, viewer_telegram_id=viewer)
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
        photo_key="admin:users:card",
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
    b.row(
        InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:find_cancel", style="danger")
    )
    sent = await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "🔎 " + bold("Поиск пользователя"),
            "",
            plain("Можно искать по:"),
            plain("• имени"),
            plain("• @username"),
            plain("• Telegram ID"),
            plain("• ID в боте"),
            plain("• ID подписки"),
            plain("• UUID в панели"),
            "",
            plain("Отправьте одно значение одним сообщением."),
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
    await cb_admin_section_users(cq, db_user)


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
    if not raw:
        await message.answer(esc("Введите значение для поиска."))
        return

    if message.bot:
        await _try_delete_message(message.bot, message.chat.id, message.message_id)
        await _try_delete_message(
            message.bot, message.chat.id, int(prompt_mid) if prompt_mid is not None else None
        )
        await _try_delete_message(
            message.bot, message.chat.id, int(last_res) if last_res is not None else None
        )

    typed = raw.strip()
    typed_username = typed[1:] if typed.startswith("@") else typed
    q_cf = typed_username.casefold()
    users: list[User] = []
    if typed.isdigit():
        if len(typed) <= 19:
            n = int(typed)
        else:
            n = _INT64_MAX + 1
        if n <= _INT64_MAX:
            conds = [User.telegram_id == n]
            if n <= _INT32_MAX:
                conds.append(User.id == n)
                sub_user_id = (
                    await session.execute(
                        select(Subscription.user_id).where(Subscription.id == n).limit(1)
                    )
                ).scalar_one_or_none()
                if sub_user_id is not None:
                    conds.append(User.id == int(sub_user_id))
            users = list(
                (
                    await session.execute(
                        select(User).where(or_(*conds)).order_by(desc(User.id)).limit(20)
                    )
                ).scalars()
            )
        else:
            users = []
    else:
        users = list(
            (
                await session.execute(
                    select(User)
                    .where(
                        or_(
                            func.lower(func.coalesce(User.username, "")).like(f"%{q_cf}%"),
                            func.lower(func.coalesce(User.first_name, "")).like(f"%{q_cf}%"),
                            func.lower(func.coalesce(User.last_name, "")).like(f"%{q_cf}%"),
                            func.lower(func.cast(User.remnawave_uuid, String)).like(f"%{q_cf}%"),
                        )
                    )
                    .order_by(desc(User.id))
                    .limit(20)
                )
            ).scalars()
        )
    settings = get_settings()

    if not users:
        kb_nf = InlineKeyboardBuilder()
        kb_nf.row(
            InlineKeyboardButton(
                text="⬅️ Пользователи",
                callback_data="admin:section:users",
                style="danger",
            )
        )
        sent = await send_profile_screen(
            message.bot,
            chat_id=message.chat.id,
            caption=join_lines(
                "🔎 " + bold("Ничего не найдено"),
                plain("Запрос: ") + code(typed),
                plain("Попробуйте другой @username, имя, ID или UUID из панели."),
            ),
            reply_markup=kb_nf.as_markup(),
            settings=settings,
            photo_key="admin:find",
        )
        await state.update_data(find_last_result_mid=sent.message_id, find_prompt_mid=None)
        await state.set_state(None)
        return
    if len(users) > 1:
        b = InlineKeyboardBuilder()
        for u in users[:8]:
            nm = (u.first_name or u.username or "?").strip()
            if len(nm) > 18:
                nm = nm[:17] + "…"
            b.row(
                InlineKeyboardButton(
                    text=f"👤 #{u.id} {nm}"[:64],
                    callback_data=f"admin:u:{u.id}",
                )
            )
        b.row(
            InlineKeyboardButton(
                text="⬅️ Пользователи",
                callback_data="admin:section:users",
                style="danger",
            )
        )
        sent = await send_profile_screen(
            message.bot,
            chat_id=message.chat.id,
            caption=join_lines(
                "🔎 " + bold("Найдено несколько пользователей"),
                plain("Запрос: ") + code(typed),
                plain("Выберите нужного из списка ниже."),
            ),
            reply_markup=b.as_markup(),
            settings=settings,
            delete_message=None,
            photo_key="admin:find",
        )
        await state.update_data(find_last_result_mid=sent.message_id, find_prompt_mid=None)
        await state.set_state(None)
        return
    u = users[0]

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
    adm.row(
        InlineKeyboardButton(
            text="🛠 Карточка", callback_data=f"admin:u:{u.id}"
        )
    )
    adm.row(
        InlineKeyboardButton(
            text="⬅️ Пользователи", callback_data="admin:section:users", style="danger"
        )
    )
    if message.bot is None:
        return
    sent2 = await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=join_lines(*lines),
        reply_markup=adm.as_markup(),
        settings=settings,
        delete_message=None,
        photo_key="admin:find",
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
        reply_markup=await admin_panel_keyboard(),
        settings=settings,
        delete_message=None,
        photo_key="admin:panel",
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
        InlineKeyboardButton(
            text="✅ Продолжить к проверкам",
            callback_data="admin:reset:proceed",
            style="danger",
        ),
    )
    b.row(
        InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:reset:cancel", style="danger")
    )
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
        reply_markup=await admin_panel_keyboard(),
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


def _broadcast_input_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data="admin:broadcast_cancel", style="danger"
        )
    )
    return kb.as_markup()


def _broadcast_confirm_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="✅ Отправить всем", callback_data="admin:broadcast_go"
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data="admin:broadcast_cancel", style="danger"
        )
    )
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
    if cq.message is None:
        return
    sent = await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "📢 " + bold("Рассылка всем пользователям"),
            "",
            plain("Отправьте одним сообщением текст черновика."),
            plain("Поддерживается разметка формата Telegram MarkdownV2:"),
            "",
            plain("Примеры: ")
            + code("**жирный**")
            + plain(", ")
            + code("_курсив_")
            + plain(", ")
            + code("__подчёркнутый__")
            + plain(", "),
            plain("ссылка: ") + code("[текст](https://example.com)"),
            plain("код: ") + code("`фрагмент`") + plain(", блок: ") + code("```блок```"),
            "",
            plain("Смайлики можно вставлять как обычно. До ")
            + code(str(MAX_MESSAGE_LEN))
            + plain(" символов в итоговом сообщении."),
            "",
            italic("Не получат пользователи, отмеченные в боте как заблокированные."),
        ),
        reply_markup=_broadcast_input_markup(),
        settings=get_settings(),
    )
    await state.update_data(
        broadcast_prompt_mid=(sent.message_id if sent else None),
        broadcast_preview_mid=None,
    )

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
    await cb_admin_section_analytics(cq, db_user)


@router.message(StateFilter(AdminBroadcastStates.waiting_text), F.text)
async def msg_broadcast_receive_text(
    message: Message,
    state: FSMContext,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    if (message.text or "").strip().startswith("/"):
        await message.answer(esc("Пришлите текст рассылки обычным сообщением или нажмите «Назад»."))
        return
    data0 = await state.get_data()
    old_prompt_mid = data0.get("broadcast_prompt_mid")
    old_preview_mid = data0.get("broadcast_preview_mid")
    if message.bot is not None:
        await _try_delete_message(message.bot, message.chat.id, message.message_id)
        if isinstance(old_prompt_mid, int):
            await _try_delete_message(message.bot, message.chat.id, old_prompt_mid)
        if isinstance(old_preview_mid, int):
            await _try_delete_message(message.bot, message.chat.id, old_preview_mid)

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
        sent = await message.answer(
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

    await state.update_data(broadcast_text=raw, broadcast_preview_mid=sent.message_id)
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
