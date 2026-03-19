"""Загрузка пользователя из БД + last_activity_at."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.user import User
from shared.telegram_utils import user_from_update


class UserContextMiddleware(BaseMiddleware):
    """Кладёт в data: db_user, tg_user."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        session: AsyncSession | None = data.get("session")
        if session is None:
            data["db_user"] = None
            data["tg_user"] = None
            return await handler(event, data)

        tg_user = user_from_update(event)
        if tg_user is None or tg_user.is_bot:
            data["db_user"] = None
            data["tg_user"] = tg_user
            return await handler(event, data)

        res = await session.execute(select(User).where(User.telegram_id == tg_user.id))
        db_user = res.scalar_one_or_none()
        if db_user is not None:
            db_user.last_activity_at = datetime.now(timezone.utc)

        data["db_user"] = db_user
        data["tg_user"] = tg_user
        return await handler(event, data)
