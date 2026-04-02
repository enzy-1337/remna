"""Crypto Pay (@CryptoBot) — createInvoice, курс, проверка вебхука."""

from __future__ import annotations

import hashlib
import hmac
import logging
from decimal import Decimal
from typing import Any

import httpx

from shared.config import Settings
from shared.payments.base import BasePaymentProvider, CreatePaymentResult, ParsedWebhookTopup

logger = logging.getLogger(__name__)

CRYPTO_PAY_API = "https://pay.crypt.bot"


def _verify_cryptobot_signature(api_token: str, body: bytes, signature_hex: str) -> bool:
    """HMAC-SHA256(body, SHA256(token)) — как в документации Crypto Pay API."""
    secret = hashlib.sha256(api_token.encode("utf-8")).digest()
    mac = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, (signature_hex or "").strip().lower())


class CryptoBotProvider(BasePaymentProvider):
    name = "cryptobot"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._token = (settings.cryptobot_token or "").strip()
        self._stub = settings.cryptobot_stub

    def _headers(self) -> dict[str, str]:
        return {"Crypto-Pay-API-Token": self._token}

    async def get_exchange_rate(self, asset: str, fiat: str = "RUB") -> Decimal:
        if self._stub:
            return Decimal("100")
        asset_u = asset.upper()
        fiat_u = fiat.upper()
        async with httpx.AsyncClient(base_url=CRYPTO_PAY_API, timeout=15.0) as client:
            r = await client.get("/api/getExchangeRates", headers=self._headers())
            r.raise_for_status()
            data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"CryptoBot getExchangeRates: {data}")
        rates = data.get("result") or []
        # result: list of {source, target, rate, is_valid, ...}
        for row in rates:
            if (row.get("source") or "").upper() == asset_u and (row.get("target") or "").upper() == fiat_u:
                return Decimal(str(row["rate"]))
        raise RuntimeError(f"Нет курса {asset}/{fiat} в ответе CryptoBot")

    async def create_topup_invoice(
        self,
        *,
        amount_rub: Decimal,
        internal_transaction_id: int,
        description: str,
    ) -> CreatePaymentResult:
        if self._stub:
            return CreatePaymentResult(
                external_payment_id="stub_invoice",
                pay_url="https://t.me/CryptoBot?start=stub",
                raw={"stub": True},
            )

        # Счёт в рублях; оплата криптой — через accepted_assets (Crypto Pay API).
        payload = f"txn:{internal_transaction_id}"
        body = {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": str(amount_rub),
            "accepted_assets": "USDT,USDC,TON,BTC,ETH,LTC,TRX,BNB",
            "description": description[:1024],
            "payload": payload[:1024],
            "allow_comments": False,
            "expires_in": 3600,
        }
        async with httpx.AsyncClient(base_url=CRYPTO_PAY_API, timeout=30.0) as client:
            r = await client.post("/api/createInvoice", headers=self._headers(), json=body)
            txt = r.text
            if r.status_code >= 400:
                logger.error("createInvoice HTTP %s: %s", r.status_code, txt[:500])
            r.raise_for_status()
            data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"createInvoice: {data}")
        inv = data.get("result") or {}
        inv_id = str(inv.get("invoice_id", ""))
        pay_url = inv.get("bot_invoice_url") or inv.get("pay_url") or ""
        if not inv_id or not pay_url:
            raise RuntimeError(f"Неожиданный ответ createInvoice: {inv}")
        return CreatePaymentResult(
            external_payment_id=inv_id,
            pay_url=pay_url,
            raw=inv,
        )

    def verify_webhook(self, *, body: bytes, headers: dict[str, str]) -> bool:
        sig = (
            headers.get("crypto-pay-api-signature")
            or headers.get("x-crypto-pay-api-signature")
            or ""
        )
        if not sig:
            logger.warning("CryptoBot webhook: нет подписи в заголовках")
            return False
        return _verify_cryptobot_signature(self._token, body, sig)

    def parse_topup_webhook(self, body: dict[str, Any]) -> ParsedWebhookTopup | None:
        """
        Crypto Pay webhook: { "update_type": "invoice_paid", "payload": <Invoice>, ... }
        """
        ut = (body.get("update_type") or "").lower()
        if ut != "invoice_paid":
            return None
        inv = body.get("payload")
        if not isinstance(inv, dict):
            return None
        if (inv.get("status") or "").lower() != "paid":
            return None
        raw_pl = inv.get("payload") or ""
        if not isinstance(raw_pl, str) or not raw_pl.startswith("txn:"):
            return None
        try:
            tid = int(raw_pl.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        inv_id = str(inv.get("invoice_id", ""))
        # fiat-счёт: paid_amount в валюте счёта (RUB)
        paid_amt = inv.get("paid_amount") or inv.get("amount")
        amount_rub = Decimal(str(paid_amt)) if paid_amt is not None else Decimal("0")
        return ParsedWebhookTopup(
            internal_transaction_id=tid,
            external_payment_id=inv_id,
            amount_rub=amount_rub,
            paid=True,
        )

    async def is_invoice_paid(self, invoice_id: str) -> tuple[bool, Decimal | None, dict[str, Any]]:
        """
        Ручная проверка: получить invoice по id и понять, оплачен ли он.
        Используем Crypto Pay API (pay.crypt.bot) метод getInvoices.
        """
        if self._stub:
            return True, Decimal("100"), {"stub": True, "invoice_id": invoice_id, "status": "paid"}
        iid = (invoice_id or "").strip()
        if not iid:
            return False, None, {"error": "empty invoice_id"}
        async with httpx.AsyncClient(base_url=CRYPTO_PAY_API, timeout=15.0) as client:
            r = await client.post(
                "/api/getInvoices",
                headers=self._headers(),
                json={"invoice_ids": iid},
            )
            r.raise_for_status()
            data = r.json()
        if not data.get("ok"):
            return False, None, {"error": "api_not_ok", "raw": data}
        res = data.get("result") or {}
        items = res.get("items") or res.get("invoices") or res.get("result") or []
        if isinstance(items, dict):
            items = [items]
        inv: dict[str, Any] | None = None
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and str(it.get("invoice_id") or "") == iid:
                    inv = it
                    break
            if inv is None and len(items) == 1 and isinstance(items[0], dict):
                inv = items[0]
        if not isinstance(inv, dict):
            return False, None, {"error": "invoice_not_found", "raw": data}
        st = str(inv.get("status") or "").lower()
        paid = st in ("paid", "completed")
        paid_amt = inv.get("paid_amount") or inv.get("amount")
        amt = Decimal(str(paid_amt)) if paid_amt is not None else None
        return paid, amt, inv
