"""Флаги функций с быстрым переключением через Redis (бот без перезапуска)."""

from __future__ import annotations

import redis.asyncio as redis

from shared.config import Settings, get_settings

_REDIS_KEY_TARIFF_PURCHASES = "remna:feature:bot_tariff_purchases_enabled"


def _client(url: str) -> redis.Redis:
    return redis.from_url(url, encoding="utf-8", decode_responses=True)


async def tariff_purchases_enabled(settings: Settings | None = None) -> bool:
    """Покупка тарифов в боте: Redis переопределяет .env, если ключ задан."""
    s = settings or get_settings()
    try:
        r = _client(s.redis_url)
        try:
            v = await r.get(_REDIS_KEY_TARIFF_PURCHASES)
            if v is not None:
                return str(v).strip().lower() in ("1", "true", "yes", "on")
        finally:
            await r.aclose()
    except Exception:
        pass
    return bool(getattr(s, "bot_tariff_purchases_enabled", True))


async def set_tariff_purchases_enabled_redis(settings: Settings, enabled: bool) -> None:
    r = _client(settings.redis_url)
    try:
        await r.set(_REDIS_KEY_TARIFF_PURCHASES, "1" if enabled else "0")
    finally:
        await r.aclose()
