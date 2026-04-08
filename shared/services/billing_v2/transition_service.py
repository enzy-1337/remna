from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.database import get_session_factory
import asyncio
import logging

logger = logging.getLogger(__name__)


def user_is_transition_exempt(user: User, sub: Subscription | None, settings: Settings) -> bool:
    if user.telegram_id in settings.admin_telegram_ids:
        return True
    if user.lifetime_exempt_flag:
        return True
    if sub is None:
        return False
    cutoff = datetime(settings.billing_legacy_lifetime_cutoff_year, 1, 1, tzinfo=timezone.utc)
    return bool(sub.expires_at and sub.expires_at >= cutoff)


def is_transition_due(*, expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return True
    return expires_at <= now


async def maybe_switch_to_hybrid(
    session: AsyncSession,
    *,
    user: User,
    now: datetime | None,
    settings: Settings,
) -> bool:
    if user.billing_mode == "hybrid":
        return False
    ts = now or datetime.now(timezone.utc)
    active_sub = (
        await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if user_is_transition_exempt(user, active_sub, settings):
        return False
    if active_sub is not None and not is_transition_due(expires_at=active_sub.expires_at, now=ts):
        return False
    user.billing_mode = "hybrid"
    session.add(
        Transaction(
            user_id=user.id,
            type="billing_transition",
            amount=0,
            currency="RUB",
            payment_provider="system",
            payment_id=f"transition:{user.id}:{int(ts.timestamp())}",
            status="completed",
            description="Автопереход legacy -> hybrid",
            meta={"from_mode": "legacy", "to_mode": "hybrid"},
        )
    )
    await session.flush()
    return True


async def process_due_legacy_transitions(session: AsyncSession, settings: Settings) -> int:
    now = datetime.now(timezone.utc)
    users = list(
        (
            await session.execute(
                select(User).where(User.billing_mode == "legacy").with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    switched = 0
    for user in users:
        if await maybe_switch_to_hybrid(session, user=user, now=now, settings=settings):
            switched += 1
    return switched


async def legacy_transition_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    interval = max(5, int(settings.billing_transition_check_interval_sec))
    while not stop_event.is_set():
        try:
            async with get_session_factory()() as session:
                async with session.begin():
                    n = await process_due_legacy_transitions(session, settings)
                if n:
                    logger.info("legacy_transition: switched users=%s", n)
        except Exception:
            logger.exception("legacy_transition loop failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
