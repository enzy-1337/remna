from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from bot.handlers.downloader import router as downloader_router
from bot.middlewares.db_session import DbSessionMiddleware
from bot.middlewares.private_chat_only import PrivateChatOnlyMiddleware
from bot.middlewares.user_context import UserContextMiddleware
from shared.config import get_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
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
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(UserContextMiddleware())
    dp.include_router(downloader_router)

    async def _on_startup(*_args, **_kwargs) -> None:
        await bot.set_my_commands(
            commands=[BotCommand(command="start", description="Начать работу с ботом")],
            scope=BotCommandScopeAllPrivateChats(),
        )

    dp.startup.register(_on_startup)
    dp.run_polling(bot)


if __name__ == "__main__":
    main()
