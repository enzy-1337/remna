"""Логирующий handler: пересылает ERROR+ записи в тему форума ADMIN_LOG_TOPIC_ERRORS
и сохраняет их в БД (app_error_logs), чтобы искать/просматривать логи из бота
командой /logs, не заходя на сервер (см. bot/handlers/logs_viewer.py).

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
from shared.md2 import bold, code, plain
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import _admin_chat_configured
from shared.services.telegram_notify import send_telegram_document, send_telegram_message

_INSTALLED = False

# Сообщение целиком не должно превышать лимит Telegram (4096) — оставляем запас под обёртку.
_MAX_INLINE_BODY = 3500

# Модули, чьи ошибки нельзя пересылать — иначе при сбое самой отправки/записи в БД
# получаем бесконечную рекурсию логов.
_SUPPRESSED_LOGGERS = {
    "shared.services.telegram_notify",
    "shared.services.admin_notify",
    "shared.services.admin_error_log_handler",
    "shared.database",
}

# aiogram.dispatcher логирует эти как logger.error(), хотя сам же polling-луп их и лечит —
# ретраит с бэкоффом на следующей итерации без вмешательства. Особенно часто вылезают пачкой
# сразу после перезапуска процесса/хоста (обрыв TLS-сессии, недо-отпущенный getUpdates от
# прошлого инстанса → flood control, кратковременный 502 у Telegram) и сами закрываются за
# секунды — пересылка каждой такой в админ-чат/Логи только шумит без всякого действия админа.
# Настоящий сбой (токен невалиден, Telegram недоступен часами) продолжит валиться в docker logs
# как обычно (aiogram сам это логирует), просто не будет дублироваться сюда.
_TRANSIENT_POLLING_PREFIX = "Failed to fetch updates"
_TRANSIENT_POLLING_MARKERS = ("TelegramNetworkError", "TelegramRetryAfter", "TelegramServerError")


def _is_transient_polling_hiccup(record: logging.LogRecord, message: str) -> bool:
    return (
        record.name == "aiogram.dispatcher"
        and message.startswith(_TRANSIENT_POLLING_PREFIX)
        and any(marker in message for marker in _TRANSIENT_POLLING_MARKERS)
    )


class AdminErrorTelegramHandler(logging.Handler):
    def __init__(self, settings: Settings, service: str = "app") -> None:
        super().__init__(level=logging.ERROR)
        self._settings = settings
        self._service = service

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
            message = record.getMessage()
            tb = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else None
            body = f"{message}\n\n{tb}" if (message and tb) else (tb or message)
        except Exception:
            return
        if _is_transient_polling_hiccup(record, message):
            return
        loop.create_task(self._send(record.name, body))
        loop.create_task(self._persist(record.name, message, tb))

    async def _send(self, logger_name: str, body: str) -> None:
        if not _admin_chat_configured(self._settings):
            return
        header = f"logger: {plain(logger_name)}"
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
                        caption=f"🛑 Ошибка\nlogger: {logger_name}",
                        message_thread_id=thread,
                        settings=self._settings,
                    )
                finally:
                    path.unlink(missing_ok=True)
        except Exception:
            pass

    async def _persist(self, logger_name: str, message: str, tb: str | None) -> None:
        try:
            from shared.database import get_session_factory
            from shared.models.app_error_log import AppErrorLog

            factory = get_session_factory()
            async with factory() as session:
                session.add(
                    AppErrorLog(
                        service=self._service,
                        logger_name=logger_name[:255],
                        message=message[:20000],
                        traceback=tb[:20000] if tb else None,
                    )
                )
                await session.commit()
        except Exception:
            pass


def install_admin_error_log_handler(settings: Settings | None = None, *, service: str = "app") -> None:
    """Подключить пересылку ошибок логов в Telegram-тему и в БД. Безопасно вызывать многократно."""
    global _INSTALLED
    if _INSTALLED:
        return
    s = settings or get_settings()
    logging.getLogger().addHandler(AdminErrorTelegramHandler(s, service=service))
    _INSTALLED = True
