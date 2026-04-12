from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from shared.services.billing_calculator import (
    compare_plan_vs_payg_estimate,
    estimate_pay_per_use_30d_rub,
    estimate_payg_scenario_rub,
    plan_charge_for_compare_period_rub,
    plan_fields_for_ppu_estimate,
    transition_credit_for_remaining_legacy_rub,
)


def _settings(**kwargs: object) -> SimpleNamespace:
    base = {
        "billing_device_daily_rub": Decimal("2.5"),
        "billing_gb_step_rub": Decimal("5"),
        "billing_mobile_gb_extra_rub": Decimal("2.5"),
        "billing_optimized_route_gb_extra_rub": Decimal("2.5"),
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

    def test_mobile_addon_disabled_in_estimate(self) -> None:
        s = _settings()
        r = estimate_pay_per_use_30d_rub(s, device_count=1, gb_per_month=1, mobile_gb_per_month=2)
        self.assertEqual(r["mobile_extra_rub"], Decimal("0"))


class PaygScenarioTests(unittest.TestCase):
    def test_device_days_and_optimized(self) -> None:
        s = _settings()
        r = estimate_payg_scenario_rub(
            s, device_days=30, gb_steps=15, mobile_gb_steps=0, optimized_route=True
        )
        self.assertEqual(r["device_rub"], Decimal("75"))
        self.assertEqual(r["traffic_rub"], Decimal("75"))
        self.assertEqual(r["optimized_extra_rub"], Decimal("37.5"))
        self.assertEqual(r["total_rub"], Decimal("187.50"))

    def test_seven_days_one_device(self) -> None:
        s = _settings()
        r = estimate_payg_scenario_rub(s, device_days=7, gb_steps=5, mobile_gb_steps=0, optimized_route=False)
        self.assertEqual(r["device_rub"], Decimal("17.50"))
        self.assertEqual(r["traffic_rub"], Decimal("25"))
        self.assertEqual(r["total_rub"], Decimal("42.50"))


class PlanCompareTests(unittest.TestCase):
    def test_prorate_and_delta(self) -> None:
        pl = MagicMock()
        pl.duration_days = 30
        pl.price_rub = Decimal("300")
        self.assertEqual(plan_charge_for_compare_period_rub(pl, 15), Decimal("150.00"))
        est = {"total_rub": Decimal("100")}
        c = compare_plan_vs_payg_estimate(pl, period_days=15, payg_estimate=est)
        self.assertEqual(c["plan_rub"], Decimal("150.00"))
        self.assertEqual(c["payg_rub"], Decimal("100"))
        self.assertEqual(c["delta_rub"], Decimal("50.00"))


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
