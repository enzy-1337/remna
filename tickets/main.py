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

from bot.middlewares.db_session import DbSessionMiddleware
from bot.middlewares.private_chat_only import PrivateChatOnlyMiddleware
from shared.config import get_settings
from shared.services.admin_error_log_handler import install_admin_error_log_handler
from shared.telegram_connect import safe_set_bot_commands, wait_telegram_online
from tickets.config import config
from tickets.router import tickets_router
from tickets.scheduler import TicketScheduler


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    install_admin_error_log_handler(service="tickets")
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
        await safe_set_bot_commands(
            bot,
            service="tickets",
            private_commands=[BotCommand(command="start", description="Поддержка и тикеты")],
        )
        await scheduler.start()
        try:
            settings = get_settings()
            chat_id = settings.admin_log_chat_id
            if chat_id is None or (isinstance(chat_id, str) and not chat_id.strip()):
                return
            thread_id = settings.admin_log_topic_boot or settings.admin_log_topic_id
            boot_ts = datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).strftime(
                "%H:%M:%S | %d-%m-%Y | МСК"
            )
            from shared.md2 import bold, join_lines, plain

            boot_text = join_lines("🛟 " + bold("Support bot запущен"), plain(boot_ts))
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=boot_text,
                parse_mode="MarkdownV2",
            )
            logging.getLogger(__name__).info(
                "Уведомление о запуске бота поддержки отправлено от имени самого бота (тема BOOT)."
            )
        except Exception:
            logging.getLogger(__name__).exception("Не удалось отправить BOOT-уведомление от tickets-бота")

    async def _on_shutdown(*_args, **_kwargs) -> None:
        await scheduler.stop()

    dp.startup.register(_on_startup)
    dp.shutdown.register(_on_shutdown)

    dp.update.middleware(PrivateChatOnlyMiddleware(allowed_chat_ids={int(config.support_group_id)}))
    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(tickets_router())

    await wait_telegram_online(bot, service="tickets")
    await dp.start_polling(bot)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
