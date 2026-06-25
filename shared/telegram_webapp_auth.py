"""Валидация Telegram WebApp initData (Mini App) — см. https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class WebAppAuthError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, *, max_age_seconds: int = 86400) -> dict:
    """
    Проверяет подпись initData и возвращает распарсенные данные (включая ключ "user" как dict).
    Бросает WebAppAuthError при невалидной подписи/истёкшем auth_date.
    """
    if not init_data or not bot_token:
        raise WebAppAuthError("empty init_data or bot_token")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    data: dict[str, str] = dict(pairs)
    received_hash = data.pop("hash", "")
    if not received_hash:
        raise WebAppAuthError("missing hash")
    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        raise WebAppAuthError("bad signature")
    auth_date = data.get("auth_date")
    if auth_date:
        try:
            if time.time() - int(auth_date) > max_age_seconds:
                raise WebAppAuthError("init_data expired")
        except ValueError:
            pass
    result: dict = dict(data)
    if "user" in result:
        try:
            result["user"] = json.loads(result["user"])
        except (ValueError, TypeError):
            result["user"] = None
    return result


def extract_telegram_id(init_data: str, bot_token: str) -> int:
    """Удобный шорткат: вернуть telegram_id пользователя из валидного initData."""
    parsed = validate_init_data(init_data, bot_token)
    user = parsed.get("user") or {}
    tg_id = user.get("id")
    if not tg_id:
        raise WebAppAuthError("no user.id in init_data")
    return int(tg_id)
