"""ID-бот: показывает Telegram ID пользователя и чата.

Запуск: python -m idbot.main

Поведение:
- /start в личке -> "Имя / Тэг / Юзер ID" + кнопка-приписка (как у reels-бота)
- /start в группе/супергруппе -> "Имя / Тэг / Юзер ID / Чат ID" + кнопка
- При добавлении бота в группу/супергруппу -> разовое сообщение "Чат ID: ..." + кнопка
- При старте процесса -> BOOT-уведомление в админ-лог (ADMIN_LOG_CHAT_ID / ADMIN_LOG_TOPIC_BOOT).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from idbot.config import IdBotSettings, get_idbot_settings  # noqa: E402
from shared.md2 import bold, esc, join_lines, plain  # noqa: E402

logger = logging.getLogger("idbot")

router = Router()


def _cta_keyboard(bot_username: str | None) -> InlineKeyboardMarkup | None:
    """Inline-кнопка-«приписка» в стиле reels-бота: ❤️ @<bot_username>."""
    uname = (bot_username or "").strip().lstrip("@")
    if not uname:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"❤️ @{uname}", url=f"https://t.me/{uname}")],
        ]
    )


def _display_name(first_name: str | None, last_name: str | None) -> str:
    parts = [(first_name or "").strip(), (last_name or "").strip()]
    full = " ".join(p for p in parts if p)
    return full or "—"


def _format_user_lines(*, name: str, username: str | None, user_id: int) -> list[str]:
    tag = f"@{username}" if username else "—"
    return [
        "👤 " + bold("Имя: ") + esc(name),
        "🏷 " + bold("Тэг: ") + esc(tag),
        "🆔 " + bold("Юзер ID: ") + bold(str(user_id)),
    ]


def _format_chat_line(chat_id: int) -> str:
    return "💬 " + bold("Чат ID: ") + bold(str(chat_id))


def _private_text(*, name: str, username: str | None, user_id: int) -> str:
    return join_lines(*_format_user_lines(name=name, username=username, user_id=user_id))


def _group_text(*, name: str, username: str | None, user_id: int, chat_id: int) -> str:
    return join_lines(
        *_format_user_lines(name=name, username=username, user_id=user_id),
        _format_chat_line(chat_id),
    )


def _group_welcome_text(chat_id: int) -> str:
    return _format_chat_line(chat_id)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    settings = get_idbot_settings()
    tg = message.from_user
    if tg is None:
        return
    name = _display_name(tg.first_name, tg.last_name)
    kb = _cta_keyboard(settings.bot_username)

    if message.chat.type == ChatType.PRIVATE:
        text = _private_text(name=name, username=tg.username, user_id=tg.id)
    else:
        text = _group_text(
            name=name,
            username=tg.username,
            user_id=tg.id,
            chat_id=message.chat.id,
        )
    await message.answer(text, reply_markup=kb)


@router.my_chat_member()
async def on_added_to_chat(event: ChatMemberUpdated) -> None:
    """Бота добавили в группу/супергруппу — разово отправляем «Чат ID: ...»."""
    if event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    if new_status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return
    if old_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        # Например, поменяли права с member на administrator — не спамим.
        return

    settings = get_idbot_settings()
    kb = _cta_keyboard(settings.bot_username)
    try:
        await event.bot.send_message(
            chat_id=event.chat.id,
            text=_group_welcome_text(event.chat.id),
            reply_markup=kb,
        )
    except Exception:
        logger.exception("idbot: не удалось отправить приветствие в чат %s", event.chat.id)


async def _on_startup(bot: Bot, settings: IdBotSettings) -> None:
    try:
        await bot.set_my_commands(
            commands=[BotCommand(command="start", description="Показать ваш Telegram ID")],
            scope=BotCommandScopeAllPrivateChats(),
        )
    except Exception:
        logger.exception("idbot: set_my_commands failed")

    chat_id = settings.admin_log_chat_id
    if chat_id is None or (isinstance(chat_id, str) and not chat_id.strip()):
        return
    boot_ts = datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M:%S | %d-%m-%Y | МСК")
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=settings.admin_log_topic_boot or settings.admin_log_topic_id,
            text=f"🆔 ID bot запущен\n{boot_ts}",
            parse_mode=None,
        )
    except Exception:
        logger.exception("idbot: BOOT-уведомление не доставлено")


async def _run() -> None:
    settings = get_idbot_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = (settings.idbot_bot_token or "").strip()
    if not token:
        raise RuntimeError("IDBOT_BOT_TOKEN is empty")

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await _on_startup(bot, settings)

    allowed_updates = dp.resolve_used_update_types()
    await dp.start_polling(bot, allowed_updates=allowed_updates)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
