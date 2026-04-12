from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.billing_daily_summary import BillingDailySummary
from shared.models.billing_usage_event import BillingUsageEvent
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.billing_v2.billing_calendar import (
    billing_local_day_end_utc_exclusive,
    billing_local_day_start_utc,
    billing_today,
)


def month_bounds(anchor_day: date) -> tuple[date, date]:
    month_start = date(anchor_day.year, anchor_day.month, 1)
    if anchor_day.month == 12:
        next_month = date(anchor_day.year + 1, 1, 1)
    else:
        next_month = date(anchor_day.year, anchor_day.month + 1, 1)
    return month_start, next_month


async def get_today_summary(session: AsyncSession, *, user_id: int, today: date | None = None) -> BillingDailySummary | None:
    """``day`` в ``BillingDailySummary`` — календарная дата в ``BILLING_CALENDAR_TIMEZONE``; для корректной выборки передавайте ``billing_today(settings)``."""
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


async def format_hybrid_billing_today_for_support_topic(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
) -> str | None:
    """
    Краткая сводка списаний за текущие сутки по биллинговой таймзоне (для топика тикета / уведомлений поддержки).
    Только hybrid + ``BILLING_V2_ENABLED``; иначе ``None``.
    """
    if not settings.billing_v2_enabled or user.billing_mode != "hybrid":
        return None

    today = billing_today(settings)
    row = await get_today_summary(session, user_id=user.id, today=today)
    from_dt = billing_local_day_start_utc(settings, today)
    to_dt = billing_local_day_end_utc_exclusive(settings, today)
    pack = await usage_package_breakdown(session, user_id=user.id, from_dt=from_dt, to_dt=to_dt)
    tz_label = settings.billing_calendar_timezone
    head = f"<b>Списания за сегодня</b> ({today.strftime('%d.%m.%Y')}, {tz_label})"
    if row is None:
        body = "За текущие сутки списаний нет."
    else:
        body = (
            f"Всего: <b>{row.total_amount_rub} ₽</b> "
            f"(ГБ {row.gb_amount_rub} ₽ · устройства {row.device_amount_rub} ₽ · моб. {row.mobile_amount_rub} ₽). "
            f"Пакет: ГБ покрыто {pack['gb_covered']}, устр. {pack['device_covered']}; "
            f"сверх пакета ГБ {pack['gb_charged']}, устр. {pack['device_charged']}."
        )
    return f"{head}\n{body}"


_TXN_TARIFF_TYPES = frozenset({"subscription", "subscription_autorenew"})

_TXN_CREDIT_TYPES = frozenset(
    {
        "topup",
        "admin_balance_add",
        "promo_topup_bonus",
        "first_topup_balance_bonus",
        "referral_signup",
        "referral_signup_invited",
        "referral_payment_percent",
    }
)

_TXN_DEBIT_DETAIL_TYPES = frozenset(
    {
        "usage_charge",
        "subscription",
        "subscription_autorenew",
        "manual_add",
        "billing_transition",
    }
)


async def user_has_tariff_subscription_charges(session: AsyncSession, user_id: int) -> bool:
    """Была ли оплата тарифа / автопродления с баланса (для вкладки «Тариф» в детализации)."""
    n = (
        await session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.status == "completed",
                Transaction.type.in_(tuple(_TXN_TARIFF_TYPES)),
                Transaction.amount > 0,
            )
        )
    ).scalar_one()
    return int(n or 0) > 0


async def list_completed_transactions_billing_local_range(
    session: AsyncSession,
    *,
    user_id: int,
    settings: Settings,
    from_day: date,
    to_day_inclusive: date,
    *,
    tariff_only: bool = False,
) -> list[Transaction]:
    """Транзакции ``completed`` за полуинтервал локальных суток ``[from_day, to_day_inclusive]``."""
    t0 = billing_local_day_start_utc(settings, from_day)
    t1 = billing_local_day_start_utc(settings, to_day_inclusive + timedelta(days=1))
    q = (
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.status == "completed",
            Transaction.created_at >= t0,
            Transaction.created_at < t1,
        )
        .order_by(Transaction.created_at.desc())
    )
    rows = list((await session.execute(q)).scalars())
    if not tariff_only:
        return rows
    return [r for r in rows if r.type in _TXN_TARIFF_TYPES and r.amount > 0]


def transaction_detail_bucket(txn: Transaction) -> Literal["credit", "debit", "skip"]:
    """Группировка строк для экрана детализации (без дублей смысла)."""
    if txn.type == "payg_bootstrap" or txn.amount == 0 and txn.type not in _TXN_CREDIT_TYPES:
        return "skip"
    if txn.type in _TXN_CREDIT_TYPES and txn.amount > 0:
        return "credit"
    if txn.type in _TXN_DEBIT_DETAIL_TYPES and txn.amount > 0:
        return "debit"
    return "skip"
