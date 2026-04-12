"""Парсинг вспомогательных полей устройства из payload вебхука."""

from __future__ import annotations

import unittest

from shared.services.billing_v2.webhook_ingress_service import device_identity_meta_from_payload


class DeviceIdentityMetaTests(unittest.TestCase):
    def test_nested_hwid_user_device(self) -> None:
        payload = {
            "data": {
                "hwidUserDevice": {
                    "hwid": "abc",
                    "name": "Pixel",
                    "model": "G9",
                    "uuid": "550e8400-e29b-41d4-a716-446655440000",
                }
            }
        }
        m = device_identity_meta_from_payload(payload)
        self.assertEqual(m.get("device_name"), "Pixel")
        self.assertEqual(m.get("device_model"), "G9")
        self.assertEqual(m.get("rw_device_uuid"), "550e8400-e29b-41d4-a716-446655440000")

    def test_empty_payload(self) -> None:
        self.assertEqual(device_identity_meta_from_payload({}), {})


if __name__ == "__main__":
    unittest.main()
