from __future__ import annotations

import hashlib
import hmac
import unittest
from datetime import datetime, timezone

from shared.services.billing_v2.webhook_ingress_service import verify_remnawave_signature


class _SettingsStub:
    remnawave_webhook_secret = "test-secret"
    remnawave_webhook_signature_ttl_sec = 300


class _EmptySecretSettingsStub:
    remnawave_webhook_secret = ""
    remnawave_webhook_signature_ttl_sec = 300


class RemnawaveWebhookSignatureTests(unittest.TestCase):
    def test_signature_valid(self) -> None:
        body = b'{"event_id":"abc"}'
        ts = str(int(datetime.now(timezone.utc).timestamp()))
        digest = hmac.new(
            _SettingsStub.remnawave_webhook_secret.encode("utf-8"),
            f"{ts}.".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        sig = f"sha256={digest}"
        self.assertTrue(
            verify_remnawave_signature(
                body=body,
                ts_header=ts,
                signature_header=sig,
                settings=_SettingsStub(),  # type: ignore[arg-type]
            )
        )

    def test_signature_invalid(self) -> None:
        body = b"{}"
        ts = str(int(datetime.now(timezone.utc).timestamp()))
        self.assertFalse(
            verify_remnawave_signature(
                body=body,
                ts_header=ts,
                signature_header="sha256=bad",
                settings=_SettingsStub(),  # type: ignore[arg-type]
            )
        )

    def test_signature_expired_timestamp(self) -> None:
        body = b'{"event_id":"abc"}'
        ts = str(int(datetime.now(timezone.utc).timestamp()) - 1000)
        digest = hmac.new(
            _SettingsStub.remnawave_webhook_secret.encode("utf-8"),
            f"{ts}.".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        sig = f"sha256={digest}"
        self.assertFalse(
            verify_remnawave_signature(
                body=body,
                ts_header=ts,
                signature_header=sig,
                settings=_SettingsStub(),  # type: ignore[arg-type]
            )
        )

    def test_signature_invalid_timestamp_header(self) -> None:
        self.assertFalse(
            verify_remnawave_signature(
                body=b"{}",
                ts_header="bad-ts",
                signature_header="sha256=abc",
                settings=_SettingsStub(),  # type: ignore[arg-type]
            )
        )

    def test_signature_empty_secret(self) -> None:
        self.assertFalse(
            verify_remnawave_signature(
                body=b"{}",
                ts_header=str(int(datetime.now(timezone.utc).timestamp())),
                signature_header="sha256=abc",
                settings=_EmptySecretSettingsStub(),  # type: ignore[arg-type]
            )
        )


if __name__ == "__main__":
    unittest.main()
