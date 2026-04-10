from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from shared.services.device_telegram_notify import notify_device_attached_replace_message
from shared.services.referral_service import replace_referrer_bonus_telegram_message


class _DummySession:
    def __init__(self) -> None:
        self.flush_calls = 0

    async def flush(self) -> None:
        self.flush_calls += 1


class ReplaceReferrerMessageTests(unittest.IsolatedAsyncioTestCase):
    @patch("shared.services.referral_service.send_telegram_message", new_callable=AsyncMock)
    @patch("shared.services.referral_service.delete_telegram_message", new_callable=AsyncMock)
    async def test_replaces_even_if_old_delete_failed(
        self,
        mock_delete: AsyncMock,
        mock_send: AsyncMock,
    ) -> None:
        mock_delete.return_value = False
        mock_send.return_value = 777
        session = _DummySession()
        user = SimpleNamespace(telegram_id=12345, referral_bonus_message_id=101)

        await replace_referrer_bonus_telegram_message(
            session,
            user,
            "bonus text",
            settings=SimpleNamespace(),
        )

        mock_delete.assert_awaited_once_with(12345, 101, settings=unittest.mock.ANY)
        mock_send.assert_awaited_once()
        self.assertEqual(user.referral_bonus_message_id, 777)
        self.assertEqual(session.flush_calls, 1)

    @patch("shared.services.referral_service.send_telegram_message", new_callable=AsyncMock)
    @patch("shared.services.referral_service.delete_telegram_message", new_callable=AsyncMock)
    async def test_sets_none_when_send_failed(
        self,
        mock_delete: AsyncMock,
        mock_send: AsyncMock,
    ) -> None:
        mock_delete.return_value = True
        mock_send.return_value = None
        session = _DummySession()
        user = SimpleNamespace(telegram_id=12345, referral_bonus_message_id=101)

        await replace_referrer_bonus_telegram_message(
            session,
            user,
            "bonus text",
            settings=SimpleNamespace(),
        )

        self.assertIsNone(user.referral_bonus_message_id)
        self.assertEqual(session.flush_calls, 1)


class ReplaceDeviceMessageTests(unittest.IsolatedAsyncioTestCase):
    @patch("shared.services.device_telegram_notify.send_telegram_message", new_callable=AsyncMock)
    @patch("shared.services.device_telegram_notify.delete_telegram_message", new_callable=AsyncMock)
    async def test_replaces_even_if_old_delete_failed(
        self,
        mock_delete: AsyncMock,
        mock_send: AsyncMock,
    ) -> None:
        mock_delete.return_value = False
        mock_send.return_value = 888
        session = _DummySession()
        user = SimpleNamespace(telegram_id=555, device_notify_message_id=41)

        await notify_device_attached_replace_message(
            session,
            user,
            settings=SimpleNamespace(),
            first_ever=True,
        )

        mock_delete.assert_awaited_once()
        mock_send.assert_awaited_once()
        self.assertEqual(user.device_notify_message_id, 888)
        self.assertEqual(session.flush_calls, 1)

    @patch("shared.services.device_telegram_notify.send_telegram_message", new_callable=AsyncMock)
    @patch("shared.services.device_telegram_notify.delete_telegram_message", new_callable=AsyncMock)
    async def test_sets_none_when_send_failed(
        self,
        mock_delete: AsyncMock,
        mock_send: AsyncMock,
    ) -> None:
        mock_delete.return_value = True
        mock_send.return_value = None
        session = _DummySession()
        user = SimpleNamespace(telegram_id=555, device_notify_message_id=41)

        await notify_device_attached_replace_message(
            session,
            user,
            settings=SimpleNamespace(),
            first_ever=False,
        )

        self.assertIsNone(user.device_notify_message_id)
        self.assertEqual(session.flush_calls, 1)


if __name__ == "__main__":
    unittest.main()
