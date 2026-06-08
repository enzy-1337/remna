"""Фоновый loop: уведомления пользователям, не подключившим устройство за 24 часа."""

from __future__ import annotations

import asyncio
import logging

from shared.config import Settings
from shared.database import get_session_factory

logger = logging.getLogger(__name__)

_INTERVAL_SEC = 30 * 60  # раз в 30 минут


async def connection_notify_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    from aiogram import Bot
    from shared.services.connection_notify_service import run_connection_notify_loop

    bot_token = (settings.bot_token or "").strip()
    if not bot_token:
        logger.warning("connection_notify_loop: BOT_TOKEN не задан, loop не запущен")
        return

    bot = Bot(token=bot_token)
    try:
        while not stop_event.is_set():
            try:
                factory = get_session_factory()
                async with factory() as session:
                    async with session.begin():
                        await run_connection_notify_loop(bot, settings, session)
            except Exception:
                logger.exception("connection_notify_loop: итерация не удалась")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
    finally:
        await bot.session.close()
