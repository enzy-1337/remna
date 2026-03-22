"""Фоновая отправка ежедневного отчёта в админ-чат."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from shared.config import Settings
from shared.services.admin_daily_report import send_daily_admin_report

logger = logging.getLogger(__name__)


def _seconds_until_next_utc_hour(hour_utc: int) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


async def admin_report_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        if not settings.admin_report_enabled:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass
            continue

        delay = _seconds_until_next_utc_hour(settings.admin_report_hour_utc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass

        if stop_event.is_set():
            break

        try:
            await send_daily_admin_report(settings)
        except Exception:
            logger.exception("admin_report_loop: send_daily_admin_report failed")
