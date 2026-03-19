"""Фабрика провайдеров по имени."""

from __future__ import annotations

from shared.config import Settings
from shared.payments.base import BasePaymentProvider
from shared.payments.cryptobot import CryptoBotProvider
from shared.payments.platega import PlategaProvider


def get_payment_provider(name: str, settings: Settings) -> BasePaymentProvider:
    key = (name or "").lower().strip()
    if key == "cryptobot":
        return CryptoBotProvider(settings)
    if key in ("platega", "platega_io"):
        return PlategaProvider(settings)
    raise KeyError(f"Неизвестный провайдер: {name}")
