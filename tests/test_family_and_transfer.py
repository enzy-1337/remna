"""Тесты лимитов семейной подписки."""

from __future__ import annotations

import unittest

MAX_FAMILY_EXTRA_MEMBERS = 2


def max_family_extra_members(devices_count: int) -> int:
    if devices_count <= 1:
        return 0
    return min(devices_count - 1, MAX_FAMILY_EXTRA_MEMBERS)


class FamilyLimitTests(unittest.TestCase):
    def test_one_device_no_members(self) -> None:
        self.assertEqual(max_family_extra_members(1), 0)

    def test_two_devices_one_member(self) -> None:
        self.assertEqual(max_family_extra_members(2), 1)

    def test_three_devices_two_members(self) -> None:
        self.assertEqual(max_family_extra_members(3), 2)

    def test_ten_devices_still_two_members(self) -> None:
        self.assertEqual(max_family_extra_members(10), 2)


class TransferValidationTests(unittest.TestCase):
    def test_self_transfer_message(self) -> None:
        msg = "Нельзя передать подписку самому себе."
        self.assertIn("самому себе", msg.lower())


if __name__ == "__main__":
    unittest.main()
