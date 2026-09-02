"""Детектор шеринга подписки по всплеску трафика — для ВСЕХ пользователей вне зависимости
от billing_mode (в отличие от billing_v2.traffic_meter_poll_loop, который считает только
hybrid — см. TODO ниже).

TODO(hybrid-retirement): когда billing_mode="hybrid" уйдёт насовсем и останется только
legacy, у этого опроса и у того, что заменит traffic_meter_poll_loop, окажется во многом
пересекающийся набор пользователей и один и тот же вызов panel.get_user — стоит слить
в один проход. Сейчас не трогаем: hybrid ещё существует, это вне рамок текущей задачи.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.integrations.rw_traffic import extract_traffic_gb_from_rw_user
from shared.md2 import plain
from shared.models.traffic_sample import TrafficSample
from shared.models.user import User
from shared.services.fraud.incident_service import record_incident

logger = logging.getLogger(__name__)

_RETENTION = timedelta(hours=48)
_BYTES_PER_GB = 1024**3


async def _last_sample(session: AsyncSession, user_id: int) -> TrafficSample | None:
    return (
        await session.execute(
            select(TrafficSample)
            .where(TrafficSample.user_id == user_id)
            .order_by(TrafficSample.sampled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def check_user_traffic_spike(
    session: AsyncSession,
    settings: Settings,
    *,
    user: User,
) -> None:
    """Пишет TrafficSample и, если темп роста трафика с прошлого замера (спроецированный
    на fraud_traffic_spike_window_sec) превышает fraud_traffic_spike_gb_threshold — заводит
    инцидент через record_incident (staged rollout, как у остальных детекторов)."""
    if user.remnawave_uuid is None:
        return
    rw = RemnaWaveClient(settings)
    try:
        uinf = await rw.get_user(str(user.remnawave_uuid))
    except RemnaWaveError as e:
        logger.debug("traffic_spike: get_user failed user_id=%s: %s", user.id, e)
        return
    used_gb, _lim = extract_traffic_gb_from_rw_user(uinf)
    if used_gb is None:
        return
    raw_bytes = int(float(used_gb) * _BYTES_PER_GB)

    now = datetime.now(timezone.utc)
    prev = await _last_sample(session, user.id)
    session.add(TrafficSample(user_id=user.id, raw_bytes_total=raw_bytes, sampled_at=now))

    if prev is None:
        return
    dt = (now - prev.sampled_at).total_seconds()
    if dt <= 0:
        return
    delta_bytes = raw_bytes - prev.raw_bytes_total
    if delta_bytes <= 0:
        # Счётчик в панели уменьшился/сбросился (revoke, ресет статистики) — не всплеск.
        return

    window = settings.fraud_traffic_spike_window_sec
    threshold_bytes = settings.fraud_traffic_spike_gb_threshold * _BYTES_PER_GB
    # Средний темп с прошлого замера, спроецированный на длину окна детекта — честная
    # оценка при переменном интервале опроса (в т.ч. когда шардинг растягивает dt дольше window).
    projected_bytes = delta_bytes * (window / dt)
    if projected_bytes < threshold_bytes:
        return

    gb_rate = projected_bytes / _BYTES_PER_GB
    ratio = gb_rate / max(settings.fraud_traffic_spike_gb_threshold, 0.01)
    confidence = max(0.5, min(1.0, 0.5 + 0.25 * (ratio - 1)))

    await record_incident(
        session,
        settings,
        user=user,
        detector="traffic_spike",
        severity="suspicious",
        confidence=confidence,
        reason_text=plain(
            f"Темп трафика ~{gb_rate:.2f} ГБ/{window}с — выше порога "
            f"{settings.fraud_traffic_spike_gb_threshold:.2f} ГБ/{window}с."
        ),
        evidence={
            "delta_bytes": delta_bytes,
            "interval_sec": dt,
            "projected_gb_per_window": gb_rate,
            "window_sec": window,
        },
        activity_lines=[
            f"{prev.sampled_at:%H:%M:%S} → {now:%H:%M:%S}: +{delta_bytes / _BYTES_PER_GB:.2f} ГБ"
        ],
    )


async def cleanup_old_traffic_samples(session: AsyncSession) -> int:
    cutoff = datetime.now(timezone.utc) - _RETENTION
    res = await session.execute(delete(TrafficSample).where(TrafficSample.sampled_at < cutoff))
    return int(res.rowcount or 0)
