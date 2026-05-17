"""Отложенный /start payload (реферал) до подписки на обязательный канал."""

from __future__ import annotations

import logging

import redis.asyncio as redis

from shared.config import Settings, get_settings
from shared.services.referral_parse import parse_referral_code_from_start_args

logger = logging.getLogger(__name__)

_PENDING_KEY = "pending_start_ref:{telegram_id}"
# Пользователь может подписаться на канал не сразу после перехода по ссылке.
_PENDING_TTL_SEC = 7 * 24 * 3600


def _client(url: str) -> redis.Redis:
    return redis.from_url(url, encoding="utf-8", decode_responses=True)


def should_store_pending_referral(start_args: str | None) -> bool:
    return parse_referral_code_from_start_args(start_args) is not None


async def save_pending_referral_start(
    telegram_id: int,
    start_args: str | None,
    *,
    settings: Settings | None = None,
) -> None:
    """Запомнить ref_X из /start, если пользователь ещё не прошёл проверку канала."""
    raw = (start_args or "").strip()
    if not raw or not should_store_pending_referral(raw):
        return
    s = settings or get_settings()
    key = _PENDING_KEY.format(telegram_id=int(telegram_id))
    try:
        r = _client(s.redis_url)
        try:
            await r.set(key, raw, ex=_PENDING_TTL_SEC)
        finally:
            await r.aclose()
    except Exception:
        logger.exception("save_pending_referral_start failed tg_id=%s", telegram_id)


async def clear_pending_referral_start(
    telegram_id: int,
    *,
    settings: Settings | None = None,
) -> None:
    s = settings or get_settings()
    key = _PENDING_KEY.format(telegram_id=int(telegram_id))
    try:
        r = _client(s.redis_url)
        try:
            await r.delete(key)
        finally:
            await r.aclose()
    except Exception:
        logger.exception("clear_pending_referral_start failed tg_id=%s", telegram_id)


async def pop_pending_referral_start(
    telegram_id: int,
    *,
    settings: Settings | None = None,
) -> str | None:
    """Вернуть и удалить сохранённый /start payload (для register_user)."""
    s = settings or get_settings()
    key = _PENDING_KEY.format(telegram_id=int(telegram_id))
    try:
        r = _client(s.redis_url)
        try:
            pipe = r.pipeline()
            pipe.get(key)
            pipe.delete(key)
            raw, _deleted = await pipe.execute()
        finally:
            await r.aclose()
    except Exception:
        logger.exception("pop_pending_referral_start failed tg_id=%s", telegram_id)
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


async def resolve_start_args_for_new_user(
    telegram_id: int,
    message_payload: str | None,
    *,
    settings: Settings | None = None,
) -> str | None:
    """
    Аргументы /start для register_user: из сообщения или из Redis (если ждали подписку на канал).
    """
    direct = (message_payload or "").strip() or None
    if direct and should_store_pending_referral(direct):
        await clear_pending_referral_start(telegram_id, settings=settings)
        return direct
    pending = await pop_pending_referral_start(telegram_id, settings=settings)
    if pending:
        return pending
    return direct
