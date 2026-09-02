"""Детектор мультиаккаунта: один HWID активен под разными Telegram-аккаунтами.

Вызывается из webhook_ingress_service.process_remnawave_event() на device.attached /
user_hwid_devices.added, сразу после add_device_history_event() и до штатного уведомления
в тему DEVICES.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import plain
from shared.models.device_history import DeviceHistory
from shared.models.fraud_incident import FraudIncident
from shared.models.user import User
from shared.services.admin_notify import admin_device_hwid_ref, admin_user_ref
from shared.services.family_service import resolve_account_user_id
from shared.services.fraud.confidence import hwid_collision_confidence
from shared.services.fraud.incident_service import record_incident


async def _recent_activity_lines(
    session: AsyncSession, *, user_id: int, hwid: str, limit: int = 5
) -> list[str]:
    rows = (
        await session.execute(
            select(DeviceHistory.event_type, DeviceHistory.event_ts)
            .where(DeviceHistory.user_id == user_id, DeviceHistory.device_hwid == hwid)
            .order_by(DeviceHistory.event_ts.desc())
            .limit(limit)
        )
    ).all()
    return [f"{ts:%Y-%m-%d %H:%M} — {ev}" for ev, ts in rows]


async def check_hwid_collision(
    session: AsyncSession,
    settings: Settings,
    *,
    user: User,
    hwid: str,
) -> None:
    """Ищет другой активный аккаунт с тем же HWID; если есть (и это не своя же семейная
    подписка) — считает confidence и заводит FraudIncident через record_incident
    (staged rollout, alert с кнопками)."""
    other_row = (
        await session.execute(
            select(DeviceHistory.user_id, func.min(DeviceHistory.event_ts).label("first_ts"))
            .where(
                DeviceHistory.device_hwid == hwid,
                DeviceHistory.user_id != user.id,
                DeviceHistory.is_active.is_(True),
            )
            .group_by(DeviceHistory.user_id)
            .order_by(func.min(DeviceHistory.event_ts).asc())
            .limit(1)
        )
    ).first()
    if other_row is None:
        return

    other_user_id, other_first_ts = other_row
    other_user = await session.get(User, other_user_id)
    if other_user is None:
        return

    # Семейная подписка — оба аккаунта резолвятся к одному владельцу, легитимный шеринг,
    # детектор вообще не должен на это срабатывать.
    this_account = await resolve_account_user_id(session, user.id)
    other_account = await resolve_account_user_id(session, other_user_id)
    if this_account == other_account:
        return

    distinct_accounts = (
        await session.execute(
            select(func.count(func.distinct(DeviceHistory.user_id))).where(
                DeviceHistory.device_hwid == hwid
            )
        )
    ).scalar_one()
    is_repeat_offender = int(distinct_accounts or 0) >= 3

    now = datetime.now(timezone.utc)
    other_established = other_first_ts is not None and (now - other_first_ts).total_seconds() >= 86400
    # ВАЖНО: other_established/is_repeat_offender НЕ форсируют автоблок мимо confidence-gate
    # (в отличие от жёсткого IP-правила 10 IP/15с и внешнего blacklist, которые действительно
    # однозначны). Живой пример бага: HWID «устоявшийся» сутки назад давало confidence 0.75,
    # но force_auto_block=True всё равно молча блокировало пользователя, игнорируя порог
    # auto_block_confidence_threshold — только помечаем событие как severity="hard_violation"
    # для истории/приоритета в списке, решение остаётся за staged rollout в record_incident.

    prior_dismissed = (
        await session.execute(
            select(func.count())
            .select_from(FraudIncident)
            .where(
                FraudIncident.user_id == user.id,
                FraudIncident.detector == "hwid_collision",
                FraudIncident.action_taken == "dismissed",
            )
        )
    ).scalar_one()

    confidence, breakdown = hwid_collision_confidence(
        other_account_established=other_established,
        is_repeat_offender=is_repeat_offender,
        prior_incidents_dismissed=int(prior_dismissed or 0),
        family_linked=False,
    )

    activity = await _recent_activity_lines(session, user_id=user.id, hwid=hwid)
    activity += await _recent_activity_lines(session, user_id=other_user_id, hwid=hwid)

    # "Жёстче" по маркировке (severity), но НЕ мгновенный автобан мимо confidence-gate — см.
    # комментарий выше про force_auto_block. HWID устоявшийся сутки+ у другого аккаунта или
    # засветившийся на 3+ аккаунтах — более тревожный случай, чем обычная коллизия, но
    # семейные/поддержка/легитимная перепродажа устройства всё ещё могут его объяснить,
    # поэтому решение остаётся за stage/confidence как у всех остальных суспишенов.
    hard_violation = other_established or is_repeat_offender

    reason_text = (
        plain("HWID ") + admin_device_hwid_ref(settings, hwid)
        + plain(" уже привязан к аккаунту ") + admin_user_ref(settings, other_user)
        + plain(f" (tg {other_user.telegram_id}), теперь активен и у ")
        + admin_user_ref(settings, user) + plain(".")
    )

    await record_incident(
        session,
        settings,
        user=user,
        detector="hwid_collision",
        severity="hard_violation" if hard_violation else "suspicious",
        confidence=confidence,
        reason_text=reason_text,
        evidence={
            "hwid": hwid,
            "other_user_id": other_user_id,
            "other_telegram_id": other_user.telegram_id,
            "is_repeat_offender": is_repeat_offender,
            "other_account_established": other_established,
            "distinct_accounts_ever": int(distinct_accounts or 0),
            "confidence_breakdown": breakdown,
        },
        activity_lines=activity,
    )
