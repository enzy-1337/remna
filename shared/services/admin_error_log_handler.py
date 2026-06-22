"""Логирующий handler: пересылает ERROR+ записи в тему форума ADMIN_LOG_TOPIC_ERRORS.

Подключается один раз на процесс (install_admin_error_log_handler) — ловит
logger.error()/logger.exception() из любого модуля так же, как остальные
уведомления уходят в тему форума. Текст ошибки оборачивается в `код`,
чтобы его можно было скопировать одним тапом; если он не помещается
в одно сообщение — отправляется файлом error.log.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import traceback
from pathlib import Path

from shared.config import Settings, get_settings
from shared.md2 import bold, code
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import _admin_chat_configured
from shared.services.telegram_notify import send_telegram_document, send_telegram_message

_INSTALLED = False

# Сообщение целиком не должно превышать лимит Telegram (4096) — оставляем запас под обёртку.
_MAX_INLINE_BODY = 3500

# Модули, чьи ошибки нельзя пересылать — иначе при сбое самой отправки
# получаем бесконечную рекурсию логов.
_SUPPRESSED_LOGGERS = {
    "shared.services.telegram_notify",
    "shared.services.admin_notify",
    "shared.services.admin_error_log_handler",
}


class AdminErrorTelegramHandler(logging.Handler):
    def __init__(self, settings: Settings) -> None:
        super().__init__(level=logging.ERROR)
        self._settings = settings

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.name in _SUPPRESSED_LOGGERS
            or record.name.startswith("httpx")
            or record.name.startswith("httpcore")
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            header, body = self._format(record)
        except Exception:
            return
        loop.create_task(self._send(header, body))

    def _format(self, record: logging.LogRecord) -> tuple[str, str]:
        message = record.getMessage()
        header = f"logger: {record.name}"
        body = message
        if record.exc_info:
            tb = "".join(traceback.format_exception(*record.exc_info))
            body = f"{message}\n\n{tb}" if message else tb
        return header, body

    async def _send(self, header: str, body: str) -> None:
        if not _admin_chat_configured(self._settings):
            return
        chat_id = self._settings.admin_log_chat_id
        thread = self._settings.admin_log_thread_for(AdminLogTopic.ERRORS)
        try:
            if len(body) <= _MAX_INLINE_BODY:
                text = f"🛑 {bold('Ошибка')}\n{header}\n{code(body)}"
                await send_telegram_message(
                    chat_id,
                    text,
                    message_thread_id=thread,
                    parse_mode="MarkdownV2",
                    settings=self._settings,
                )
            else:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".log", prefix="error_", delete=False, encoding="utf-8"
                ) as f:
                    f.write(body)
                    path = Path(f.name)
                try:
                    await send_telegram_document(
                        chat_id,
                        path,
                        caption=f"🛑 Ошибка\n{header}",
                        message_thread_id=thread,
                        settings=self._settings,
                    )
                finally:
                    path.unlink(missing_ok=True)
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
