"""Режим технических работ (MAINTENANCE_MODE)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from shared.config import Settings
from shared.md2 import bold, join_lines
from shared.telegram_utils import user_from_update

logger = logging.getLogger(__name__)

MAINTENANCE_TEXT = join_lines(
    "🚧 " + bold("Технические работы"),
    "",
    "Сервис временно недоступен. Загляните чуть позже — мы уже чиним.",
)


class MaintenanceMiddleware(BaseMiddleware):
    """Самый внешний слой: отвечает всем пользователям, не вызывая остальную цепочку."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update) or not self._settings.maintenance_mode:
            return await handler(event, data)

        tg_user = user_from_update(event)
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        bot = data.get("bot")
        if bot is None:
            return await handler(event, data)

        try:
            if event.message:
                await event.message.answer(MAINTENANCE_TEXT)
            elif event.callback_query:
                await event.callback_query.answer(
                    "Технические работы. Попробуйте позже.",
                    show_alert=True,
                )
        except Exception:
            logger.exception("maintenance reply failed")
        return None
