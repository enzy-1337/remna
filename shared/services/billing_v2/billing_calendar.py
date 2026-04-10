"""Календарные границы биллинга в настроенном часовом поясе (по умолчанию Europe/Moscow)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from shared.config import Settings


def billing_zoneinfo(settings: Settings) -> ZoneInfo:
    return ZoneInfo(settings.billing_calendar_timezone)


def billing_today(settings: Settings) -> date:
    return datetime.now(timezone.utc).astimezone(billing_zoneinfo(settings)).date()


def billing_local_day_start_utc(settings: Settings, d: date) -> datetime:
    z = billing_zoneinfo(settings)
    local_start = datetime.combine(d, datetime.min.time()).replace(tzinfo=z)
    return local_start.astimezone(timezone.utc)


def billing_local_day_end_utc_exclusive(settings: Settings, d: date) -> datetime:
    return billing_local_day_start_utc(settings, d + timedelta(days=1))
