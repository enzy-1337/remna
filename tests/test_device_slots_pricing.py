from decimal import Decimal

from shared.services.device_slots_pricing import (
    bulk_device_slots_discount_percent,
    price_for_extra_device_slots,
    slots_available_to_buy,
)


def test_slots_available_to_buy():
    assert slots_available_to_buy(7, 15) == 8
    assert slots_available_to_buy(15, 15) == 0


def test_bulk_discount_13_slots():
    assert bulk_device_slots_discount_percent(13) == Decimal("15")


def test_price_13_slots_at_50_rub():
    settings = type("S", (), {"extra_device_price_rub": Decimal("50")})()
    total, unit, disc = price_for_extra_device_slots(settings, 13)
    assert unit == Decimal("50")
    assert disc == Decimal("15")
    assert total == Decimal("552.50")
