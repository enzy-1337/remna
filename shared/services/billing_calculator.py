"""Калькуляторы: pay-as-you-go по сценарию, за 30 дн.; сравнение с планом; кредит при переходе с legacy."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from shared.config import Settings
from shared.models.plan import Plan


def estimate_payg_scenario_rub(
    settings: Settings,
    *,
    device_days: int,
    gb_steps: int,
    mobile_gb_steps: int = 0,
    optimized_route: bool = False,
) -> dict[str, Decimal]:
    """
    Оценка pay-as-you-go: сумма «устройство·сутки» × дневная ставка, шаги ГБ, моб. ГБ, при флаге —
    доплата за ГБ оптимизированного маршрута (как в charge_gb_step).
    """
    dd = max(0, int(device_days))
    g = max(0, int(gb_steps))
    max(0, int(mobile_gb_steps))  # аргумент сохранён для совместимости; в тарификации не используется
    device_total = (Decimal(dd) * settings.billing_device_daily_rub).quantize(Decimal("0.01"))
    traffic_total = (settings.billing_gb_step_rub * Decimal(g)).quantize(Decimal("0.01"))
    mobile_total = Decimal("0")
    opt_extra = (
        (settings.billing_optimized_route_gb_extra_rub * Decimal(g)).quantize(Decimal("0.01"))
        if optimized_route
        else Decimal("0")
    )
    total = (device_total + traffic_total + mobile_total + opt_extra).quantize(Decimal("0.01"))
    return {
        "device_rub": device_total,
        "traffic_rub": traffic_total,
        "mobile_extra_rub": mobile_total,
        "optimized_extra_rub": opt_extra,
        "total_rub": total,
    }


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
    out = estimate_payg_scenario_rub(
        settings,
        device_days=30 * d,
        gb_steps=g,
        mobile_gb_steps=m,
        optimized_route=False,
    )
    return {
        "device_rub": out["device_rub"],
        "traffic_rub": out["traffic_rub"],
        "mobile_extra_rub": out["mobile_extra_rub"],
        "total_rub": out["total_rub"],
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


def plan_charge_for_compare_period_rub(plan: Plan, period_days: int) -> Decimal:
    """Цена плана, пропорционально периоду сравнения (длительность плана из БД)."""
    pd = max(1, int(period_days))
    dur = max(1, int(plan.duration_days))
    return (plan.price_rub * Decimal(pd) / Decimal(dur)).quantize(Decimal("0.01"))


def compare_plan_vs_payg_estimate(
    plan: Plan,
    *,
    period_days: int,
    payg_estimate: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    """Сравнение: стоимость плана за период vs оценка pay-as-you-go (delta > 0 — план дороже сценария)."""
    plan_rub = plan_charge_for_compare_period_rub(plan, period_days)
    payg_rub = payg_estimate["total_rub"]
    return {
        "plan_rub": plan_rub,
        "payg_rub": payg_rub,
        "delta_rub": (plan_rub - payg_rub).quantize(Decimal("0.01")),
    }
