"""Игнорирует апдейты из групп/каналов: бот работает в личке (+ опц. разрешённые чаты)."""

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


def _chat_id_from_update(update: Update) -> int | None:
    if update.message and update.message.chat:
        return int(update.message.chat.id)
    if update.edited_message and update.edited_message.chat:
        return int(update.edited_message.chat.id)
    if update.callback_query and update.callback_query.message and update.callback_query.message.chat:
        return int(update.callback_query.message.chat.id)
    if update.channel_post and update.channel_post.chat:
        return int(update.channel_post.chat.id)
    if update.edited_channel_post and update.edited_channel_post.chat:
        return int(update.edited_channel_post.chat.id)
    if update.my_chat_member and update.my_chat_member.chat:
        return int(update.my_chat_member.chat.id)
    if update.chat_member and update.chat_member.chat:
        return int(update.chat_member.chat.id)
    return None


def _is_id_command_text(text: str | None) -> bool:
    """Команда `/id` или `/id@BotName` (без учёта регистра префикса)."""
    if not text:
        return False
    line = text.strip()
    if not line.startswith("/"):
        return False
    first_word = line.split()[0]
    return first_word.split("@", 1)[0].lower() == "/id"


class PrivateChatOnlyMiddleware(BaseMiddleware):
    def __init__(
        self,
        *,
        allowed_chat_ids: set[int] | None = None,
        allow_id_command_in_groups: bool = True,
    ) -> None:
        super().__init__()
        self._allowed_chat_ids = {int(x) for x in (allowed_chat_ids or set())}
        self._allow_id_command_in_groups = allow_id_command_in_groups

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        # Системные события подписки/отписки должны проходить даже из channel/supergroup.
        if event.chat_member or event.my_chat_member:
            return await handler(event, data)
        ctype = _chat_type_from_update(event)
        if ctype is not None and ctype != "private":
            chat_id = _chat_id_from_update(event)
            msg = event.message
            if (
                self._allow_id_command_in_groups
                and ctype in ("group", "supergroup")
                and msg
                and msg.text
                and _is_id_command_text(msg.text)
            ):
                return await handler(event, data)
            if chat_id is not None and chat_id in self._allowed_chat_ids:
                return await handler(event, data)
            return None
        return await handler(event, data)

