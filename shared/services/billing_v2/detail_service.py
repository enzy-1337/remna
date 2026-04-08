from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.billing_daily_summary import BillingDailySummary
from shared.models.billing_usage_event import BillingUsageEvent


def month_bounds(anchor_day: date) -> tuple[date, date]:
    month_start = date(anchor_day.year, anchor_day.month, 1)
    if anchor_day.month == 12:
        next_month = date(anchor_day.year + 1, 1, 1)
    else:
        next_month = date(anchor_day.year, anchor_day.month + 1, 1)
    return month_start, next_month


async def get_today_summary(session: AsyncSession, *, user_id: int, today: date | None = None) -> BillingDailySummary | None:
    d = today or datetime.now(timezone.utc).date()
    return (
        await session.execute(
            select(BillingDailySummary)
            .where(BillingDailySummary.user_id == user_id, BillingDailySummary.day == d)
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_month_summaries(session: AsyncSession, *, user_id: int, anchor_day: date | None = None) -> list[BillingDailySummary]:
    d = anchor_day or datetime.now(timezone.utc).date()
    month_start, next_month = month_bounds(d)
    return list(
        (
            await session.execute(
                select(BillingDailySummary)
                .where(
                    and_(
                        BillingDailySummary.user_id == user_id,
                        BillingDailySummary.day >= month_start,
                        BillingDailySummary.day < next_month,
                    )
                )
                .order_by(BillingDailySummary.day.asc())
            )
        ).scalars()
    )


async def cleanup_old_details(session: AsyncSession, *, retention_days: int) -> int:
    threshold = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
    rows = (
        await session.execute(select(BillingDailySummary).where(BillingDailySummary.day < threshold))
    ).scalars().all()
    for row in rows:
        await session.delete(row)
    await session.flush()
    return len(rows)


def summarize_month_total(rows: list[BillingDailySummary]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        total += row.total_amount_rub
    return total


async def usage_package_breakdown(
    session: AsyncSession,
    *,
    user_id: int,
    from_dt: datetime,
    to_dt: datetime,
) -> dict[str, int]:
    rows = list(
        (
            await session.execute(
                select(BillingUsageEvent).where(
                    and_(
                        BillingUsageEvent.user_id == user_id,
                        BillingUsageEvent.event_ts >= from_dt,
                        BillingUsageEvent.event_ts < to_dt,
                    )
                )
            )
        ).scalars()
    )
    out = {
        "gb_covered": 0,
        "gb_charged": 0,
        "device_covered": 0,
        "device_charged": 0,
    }
    for row in rows:
        covered = bool((row.meta or {}).get("package_covered", False))
        if row.event_type == "traffic_gb_step":
            if covered:
                out["gb_covered"] += 1
            else:
                out["gb_charged"] += 1
        elif row.event_type == "device_daily":
            if covered:
                out["device_covered"] += 1
            else:
                out["device_charged"] += 1
    return out
