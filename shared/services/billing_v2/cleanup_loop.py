from __future__ import annotations

import asyncio
import logging

from shared.config import Settings
from shared.database import get_session_factory
from shared.services.billing_v2.detail_service import cleanup_old_details

logger = logging.getLogger(__name__)


async def billing_cleanup_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            async with get_session_factory()() as session:
                removed = await cleanup_old_details(
                    session, retention_days=settings.billing_detail_retention_days
                )
                await session.commit()
                if removed > 0:
                    logger.info("billing_cleanup: removed old rows=%s", removed)
        except Exception:
            logger.exception("billing_cleanup loop error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            continue
