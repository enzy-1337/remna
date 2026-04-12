"""Маршрут POST /webhooks/telegram (режим выключен по умолчанию)."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from api.main import app


class TelegramWebhookRouteTests(unittest.TestCase):
    def test_disabled_returns_404(self) -> None:
        with TestClient(app) as client:
            r = client.post(
                "/webhooks/telegram",
                content=b"{}",
                headers={"X-Telegram-Bot-Api-Secret-Token": "any"},
            )
            self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
