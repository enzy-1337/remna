from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from shared.models.remnawave_webhook_event import RemnawaveWebhookEvent
from shared.models.user import User
from shared.services.billing_v2.webhook_ingress_service import (
    process_remnawave_event,
    store_raw_webhook_event,
)


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _SessionStore:
    def __init__(self, existing: RemnawaveWebhookEvent | None) -> None:
        self._existing = existing
        self.added: list[RemnawaveWebhookEvent] = []

    async def execute(self, _stmt: object) -> _ScalarResult:
        return _ScalarResult(self._existing)

    def add(self, row: RemnawaveWebhookEvent) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


class _SessionUserQuery:
    def __init__(self, user: User | None) -> None:
        self._user = user

    async def execute(self, _stmt: object) -> _ScalarResult:
        return _ScalarResult(self._user)

    async def flush(self) -> None:
        return None


def _test_user(*, tg_id: int = 1001) -> User:
    return User(
        id=1,
        telegram_id=tg_id,
        balance=Decimal("10.00"),
        referral_code="r" + "x" * 30,
    )


class StoreRawWebhookEventTests(IsolatedAsyncioTestCase):
    async def test_inserts_new_row(self) -> None:
        s = _SessionStore(None)
        row, dup = await store_raw_webhook_event(
            s,
            event_id="evt-1",
            event_type="traffic.gb_step",
            payload={"telegram_id": 1},
            headers={"x-signature": "sha256=ab"},
            signature_valid=True,
        )
        self.assertFalse(dup)
        self.assertEqual(len(s.added), 1)
        self.assertEqual(s.added[0].event_id, "evt-1")
        self.assertEqual(s.added[0].status, "received")

    async def test_duplicate_marks_status(self) -> None:
        existing = RemnawaveWebhookEvent(
            event_id="evt-1",
            event_type="traffic.gb_step",
            payload={"telegram_id": 1},
            headers={},
            signature_valid=True,
            status="processed",
        )
        s = _SessionStore(existing)
        row, dup = await store_raw_webhook_event(
            s,
            event_id="evt-1",
            event_type="traffic.gb_step",
            payload={"telegram_id": 1},
            headers={},
            signature_valid=True,
        )
        self.assertTrue(dup)
        self.assertIs(row, existing)
        self.assertEqual(existing.status, "duplicate")


class ProcessRemnawaveEventTests(IsolatedAsyncioTestCase):
    async def test_missing_telegram_id_ignored(self) -> None:
        row = RemnawaveWebhookEvent(
            event_id="e1",
            event_type="traffic.gb_step",
            payload={},
            headers={},
            signature_valid=True,
            status="received",
        )
        s = _SessionUserQuery(_test_user())
        settings = MagicMock()
        await process_remnawave_event(s, row=row, settings=settings)
        self.assertEqual(row.status, "ignored")

    async def test_unknown_user_ignored(self) -> None:
        row = RemnawaveWebhookEvent(
            event_id="e1",
            event_type="traffic.gb_step",
            payload={"telegram_id": 999},
            headers={},
            signature_valid=True,
            status="received",
        )
        s = _SessionUserQuery(None)
        settings = MagicMock()
        await process_remnawave_event(s, row=row, settings=settings)
        self.assertEqual(row.status, "ignored")

    @patch(
        "shared.services.billing_v2.webhook_ingress_service.charge_gb_step",
        new_callable=AsyncMock,
    )
    async def test_traffic_processed_or_rejected(self, mock_charge: AsyncMock) -> None:
        mock_charge.return_value = True
        row = RemnawaveWebhookEvent(
            event_id="e1",
            event_type="traffic.gb_step",
            payload={"telegram_id": 1001},
            headers={},
            signature_valid=True,
            status="received",
        )
        s = _SessionUserQuery(_test_user(tg_id=1001))
        settings = MagicMock()
        await process_remnawave_event(s, row=row, settings=settings)
        self.assertEqual(row.status, "processed")
        mock_charge.return_value = False
        row2 = RemnawaveWebhookEvent(
            event_id="e2",
            event_type="traffic.gb_step",
            payload={"telegram_id": 1001},
            headers={},
            signature_valid=True,
            status="received",
        )
        await process_remnawave_event(s, row=row2, settings=settings)
        self.assertEqual(row2.status, "rejected")

    async def test_unknown_event_type_ignored(self) -> None:
        row = RemnawaveWebhookEvent(
            event_id="e1",
            event_type="unknown.type",
            payload={"telegram_id": 1001},
            headers={},
            signature_valid=True,
            status="received",
        )
        s = _SessionUserQuery(_test_user(tg_id=1001))
        settings = MagicMock()
        await process_remnawave_event(s, row=row, settings=settings)
        self.assertEqual(row.status, "ignored")

    async def test_subscription_status_processed(self) -> None:
        row = RemnawaveWebhookEvent(
            event_id="e1",
            event_type="subscription.status",
            payload={"telegram_id": 1001},
            headers={},
            signature_valid=True,
            status="received",
        )
        s = _SessionUserQuery(_test_user(tg_id=1001))
        settings = MagicMock()
        await process_remnawave_event(s, row=row, settings=settings)
        self.assertEqual(row.status, "processed")

    @patch(
        "shared.services.billing_v2.webhook_ingress_service.add_device_history_event",
        new_callable=AsyncMock,
    )
    @patch(
        "shared.services.billing_v2.webhook_ingress_service.charge_daily_device_once",
        new_callable=AsyncMock,
    )
    async def test_device_attached_processed(
        self,
        mock_daily: AsyncMock,
        mock_hist: AsyncMock,
    ) -> None:
        row = RemnawaveWebhookEvent(
            event_id="e1",
            event_type="device.attached",
            payload={"telegram_id": 1001, "device_hwid": "hw-1"},
            headers={},
            signature_valid=True,
            status="received",
        )
        s = _SessionUserQuery(_test_user(tg_id=1001))
        settings = MagicMock()
        await process_remnawave_event(s, row=row, settings=settings)
        self.assertEqual(row.status, "processed")
        mock_hist.assert_awaited_once()
        mock_daily.assert_awaited_once()

    @patch(
        "shared.services.billing_v2.webhook_ingress_service.add_device_history_event",
        new_callable=AsyncMock,
    )
    @patch(
        "shared.services.billing_v2.webhook_ingress_service.charge_daily_device_once",
        new_callable=AsyncMock,
    )
    async def test_device_detached_no_daily_charge(
        self,
        mock_daily: AsyncMock,
        mock_hist: AsyncMock,
    ) -> None:
        row = RemnawaveWebhookEvent(
            event_id="e1",
            event_type="device.detached",
            payload={"telegram_id": 1001, "device_hwid": "hw-1"},
            headers={},
            signature_valid=True,
            status="received",
        )
        s = _SessionUserQuery(_test_user(tg_id=1001))
        settings = MagicMock()
        await process_remnawave_event(s, row=row, settings=settings)
        self.assertEqual(row.status, "processed")
        mock_hist.assert_awaited_once()
        mock_daily.assert_not_called()


if __name__ == "__main__":
    unittest.main()
