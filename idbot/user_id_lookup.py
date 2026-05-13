"""Команда /id и упоминание @username в личке — узнать Telegram ID (для ID-бота)."""

from __future__ import annotations

import logging
import re

from aiogram import Bot, Router
from aiogram.enums import ChatType, MessageEntityType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import Chat, Message, User as TgUser

from idbot.config import get_idbot_settings
from idbot.group_id_prompt import send_group_id_destination_prompt
from idbot.id_card import cta_keyboard, format_group_chat_peer_card, format_user_telegram_card
from shared.md2 import code, esc, join_lines, plain

logger = logging.getLogger("idbot.id_lookup")

router = Router(name="id_lookup")

_USERNAME_RE = re.compile(r"^@([a-zA-Z][a-zA-Z0-9_]{4,31})$")


class UsernameLookupFilter(Filter):
    """Личка: только строка `@username` или сущность text_mention (выбор из подсказки)."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.type != "private" or not message.text:
            return False
        text = message.text.strip()
        if text.startswith("/"):
            return False
        if _USERNAME_RE.match(text):
            return True
        for ent in message.entities or []:
            if ent.type == MessageEntityType.TEXT_MENTION and ent.user:
                return True
        return False


def _full_name(user: TgUser | None) -> str:
    if user is None:
        return "—"
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "—"


def _reply_markup():
    return cta_keyboard(get_idbot_settings().bot_username)


async def _answer_from_tg_user(message: Message, user: TgUser) -> None:
    cap = format_user_telegram_card(
        name=_full_name(user),
        username=user.username,
        user_id=user.id,
    )
    await message.answer(cap, reply_markup=_reply_markup())


async def _answer_from_chat(message: Message, chat: Chat) -> None:
    ct = chat.type
    uid = chat.id
    un = chat.username
    if ct == ChatType.PRIVATE:
        fn = " ".join(x for x in [chat.first_name or "", chat.last_name or ""] if x).strip() or "—"
        cap = format_user_telegram_card(name=fn, username=un, user_id=uid)
    else:
        title = chat.title or chat.full_name or "—"
        cap = format_group_chat_peer_card(
            chat_id=uid,
            title=title,
            chat_type=str(ct),
            username=un,
        )
    await message.answer(cap, reply_markup=_reply_markup())


async def _lookup_username(message: Message, bot: Bot, username_without_at: str) -> None:
    username_without_at = username_without_at.strip().lstrip("@")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{4,31}", username_without_at):
        await message.answer(
            plain("Некорректный username. Пример: ") + code("@nickname"),
        )
        return
    try:
        chat = await bot.get_chat(f"@{username_without_at}")
    except TelegramBadRequest as e:
        logger.debug("get_chat @%s: %s", username_without_at, e)
        await message.answer(
            join_lines(
                plain("Не удалось найти ")
                + code("@" + esc(username_without_at))
                + plain(". Убедитесь, что username указан верно и профиль публичный."),
            ),
        )
        return
    await _answer_from_chat(message, chat)


@router.message(Command("id"))
async def cmd_id(message: Message, command: CommandObject) -> None:
    bot = message.bot
    assert bot is not None

    if message.chat.type != ChatType.PRIVATE:
        tg = message.from_user
        if tg is None or tg.is_bot:
            return
        if message.reply_to_message and message.reply_to_message.from_user:
            await _answer_from_tg_user(message, message.reply_to_message.from_user)
            return
        arg = (command.args or "").strip()
        if arg.startswith("@"):
            arg = arg[1:]
        if arg.strip():
            await _lookup_username(message, bot, arg)
            return
        await send_group_id_destination_prompt(message)
        return

    if message.reply_to_message and message.reply_to_message.from_user:
        await _answer_from_tg_user(message, message.reply_to_message.from_user)
        return

    arg = (command.args or "").strip()
    if arg.startswith("@"):
        arg = arg[1:]
    arg = arg.strip()

    if arg:
        await _lookup_username(message, bot, arg)
        return

    if message.from_user:
        await _answer_from_tg_user(message, message.from_user)
        return

    await message.answer(
        join_lines(
            plain("Укажите пользователя одним из способов:"),
            plain("• ответьте на сообщение командой ") + code("/id"),
            plain("• или ") + code("/id @username"),
        ),
    )


@router.message(UsernameLookupFilter())
async def private_lookup_by_mention(message: Message) -> None:
    assert message.text is not None
    text = message.text.strip()

    for ent in message.entities or []:
        if ent.type == MessageEntityType.TEXT_MENTION and ent.user:
            await _answer_from_tg_user(message, ent.user)
            return

    m = _USERNAME_RE.match(text)
    if m:
        await _lookup_username(message, message.bot, m.group(1))
