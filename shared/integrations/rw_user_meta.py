"""Поля профиля Remnawave: последнее подключение и т.п."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _parse_rw_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def rw_user_online_at(uinf: dict[str, Any] | None) -> datetime | None:
    """Время «онлайн» / последней активности в панели (userTraffic.onlineAt)."""
    if not uinf or not isinstance(uinf.get("userTraffic"), dict):
        return None
    return _parse_rw_dt(uinf["userTraffic"].get("onlineAt"))


def rw_user_first_connected_at(uinf: dict[str, Any] | None) -> datetime | None:
    if not uinf or not isinstance(uinf.get("userTraffic"), dict):
        return None
    return _parse_rw_dt(uinf["userTraffic"].get("firstConnectedAt"))
