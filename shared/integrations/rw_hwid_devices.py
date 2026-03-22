"""Парсинг HWID-устройств из ответа Remnawave (GET /api/hwid/devices/{userUuid})."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def parse_rw_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def format_rw_device_datetime_local(raw: str | None) -> str:
    """Напр.: «13:48 17 марта 2026» (UTC)."""
    dt = parse_rw_iso_dt(raw)
    if dt is None:
        return "—"
    m = _MONTHS_RU[dt.month - 1]
    return f"{dt.hour:02d}:{dt.minute:02d} {dt.day} {m} {dt.year}"


def hwid_device_sort_key(d: dict[str, Any]) -> tuple[float, str]:
    dt = parse_rw_iso_dt(str(d.get("createdAt") or ""))
    ts = dt.timestamp() if dt else 0.0
    return (ts, str(d.get("hwid") or ""))


def normalize_hwid_devices_list(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [d for d in devices if isinstance(d, dict) and d.get("hwid")]
    out.sort(key=hwid_device_sort_key)
    return out


def hwid_device_title(d: dict[str, Any], index_1based: int) -> str:
    model = (d.get("deviceModel") or "").strip()
    plat = (d.get("platform") or "").strip()
    label = model or plat or "Устройство"
    text = f"#{index_1based} {label}"
    return text if len(text) <= 60 else text[:57] + "…"
