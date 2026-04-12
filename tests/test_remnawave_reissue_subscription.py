"""Перевыпуск ссылки подписки через Remnawave API (stub)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from shared.integrations.remnawave import RemnaWaveClient


def _rw_settings(**kw: object) -> SimpleNamespace:
    base = dict(
        remnawave_stub=True,
        remnawave_api_url="http://127.0.0.1:1",
        remnawave_api_token="t",
        remnawave_api_path_prefix="",
        remnawave_cookie="",
        remnawave_request_timeout=5.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class ResetSubscriptionStubTests(IsolatedAsyncioTestCase):
    async def test_stub_returns_subscription_url(self) -> None:
        client = RemnaWaveClient(_rw_settings())
        u = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        a = await client.reset_user_subscription_credentials(u)
        b = await client.reset_user_subscription_credentials(u)
        self.assertIn("subscriptionUrl", a)
        self.assertIn("subscriptionUrl", b)
        self.assertNotEqual(a["subscriptionUrl"], b["subscriptionUrl"])


if __name__ == "__main__":
    unittest.main()
