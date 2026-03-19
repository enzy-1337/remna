"""Базовый класс платёжного провайдера (Strategy)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(slots=True)
class CreatePaymentResult:
    """Результат создания платежа у провайдера."""

    external_payment_id: str
    pay_url: str
    raw: dict[str, Any]


@dataclass(slots=True)
class ParsedWebhookTopup:
    """Нормализованные данные вебхука пополнения."""

    internal_transaction_id: int
    external_payment_id: str
    amount_rub: Decimal
    paid: bool


class BasePaymentProvider(ABC):
    """Контракт провайдера (CryptoBot, Platega, …)."""

    name: str

    @abstractmethod
    async def create_topup_invoice(
        self,
        *,
        amount_rub: Decimal,
        internal_transaction_id: int,
        description: str,
    ) -> CreatePaymentResult:
        """Создать счёт/ссылку на оплату на сумму в ₽ (конвертация — внутри провайдера при необходимости)."""

    @abstractmethod
    def verify_webhook(self, *, body: bytes, headers: dict[str, str]) -> bool:
        """Проверить подлинность входящего HTTP-уведомления."""

    @abstractmethod
    def parse_topup_webhook(self, body: dict[str, Any]) -> ParsedWebhookTopup | None:
        """Разобрать JSON вебхука; None если это не успешное пополнение."""

    @abstractmethod
    async def get_exchange_rate(self, asset: str, fiat: str = "RUB") -> Decimal:
        """
        Курс: сколько единиц fiat за 1 единицу asset (например USDT → RUB).
        Для провайдеров без крипты можно вернуть Decimal('1').
        """
