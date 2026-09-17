"""Проверка подписи Telegram Login Widget (используется web-admin и публичным сайтом)."""

from __future__ import annotations

import hmac
from hashlib import sha256


def verify_telegram_login(payload: dict[str, str], bot_token: str) -> bool:
    check_hash = payload.get("hash", "")
    if not check_hash:
        return False
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash" and v)
    secret = sha256(bot_token.encode("utf-8")).digest()
    calc_hash = hmac.new(secret, data_check.encode("utf-8"), sha256).hexdigest()
    return hmac.compare_digest(calc_hash, check_hash)
