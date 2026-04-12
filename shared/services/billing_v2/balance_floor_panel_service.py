"""Гибрид v2: пол баланса — единый путь уведомление + Remnawave DISABLED/пустые squads; выход — ACTIVE."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.user import User
from shared.services.optimized_route_service import remnawave_squads_for_db_user
from shared.services.remnawave_description import build_remnawave_panel_description
from shared.services.subscription_service import (
    get_active_subscription,
    update_rw_user_respecting_hwid_limit,
)
from shared.services.telegram_notify import send_telegram_message

logger = logging.getLogger(__name__)


async def sync_hybrid_balance_floor_panel_state(
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    if not settings.billing_v2_enabled or user.billing_mode != "hybrid":
        return

    floor = settings.billing_balance_floor_rub.quantize(Decimal("0.01"))
    bal = user.balance.quantize(Decimal("0.01"))

    if bal > floor:
        await _unlatch_floor_restore_rw(session, user, settings)
        return

    if user.balance_floor_rw_suspended_at is not None:
        return

    rw_ok = await _remnawave_suspend_user(user, settings)
    if not rw_ok:
        return

    msg = (
        f"⚠️ Баланс достиг нижнего лимита ({floor} ₽). Доступ VPN в панели приостановлен.\n"
        "Пополните баланс, чтобы снова подключаться."
    )
    await send_telegram_message(user.telegram_id, msg, parse_mode=None, settings=settings)

    user.balance_floor_rw_suspended_at = datetime.now(timezone.utc)
    await session.flush()


async def _remnawave_suspend_user(user: User, settings: Settings) -> bool:
    if user.remnawave_uuid is None:
        return True
    rw = RemnaWaveClient(settings)
    try:
        await rw.update_user(
            str(user.remnawave_uuid),
            status="DISABLED",
            active_internal_squads=[],
        )
    except RemnaWaveError as e:
        logger.warning("balance_floor: DISABLED user=%s failed: %s", user.id, e)
        return False
    return True


async def _unlatch_floor_restore_rw(
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    if user.balance_floor_rw_suspended_at is None:
        return

    if user.remnawave_uuid is None:
        user.balance_floor_rw_suspended_at = None
        await session.flush()
        return

    now = datetime.now(timezone.utc)
    sub = await get_active_subscription(session, user.id)
    if sub is None or sub.expires_at <= now:
        user.balance_floor_rw_suspended_at = None
        await session.flush()
        return

    plan = sub.plan
    traffic_bytes = 0
    if plan is not None and plan.traffic_limit_gb is not None and plan.traffic_limit_gb > 0:
        traffic_bytes = int(plan.traffic_limit_gb) * (1024**3)

    desc = build_remnawave_panel_description(user)
    squads = remnawave_squads_for_db_user(settings, user)
    rw = RemnaWaveClient(settings)
    try:
        await update_rw_user_respecting_hwid_limit(
            rw,
            str(user.remnawave_uuid),
            devices_limit_for_panel=sub.devices_count,
            expire_at=sub.expires_at,
            traffic_limit_bytes=traffic_bytes,
            status="ACTIVE",
            description=desc,
            active_internal_squads=squads,
        )
    except RemnaWaveError as e:
        logger.warning("balance_floor: restore ACTIVE user=%s failed: %s", user.id, e)
        return

    user.balance_floor_rw_suspended_at = None
    await session.flush()

    await send_telegram_message(
        user.telegram_id,
        "✅ Баланс выше лимита — доступ VPN в панели снова активен.",
        parse_mode=None,
        settings=settings,
    )


async def reconcile_hybrid_balance_floor_panel_batch(
    session: AsyncSession,
    settings: Settings,
) -> None:
    if not settings.billing_v2_enabled:
        return
    floor = settings.billing_balance_floor_rub.quantize(Decimal("0.01"))
    stmt = select(User).where(
        User.billing_mode == "hybrid",
        or_(
            and_(User.balance <= floor, User.balance_floor_rw_suspended_at.is_(None)),
            and_(User.balance > floor, User.balance_floor_rw_suspended_at.isnot(None)),
        ),
    )
    users = list((await session.execute(stmt)).scalars())
    for u in users:
        await sync_hybrid_balance_floor_panel_state(session, u, settings)
