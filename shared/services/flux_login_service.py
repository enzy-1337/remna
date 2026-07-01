"""One-time Telegram login codes for the Flux desktop client.

Flow:
  1. Desktop asks the API to start a login -> gets a random ``code`` and a
     ``t.me/<bot>?start=fluxlogin_<code>`` deep link, opens it in the browser.
  2. The user taps Start; the bot resolves their subscription URL and stores it
     under the code in Redis (short TTL).
  3. Desktop polls the API with the code; once the URL is present it is returned
     (and consumed), and the client imports the subscription.
"""

from __future__ import annotations

import logging
import secrets

import redis.asyncio as redis

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

_KEY = "flux_login:{code}"
_TTL_SEC = 300  # 5 min to finish login before the code expires
_PREFIX = "fluxlogin_"


def _client(url: str) -> redis.Redis:
    return redis.from_url(url, encoding="utf-8", decode_responses=True)


def new_login_code() -> str:
    """A fresh URL-safe login code."""
    return secrets.token_urlsafe(18)


def parse_login_code(start_args: str | None) -> str | None:
    """Extract the code from a ``fluxlogin_<code>`` /start payload, else None."""
    raw = (start_args or "").strip()
    if raw.startswith(_PREFIX):
        code = raw[len(_PREFIX):].strip()
        return code or None
    return None


def bot_deeplink(code: str, settings: Settings | None = None) -> str | None:
    """`https://t.me/<bot>?start=fluxlogin_<code>` — None if bot username unset."""
    s = settings or get_settings()
    username = (s.bot_username or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}?start={_PREFIX}{code}"


async def save_login_url(code: str, url: str, *, settings: Settings | None = None) -> None:
    """Bind a subscription URL to a login code (set by the bot on /start)."""
    s = settings or get_settings()
    try:
        r = _client(s.redis_url)
        try:
            await r.set(_KEY.format(code=code), url, ex=_TTL_SEC)
        finally:
            await r.aclose()
    except Exception:
        logger.exception("save_login_url failed")


async def pop_login_url(code: str, *, settings: Settings | None = None) -> str | None:
    """Return + delete the subscription URL for a code (one-time). None if not set yet."""
    code = (code or "").strip()
    if not code:
        return None
    s = settings or get_settings()
    try:
        r = _client(s.redis_url)
        try:
            pipe = r.pipeline()
            pipe.get(_KEY.format(code=code))
            pipe.delete(_KEY.format(code=code))
            url, _deleted = await pipe.execute()
        finally:
            await r.aclose()
    except Exception:
        logger.exception("pop_login_url failed")
        return None
    if not url:
        return None
    text = str(url).strip()
    return text or None
