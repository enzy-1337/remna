"""Автопродление подписки: за окно до истечения списать цену «Базовый» и +duration_days."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.config import Settings
from shared.database import get_session_factory
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.subscription import Subscription
from shared.models.billing_usage_event import BillingUsageEvent
from shared.models.transaction import Transaction
from shared.services.optimized_route_service import remnawave_squads_for_db_user
from shared.services.remnawave_description import build_remnawave_panel_description
from shared.services.billing_v2.balance_floor_panel_service import sync_hybrid_balance_floor_panel_state
from shared.services.remnawave_user_panel_sync import update_rw_user_respecting_hwid_limit
from shared.services.subscription_email_notify import queue_subscription_renewed_email
from shared.services.subscription_service import get_base_subscription_plan
from shared.services.feature_flags import tariff_purchases_enabled

logger = logging.getLogger(__name__)


async def process_subscription_autorenewals(session: AsyncSession, settings: Settings) -> int:
    """
    Подписки: auto_renew, status=active, сейчас < expires_at <= now + window.
    Списываем base_plan.price_rub, продлеваем expires_at на base_plan.duration_days, обновляем Remnawave.
    Возвращает число успешных продлений.
    """
    if not settings.subscription_autorenew_enabled:
        return 0
    if not await tariff_purchases_enabled(settings):
        return 0

    base_plan = await get_base_subscription_plan(session)
    if base_plan is None:
        logger.warning("autorenew: нет активного плана «Базовый»")
        return 0

    now = datetime.now(timezone.utc)
    window = timedelta(days=30)
    horizon = now + window

    stmt = (
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            Subscription.auto_renew.is_(True),
            Subscription.status == "active",
            Subscription.expires_at > now,
            Subscription.expires_at <= horizon,
        )
        .with_for_update(skip_locked=True)
    )
    r = await session.execute(stmt)
    candidates = list(r.scalars().all())

    rw = RemnaWaveClient(settings)

    renewed = 0
    for sub in candidates:
        user = sub.user
        if user is None:
            continue
        # повторная проверка окна после блокировки
        if not (sub.expires_at > now and sub.expires_at <= horizon):
            continue
        if user.billing_mode != "hybrid":
            continue
        if user.balance < 0:
            logger.info(
                "autorenew: skip user=%s sub=%s balance=%s",
                user.id,
                sub.id,
                user.balance,
            )
            continue
        usage_since = now - timedelta(days=14)
        has_device = (
            await session.execute(
                select(BillingUsageEvent.id)
                .where(
                    BillingUsageEvent.user_id == user.id,
                    BillingUsageEvent.event_type == "device_daily",
                    BillingUsageEvent.event_ts >= usage_since,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        has_gb = (
            await session.execute(
                select(BillingUsageEvent.id)
                .where(
                    BillingUsageEvent.user_id == user.id,
                    BillingUsageEvent.event_type == "traffic_gb_step",
                    BillingUsageEvent.event_ts >= usage_since,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if has_device is None or has_gb is None:
            continue
        if user.remnawave_uuid is None:
            logger.warning("autorenew: skip user=%s no remnawave_uuid", user.id)
            continue

        payg_days = int(settings.billing_payg_subscription_days)
        new_expires = sub.expires_at + timedelta(days=payg_days)
        desc = build_remnawave_panel_description(user)
        squads = remnawave_squads_for_db_user(settings, user)
        try:
            await update_rw_user_respecting_hwid_limit(
                rw,
                str(user.remnawave_uuid),
                devices_limit_for_panel=sub.devices_count,
                expire_at=new_expires,
                traffic_limit_bytes=0,
                status="ACTIVE",
                description=desc,
                active_internal_squads=squads,
            )
        except RemnaWaveError as e:
            logger.warning("autorenew: RW failed user=%s: %s", user.id, e)
            continue

        sub.expires_at = new_expires
        sub.plan_id = base_plan.id
        session.add(
            Transaction(
                user_id=user.id,
                type="subscription_autorenew",
                amount=0,
                currency="RUB",
                payment_provider="system",
                payment_id=None,
                status="completed",
                description=f"Автопродление PAYG-подписки на {payg_days} дней",
                meta={
                    "plan_id": base_plan.id,
                    "subscription_id": sub.id,
                    "renewal_days": payg_days,
                    "charged_rub": "0",
                },
            )
        )
        if user.billing_mode == "hybrid" and settings.billing_v2_enabled:
            await sync_hybrid_balance_floor_panel_state(session, user, settings)
        queue_subscription_renewed_email(
            session,
            user=user,
            settings=settings,
            new_expires=new_expires,
            plan_name=base_plan.name,
            auto=True,
        )
        renewed += 1

    return renewed


async def subscription_autorenew_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    interval = max(60, int(settings.subscription_autorenew_interval_sec))
    while not stop_event.is_set():
        if not settings.subscription_autorenew_enabled:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            factory = get_session_factory()
            async with factory() as session:
                async with session.begin():
                    n = await process_subscription_autorenewals(session, settings)
                if n:
                    logger.info("autorenew: продлено подписок: %s", n)
        except Exception:
            logger.exception("autorenew: итерация не удалась")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
