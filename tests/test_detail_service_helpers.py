from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from shared.services.billing_v2.detail_service import month_bounds, summarize_month_total


class _SummaryRow:
    def __init__(self, amount: str) -> None:
        self.total_amount_rub = Decimal(amount)


class DetailServiceHelperTests(unittest.TestCase):
    def test_month_bounds_regular_month(self) -> None:
        start, end = month_bounds(date(2026, 4, 8))
        self.assertEqual(start, date(2026, 4, 1))
        self.assertEqual(end, date(2026, 5, 1))

    def test_month_bounds_december_rollover(self) -> None:
        start, end = month_bounds(date(2026, 12, 15))
        self.assertEqual(start, date(2026, 12, 1))
        self.assertEqual(end, date(2027, 1, 1))

    def test_summarize_month_total(self) -> None:
        rows = [_SummaryRow("2.50"), _SummaryRow("5.00"), _SummaryRow("0.10")]
        self.assertEqual(summarize_month_total(rows), Decimal("7.60"))

    def test_summarize_month_total_empty(self) -> None:
        self.assertEqual(summarize_month_total([]), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
