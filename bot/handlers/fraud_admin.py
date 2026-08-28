"""Коллбэки кнопок антифрод-алертов: Заблокировать/На учёт/Пропустить/Разблокировать.

Зеркалит паттерн admin:block:/admin:unblock: из bot/handlers/admin.py:1715-1764,
но адресуется по incident_id (не user_id напрямую) — Watch/Dismiss относятся
к конкретному инциденту, а не только к пользователю.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.models.fraud_incident import FraudIncident
from shared.models.user import User
from shared.models.user_fraud_state import UserFraudState
from shared.services.fraud.actions import block_user_for_fraud, unblock_user_for_fraud

logger = logging.getLogger(__name__)

router = Router(name="fraud_admin")


def _parse_incident_id(data: str) -> int | None:
    try:
        return int(data.split(":")[2])
    except (IndexError, ValueError):
        return None


async def _load_incident(session: AsyncSession, incident_id: int) -> tuple[FraudIncident, User] | None:
    incident = await session.get(FraudIncident, incident_id)
    if incident is None:
        return None
    user = await session.get(User, incident.user_id)
    if user is None:
        return None
    return incident, user


async def _resolve_message(cq: CallbackQuery, incident: FraudIncident, suffix: str) -> None:
    """Убирает кнопки и дописывает итог решения. Best-effort — сообщение могло
    быть отправлено в другую тему/чат либо уже отредактировано, тогда просто молчим."""
    if incident.topic_message_id is None or cq.message is None or cq.bot is None:
        return
    try:
        base_text = cq.message.text or cq.message.caption or ""
        await cq.bot.edit_message_text(
            chat_id=cq.message.chat.id,
            message_id=incident.topic_message_id,
            text=(base_text + suffix)[:4096],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
    except Exception:
        logger.debug("fraud_admin: message edit failed", exc_info=True)


@router.callback_query(F.data.startswith("fraud:block:"))
async def cb_fraud_block(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    incident_id = _parse_incident_id(cq.data or "")
    if incident_id is None:
        await cq.answer("Неверный id", show_alert=True)
        return
    loaded = await _load_incident(session, incident_id)
    if loaded is None:
        await cq.answer("Инцидент не найден", show_alert=True)
        return
    incident, user = loaded
    settings = get_settings()
    await block_user_for_fraud(
        session, settings, user, reason=f"fraud:{incident.detector}:incident_{incident.id}"
    )
    incident.action_taken = "admin_blocked"
    incident.status = "resolved"
    incident.resolved_by = str(cq.from_user.id)
    incident.resolved_at = datetime.now(timezone.utc)
    await session.commit()
    await _resolve_message(cq, incident, "\n\n🚫 Заблокировано администратором.")
    await cq.answer("Заблокировано")


@router.callback_query(F.data.startswith("fraud:watch:"))
async def cb_fraud_watch(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    incident_id = _parse_incident_id(cq.data or "")
    if incident_id is None:
        await cq.answer("Неверный id", show_alert=True)
        return
    loaded = await _load_incident(session, incident_id)
    if loaded is None:
        await cq.answer("Инцидент не найден", show_alert=True)
        return
    incident, user = loaded
    now = datetime.now(timezone.utc)
    state = await session.get(UserFraudState, user.id)
    if state is None:
        state = UserFraudState(user_id=user.id)
        session.add(state)
    state.is_watched = True
    state.watched_reason = f"incident_{incident.id}:{incident.detector}"
    state.watched_at = now
    incident.action_taken = "watched"
    incident.status = "resolved"
    incident.resolved_by = str(cq.from_user.id)
    incident.resolved_at = now
    await session.commit()
    await _resolve_message(cq, incident, "\n\n👁 Пользователь взят на учёт.")
    await cq.answer("На учёте")


@router.callback_query(F.data.startswith("fraud:dismiss:"))
async def cb_fraud_dismiss(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    incident_id = _parse_incident_id(cq.data or "")
    if incident_id is None:
        await cq.answer("Неверный id", show_alert=True)
        return
    loaded = await _load_incident(session, incident_id)
    if loaded is None:
        await cq.answer("Инцидент не найден", show_alert=True)
        return
    incident, _user = loaded
    incident.action_taken = "dismissed"
    incident.status = "resolved"
    incident.resolved_by = str(cq.from_user.id)
    incident.resolved_at = datetime.now(timezone.utc)
    await session.commit()
    await _resolve_message(cq, incident, "\n\n✅ Пропущено.")
    await cq.answer("Пропущено")


@router.callback_query(F.data.startswith("fraud:unblock:"))
async def cb_fraud_unblock(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    incident_id = _parse_incident_id(cq.data or "")
    if incident_id is None:
        await cq.answer("Неверный id", show_alert=True)
        return
    loaded = await _load_incident(session, incident_id)
    if loaded is None:
        await cq.answer("Инцидент не найден", show_alert=True)
        return
    incident, user = loaded
    settings = get_settings()
    await unblock_user_for_fraud(session, settings, user)
    incident.status = "resolved"
    incident.resolved_by = str(cq.from_user.id)
    incident.resolved_at = datetime.now(timezone.utc)
    await session.commit()
    await _resolve_message(cq, incident, "\n\n✅ Разблокировано администратором.")
    await cq.answer("Разблокировано")
