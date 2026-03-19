"""
Обязательная подписка на канал: проверка при каждом апдейте.
Кэш в Redis: channel_sub:{telegram_id}, TTL 5 минут (настраивается).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import redis.asyncio as redis
from aiogram import BaseMiddleware
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    TelegramObject,
    Update,
)

from bot.keyboards.inline import channel_required_keyboard
from shared.config import Settings, get_settings
from shared.md2 import esc
from shared.telegram_utils import user_from_update

logger = logging.getLogger(__name__)

_CACHE_KEY = "channel_sub:{tid}"


def _member_is_subscribed(member) -> bool:
    """Пользователь состоит в канале (включая restricted с is_member)."""
    if isinstance(member, (ChatMemberOwner, ChatMemberAdministrator, ChatMemberMember)):
        return True
    if isinstance(member, ChatMemberRestricted):
        return bool(member.is_member)
    if isinstance(member, (ChatMemberLeft, ChatMemberBanned)):
        return False
    return False


def _is_allowed_without_subscription(update: Update) -> bool:
    """До подписки в хендлеры попадает только /start (колбэк channel:check — в middleware)."""
    if update.message and update.message.text:
        parts = update.message.text.split(maxsplit=1)
        if parts and parts[0].startswith("/start"):
            return True
    return False


class ChannelSubscriptionMiddleware(BaseMiddleware):
    """
    Блокирует все апдейты пользователя, пока он не подписан на REQUIRED_CHANNEL_ID.
    Результат getChatMember кэшируется в Redis.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(
                self._settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        return self._redis

    async def _is_subscribed_cached(
        self,
        telegram_id: int,
        *,
        force_refresh: bool,
    ) -> bool | None:
        """None = нет в кэше, нужен запрос к API."""
        if force_refresh:
            return None
        r = await self._get_redis()
        key = _CACHE_KEY.format(tid=telegram_id)
        raw = await r.get(key)
        if raw is None:
            return None
        return raw == "1"

    async def _set_cache(self, telegram_id: int, subscribed: bool) -> None:
        r = await self._get_redis()
        key = _CACHE_KEY.format(tid=telegram_id)
        ttl = self._settings.channel_sub_cache_ttl
        await r.set(key, "1" if subscribed else "0", ex=ttl)

    async def _fetch_subscription(
        self,
        bot,
        telegram_id: int,
    ) -> bool:
        chat_id = self._settings.required_channel_id
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=telegram_id)
        except Exception:
            logger.exception(
                "getChatMember failed for user_id=%s channel=%s",
                telegram_id,
                chat_id,
            )
            # Безопасный режим: считаем не подписанным
            return False
        return _member_is_subscribed(member)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        user = user_from_update(event)
        if user is None or user.is_bot:
            return await handler(event, data)

        bot = data.get("bot")
        if bot is None:
            return await handler(event, data)

        cq = event.callback_query

        # «Я подписался» — всегда свежая проверка, ответ без прохода в хендлеры
        if cq and cq.data == "channel:check":
            subscribed = await self._fetch_subscription(bot, user.id)
            await self._set_cache(user.id, subscribed)
            data["is_channel_member"] = subscribed
            text_ok = esc(
                "✅ Подписка подтверждена!\n\n"
                "Теперь вам доступны все функции бота."
            )
            text_fail = esc(
                "Мы пока не видим вашу подписку на канал.\n"
                "Убедитесь, что вы подписались, и нажмите кнопку снова."
            )
            kb = channel_required_keyboard(self._settings.required_channel_username)
            if subscribed:
                await cq.answer()
                if cq.message:
                    try:
                        await cq.message.edit_text(text_ok, reply_markup=None)
                    except Exception:
                        await cq.message.answer(text_ok)
            else:
                await cq.answer(
                    "Подписка не найдена. Подпишитесь на канал.",
                    show_alert=True,
                )
                if cq.message:
                    try:
                        await cq.message.edit_text(text_fail, reply_markup=kb)
                    except Exception:
                        await cq.message.answer(text_fail, reply_markup=kb)
            return None

        cached = await self._is_subscribed_cached(user.id, force_refresh=False)
        if cached is None:
            subscribed = await self._fetch_subscription(bot, user.id)
            await self._set_cache(user.id, subscribed)
        else:
            subscribed = cached

        data["is_channel_member"] = subscribed

        if subscribed:
            return await handler(event, data)

        if _is_allowed_without_subscription(event):
            return await handler(event, data)

        # Блокируем остальной функционал
        text = esc(
            "📢 Чтобы пользоваться ботом, подпишитесь на наш канал.\n\n"
            "После подписки нажмите «✅ Я подписался»."
        )
        kb = channel_required_keyboard(self._settings.required_channel_username)

        if event.message:
            await event.message.answer(text, reply_markup=kb)
        elif event.callback_query:
            cq = event.callback_query
            await cq.answer(
                "Сначала подпишитесь на канал.",
                show_alert=True,
            )
            if cq.message:
                try:
                    await cq.message.edit_text(text, reply_markup=kb)
                except Exception:
                    await cq.message.answer(text, reply_markup=kb)
        else:
            # Прочие типы апдейтов без сообщения — просто игнорируем обработчики
            logger.debug("Blocked update type without message: user_id=%s", user.id)

        return None
