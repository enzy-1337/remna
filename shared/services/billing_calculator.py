"""Калькуляторы для админки: эквивалент pay-per-use за 30 дн. и кредит при переходе с legacy."""

from __future__ import annotations

from decimal import Decimal

from shared.config import Settings
from shared.models.plan import Plan


def estimate_pay_per_use_30d_rub(
    settings: Settings,
    *,
    device_count: int,
    gb_per_month: int,
    mobile_gb_per_month: int = 0,
) -> dict[str, Decimal]:
    """
    Оценка расхода по правилам v2 за 30 календарных дней при заданном числе устройств и «полных» шагах ГБ.
    Мобильный интернет — опционально (+ за каждый ГБ сверху).
    """
    d = max(0, int(device_count))
    g = max(0, int(gb_per_month))
    m = max(0, int(mobile_gb_per_month))
    device_total = (Decimal("30") * settings.billing_device_daily_rub * Decimal(d)).quantize(Decimal("0.01"))
    traffic_total = (settings.billing_gb_step_rub * Decimal(g)).quantize(Decimal("0.01"))
    mobile_total = (settings.billing_mobile_gb_extra_rub * Decimal(m)).quantize(Decimal("0.01"))
    total = (device_total + traffic_total + mobile_total).quantize(Decimal("0.01"))
    return {
        "device_rub": device_total,
        "traffic_rub": traffic_total,
        "mobile_extra_rub": mobile_total,
        "total_rub": total,
    }


def transition_credit_for_remaining_legacy_rub(settings: Settings, *, remaining_days: int) -> Decimal:
    """
    Инструмент для админов: остаток старой подписки в днях → сумма на баланс.
    Берётся доля от базовой цены месяца (конфиг), минус комиссия % (конфиг).
    """
    d = max(0, int(remaining_days))
    if d <= 0:
        return Decimal("0")
    base = settings.billing_transition_base_month_rub
    fee_pct = settings.billing_transition_fee_percent
    prop = (Decimal(d) / Decimal("30")) * base
    net = prop * (Decimal("100") - fee_pct) / Decimal("100")
    return net.quantize(Decimal("0.01"))


def plan_fields_for_ppu_estimate(plan: Plan) -> tuple[int, int]:
    """Устройства и ГБ для отображения калькулятора на карточке тарифа (ORM Plan)."""
    devices = int(plan.device_limit) if plan.device_limit is not None and int(plan.device_limit) > 0 else 1
    if bool(getattr(plan, "is_package_monthly", False)):
        gb = int(plan.monthly_gb_limit) if plan.monthly_gb_limit is not None and int(plan.monthly_gb_limit) > 0 else 0
    else:
        gb = int(plan.traffic_limit_gb) if plan.traffic_limit_gb is not None and int(plan.traffic_limit_gb) > 0 else 0
    return devices, gb
