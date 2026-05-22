"""Форматирование дат в Europe/Moscow для сообщений."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_MSK_TZ = ZoneInfo("Europe/Moscow")


def fmt_dt_msk(dt: datetime | None, *, with_suffix: bool = True) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    s = dt.astimezone(_MSK_TZ).strftime("%d.%m.%Y %H:%M")
    return f"{s} МСК" if with_suffix else s
