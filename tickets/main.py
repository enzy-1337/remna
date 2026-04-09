from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.middlewares.db_session import DbSessionMiddleware
from shared.config import get_settings
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin_plain
from tickets.config import config
from tickets.router import tickets_router
from tickets.scheduler import TicketScheduler

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
    scheduler = TicketScheduler(bot)

    async def _on_startup(*_args, **_kwargs) -> None:
        await scheduler.start()
        try:
            settings = get_settings()
            boot_ts = datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S")
            sent = await notify_admin_plain(
                settings,
                text=f"🛟 Бот поддержки запущен\n{boot_ts} (МСК)",
                topic=AdminLogTopic.BOOT,
                event_type="tickets_bot_startup",
            )
            if sent:
                logging.getLogger(__name__).info(
                    "Уведомление о запуске бота поддержки отправлено в админ-чат (тема BOOT)."
                )
        except Exception:
            logging.getLogger(__name__).exception("Не удалось отправить BOOT-уведомление для бота поддержки")

    async def _on_shutdown(*_args, **_kwargs) -> None:
        await scheduler.stop()

    dp.startup.register(_on_startup)
    dp.shutdown.register(_on_shutdown)

    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(tickets_router())
    dp.run_polling(bot)


if __name__ == "__main__":
    main()

