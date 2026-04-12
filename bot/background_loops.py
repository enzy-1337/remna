"""Фоновые циклы, общие для polling-бота и режима Telegram webhook в API."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from shared.config import Settings
from shared.services.autorenew_service import subscription_autorenew_loop
from shared.services.backup_loop import backup_loop
from shared.services.billing_v2.cleanup_loop import billing_cleanup_loop
from shared.services.billing_v2.device_daily_midnight_loop import device_daily_midnight_loop
from shared.services.billing_v2.negative_balance_notify_loop import negative_balance_notify_loop
from shared.services.billing_v2.transition_service import legacy_transition_loop
from shared.services.admin_report_loop import admin_report_loop
from shared.services.expiry_notify_service import subscription_expiry_notify_loop
from shared.services.remnawave_sync import sync_loop

logger = logging.getLogger(__name__)


def start_background_loops(settings: Settings, stop_event: asyncio.Event) -> list[asyncio.Task]:
    """Запуск asyncio-задач; при остановке передать stop_event.set() и отменить задачи."""
    tasks: list[asyncio.Task] = []
    if settings.remnawave_sync_enabled and not settings.remnawave_stub:
        tasks.append(asyncio.create_task(sync_loop(settings, stop_event)))
    if settings.admin_report_enabled:
        tasks.append(asyncio.create_task(admin_report_loop(settings, stop_event)))
    if settings.backup_enabled:
        tasks.append(asyncio.create_task(backup_loop(settings, stop_event)))
    tasks.append(asyncio.create_task(subscription_autorenew_loop(settings, stop_event)))
    tasks.append(asyncio.create_task(subscription_expiry_notify_loop(settings, stop_event)))
    if settings.billing_v2_enabled:
        tasks.append(asyncio.create_task(billing_cleanup_loop(settings, stop_event)))
        tasks.append(asyncio.create_task(device_daily_midnight_loop(settings, stop_event)))
        if settings.billing_negative_notify_enabled:
            tasks.append(asyncio.create_task(negative_balance_notify_loop(settings, stop_event)))
        tasks.append(asyncio.create_task(legacy_transition_loop(settings, stop_event)))
    return tasks


async def cancel_background_tasks(tasks: list[asyncio.Task]) -> None:
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
