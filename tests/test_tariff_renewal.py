"""Тарифы: расчёт цены со скидкой и окно продления подписки."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from shared.models.subscription import Subscription
from shared.services.subscription_service import (
    calculate_tariff_price_from_base_month,
    can_renew_subscription_with_tariff,
    is_one_month_duration,
    subscription_days_left,
    subscription_extension_would_stack,
)


def test_calculate_tariff_price_floor_two_months_two_percent() -> None:
    price = calculate_tariff_price_from_base_month(
        Decimal("179"),
        duration_days=60,
        discount_percent=Decimal("2"),
    )
    assert price == Decimal("350")


def test_calculate_tariff_price_floor_three_months_five_percent() -> None:
    price = calculate_tariff_price_from_base_month(
        Decimal("179"),
        duration_days=90,
        discount_percent=Decimal("5"),
    )
    assert price == Decimal("510")


def test_extension_would_stack() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    sub = Subscription(
        id=1,
        user_id=1,
        plan_id=1,
        expires_at=now + timedelta(days=14),
        status="active",
        devices_count=2,
    )
    assert subscription_extension_would_stack(sub, now=now) is True
    sub_long = Subscription(
        id=2,
        user_id=1,
        plan_id=1,
        expires_at=now + timedelta(days=400),
        status="active",
        devices_count=2,
    )
    assert subscription_extension_would_stack(sub_long, now=now) is False


def test_renewal_allowed_within_window() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    sub = Subscription(
        id=1,
        user_id=1,
        plan_id=1,
        expires_at=now + timedelta(days=6),
        status="active",
        devices_count=2,
    )
    assert can_renew_subscription_with_tariff(sub, window_days=7, now=now) is True


def test_is_one_month_duration() -> None:
    assert is_one_month_duration(30) is True
    assert is_one_month_duration(60) is False


def test_renewal_blocked_too_early() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    sub = Subscription(
        id=1,
        user_id=1,
        plan_id=1,
        expires_at=now + timedelta(days=14),
        status="active",
        devices_count=2,
    )
    assert can_renew_subscription_with_tariff(sub, window_days=7, now=now) is False
    assert subscription_days_left(sub.expires_at, now=now) == 14
