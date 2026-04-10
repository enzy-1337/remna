from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from shared.services.billing_calculator import (
    estimate_pay_per_use_30d_rub,
    plan_fields_for_ppu_estimate,
    transition_credit_for_remaining_legacy_rub,
)


def _settings(**kwargs: object) -> SimpleNamespace:
    base = {
        "billing_device_daily_rub": Decimal("2.5"),
        "billing_gb_step_rub": Decimal("5"),
        "billing_mobile_gb_extra_rub": Decimal("2.5"),
        "billing_transition_base_month_rub": Decimal("130"),
        "billing_transition_fee_percent": Decimal("10"),
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class PayPerUseEstimateTests(unittest.TestCase):
    def test_one_device_15gb_matches_spec_example(self) -> None:
        s = _settings()
        r = estimate_pay_per_use_30d_rub(s, device_count=1, gb_per_month=15, mobile_gb_per_month=0)
        self.assertEqual(r["device_rub"], Decimal("75"))
        self.assertEqual(r["traffic_rub"], Decimal("75"))
        self.assertEqual(r["total_rub"], Decimal("150"))

    def test_mobile_addon(self) -> None:
        s = _settings()
        r = estimate_pay_per_use_30d_rub(s, device_count=1, gb_per_month=1, mobile_gb_per_month=2)
        self.assertEqual(r["mobile_extra_rub"], Decimal("5"))


class TransitionCreditTests(unittest.TestCase):
    def test_30_days_minus_10_percent(self) -> None:
        s = _settings()
        self.assertEqual(transition_credit_for_remaining_legacy_rub(s, remaining_days=30), Decimal("117.00"))

    def test_zero_days(self) -> None:
        s = _settings()
        self.assertEqual(transition_credit_for_remaining_legacy_rub(s, remaining_days=0), Decimal("0"))


class PlanFieldsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        pl = MagicMock()
        pl.device_limit = None
        pl.is_package_monthly = False
        pl.monthly_gb_limit = None
        pl.traffic_limit_gb = None
        self.assertEqual(plan_fields_for_ppu_estimate(pl), (1, 0))

    def test_package_monthly(self) -> None:
        pl = MagicMock()
        pl.device_limit = 2
        pl.is_package_monthly = True
        pl.monthly_gb_limit = 10
        pl.traffic_limit_gb = 99
        self.assertEqual(plan_fields_for_ppu_estimate(pl), (2, 10))


if __name__ == "__main__":
    unittest.main()
