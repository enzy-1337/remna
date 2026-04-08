from __future__ import annotations

import unittest
from datetime import timedelta

from shared.services.billing_v2.negative_balance_notify_loop import classify_eta_windows


class NegativeBalanceWindowTests(unittest.TestCase):
    def test_24h_window_inclusive_boundaries(self) -> None:
        self.assertEqual(
            classify_eta_windows(timedelta(hours=22)),
            (True, False, False, True),
        )
        self.assertEqual(
            classify_eta_windows(timedelta(hours=26)),
            (True, False, False, True),
        )

    def test_1h_window_inclusive_boundaries(self) -> None:
        self.assertEqual(
            classify_eta_windows(timedelta(minutes=40)),
            (False, True, False, False),
        )
        self.assertEqual(
            classify_eta_windows(timedelta(hours=1, minutes=20)),
            (False, True, False, False),
        )

    def test_resets_when_eta_far_in_future(self) -> None:
        self.assertEqual(
            classify_eta_windows(timedelta(hours=30)),
            (False, False, True, True),
        )

    def test_no_resets_when_eta_already_soon(self) -> None:
        self.assertEqual(
            classify_eta_windows(timedelta(minutes=10)),
            (False, False, False, False),
        )


if __name__ == "__main__":
    unittest.main()
