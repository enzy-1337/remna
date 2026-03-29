"""
Точка входа Telegram-бота.
Запуск из корня репозитория: python -m bot.main
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import socket
from datetime import UTC, datetime
from pathlib import Path

# Корень проекта в PYTHONPATH (без установки пакета)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers.admin import router as admin_router
from bot.handlers.admin_promo import router as admin_promo_router
from bot.handlers.balance import router as balance_router
from bot.handlers.devices import router as devices_router
from bot.handlers.fallback import router as fallback_router
from bot.handlers.subscription import router as subscription_router
from bot.handlers.menu import router as menu_router
from bot.handlers.promo import router as promo_router
from bot.handlers.referrals import router as referrals_router
from bot.handlers.start import router as start_router
from bot.middlewares.channel_sub import ChannelSubscriptionMiddleware
from bot.middlewares.db_session import DbSessionMiddleware
from bot.middlewares.maintenance import MaintenanceMiddleware
from bot.middlewares.user_context import UserContextMiddleware
from shared.config import get_settings
from shared.database import get_session_factory
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin_plain
from shared.services.admin_report_loop import admin_report_loop
from shared.services.backup_loop import backup_loop
from shared.services.autorenew_service import subscription_autorenew_loop
from shared.services.expiry_notify_service import subscription_expiry_notify_loop
from shared.services.plan_seed import ensure_default_plans_if_needed
from shared.services.remnawave_sync import sync_loop
from shared.services.schema_patches import ensure_promo_columns, ensure_subscription_expiry_notify_columns


async def main() -> None:
    # Force IPv4-only DNS resolution to avoid Telegram IPv6 routing/TLS issues.
    _real_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        if family in (0, socket.AF_UNSPEC, socket.AF_INET6, None):
            family = socket.AF_INET
        return _real_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = _getaddrinfo_ipv4  # type: ignore[assignment]

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    factory = get_session_factory()
    async with factory() as s:
        await ensure_default_plans_if_needed(s)
        await ensure_subscription_expiry_notify_columns(s)
        await ensure_promo_columns(s)
        await s.commit()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.update.middleware(MaintenanceMiddleware(settings))
    dp.update.middleware(ChannelSubscriptionMiddleware(settings))
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(UserContextMiddleware())

    dp.include_router(admin_router)
    dp.include_router(admin_promo_router)
    dp.include_router(subscription_router)
    dp.include_router(devices_router)
    dp.include_router(referrals_router)
    dp.include_router(promo_router)
    dp.include_router(balance_router)
    dp.include_router(menu_router)
    dp.include_router(start_router)
    dp.include_router(fallback_router)
    stop_event = asyncio.Event()
    sync_task: asyncio.Task | None = None
    report_task: asyncio.Task | None = None
    backup_task: asyncio.Task | None = None
    autorenew_task: asyncio.Task | None = None
    expiry_notify_task: asyncio.Task | None = None
    if settings.remnawave_sync_enabled and not settings.remnawave_stub:
        sync_task = asyncio.create_task(sync_loop(settings, stop_event))
    if settings.admin_report_enabled:
        report_task = asyncio.create_task(admin_report_loop(settings, stop_event))
    if settings.backup_enabled:
        backup_task = asyncio.create_task(backup_loop(settings, stop_event))
    autorenew_task = asyncio.create_task(subscription_autorenew_loop(settings, stop_event))
    expiry_notify_task = asyncio.create_task(subscription_expiry_notify_loop(settings, stop_event))
    try:
        boot_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        sent = await notify_admin_plain(
            settings,
            text=f"🚀 Бот запущен\n{boot_ts} UTC",
            topic=AdminLogTopic.BOOT,
            event_type="bot_startup",
        )
        if sent:
            logging.getLogger(__name__).info("Уведомление о запуске отправлено в админ-чат (тема BOOT).")
        await dp.start_polling(bot)
    finally:
        stop_event.set()
        if sync_task is not None:
            sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sync_task
        if report_task is not None:
            report_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await report_task
        if backup_task is not None:
            backup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await backup_task
        if autorenew_task is not None:
            autorenew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await autorenew_task
        if expiry_notify_task is not None:
            expiry_notify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await expiry_notify_task


if __name__ == "__main__":
    asyncio.run(main())
