"""Platega.io — POST /transaction/process, вебхук по заголовкам + JSON."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

import httpx

from shared.config import Settings
from shared.payments.base import BasePaymentProvider, CreatePaymentResult, ParsedWebhookTopup

logger = logging.getLogger(__name__)


class PlategaProvider(BasePaymentProvider):
    name = "platega"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._base = (settings.platega_api_base_url or "https://api.platega.io").rstrip("/")
        self._merchant_id = (settings.platega_merchant_id or "").strip()
        self._secret = (settings.platega_secret_key or "").strip()
        self._stub = settings.platega_stub

    def _headers(self) -> dict[str, str]:
        return {
            "X-MerchantId": self._merchant_id,
            "X-Secret": self._secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def get_exchange_rate(self, asset: str, fiat: str = "RUB") -> Decimal:
        return Decimal("1")

    async def create_topup_invoice(
        self,
        *,
        amount_rub: Decimal,
        internal_transaction_id: int,
        description: str,
    ) -> CreatePaymentResult:
        if self._stub:
            return CreatePaymentResult(
                external_payment_id=str(uuid.uuid4()),
                pay_url="https://platega.io/stub-payment",
                raw={"stub": True},
            )

        payload = f"txn:{internal_transaction_id}"
        body: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "paymentMethod": self._s.platega_payment_method,
            "paymentDetails": {
                "amount": float(amount_rub),
                "currency": "RUB",
            },
            "description": description[:512],
            "payload": payload,
        }
        if self._s.platega_success_url:
            body["return"] = self._s.platega_success_url
        if self._s.platega_fail_url:
            body["failedUrl"] = self._s.platega_fail_url

        async with httpx.AsyncClient(base_url=self._base, timeout=30.0) as client:
            r = await client.post("/transaction/process", headers=self._headers(), json=body)
            txt = r.text
            if r.status_code >= 400:
                logger.error("Platega process HTTP %s: %s", r.status_code, txt[:800])
            r.raise_for_status()
            data = r.json()

        tx_id = str(data.get("transactionId") or data.get("id") or "")
        pay_url = data.get("redirect") or data.get("payUrl") or ""
        if not tx_id or not pay_url:
            raise RuntimeError(f"Неожиданный ответ Platega: {data}")
        return CreatePaymentResult(
            external_payment_id=tx_id,
            pay_url=pay_url,
            raw=data,
        )

    def verify_webhook(self, *, body: bytes, headers: dict[str, str]) -> bool:
        """
        Callback Platega: заголовки X-MerchantId и X-Secret (как в docs.platega.io).
        X-Secret должен совпадать с API-ключом или с PLATEGA_WEBHOOK_SECRET (если задан отдельно).
        """
        if self._s.platega_skip_webhook_auth:
            logger.warning("Platega webhook: PLATEGA_SKIP_WEBHOOK_AUTH — проверка заголовков отключена")
            return True
        mid = (headers.get("x-merchantid") or "").strip()
        sec = (headers.get("x-secret") or "").strip()
        if not mid or not sec:
            logger.warning("Platega webhook: нет X-MerchantId / X-Secret")
            return False
        if mid != self._merchant_id:
            return False
        wh = (self._s.platega_webhook_secret or "").strip()
        if sec == self._secret:
            return True
        if wh and sec == wh:
            return True
        return False

    def parse_topup_webhook(self, body: dict[str, Any]) -> ParsedWebhookTopup | None:
        """
        Wiki-пример: { signature, status, transaction: { id, status, pricing, ... } }
        """
        status_root = (body.get("status") or "").upper()
        tx = body.get("transaction")
        if isinstance(tx, dict):
            st = (tx.get("status") or status_root).upper()
            tid = str(tx.get("id") or tx.get("invoiceId") or "")
            raw_pl = tx.get("payload") or ""
        else:
            st = status_root
            tid = str(body.get("id") or body.get("transactionId") or "")
            raw_pl = body.get("payload") or ""

        if st not in ("CONFIRMED", "PAID", "SUCCESS", "COMPLETED"):
            return None
        if not isinstance(raw_pl, str) or not raw_pl.startswith("txn:"):
            return None
        try:
            internal_id = int(raw_pl.split(":", 1)[1])
        except (ValueError, IndexError):
            return None

        amount_rub = Decimal("0")
        pricing = (tx or {}).get("pricing") if isinstance(tx, dict) else None
        if isinstance(pricing, dict):
            loc = pricing.get("local") or {}
            if isinstance(loc, dict) and loc.get("amount") is not None:
                amount_rub = Decimal(str(loc["amount"]))
        if amount_rub <= 0 and isinstance(tx, dict):
            pd = tx.get("paymentDetails") or {}
            if isinstance(pd, dict) and pd.get("amount") is not None:
                amount_rub = Decimal(str(pd["amount"]))

        return ParsedWebhookTopup(
            internal_transaction_id=internal_id,
            external_payment_id=tid,
            amount_rub=amount_rub,
            paid=True,
        )
