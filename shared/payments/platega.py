"""Platega.io — POST /transaction/process, вебхук по заголовкам + JSON."""

from __future__ import annotations

import logging
import re
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
        self._base = (settings.platega_api_base_url or "https://app.platega.io").rstrip("/")
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
        def _walk_strings(v: Any):
            if isinstance(v, str):
                yield v
                return
            if isinstance(v, dict):
                for vv in v.values():
                    yield from _walk_strings(vv)
                return
            if isinstance(v, list):
                for vv in v:
                    yield from _walk_strings(vv)
                return

        status_root = (body.get("status") or "").upper()
        tx = body.get("transaction")
        tx_dict = tx if isinstance(tx, dict) else None

        # --- status
        st = status_root
        if tx_dict is not None:
            st = (tx_dict.get("status") or status_root).upper()
        allowed_status = {"CONFIRMED", "PAID", "SUCCESS", "COMPLETED"}
        if st and st not in allowed_status:
            return None

        # --- internal transaction id (from payload: "txn:<int>")
        payload_candidates: list[str] = []
        for v in (
            (tx_dict.get("payload") if tx_dict is not None else None),
            body.get("payload"),
        ):
            if isinstance(v, str) and "txn:" in v.lower():
                payload_candidates.append(v)

        internal_id: int | None = None
        for s in payload_candidates:
            m = re.search(r"txn:(\d+)", s, flags=re.IGNORECASE)
            if m:
                internal_id = int(m.group(1))
                break
        if internal_id is None:
            for s in _walk_strings(body):
                if "txn:" not in s.lower():
                    continue
                m = re.search(r"txn:(\d+)", s, flags=re.IGNORECASE)
                if m:
                    internal_id = int(m.group(1))
                    break
        if internal_id is None:
            return None

        # --- external payment id (for logging/idempotency)
        ext_candidates: list[str] = []
        if tx_dict is not None:
            for k in ("transactionId", "id", "invoiceId", "paymentId"):
                v = tx_dict.get(k)
                if v is not None and str(v).strip():
                    ext_candidates.append(str(v).strip())
        for k in ("transactionId", "id", "invoiceId", "paymentId"):
            v = body.get(k)
            if v is not None and str(v).strip():
                ext_candidates.append(str(v).strip())
        external_id = ext_candidates[0] if ext_candidates else ""

        # --- amount (optional; зачисление всё равно идемпотентно по txn.amount)
        amount_rub = Decimal("0")
        if tx_dict is not None:
            pricing = tx_dict.get("pricing")
            if isinstance(pricing, dict):
                loc = pricing.get("local") or {}
                if isinstance(loc, dict) and loc.get("amount") is not None:
                    amount_rub = Decimal(str(loc["amount"]))
            if amount_rub <= 0:
                pd = tx_dict.get("paymentDetails") or {}
                if isinstance(pd, dict) and pd.get("amount") is not None:
                    amount_rub = Decimal(str(pd["amount"]))

        if amount_rub <= 0 and body.get("amount") is not None:
            try:
                amount_rub = Decimal(str(body["amount"]))
            except Exception:
                pass

        return ParsedWebhookTopup(
            internal_transaction_id=internal_id,
            external_payment_id=external_id,
            amount_rub=amount_rub,
            paid=True,
        )
