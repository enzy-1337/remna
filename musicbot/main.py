from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from bot.middlewares.private_chat_only import PrivateChatOnlyMiddleware
from musicbot.config import get_musicbot_settings
from musicbot.handlers import router as music_router
from musicbot.middlewares.db_session import MusicDbSessionMiddleware


def main() -> None:
    settings = get_musicbot_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = (settings.music_bot_token or "").strip()
    if not token:
        raise RuntimeError("MUSIC_BOT_TOKEN is empty")
    if settings.music_forum_chat_id is None:
        raise RuntimeError("MUSIC_FORUM_CHAT_ID is empty")

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(PrivateChatOnlyMiddleware())
    dp.update.middleware(MusicDbSessionMiddleware())
    dp.include_router(music_router)

    async def _on_startup(*_args, **_kwargs) -> None:
        await bot.set_my_commands(
            commands=[BotCommand(command="start", description="Поиск музыки")],
            scope=BotCommandScopeAllPrivateChats(),
        )
        chat_id = settings.admin_log_chat_id
        if chat_id is not None and (not isinstance(chat_id, str) or chat_id.strip()):
            boot_ts = datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).strftime(
                "%H:%M:%S | %d-%m-%Y | МСК"
            )
            try:
                from shared.md2 import bold, join_lines, plain

                boot_text = join_lines("🎵 " + bold("Music bot запущен"), plain(boot_ts))
                await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=settings.admin_log_topic_boot or settings.admin_log_topic_id,
                    text=boot_text,
                    parse_mode="MarkdownV2",
                )
            except Exception:
                logging.getLogger(__name__).exception("BOOT musicbot failed")

    dp.startup.register(_on_startup)
    dp.run_polling(bot)


if __name__ == "__main__":
    main()
