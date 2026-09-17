from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from shared.database import get_session_factory

logger = logging.getLogger(__name__)


class IdBotDbSessionMiddleware(BaseMiddleware):
    """Даёт хендлерам ID-бота доступ к общей БД (поиск уже известных пользователей по username)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        factory = get_session_factory()
        async with factory() as session:
            data["session"] = session
            try:
                return await handler(event, data)
            finally:
                await session.rollback()
