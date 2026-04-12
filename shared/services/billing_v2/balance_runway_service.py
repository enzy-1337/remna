"""Оценка «на сколько дней хватит баланса» для PAYG без купленного пакетного тарифа."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.billing_daily_summary import BillingDailySummary
from shared.models.user import User
from shared.services.billing_v2.billing_calendar import billing_today


@dataclass(frozen=True, slots=True)
class BalanceRunwayEstimate:
    """Средний дневной расход по календарным дням между первым и последним днём со списаниями."""

    avg_daily_rub: Decimal
    span_calendar_days: int
    days_with_charges: int
    estimated_days_int: int
    until_day: date


async def compute_balance_runway(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
    lookback_days: int = 120,
) -> BalanceRunwayEstimate | None:
    """
    Нужно минимум **3 разных календарных дня** с ненулевыми списаниями в ``billing_daily_summary``
    и окно между первым и последним таким днём не меньше 3 календарных дней — иначе ``None``.
    """
    anchor = billing_today(settings)
    from_day = anchor - timedelta(days=lookback_days)
    rows = list(
        (
            await session.execute(
                select(BillingDailySummary)
                .where(
                    BillingDailySummary.user_id == user.id,
                    BillingDailySummary.day >= from_day,
                    BillingDailySummary.total_amount_rub > 0,
                )
                .order_by(BillingDailySummary.day.asc())
            )
        ).scalars()
    )
    if len(rows) < 3:
        return None
    first_d = rows[0].day
    last_d = rows[-1].day
    span = (last_d - first_d).days + 1
    if span < 3:
        return None
    total = sum((r.total_amount_rub for r in rows), Decimal("0"))
    avg_daily = (total / Decimal(span)).quantize(Decimal("0.01"))
    if avg_daily <= 0:
        return None
    spendable = (user.balance - settings.billing_balance_floor_rub).quantize(Decimal("0.01"))
    if spendable <= 0:
        return BalanceRunwayEstimate(
            avg_daily_rub=avg_daily,
            span_calendar_days=span,
            days_with_charges=len(rows),
            estimated_days_int=0,
            until_day=anchor,
        )
    est = int((spendable / avg_daily).to_integral_value(rounding="ROUND_FLOOR"))
    est = max(0, est)
    until = anchor + timedelta(days=est)
    return BalanceRunwayEstimate(
        avg_daily_rub=avg_daily,
        span_calendar_days=span,
        days_with_charges=len(rows),
        estimated_days_int=est,
        until_day=until,
    )
