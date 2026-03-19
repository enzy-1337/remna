"""Умная корзина (Redis): выбранный тариф при нехватке баланса — TTL 30 мин."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import redis.asyncio as redis

from shared.config import Settings, get_settings

CART_KEY = "smart_cart:{telegram_id}"
TTL_SEC = 30 * 60


def _client(url: str) -> redis.Redis:
    return redis.from_url(url, encoding="utf-8", decode_responses=True)


async def set_cart_plan(
    telegram_id: int,
    *,
    plan_id: int,
    amount_rub: Decimal | None = None,
    settings: Settings | None = None,
) -> None:
    s = settings or get_settings()
    r = _client(s.redis_url)
    payload: dict[str, Any] = {"plan_id": plan_id}
    if amount_rub is not None:
        payload["amount_rub"] = str(amount_rub)
    await r.setex(CART_KEY.format(telegram_id=telegram_id), TTL_SEC, json.dumps(payload))
    await r.aclose()


async def get_cart(telegram_id: int, settings: Settings | None = None) -> dict[str, Any] | None:
    s = settings or get_settings()
    r = _client(s.redis_url)
    try:
        raw = await r.get(CART_KEY.format(telegram_id=telegram_id))
        if not raw:
            return None
        return json.loads(raw)
    finally:
        await r.aclose()


async def clear_cart(telegram_id: int, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    r = _client(s.redis_url)
    try:
        await r.delete(CART_KEY.format(telegram_id=telegram_id))
    finally:
        await r.aclose()
