"""Вход на сайт через бота (диплинк + код) — вместо Telegram Login Widget, который сейчас
не работает у Telegram (oauth.telegram.org/auth отключён — проверено, отвечает "deprecated"
на любой запрос, включая Telegram.Login.auth()). Тот же принцип, что и у shared/services/
flux_login_service.py (десктоп-клиент), только результат — telegram_id для сайта, а не
ссылка подписки, плюс сохраняем реферальный код с сайта до перехода в бота.
"""

from __future__ import annotations

import logging
import secrets

import redis.asyncio as redis

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

_RESULT_KEY = "site_login:{code}"
_REF_KEY = "site_login_ref:{code}"
_TTL_SEC = 300  # 5 минут на подтверждение в боте
_PREFIX = "sitelogin_"


def _client(url: str) -> redis.Redis:
    return redis.from_url(url, encoding="utf-8", decode_responses=True)


def new_login_code() -> str:
    return secrets.token_urlsafe(18)


def parse_site_login_code(start_args: str | None) -> str | None:
    raw = (start_args or "").strip()
    if raw.startswith(_PREFIX):
        code = raw[len(_PREFIX):].strip()
        return code or None
    return None


def site_bot_deeplink(code: str, settings: Settings | None = None) -> str | None:
    s = settings or get_settings()
    username = (s.bot_username or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}?start={_PREFIX}{code}"


async def save_pending_ref(code: str, ref_code: str, *, settings: Settings | None = None) -> None:
    if not ref_code:
        return
    s = settings or get_settings()
    try:
        r = _client(s.redis_url)
        try:
            await r.set(_REF_KEY.format(code=code), ref_code, ex=_TTL_SEC)
        finally:
            await r.aclose()
    except Exception:
        logger.exception("save_pending_ref failed")


async def pop_pending_ref(code: str, *, settings: Settings | None = None) -> str | None:
    s = settings or get_settings()
    try:
        r = _client(s.redis_url)
        try:
            pipe = r.pipeline()
            pipe.get(_REF_KEY.format(code=code))
            pipe.delete(_REF_KEY.format(code=code))
            val, _deleted = await pipe.execute()
        finally:
            await r.aclose()
    except Exception:
        logger.exception("pop_pending_ref failed")
        return None
    return (str(val).strip() or None) if val else None


async def mark_login_done(code: str, telegram_id: int, *, settings: Settings | None = None) -> None:
    """Вызывается ботом на /start sitelogin_<code> — сохраняет подтверждённого пользователя."""
    s = settings or get_settings()
    try:
        r = _client(s.redis_url)
        try:
            await r.set(_RESULT_KEY.format(code=code), str(int(telegram_id)), ex=_TTL_SEC)
        finally:
            await r.aclose()
    except Exception:
        logger.exception("mark_login_done failed")


async def pop_login_result(code: str, *, settings: Settings | None = None) -> int | None:
    """Сайт поллит этим — вернёт telegram_id один раз, затем код удаляется."""
    code = (code or "").strip()
    if not code:
        return None
    s = settings or get_settings()
    try:
        r = _client(s.redis_url)
        try:
            pipe = r.pipeline()
            pipe.get(_RESULT_KEY.format(code=code))
            pipe.delete(_RESULT_KEY.format(code=code))
            val, _deleted = await pipe.execute()
        finally:
            await r.aclose()
    except Exception:
        logger.exception("pop_login_result failed")
        return None
    if not val:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
