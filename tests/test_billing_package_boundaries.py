from __future__ import annotations

import unittest

from shared.services.billing_v2.rating_service import (
    is_device_covered_by_package,
    is_gb_step_covered_by_package,
)


class BillingPackageBoundariesTests(unittest.TestCase):
    def test_gb_boundary_last_covered(self) -> None:
        self.assertTrue(is_gb_step_covered_by_package(used_steps_in_month=4, monthly_gb_limit=5))

    def test_gb_boundary_first_paid_after_limit(self) -> None:
        self.assertFalse(is_gb_step_covered_by_package(used_steps_in_month=5, monthly_gb_limit=5))

    def test_gb_no_package_limit(self) -> None:
        self.assertFalse(is_gb_step_covered_by_package(used_steps_in_month=0, monthly_gb_limit=None))

    def test_device_boundary_within_package_limit(self) -> None:
        active = ["hwid-b", "hwid-a", "hwid-c"]
        self.assertTrue(
            is_device_covered_by_package(
                device_hwid="hwid-a",
                active_hwids=active,
                device_limit=2,
            )
        )

    def test_device_boundary_over_package_limit_paid(self) -> None:
        active = ["hwid-b", "hwid-a", "hwid-c"]
        self.assertFalse(
            is_device_covered_by_package(
                device_hwid="hwid-c",
                active_hwids=active,
                device_limit=2,
            )
        )

    def test_device_no_package_limit(self) -> None:
        self.assertFalse(
            is_device_covered_by_package(
                device_hwid="hwid-a",
                active_hwids=["hwid-a"],
                device_limit=None,
            )
        )


if __name__ == "__main__":
    unittest.main()
