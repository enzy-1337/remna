"""Периодический опрос Remnawave по трафику и списание шагов ceil(used_gb)."""

from __future__ import annotations

import asyncio
import logging

from shared.config import Settings
from shared.database import get_session_factory
from shared.services.billing_v2.traffic_meter_poll_service import sync_traffic_meter_for_hybrid_users_sharded

logger = logging.getLogger(__name__)

_traffic_meter_phase = 0


async def traffic_meter_poll_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    global _traffic_meter_phase
    interval = max(30, int(settings.billing_traffic_meter_poll_interval_sec))
    while not stop_event.is_set():
        try:
            if settings.billing_v2_enabled and settings.billing_traffic_rw_meter_enabled:
                async with get_session_factory()() as session:
                    async with session.begin():
                        touched, charges = await sync_traffic_meter_for_hybrid_users_sharded(
                            session,
                            settings,
                            phase=_traffic_meter_phase,
                            phases=10,
                        )
                        _traffic_meter_phase += 1
                        if charges:
                            logger.info(
                                "traffic_meter_poll_loop: users_touched=%s gb_charges=%s",
                                touched,
                                charges,
                            )
        except Exception:
            logger.exception("traffic_meter_poll_loop failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
