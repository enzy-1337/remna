"""Фоновый синк внешнего чёрного списка Telegram ID (Settings.fraud_blacklist_sync_interval_sec)."""

from __future__ import annotations

import asyncio
import logging

from shared.config import Settings
from shared.database import get_session_factory
from shared.services.fraud.blacklist_sync import sync_telegram_blacklist

logger = logging.getLogger(__name__)


async def blacklist_sync_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    interval = max(60, int(settings.fraud_blacklist_sync_interval_sec))
    while not stop_event.is_set():
        try:
            async with get_session_factory()() as session:
                async with session.begin():
                    blocked = await sync_telegram_blacklist(session, settings)
                if blocked:
                    logger.info("fraud blacklist sync: blocked %s existing users", blocked)
        except Exception:
            logger.exception("blacklist_sync_loop failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
