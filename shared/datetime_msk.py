"""Форматирование дат в Europe/Moscow для сообщений."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MSK_KEY = "Europe/Moscow"


@lru_cache(maxsize=1)
def msk_tzinfo():
    """IANA Europe/Moscow; на slim-образах без tzdata — фиксированный UTC+3."""
    try:
        return ZoneInfo(_MSK_KEY)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=3))


def fmt_dt_msk(dt: datetime | None, *, with_suffix: bool = True) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = dt.astimezone(msk_tzinfo()).strftime("%d.%m.%Y %H:%M")
    return f"{s} МСК" if with_suffix else s
