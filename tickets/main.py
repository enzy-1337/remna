from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.middlewares.db_session import DbSessionMiddleware
from tickets.config import config
from tickets.router import tickets_router

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("tickets")
    log.info(
        "Tickets config loaded: TICKETS_BOT_TOKEN=%s SUPPORT_GROUP_ID=%s REMINDER_HOURS=%s AUTO_CLOSE_DAYS=%s ADMIN_IDS=%s",
        ("set" if config.bot_token else "empty"),
        config.support_group_id,
        config.reminder_hours,
        config.auto_close_days,
        config.admin_ids,
    )
    if not config.bot_token:
        raise RuntimeError("TICKETS_BOT_TOKEN is empty")

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(tickets_router())
    dp.run_polling(bot)


if __name__ == "__main__":
    main()

