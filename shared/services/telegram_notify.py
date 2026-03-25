"""Отправка сообщений пользователю через Bot API (без polling-цикла)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

MAX_CAPTION = 1024


async def send_telegram_message(
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str | None = "MarkdownV2",
    message_thread_id: int | None = None,
    settings: Settings | None = None,
) -> bool:
    s = settings or get_settings()
    url = f"https://api.telegram.org/bot{s.bot_token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload)
            data = r.json()
        if not data.get("ok"):
            logger.error("Telegram sendMessage failed: %s", data)
            return False
        return True
    except Exception:
        logger.exception("Telegram sendMessage error")
        return False


async def send_telegram_document(
    chat_id: int | str,
    document_path: str | Path,
    *,
    caption: str | None = None,
    message_thread_id: int | None = None,
    settings: Settings | None = None,
) -> bool:
    """Отправка файла (sendDocument)."""
    s = settings or get_settings()
    path = Path(document_path)
    if not path.is_file():
        logger.error("sendDocument: файл не найден: %s", path)
        return False
    url = f"https://api.telegram.org/bot{s.bot_token}/sendDocument"
    data: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:MAX_CAPTION]
    if message_thread_id is not None:
        data["message_thread_id"] = message_thread_id
    try:
        content = path.read_bytes()
        files = {"document": (path.name, content)}
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(url, data=data, files=files)
            resp = r.json()
        if not resp.get("ok"):
            logger.error("Telegram sendDocument failed: %s", resp)
            return False
        return True
    except Exception:
        logger.exception("Telegram sendDocument error")
        return False
