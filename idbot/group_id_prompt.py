"""Меню «куда отправить ID» в группе/канале — общее для /start и /id."""

from __future__ import annotations

import logging

from aiogram.enums import ChatType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from shared.md2 import bold, italic, join_lines, plain

logger = logging.getLogger("idbot.group_prompt")

# Префикс callback_data: "idbot:r:<G|D|B>:<user_id>".  G=group, D=dm, B=both.
REPLY_DEST_CALLBACK_PREFIX = "idbot:r:"


def destination_keyboard(user_id: int) -> InlineKeyboardMarkup:
    p = REPLY_DEST_CALLBACK_PREFIX
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 В группу",
                    callback_data=f"{p}G:{user_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📩 В личку",
                    callback_data=f"{p}D:{user_id}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 И туда, и туда",
                    callback_data=f"{p}B:{user_id}",
                    style="success",
                ),
            ],
        ]
    )


def parse_reply_destination_cb(data: str | None) -> tuple[str, int] | None:
    if not data or not data.startswith(REPLY_DEST_CALLBACK_PREFIX):
        return None
    rest = data[len(REPLY_DEST_CALLBACK_PREFIX) :]
    parts = rest.split(":", 1)
    if len(parts) != 2:
        return None
    kind, uid_s = parts
    if kind not in ("G", "D", "B"):
        return None
    try:
        return kind, int(uid_s)
    except ValueError:
        return None


def display_name(first_name: str | None, last_name: str | None) -> str:
    parts = [(first_name or "").strip(), (last_name or "").strip()]
    full = " ".join(p for p in parts if p)
    return full or "—"


async def send_group_id_destination_prompt(message: Message) -> None:
    """Удаляет команду в группе/канале и отправляет меню «куда отправить ваш ID»."""
    if message.chat.type == ChatType.PRIVATE:
        return
    tg = message.from_user
    if tg is None or tg.is_bot:
        return
    bot = message.bot
    assert bot is not None
    name = display_name(tg.first_name, tg.last_name)
    mention_md = bold(f"@{tg.username}") if tg.username else bold(name)
    prompt = join_lines(
        mention_md + plain(", куда отправить ваш ID?"),
        italic("Выбор доступен только вам."),
    )
    kb = destination_keyboard(tg.id)

    try:
        await message.delete()
    except Exception:
        logger.debug("idbot: не удалось удалить команду в чате %s", message.chat.id)

    try:
        await bot.send_message(
            chat_id=message.chat.id,
            text=prompt,
            reply_markup=kb,
            message_thread_id=message.message_thread_id,
            disable_notification=True,
        )
    except Exception:
        logger.exception("idbot: не удалось отправить меню выбора в чат %s", message.chat.id)
