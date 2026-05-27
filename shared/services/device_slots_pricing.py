"""Цены и лимиты слотов устройств (докупка)."""

from __future__ import annotations

from decimal import Decimal

from shared.config import Settings
from shared.models.user import User
from shared.services.subscription_service import MAX_DEVICES, MIN_DEVICES


def is_admin_unlimited_devices(user: User, settings: Settings) -> bool:
    try:
        return int(user.telegram_id) in set(settings.admin_telegram_ids)
    except (TypeError, ValueError):
        return False


def device_slot_cap(user: User, settings: Settings, *, is_bot_admin: bool = False) -> int | None:
    """None = без лимита (админ)."""
    if is_bot_admin or is_admin_unlimited_devices(user, settings):
        return None
    return MAX_DEVICES


def max_slots_user_can_have(user: User, settings: Settings, *, is_bot_admin: bool = False) -> int | None:
    return device_slot_cap(user, settings, is_bot_admin=is_bot_admin)


def slots_available_to_buy(current_slots: int, cap: int | None) -> int:
    if cap is None:
        return 99
    return max(0, int(cap) - int(current_slots))


def bulk_device_slots_discount_percent(quantity: int) -> Decimal:
    """Скидка на одну покупку N слотов (%)."""
    q = int(quantity)
    if q >= 10:
        return Decimal("15")
    if q >= 5:
        return Decimal("10")
    if q >= 3:
        return Decimal("5")
    return Decimal("0")


def price_for_extra_device_slots(settings: Settings, quantity: int) -> tuple[Decimal, Decimal, Decimal]:
    """
    Возвращает (итого к списанию, цена за слот до скидки, процент скидки).
  """
    q = max(0, int(quantity))
    if q <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0")
    unit = Decimal(str(settings.extra_device_price_rub)).quantize(Decimal("0.01"))
    subtotal = (unit * q).quantize(Decimal("0.01"))
    pct = bulk_device_slots_discount_percent(q)
    total = (subtotal * (Decimal("100") - pct) / Decimal("100")).quantize(Decimal("0.01"))
    return total, unit, pct
