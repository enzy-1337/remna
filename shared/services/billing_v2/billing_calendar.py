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


def billing_package_month_utc_bounds(settings: Settings, event_ts: datetime) -> tuple[datetime, datetime]:
    """
    Полуинтервал [start_utc, end_utc) — календарный месяц в ``BILLING_CALENDAR_TIMEZONE``,
    в который попадает момент ``event_ts`` (для лимита пакетных ГБ по месяцу).

    Раньше в ``rating_service`` месяц считался по UTC-календарю от ``event_ts``, что расходилось
    с дневной детализацией (``billing_today`` и сутки по той же таймзоне).
    """
    z = billing_zoneinfo(settings)
    local = event_ts.astimezone(z)
    first = date(local.year, local.month, 1)
    if local.month == 12:
        next_first = date(local.year + 1, 1, 1)
    else:
        next_first = date(local.year, local.month + 1, 1)
    start_utc = billing_local_day_start_utc(settings, first)
    end_utc = billing_local_day_start_utc(settings, next_first)
    return start_utc, end_utc
