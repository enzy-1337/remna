"""
Точка входа Telegram-бота.
Запуск из корня репозитория: python -m bot.main

При TELEGRAM_WEBHOOK_ENABLED=true polling не используется — апдейты принимает API (POST /webhooks/telegram), см. api.main.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Корень проекта в PYTHONPATH (без установки пакета)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot.background_loops import cancel_background_tasks, start_background_loops
from bot.bootstrap_db import bootstrap_bot_database_schema
from bot.factory import apply_ipv4_preferred_dns, create_bot_and_dispatcher
from shared.config import get_settings
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin_plain


async def main() -> None:
    apply_ipv4_preferred_dns()

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger(__name__)

    if settings.telegram_webhook_enabled:
        log.error(
            "TELEGRAM_WEBHOOK_ENABLED=true: polling отключён. Запустите uvicorn api.main:app "
            "и направьте TELEGRAM_WEBHOOK_URL на POST /webhooks/telegram этого сервиса."
        )
        raise SystemExit(2)

    await bootstrap_bot_database_schema()

    bot, dp = await create_bot_and_dispatcher(settings)
    stop_event = asyncio.Event()
    bg_tasks = start_background_loops(settings, stop_event)
    try:
        boot_ts = datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M:%S | %d-%m-%Y | МСК")
        sent = await notify_admin_plain(
            settings,
            text=f"🚀 Основной бот запущен (polling)\n{boot_ts}",
            topic=AdminLogTopic.BOOT,
            event_type="bot_startup",
        )
        if sent:
            log.info("Уведомление о запуске отправлено в админ-чат (тема BOOT).")
        await dp.start_polling(bot)
    finally:
        stop_event.set()
        await cancel_background_tasks(bg_tasks)


if __name__ == "__main__":
    asyncio.run(main())
