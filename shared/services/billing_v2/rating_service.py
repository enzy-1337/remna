from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.billing_daily_summary import BillingDailySummary
from shared.models.billing_usage_event import BillingUsageEvent
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.user import User
from shared.services.billing_v2.billing_calendar import billing_package_month_utc_bounds, billing_zoneinfo
from shared.services.billing_v2.device_service import list_active_device_hwids
from shared.services.billing_v2.ledger_service import apply_debit


def is_gb_step_covered_by_package(*, used_steps_in_month: int, monthly_gb_limit: int | None) -> bool:
    if monthly_gb_limit is None or monthly_gb_limit <= 0:
        return False
    return used_steps_in_month < monthly_gb_limit


def is_device_covered_by_package(*, device_hwid: str, active_hwids: list[str], device_limit: int | None) -> bool:
    if device_limit is None or device_limit <= 0:
        return False
    free_hwids = sorted(active_hwids)[: int(device_limit)]
    return device_hwid in free_hwids


async def _upsert_daily(
    session: AsyncSession,
    *,
    user_id: int,
    day: date,
) -> BillingDailySummary:
    summary = (
        await session.execute(
            select(BillingDailySummary)
            .where(BillingDailySummary.user_id == user_id, BillingDailySummary.day == day)
            .limit(1)
        )
    ).scalar_one_or_none()
    if summary is not None:
        return summary
    summary = BillingDailySummary(user_id=user_id, day=day)
    session.add(summary)
    await session.flush()
    return summary


async def _active_package_plan(session: AsyncSession, *, user_id: int, now: datetime) -> Plan | None:
    row = (
        await session.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_(("active", "trial")),
                Subscription.expires_at > now,
                Plan.is_package_monthly.is_(True),
            )
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    _sub, plan = row
    return plan


async def charge_gb_step(
    session: AsyncSession,
    *,
    user: User,
    event_id: str,
    event_ts: datetime,
    is_mobile_internet: bool,
    settings: Settings,
) -> bool:
    dup = (
        await session.execute(
            select(BillingUsageEvent.id).where(BillingUsageEvent.event_id == event_id).limit(1)
        )
    ).scalar_one_or_none()
    if dup is not None:
        return True

    plan = await _active_package_plan(session, user_id=user.id, now=event_ts)
    package_covered = False
    if plan is not None and plan.monthly_gb_limit is not None and plan.monthly_gb_limit > 0:
        month_start_utc, month_end_utc = billing_package_month_utc_bounds(settings, event_ts)
        used_gb_units = (
            await session.execute(
                select(func.count(BillingUsageEvent.id)).where(
                    and_(
                        BillingUsageEvent.user_id == user.id,
                        BillingUsageEvent.event_type == "traffic_gb_step",
                        BillingUsageEvent.event_ts >= month_start_utc,
                        BillingUsageEvent.event_ts < month_end_utc,
                    )
                )
            )
        ).scalar_one()
        package_covered = is_gb_step_covered_by_package(
            used_steps_in_month=int(used_gb_units or 0),
            monthly_gb_limit=int(plan.monthly_gb_limit),
        )

    if package_covered:
        session.add(
            BillingUsageEvent(
                user_id=user.id,
                event_id=event_id,
                event_type="traffic_gb_step",
                event_ts=event_ts,
                usage_gb_step=1,
                is_mobile_internet=is_mobile_internet,
                meta={"package_covered": True},
            )
        )
        return True

    amount = settings.billing_gb_step_rub
    mobile_extra = settings.billing_mobile_gb_extra_rub if is_mobile_internet else Decimal("0")
    opt_extra = (
        settings.billing_optimized_route_gb_extra_rub if user.optimized_route_enabled else Decimal("0")
    )
    total = amount + mobile_extra + opt_extra
    debit_meta: dict = {"is_mobile_internet": is_mobile_internet}
    if opt_extra > 0:
        debit_meta["optimized_route"] = True
        debit_meta["optimized_route_extra_rub"] = str(opt_extra)
    lr = await apply_debit(
        session,
        user=user,
        amount_rub=total,
        idempotency_key=f"gb:{event_id}",
        source="traffic",
        source_ref=event_id,
        settings=settings,
        meta=debit_meta,
    )
    if not lr.applied:
        return False

    usage_meta: dict = {"package_covered": False, "is_mobile_internet": is_mobile_internet}
    if opt_extra > 0:
        usage_meta["optimized_route"] = True
        usage_meta["optimized_route_extra_rub"] = str(opt_extra)
    session.add(
        BillingUsageEvent(
            user_id=user.id,
            event_id=event_id,
            event_type="traffic_gb_step",
            event_ts=event_ts,
            usage_gb_step=1,
            is_mobile_internet=is_mobile_internet,
            meta=usage_meta,
        )
    )
    summary_day = event_ts.astimezone(billing_zoneinfo(settings)).date()
    summary = await _upsert_daily(session, user_id=user.id, day=summary_day)
    summary.gb_units += 1
    summary.gb_amount_rub += amount
    if is_mobile_internet:
        summary.mobile_gb_units += 1
        summary.mobile_amount_rub += mobile_extra
    if opt_extra > 0:
        summary.gb_amount_rub += opt_extra
    summary.total_amount_rub += total
    return True


async def charge_daily_device_once(
    session: AsyncSession,
    *,
    user: User,
    device_hwid: str,
    day: date,
    settings: Settings,
    eval_at: datetime | None = None,
    active_hwids_for_package: list[str] | None = None,
) -> bool:
    event_id = f"device_daily:{user.id}:{device_hwid}:{day.isoformat()}"
    existing = (
        await session.execute(
            select(BillingUsageEvent.id).where(BillingUsageEvent.event_id == event_id).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return True

    ev_ts = eval_at or datetime.now(timezone.utc)
    plan = await _active_package_plan(session, user_id=user.id, now=ev_ts)
    package_covered = False
    if plan is not None and plan.device_limit is not None and plan.device_limit > 0:
        active_hwids = (
            active_hwids_for_package
            if active_hwids_for_package is not None
            else await list_active_device_hwids(session, user_id=user.id)
        )
        if is_device_covered_by_package(
            device_hwid=device_hwid,
            active_hwids=active_hwids,
            device_limit=int(plan.device_limit),
        ):
            package_covered = True
    if package_covered:
        session.add(
            BillingUsageEvent(
                user_id=user.id,
                event_id=event_id,
                event_type="device_daily",
                event_ts=ev_ts,
                device_hwid=device_hwid,
                is_mobile_internet=False,
                meta={"package_covered": True},
            )
        )
        return True
    daily_key = f"device:{user.id}:{device_hwid}:{day.isoformat()}"
    lr = await apply_debit(
        session,
        user=user,
        amount_rub=settings.billing_device_daily_rub,
        idempotency_key=daily_key,
        source="device_daily",
        source_ref=device_hwid,
        settings=settings,
    )
    if not lr.applied:
        return False
    session.add(
        BillingUsageEvent(
            user_id=user.id,
            event_id=event_id,
            event_type="device_daily",
            event_ts=ev_ts,
            device_hwid=device_hwid,
            is_mobile_internet=False,
            meta={"package_covered": False},
        )
    )
    summary = await _upsert_daily(session, user_id=user.id, day=day)
    summary.device_units += 1
    summary.device_amount_rub += settings.billing_device_daily_rub
    summary.total_amount_rub += settings.billing_device_daily_rub
    return True
