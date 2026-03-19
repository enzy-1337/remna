"""
Точка входа Telegram-бота.
Запуск из корня репозитория: python -m bot.main
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Корень проекта в PYTHONPATH (без установки пакета)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

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


async def main() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.update.middleware(MaintenanceMiddleware(settings))
    dp.update.middleware(ChannelSubscriptionMiddleware(settings))
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(UserContextMiddleware())

    dp.include_router(subscription_router)
    dp.include_router(devices_router)
    dp.include_router(referrals_router)
    dp.include_router(promo_router)
    dp.include_router(balance_router)
    dp.include_router(menu_router)
    dp.include_router(start_router)
    dp.include_router(fallback_router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
