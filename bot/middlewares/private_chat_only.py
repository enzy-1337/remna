"""Игнорирует апдейты из групп/каналов: бот работает только в личке."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update


def _chat_type_from_update(update: Update) -> str | None:
    if update.message and update.message.chat:
        return update.message.chat.type
    if update.edited_message and update.edited_message.chat:
        return update.edited_message.chat.type
    if update.callback_query and update.callback_query.message and update.callback_query.message.chat:
        return update.callback_query.message.chat.type
    if update.channel_post and update.channel_post.chat:
        return update.channel_post.chat.type
    if update.edited_channel_post and update.edited_channel_post.chat:
        return update.edited_channel_post.chat.type
    if update.my_chat_member and update.my_chat_member.chat:
        return update.my_chat_member.chat.type
    if update.chat_member and update.chat_member.chat:
        return update.chat_member.chat.type
    return None


class PrivateChatOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        ctype = _chat_type_from_update(event)
        if ctype is not None and ctype != "private":
            return None
        return await handler(event, data)

