"""Отправка сообщений пользователю через Bot API (без polling-цикла)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)


async def send_telegram_message(
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str = "HTML",
    message_thread_id: int | None = None,
    settings: Settings | None = None,
) -> bool:
    s = settings or get_settings()
    url = f"https://api.telegram.org/bot{s.bot_token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
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
