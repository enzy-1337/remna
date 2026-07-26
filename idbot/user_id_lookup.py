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


def _extract_forward_user(msg: Message) -> TgUser | None:
    """Автор пересланного сообщения: сперва forward_origin (Bot API 7.0+), затем legacy forward_from."""
    origin = getattr(msg, "forward_origin", None)
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        return sender_user
    return getattr(msg, "forward_from", None)


def _is_hidden_forward(msg: Message) -> bool:
    """True, если сообщение переслано, но автор скрыл пересылку (нельзя узнать ID)."""
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        return getattr(origin, "sender_user", None) is None and getattr(origin, "sender_user_name", None) is not None
    return bool(getattr(msg, "forward_sender_name", None)) and getattr(msg, "forward_from", None) is None


def _resolve_target_user(target: Message) -> TgUser | None:
    """Для реплая: автор исходного (пересланного) сообщения, а не тот, кто его переслал."""
    forwarded = _extract_forward_user(target)
    if forwarded is not None:
        return forwarded
    return target.from_user


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


class ForwardedMessageFilter(Filter):
    """Личка: пользователь напрямую переслал сообщение (без команды /id)."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.type != "private" or message.reply_to_message:
            return False
        if message.text and message.text.strip().startswith("/"):
            return False
        return _extract_forward_user(message) is not None or _is_hidden_forward(message)


def _full_name(user: TgUser | None) -> str:
    if user is None:
        return "—"
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "—"


def _reply_markup():
    settings = get_idbot_settings()
    return cta_keyboard(settings.bot_username, label_template=settings.bot_cta_label)


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
        if message.reply_to_message:
            target_user = _resolve_target_user(message.reply_to_message)
            if target_user:
                await _answer_from_tg_user(message, target_user)
                return
        arg = (command.args or "").strip()
        if arg.startswith("@"):
            arg = arg[1:]
        if arg.strip():
            await _lookup_username(message, bot, arg)
            return
        await send_group_id_destination_prompt(message)
        return

    if message.reply_to_message:
        target_user = _resolve_target_user(message.reply_to_message)
        if target_user:
            await _answer_from_tg_user(message, target_user)
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


@router.message(ForwardedMessageFilter())
async def private_lookup_by_forward(message: Message) -> None:
    """Личка: переслали сообщение пользователя напрямую (без /id) — сразу отдаём его ID."""
    forwarded_user = _extract_forward_user(message)
    if forwarded_user is not None:
        await _answer_from_tg_user(message, forwarded_user)
        return
    await message.answer(
        plain("Автор пересланного сообщения скрыл информацию о пересылке — узнать ID не получится."),
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
