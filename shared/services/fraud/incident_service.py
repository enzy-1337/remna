"""Единая точка входа для всех детекторов антифрода.

Пишет FraudIncident, по стадии детектора (FraudDetectorState: learning/advisory/autonomous)
и уверенности решает действие, при необходимости блокирует пользователя и всегда шлёт
(если стадия не learning) алерт с кнопками в нужный топик. Все детекторы (blacklist,
hwid_collision, ip_hop, traffic_spike) должны звать record_incident, а не слать алерты сами —
это единственное место, знающее про staged rollout и confidence-gate.

См. план: C:\\Users\\admin\\.claude\\plans\\misty-growing-seahorse.md, разделы 3-4.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import bold, plain
from shared.models.fraud_detector_state import FraudDetectorState
from shared.models.fraud_incident import FraudIncident
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import admin_user_profile_url, notify_admin_with_keyboard
from shared.services.fraud.actions import block_user_for_fraud
from shared.services.fraud.keyboards import autoblock_keyboard, suspicion_keyboard

logger = logging.getLogger(__name__)

_DETECTOR_TITLES = {
    "ip_hop": "🌐 Подозрение: смена IP",
    "hwid_collision": "📱 Подозрение: мультиаккаунт (HWID)",
    "traffic_spike": "📈 Подозрение: шеринг подписки (всплеск трафика)",
    "blacklist": "⛔ Внешний чёрный список",
}


async def record_incident(
    session: AsyncSession,
    settings: Settings,
    *,
    user: User,
    detector: str,
    severity: str,
    confidence: float,
    reason_text: str,
    evidence: dict | None = None,
    activity_lines: list[str] | None = None,
    subscription_id: int | None = None,
    event_ts: datetime | None = None,
    force_auto_block: bool = False,
) -> FraudIncident | None:
    """
    force_auto_block=True — для событий вне staged rollout (жёсткое IP-правило 10 IP/15с,
    внешний blacklist, повторный HWID): решение всегда "auto_blocked", FraudDetectorState
    не смотрим.

    Админы бота и lifetime-подписки (см. exemptions.is_fraud_exempt) полностью игнорируются —
    ни инцидент не создаётся, ни алерт не шлётся; возвращает None.
    """
    from shared.services.fraud.exemptions import is_fraud_exempt

    if await is_fraud_exempt(session, settings, user=user):
        return None

    stage = "immediate"
    action = "auto_blocked" if force_auto_block else "none"

    if not force_auto_block:
        row = await session.get(FraudDetectorState, detector)
        stage = row.stage if row is not None else "learning"
        if stage == "learning":
            action = "none"
        elif stage == "advisory":
            action = "advisory_sent"
        elif stage == "autonomous":
            threshold = row.auto_block_confidence_threshold if row is not None else 0.9
            action = "auto_blocked" if confidence >= threshold else "advisory_sent"
        else:
            action = "advisory_sent"

    incident = FraudIncident(
        user_id=user.id,
        subscription_id=subscription_id,
        detector=detector,
        severity=severity,
        confidence=confidence,
        stage_at_detection=stage,
        evidence=evidence,
        action_taken=action,
        status="open",
        event_ts=event_ts or datetime.now(timezone.utc),
    )
    session.add(incident)
    await session.flush()

    if action == "none":
        return incident

    title = plain(_DETECTOR_TITLES.get(detector, "🚩 Подозрение антифрода"))
    lines = [plain(reason_text), plain(f"Уверенность: {confidence:.0%}")]
    if activity_lines:
        lines.append(bold("Активность:"))
        lines.extend(plain(line) for line in activity_lines[:10])

    if action == "auto_blocked":
        await block_user_for_fraud(
            session, settings, user, reason=f"fraud:{detector}:incident_{incident.id}"
        )
        profile_url = admin_user_profile_url(settings, user) or None
        mid = await notify_admin_with_keyboard(
            settings,
            title="🚫 " + bold("Автоблокировка") + " · " + title,
            lines=lines,
            event_type=f"fraud_autoblock_{detector}",
            topic=AdminLogTopic.FRAUD_AUTOBLOCK,
            reply_markup=autoblock_keyboard(incident.id, profile_url),
            subject_user=user,
            session=session,
        )
    else:
        mid = await notify_admin_with_keyboard(
            settings,
            title=title,
            lines=lines,
            event_type=f"fraud_suspicion_{detector}",
            topic=AdminLogTopic.FRAUD_SUSPICION,
            reply_markup=suspicion_keyboard(incident.id),
            subject_user=user,
            session=session,
        )

    incident.topic_message_id = mid
    return incident
