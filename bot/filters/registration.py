"""Фильтры для роутеров."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import Message

from shared.models.user import User


class UnregisteredChannelMemberTextFilter(BaseFilter):
    """Текст (не команда), канал пройден, пользователя ещё нет в БД."""

    async def __call__(
        self,
        message: Message,
        is_channel_member: bool,
        db_user: User | None,
    ) -> bool:
        if not is_channel_member or db_user is not None:
            return False
        if not message.text or message.text.startswith("/"):
            return False
        return True
