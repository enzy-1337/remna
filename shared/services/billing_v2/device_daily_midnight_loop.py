"""Фоновая догонка суточного списания за устройства (после полуночи по billing_calendar_timezone)."""

from __future__ import annotations

import asyncio
import logging

from shared.config import Settings
from shared.database import get_session_factory
from shared.services.billing_v2.device_daily_batch_service import catch_up_device_daily_charges

logger = logging.getLogger(__name__)


async def device_daily_midnight_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    interval = max(60, int(settings.billing_device_daily_job_interval_sec))
    while not stop_event.is_set():
        try:
            if settings.billing_v2_enabled:
                async with get_session_factory()() as session:
                    async with session.begin():
                        n = await catch_up_device_daily_charges(session, settings)
                if n:
                    logger.info("device_daily_midnight_loop: processed charge attempts=%s", n)
        except Exception:
            logger.exception("device_daily_midnight_loop failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
