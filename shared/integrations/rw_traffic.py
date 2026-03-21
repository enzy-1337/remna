"""Извлечение использованного/лимита трафика из ответа Remnawave (поля могут отличаться по версии панели)."""

from __future__ import annotations

from typing import Any


def _bytes_to_gb(value: Any) -> float | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return round(n / (1024**3), 2)


def extract_traffic_gb_from_rw_user(u: dict[str, Any]) -> tuple[float | None, float | None]:
    """
    Возвращает (used_gb, limit_gb).
    Если лимит 0 в API — часто значит «без лимита» → limit_gb = None.
    """
    used: float | None = None
    for key in (
        "usedTrafficBytes",
        "usedBytes",
        "userTrafficInBytes",
        "totalUsedBytes",
        "consumedTrafficBytes",
    ):
        if key in u and u[key] is not None:
            used = _bytes_to_gb(u[key])
            if used is not None:
                break

    if used is None:
        nested = u.get("trafficStatistics") or u.get("statistics") or {}
        if isinstance(nested, dict):
            for key in ("usedBytes", "usedTrafficBytes", "totalUsedBytes", "used"):
                if key in nested and nested[key] is not None:
                    used = _bytes_to_gb(nested[key])
                    if used is not None:
                        break

    raw_limit = u.get("trafficLimitBytes")
    limit_gb = _bytes_to_gb(raw_limit) if raw_limit is not None else None
    if raw_limit is not None:
        try:
            if int(raw_limit) == 0:
                limit_gb = None
        except (TypeError, ValueError):
            pass

    return used, limit_gb


def extract_connected_devices_from_rw_user(u: dict[str, Any]) -> int | None:
    for key in (
        "connectedDevices",
        "connectedClients",
        "activeConnections",
        "usedDevices",
        "activeDevices",
        "clientsCount",
    ):
        val = u.get(key)
        if val is None:
            continue
        try:
            n = int(val)
            if n >= 0:
                return n
        except (TypeError, ValueError):
            continue
    nested = u.get("statistics") or u.get("trafficStatistics") or {}
    if isinstance(nested, dict):
        for key in ("connectedDevices", "activeConnections", "usedDevices"):
            val = nested.get(key)
            if val is None:
                continue
            try:
                n = int(val)
                if n >= 0:
                    return n
            except (TypeError, ValueError):
                continue
    return None
