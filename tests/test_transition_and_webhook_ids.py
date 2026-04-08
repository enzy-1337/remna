from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from shared.services.billing_v2.transition_service import is_transition_due
from shared.services.billing_v2.webhook_ingress_service import (
    event_id_from_payload,
    telegram_id_from_payload,
)


class TransitionBoundaryTests(unittest.TestCase):
    def test_transition_due_at_exact_expiry(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertTrue(is_transition_due(expires_at=now, now=now))

    def test_transition_not_due_before_expiry(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertFalse(is_transition_due(expires_at=now + timedelta(seconds=1), now=now))

    def test_transition_due_when_expired(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertTrue(is_transition_due(expires_at=now - timedelta(seconds=1), now=now))


class WebhookEventIdTests(unittest.TestCase):
    def test_event_id_from_event_id_field(self) -> None:
        payload = {"event_id": "evt-123", "id": "legacy-1"}
        self.assertEqual(event_id_from_payload(payload, fallback="fb"), "evt-123")

    def test_event_id_from_id_field(self) -> None:
        payload = {"id": "legacy-1"}
        self.assertEqual(event_id_from_payload(payload, fallback="fb"), "legacy-1")

    def test_event_id_fallback(self) -> None:
        payload: dict = {}
        self.assertEqual(event_id_from_payload(payload, fallback="fb"), "fb")

    def test_event_id_trim_spaces(self) -> None:
        payload = {"event_id": "  evt-1  "}
        self.assertEqual(event_id_from_payload(payload, fallback="fb"), "evt-1")

    def test_event_id_truncated_to_db_limit(self) -> None:
        payload = {"event_id": "x" * 256}
        self.assertEqual(len(event_id_from_payload(payload, fallback="fb")), 128)


class WebhookTelegramIdTests(unittest.TestCase):
    def test_telegram_id_int(self) -> None:
        self.assertEqual(telegram_id_from_payload({"telegram_id": 12345}), 12345)

    def test_telegram_id_digit_string(self) -> None:
        self.assertEqual(telegram_id_from_payload({"telegram_id": "12345"}), 12345)

    def test_telegram_id_digit_string_with_spaces(self) -> None:
        self.assertEqual(telegram_id_from_payload({"telegram_id": " 12345 "}), 12345)

    def test_telegram_id_invalid_string(self) -> None:
        self.assertIsNone(telegram_id_from_payload({"telegram_id": "abc"}))

    def test_telegram_id_missing(self) -> None:
        self.assertIsNone(telegram_id_from_payload({}))


if __name__ == "__main__":
    unittest.main()
