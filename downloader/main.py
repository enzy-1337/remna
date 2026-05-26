from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from bot.handlers.downloader import router as downloader_router
from bot.middlewares.private_chat_only import PrivateChatOnlyMiddleware
from downloader.config import get_downloader_settings
from downloader.middlewares.db_session import DownloaderDbSessionMiddleware
from shared.telegram_connect import safe_set_bot_commands, wait_telegram_online


async def _run() -> None:
    settings = get_downloader_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = (settings.downloader_bot_token or "").strip()
    if not token:
        raise RuntimeError("DOWNLOADER_BOT_TOKEN is empty")
    if settings.downloader_forum_chat_id is None:
        raise RuntimeError("DOWNLOADER_FORUM_CHAT_ID is empty")

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(PrivateChatOnlyMiddleware())
    dp.update.middleware(DownloaderDbSessionMiddleware())
    dp.include_router(downloader_router)

    async def _on_startup(*_args, **_kwargs) -> None:
        await safe_set_bot_commands(
            bot,
            service="downloader",
            private_commands=[BotCommand(command="start", description="Начать работу с ботом")],
        )
        chat_id = settings.admin_log_chat_id
        if chat_id is not None and (not isinstance(chat_id, str) or chat_id.strip()):
            boot_ts = datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M:%S | %d-%m-%Y | МСК")
            try:
                from shared.md2 import bold, join_lines, plain

                boot_text = join_lines("🎬 " + bold("Reels bot запущен"), plain(boot_ts))
                await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=settings.admin_log_topic_boot or settings.admin_log_topic_id,
                    text=boot_text,
                    parse_mode="MarkdownV2",
                )
            except Exception:
                logging.getLogger(__name__).exception("Не удалось отправить BOOT-уведомление downloader-бота")

    dp.startup.register(_on_startup)
    await wait_telegram_online(bot, service="downloader")
    await dp.start_polling(bot)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
