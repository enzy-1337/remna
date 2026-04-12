"""Границы календарного месяца для пакетного лимита ГБ (BILLING_CALENDAR_TIMEZONE)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from shared.services.billing_v2.billing_calendar import billing_package_month_utc_bounds


class BillingPackageMonthBoundsTests(unittest.TestCase):
    def test_moscow_march_31_utc_evening_is_april_month(self) -> None:
        """31.03 21:00 UTC = 01.04 00:00 МСК → месяц апрель в биллинге."""
        settings = SimpleNamespace(billing_calendar_timezone="Europe/Moscow")
        ts = datetime(2026, 3, 31, 21, 0, 0, tzinfo=timezone.utc)
        start_utc, end_utc = billing_package_month_utc_bounds(settings, ts)
        self.assertEqual(start_utc, datetime(2026, 3, 31, 21, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end_utc, datetime(2026, 4, 30, 21, 0, 0, tzinfo=timezone.utc))

    def test_moscow_mid_april(self) -> None:
        settings = SimpleNamespace(billing_calendar_timezone="Europe/Moscow")
        ts = datetime(2026, 4, 12, 12, 0, 0, tzinfo=timezone.utc)
        start_utc, end_utc = billing_package_month_utc_bounds(settings, ts)
        self.assertEqual(start_utc, datetime(2026, 3, 31, 21, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end_utc, datetime(2026, 4, 30, 21, 0, 0, tzinfo=timezone.utc))

    def test_utc_calendar_same_as_naive_month(self) -> None:
        settings = SimpleNamespace(billing_calendar_timezone="UTC")
        ts = datetime(2026, 4, 12, 12, 0, 0, tzinfo=timezone.utc)
        start_utc, end_utc = billing_package_month_utc_bounds(settings, ts)
        self.assertEqual(start_utc, datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end_utc, datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
