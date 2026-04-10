from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.models.billing_daily_summary import BillingDailySummary
from shared.models.user import User
from shared.services.telegram_notify import send_telegram_message

logger = logging.getLogger(__name__)

_WINDOW_24H = (timedelta(hours=22), timedelta(hours=26))
_WINDOW_1H = (timedelta(minutes=40), timedelta(hours=1, minutes=20))


def classify_eta_windows(remaining: timedelta) -> tuple[bool, bool, bool, bool]:
    in_24h = _WINDOW_24H[0] <= remaining <= _WINDOW_24H[1]
    in_1h = _WINDOW_1H[0] <= remaining <= _WINDOW_1H[1]
    reset_24h = (not in_24h) and remaining > _WINDOW_24H[1]
    reset_1h = (not in_1h) and remaining > _WINDOW_1H[1]
    return in_24h, in_1h, reset_24h, reset_1h


async def _avg_daily_spend_last_days(session: AsyncSession, *, user_id: int, days: int = 3) -> Decimal:
    today = datetime.now(timezone.utc).date()
    window = max(1, int(days))
    from_day = today - timedelta(days=window - 1)
    rows = list(
        (
            await session.execute(
                select(BillingDailySummary).where(
                    and_(
                        BillingDailySummary.user_id == user_id,
                        BillingDailySummary.day >= from_day,
                        BillingDailySummary.day <= today,
                    )
                )
            )
        ).scalars()
    )
    if not rows:
        return Decimal("0")
    total = Decimal("0")
    unique_days: set[date] = set()
    for row in rows:
        total += row.total_amount_rub
        unique_days.add(row.day)
    if not unique_days:
        return Decimal("0")
    return (total / Decimal(len(unique_days))).quantize(Decimal("0.01"))


async def process_negative_balance_notifications(session: AsyncSession, settings: Settings) -> tuple[int, int]:
    if not settings.billing_negative_notify_enabled:
        return 0, 0
    users = list(
        (
            await session.execute(
                select(User).where(User.is_blocked.is_(False), User.billing_mode == "hybrid")
            )
        ).scalars()
    )
    sent24 = 0
    sent1 = 0
    now = datetime.now(timezone.utc)
    for user in users:
        avg_daily = await _avg_daily_spend_last_days(session, user_id=user.id, days=3)
        if avg_daily <= 0:
            user.risk_notified_24h_at = None
            user.risk_notified_1h_at = None
            continue
        hourly = (avg_daily / Decimal("24")).quantize(Decimal("0.0001"))
        if hourly <= 0:
            user.risk_notified_24h_at = None
            user.risk_notified_1h_at = None
            continue
        remain_to_floor = (user.balance - settings.billing_balance_floor_rub).quantize(Decimal("0.01"))
        if remain_to_floor <= 0:
            user.risk_notified_24h_at = None
            user.risk_notified_1h_at = None
            continue
        hours_to_floor = float(remain_to_floor / hourly)
        eta = now + timedelta(hours=hours_to_floor)
        remaining = eta - now

        in_24h, in_1h, reset_24h, reset_1h = classify_eta_windows(remaining)

        if in_24h and user.risk_notified_24h_at is None:
            ok = (
                await send_telegram_message(
                    user.telegram_id,
                    (
                        "⏰ Предупреждение: при текущем расходе баланс может уйти в минус примерно через 24 часа.\n"
                        f"Текущий баланс: {user.balance} ₽.\n"
                        "Рекомендуем пополнить баланс заранее."
                    ),
                    parse_mode=None,
                    settings=settings,
                )
                is not None
            )
            if ok:
                user.risk_notified_24h_at = now
                sent24 += 1
        if in_1h and user.risk_notified_1h_at is None:
            ok = (
                await send_telegram_message(
                    user.telegram_id,
                    (
                        "⏰ Срочно: при текущем расходе баланс может уйти в минус примерно через 1 час.\n"
                        f"Текущий баланс: {user.balance} ₽.\n"
                        "Пополните баланс, чтобы избежать ограничений."
                    ),
                    parse_mode=None,
                    settings=settings,
                )
                is not None
            )
            if ok:
                user.risk_notified_1h_at = now
                sent1 += 1

        if reset_24h:
            user.risk_notified_24h_at = None
        if reset_1h:
            user.risk_notified_1h_at = None

    return sent24, sent1


async def negative_balance_notify_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    interval = max(120, int(settings.billing_negative_notify_interval_sec))
    while not stop_event.is_set():
        try:
            async with get_session_factory()() as session:
                async with session.begin():
                    a, b = await process_negative_balance_notifications(session, settings)
                if a or b:
                    logger.info("negative_notify: sent 24h=%s 1h=%s", a, b)
        except Exception:
            logger.exception("negative_notify loop failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
