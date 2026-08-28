"""Фоновый опрос трафика для детектора шеринга подписки — ВСЕ пользователи с привязкой
к панели, независимо от billing_mode. Интервал: Settings.fraud_traffic_spike_poll_interval_sec.

Шардинг по 10 фазам (как device_daily_midnight_loop) — иначе каждый тик дёргает panel.get_user
для всех пользователей разом, что при большой базе бьёт по Remnawave API. Плата за это —
конкретный пользователь реально опрашивается раз в ~10*interval, а не каждый interval;
check_user_traffic_spike компенсирует это, проецируя фактическую скорость на окно детекта,
а не полагаясь на фиксированный шаг между замерами.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.models.user import User
from shared.services.fraud.traffic_spike_detector import (
    check_user_traffic_spike,
    cleanup_old_traffic_samples,
)

logger = logging.getLogger(__name__)

_PHASES = 10
_CLEANUP_EVERY_N_TICKS = 60
_tick_counter = 0


async def _scan_phase(session: AsyncSession, settings: Settings, *, phase: int) -> int:
    users = list(
        (
            await session.execute(
                select(User).where(User.is_blocked.is_(False), User.remnawave_uuid.isnot(None))
            )
        ).scalars()
    )
    touched = 0
    for u in users:
        if int(u.id) % _PHASES != phase:
            continue
        touched += 1
        try:
            await check_user_traffic_spike(session, settings, user=u)
        except Exception:
            logger.exception("traffic_spike_scan: user_id=%s", u.id)
    return touched


async def traffic_spike_scan_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    global _tick_counter
    interval = max(15, int(settings.fraud_traffic_spike_poll_interval_sec))
    phase = 0
    while not stop_event.is_set():
        try:
            async with get_session_factory()() as session:
                async with session.begin():
                    touched = await _scan_phase(session, settings, phase=phase)
                    _tick_counter += 1
                    if _tick_counter % _CLEANUP_EVERY_N_TICKS == 0:
                        removed = await cleanup_old_traffic_samples(session)
                        if removed:
                            logger.info("traffic_spike_scan: cleaned %s old samples", removed)
                if touched:
                    logger.debug("traffic_spike_scan: phase=%s touched=%s", phase, touched)
        except Exception:
            logger.exception("traffic_spike_scan_loop failed")
        phase = (phase + 1) % _PHASES
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
