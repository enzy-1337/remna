"""Логирующий handler: пересылает ERROR+ записи в тему форума ADMIN_LOG_TOPIC_ERRORS.

Подключается один раз на процесс (install_admin_error_log_handler) — ловит
logger.error()/logger.exception() из любого модуля так же, как остальные
уведомления уходят в notify_admin_plain.
"""

from __future__ import annotations

import asyncio
import logging
import traceback

from shared.config import Settings, get_settings
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin_plain

_INSTALLED = False

# Модули, чьи ошибки нельзя пересылать — иначе при сбое самой отправки
# получаем бесконечную рекурсию логов.
_SUPPRESSED_LOGGERS = {
    "shared.services.telegram_notify",
    "shared.services.admin_notify",
    "shared.services.admin_error_log_handler",
    "httpx",
    "httpcore",
}


class AdminErrorTelegramHandler(logging.Handler):
    def __init__(self, settings: Settings) -> None:
        super().__init__(level=logging.ERROR)
        self._settings = settings

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in _SUPPRESSED_LOGGERS or record.name.startswith("httpx") or record.name.startswith("httpcore"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            text = self._format_plain(record)
        except Exception:
            return
        loop.create_task(self._send(text))

    def _format_plain(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        lines = [
            "🛑 Ошибка",
            f"logger: {record.name}",
            f"{message}",
        ]
        if record.exc_info:
            tb = "".join(traceback.format_exception(*record.exc_info))
            lines.append(tb[-3000:])
        return "\n".join(lines)

    async def _send(self, text: str) -> None:
        try:
            await notify_admin_plain(
                self._settings,
                text=text,
                topic=AdminLogTopic.ERRORS,
                event_type="error_log",
            )
        except Exception:
            pass


def install_admin_error_log_handler(settings: Settings | None = None) -> None:
    """Подключить пересылку ошибок логов в Telegram-тему. Безопасно вызывать многократно."""
    global _INSTALLED
    if _INSTALLED:
        return
    s = settings or get_settings()
    logging.getLogger().addHandler(AdminErrorTelegramHandler(s))
    _INSTALLED = True
