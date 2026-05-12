"""ID-бот: показывает Telegram ID пользователя и чата.

Запуск: python -m idbot.main

Поведение:
- /start в личке -> "Имя / Тэг / Юзер ID" (ID копируется тапом).
- /start в личке c deep-link payload `cid_<chatid>` -> добавляет «Чат ID» из ссылки.
- /start в группе/супергруппе/канале -> сообщение пользователя удаляется,
  ID этого чата отправляется в личные сообщения. Если бот не может писать в ЛС —
  даёт кнопку с deep-link на @<bot>?start=cid_<chatid>.
- При добавлении бота в чат -> ничего не отправляет.
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
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from idbot.config import IdBotSettings, get_idbot_settings  # noqa: E402
from shared.md2 import bold, code, esc, join_lines, plain  # noqa: E402

logger = logging.getLogger("idbot")

router = Router()

_CID_PAYLOAD_PREFIX = "cid_"


def _cta_keyboard(bot_username: str | None) -> InlineKeyboardMarkup | None:
    """Inline-кнопка-«приписка» в стиле reels-бота: 💜 @<bot_username>."""
    uname = (bot_username or "").strip().lstrip("@")
    if not uname:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💜 @{uname}",
                    url=f"https://t.me/{uname}",
                    style="primary",
                )
            ],
        ]
    )


def _open_dm_keyboard(bot_username: str, chat_id: int) -> InlineKeyboardMarkup | None:
    uname = (bot_username or "").strip().lstrip("@")
    if not uname:
        return None
    payload = f"{_CID_PAYLOAD_PREFIX}{chat_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📩 Открыть в личке",
                    url=f"https://t.me/{uname}?start={payload}",
                    style="primary",
                )
            ],
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
        "🆔 " + bold("Юзер ID: ") + code(str(user_id)),
    ]


def _format_chat_line(chat_id: int) -> str:
    return "💬 " + bold("Чат ID: ") + code(str(chat_id))


def _private_text(*, name: str, username: str | None, user_id: int) -> str:
    return join_lines(*_format_user_lines(name=name, username=username, user_id=user_id))


def _group_text(*, name: str, username: str | None, user_id: int, chat_id: int) -> str:
    return join_lines(
        *_format_user_lines(name=name, username=username, user_id=user_id),
        _format_chat_line(chat_id),
    )


def _parse_chat_payload(args: str | None) -> int | None:
    raw = (args or "").strip()
    if not raw.startswith(_CID_PAYLOAD_PREFIX):
        return None
    tail = raw[len(_CID_PAYLOAD_PREFIX) :]
    try:
        return int(tail)
    except ValueError:
        return None


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start_private(message: Message, command: CommandObject) -> None:
    settings = get_idbot_settings()
    tg = message.from_user
    if tg is None:
        return
    name = _display_name(tg.first_name, tg.last_name)
    kb = _cta_keyboard(settings.bot_username)

    chat_id_from_payload = _parse_chat_payload(command.args)
    if chat_id_from_payload is not None:
        text = _group_text(
            name=name,
            username=tg.username,
            user_id=tg.id,
            chat_id=chat_id_from_payload,
        )
    else:
        text = _private_text(name=name, username=tg.username, user_id=tg.id)
    await message.answer(text, reply_markup=kb)


@router.message(CommandStart())
async def cmd_start_group(message: Message) -> None:
    """/start в группе/супергруппе/канале: удалить и ответить в ЛС."""
    if message.chat.type == ChatType.PRIVATE:
        return  # подстраховка: приватный кейс ловит cmd_start_private

    settings = get_idbot_settings()
    tg = message.from_user
    if tg is None or tg.is_bot:
        return

    bot = message.bot
    name = _display_name(tg.first_name, tg.last_name)
    text = _group_text(
        name=name,
        username=tg.username,
        user_id=tg.id,
        chat_id=message.chat.id,
    )
    kb = _cta_keyboard(settings.bot_username)

    delivered_to_dm = False
    try:
        await bot.send_message(chat_id=tg.id, text=text, reply_markup=kb)
        delivered_to_dm = True
    except TelegramForbiddenError:
        logger.info("idbot: ЛС закрыты для tg=%s, fallback с deep-link", tg.id)
    except TelegramBadRequest as e:
        logger.info("idbot: send_message в ЛС tg=%s упал: %s", tg.id, e)
    except Exception:
        logger.exception("idbot: не удалось отправить ЛС tg=%s", tg.id)

    try:
        await message.delete()
    except Exception:
        logger.debug("idbot: не удалось удалить /start в чате %s", message.chat.id)

    if not delivered_to_dm:
        bot_uname = (settings.bot_username or "").strip().lstrip("@")
        if not bot_uname:
            try:
                me = await bot.get_me()
                bot_uname = (me.username or "").strip()
            except Exception:
                bot_uname = ""
        if bot_uname:
            kb_dm = _open_dm_keyboard(bot_uname, message.chat.id)
            mention = f"@{tg.username}" if tg.username else esc(name)
            notice = join_lines(
                plain(f"{mention}, чтобы получить ID, откройте бота в личке и нажмите ") + bold("Start") + plain("."),
            )
            try:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=notice,
                    reply_markup=kb_dm,
                    disable_notification=True,
                )
            except Exception:
                logger.debug("idbot: не удалось отправить fallback-уведомление в чат %s", message.chat.id)


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
