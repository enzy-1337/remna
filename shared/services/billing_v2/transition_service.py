from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.billing_v2.traffic_meter_poll_service import baseline_meter_at_hybrid_transition
from shared.database import get_session_factory
import asyncio
import logging

logger = logging.getLogger(__name__)


def _parse_rw_expire_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
    if not settings.billing_v2_enabled:
        return False
    if user.billing_mode == "hybrid":
        return False
    ts = now or datetime.now(timezone.utc)
    latest_sub = (
        await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if user_is_transition_exempt(user, latest_sub, settings):
        return False
    # Переводим только пользователей, у которых была подписка и она уже истекла.
    if latest_sub is None or latest_sub.expires_at is None:
        return False
    # Если срок в БД истёк, но в панели подписка уже продлена вручную, не переводим в hybrid.
    if latest_sub.expires_at <= ts and user.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            info = await rw.get_user(str(user.remnawave_uuid))
        except RemnaWaveError as e:
            logger.warning(
                "legacy_transition: panel recheck failed user_id=%s rw_uuid=%s: %s",
                user.id,
                user.remnawave_uuid,
                e,
            )
        else:
            panel_exp = _parse_rw_expire_at(
                info.get("expireAt")
                or info.get("expiresAt")
                or info.get("expire_at")
                or info.get("expires_at")
            )
            if panel_exp is not None and panel_exp > ts:
                latest_sub.expires_at = panel_exp
                latest_sub.status = "active"
                logger.info(
                    "legacy_transition: skip switch due panel extension user_id=%s db_exp=%s panel_exp=%s",
                    user.id,
                    ts.isoformat(),
                    panel_exp.isoformat(),
                )
                return False
    if not is_transition_due(expires_at=latest_sub.expires_at, now=ts):
        return False

    # Дополнительная защита: активный статус/триал в БД блокирует переход.
    has_active_sub = (
        await session.execute(
            select(Subscription.id)
            .where(
                Subscription.user_id == user.id,
                Subscription.status.in_(("active", "trial")),
                Subscription.expires_at > ts,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if has_active_sub is not None:
        return False

    # Переход только после пополнения, сделанного уже после окончания подписки.
    has_topup_after_expiry = (
        await session.execute(
            select(Transaction.id)
            .where(
                Transaction.user_id == user.id,
                Transaction.status == "completed",
                Transaction.type.in_(("topup", "admin_balance_add")),
                Transaction.created_at >= latest_sub.expires_at,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if has_topup_after_expiry is None:
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
    try:
        await baseline_meter_at_hybrid_transition(session, user=user, settings=settings)
    except Exception:
        logger.exception("baseline_meter_at_hybrid_transition failed user_id=%s", user.id)
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
            n = 0
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
