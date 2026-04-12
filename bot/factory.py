"""Сборка Bot + Dispatcher для polling и для FastAPI webhook."""

from __future__ import annotations

import logging
import socket

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from bot.handlers.admin import router as admin_router
from bot.handlers.admin_promo import router as admin_promo_router
from bot.handlers.balance import router as balance_router
from bot.handlers.calculator import router as calculator_router
from bot.handlers.devices import router as devices_router
from bot.handlers.fallback import router as fallback_router
from bot.handlers.menu import router as menu_router
from bot.handlers.promo import router as promo_router
from bot.handlers.referrals import router as referrals_router
from bot.handlers.start import router as start_router
from bot.handlers.subscription import router as subscription_router
from bot.middlewares.channel_sub import ChannelSubscriptionMiddleware
from bot.middlewares.db_session import DbSessionMiddleware
from bot.middlewares.maintenance import MaintenanceMiddleware
from bot.middlewares.user_context import UserContextMiddleware
from shared.config import Settings

logger = logging.getLogger(__name__)

_real_getaddrinfo = socket.getaddrinfo


def apply_ipv4_preferred_dns() -> None:
    """Снижает проблемы TLS к api.telegram.org при битом IPv6-маршруте (как в bot.main)."""

    def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        if family in (0, socket.AF_UNSPEC, socket.AF_INET6, None):
            family = socket.AF_INET
        return _real_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = _getaddrinfo_ipv4  # type: ignore[assignment]


def _mount_dispatcher(dp: Dispatcher, settings: Settings) -> None:
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
    dp.include_router(calculator_router)
    dp.include_router(menu_router)
    dp.include_router(start_router)
    dp.include_router(fallback_router)


async def create_bot_and_dispatcher(settings: Settings) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )
    await bot.set_my_commands(
        commands=[
            BotCommand(command="start", description="Перезапустить бота"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    _mount_dispatcher(dp, settings)
    return bot, dp


def webhook_allowed_updates(dp: Dispatcher) -> list[str] | None:
    """Список типов апдейтов для setWebhook (меньше шуму)."""
    try:
        resolved = dp.resolve_used_update_types()
    except Exception:
        logger.debug("resolve_used_update_types недоступен", exc_info=True)
        return None
    if not resolved:
        return None
    return list(resolved)
