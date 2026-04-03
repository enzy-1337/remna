"""Фоновый ежедневный бэкап PostgreSQL в админ-чат."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from shared.config import Settings
from shared.services.backup_service import run_daily_backup

logger = logging.getLogger(__name__)


def _seconds_until_next_utc_hour(hour_utc: int) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _seconds_until_next_local_hour(hour_local: int, tz_name: str) -> float:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    target_local = now_local.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    if target_local <= now_local:
        target_local += timedelta(days=1)
    delta = target_local.astimezone(timezone.utc) - datetime.now(timezone.utc)
    return max(1.0, delta.total_seconds())


async def backup_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        if not settings.backup_enabled:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass
            continue

        if settings.backup_hour_local is not None:
            delay = _seconds_until_next_local_hour(settings.backup_hour_local, settings.backup_timezone)
        else:
            delay = _seconds_until_next_utc_hour(settings.backup_hour_utc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass

        if stop_event.is_set():
            break

        try:
            await run_daily_backup(settings)
        except Exception:
            logger.exception("backup_loop: run_daily_backup failed")
