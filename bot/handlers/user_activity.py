"""Единая лента активности пользователя (транзакции, устройства, инциденты антифрода)
для карточки пользователя в админке бота — кнопка "📜 Активность"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.utils.screen_photo import answer_callback_with_photo_screen
from shared.config import get_settings
from shared.md2 import bold, code, join_lines, plain
from shared.models.device_history import DeviceHistory
from shared.models.fraud_incident import FraudIncident
from shared.models.transaction import Transaction
from shared.models.user import User

router = Router(name="user_activity")

_MSK_TZ = ZoneInfo("Europe/Moscow")
_LIMIT = 20

_TXN_TYPE_LABELS = {
    "admin_balance_add": "Админ пополнил баланс",
    "admin_mass_balance_add": "Массовая выдача баланса",
    "topup": "Пополнение",
    "purchase": "Покупка",
}

_DEVICE_EVENT_LABELS = {
    "device.attached": "Устройство подключено",
    "device.detached": "Устройство отключено",
    "user_hwid_devices.added": "HWID добавлен",
    "user_hwid_devices.deleted": "HWID удалён",
}


@dataclass
class _Event:
    ts: datetime
    line: str


def _fmt_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK_TZ).strftime("%d.%m %H:%M")


async def _collect_events(session: AsyncSession, user_id: int) -> list[_Event]:
    events: list[_Event] = []

    txns = list(
        (
            await session.execute(
                select(Transaction)
                .where(Transaction.user_id == user_id)
                .order_by(desc(Transaction.created_at))
                .limit(_LIMIT)
            )
        ).scalars()
    )
    for t in txns:
        label = _TXN_TYPE_LABELS.get(t.type, t.type)
        sign = "+" if t.amount >= 0 else ""
        events.append(
            _Event(
                ts=t.created_at,
                line=f"💳 {_fmt_ts(t.created_at)} · {label}: {sign}{t.amount} {t.currency} ({t.status})",
            )
        )

    devices = list(
        (
            await session.execute(
                select(DeviceHistory)
                .where(DeviceHistory.user_id == user_id)
                .order_by(desc(DeviceHistory.event_ts))
                .limit(_LIMIT)
            )
        ).scalars()
    )
    for d in devices:
        label = _DEVICE_EVENT_LABELS.get(d.event_type, d.event_type)
        hwid_short = (d.device_hwid or "")[:12]
        events.append(
            _Event(ts=d.event_ts, line=f"📱 {_fmt_ts(d.event_ts)} · {label} (hwid {hwid_short}…)")
        )

    incidents = list(
        (
            await session.execute(
                select(FraudIncident)
                .where(FraudIncident.user_id == user_id)
                .order_by(desc(FraudIncident.event_ts))
                .limit(_LIMIT)
            )
        ).scalars()
    )
    for f in incidents:
        events.append(
            _Event(
                ts=f.event_ts,
                line=(
                    f"🛡 {_fmt_ts(f.event_ts)} · Антифрод [{f.detector}] "
                    f"conf={f.confidence:.0%} → {f.action_taken}"
                ),
            )
        )

    events.sort(key=lambda e: e.ts, reverse=True)
    return events[:_LIMIT]


def _keyboard(user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ К карточке", callback_data=f"admin:u:{user_id}", style="danger"))
    return b.as_markup()


@router.callback_query(F.data.startswith("admin:activity:"))
async def cb_user_activity(
    cq: CallbackQuery, session: AsyncSession, db_user: User | None, is_bot_admin: bool = False
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    try:
        user_id = int((cq.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await cq.answer("Некорректные данные.", show_alert=True)
        return

    target = await session.get(User, user_id)
    if target is None:
        await cq.answer("Пользователь не найден.", show_alert=True)
        return

    events = await _collect_events(session, user_id)
    body = (
        join_lines(*(e.line for e in events))
        if events
        else plain("Активности не найдено.")
    )
    caption = join_lines(
        "📜 " + bold(f"Активность · #{target.id}"),
        plain(f"Telegram: ") + code(str(target.telegram_id)),
        "",
        body,
    )
    await cq.answer()
    await answer_callback_with_photo_screen(
        cq,
        caption=caption,
        reply_markup=_keyboard(user_id),
        settings=get_settings(),
        photo_key=f"admin:u:{user_id}",
    )
