"""Фоновая догонка суточного списания за устройства (после полуночи по billing_calendar_timezone)."""

from __future__ import annotations

import asyncio
import logging

from shared.config import Settings
from shared.database import get_session_factory
from shared.services.billing_v2.device_daily_batch_service import catch_up_device_daily_charges

logger = logging.getLogger(__name__)

_hwid_reconcile_phase = 0


async def device_daily_midnight_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    global _hwid_reconcile_phase
    interval = max(60, int(settings.billing_device_daily_job_interval_sec))
    while not stop_event.is_set():
        try:
            if settings.billing_v2_enabled:
                async with get_session_factory()() as session:
                    async with session.begin():
                        from shared.services.billing_v2.hwid_panel_reconcile_service import (
                            reconcile_hwid_devices_for_hybrid_users_sharded,
                        )

                        touched, ev = await reconcile_hwid_devices_for_hybrid_users_sharded(
                            session,
                            settings,
                            phase=_hwid_reconcile_phase,
                            phases=10,
                        )
                        _hwid_reconcile_phase += 1
                        if ev:
                            logger.info(
                                "device_daily_midnight_loop: hwid_panel_reconcile users=%s events=%s",
                                touched,
                                ev,
                            )
                        n = await catch_up_device_daily_charges(session, settings)
                if n:
                    logger.info("device_daily_midnight_loop: processed charge attempts=%s", n)
        except Exception:
            logger.exception("device_daily_midnight_loop failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
