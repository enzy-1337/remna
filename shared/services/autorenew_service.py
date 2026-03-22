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
from shared.models.transaction import Transaction
from shared.services.remnawave_description import build_remnawave_panel_description
from shared.services.subscription_service import get_base_subscription_plan

logger = logging.getLogger(__name__)


async def process_subscription_autorenewals(session: AsyncSession, settings: Settings) -> int:
    """
    Подписки: auto_renew, status=active, сейчас < expires_at <= now + window.
    Списываем base_plan.price_rub, продлеваем expires_at на base_plan.duration_days, обновляем Remnawave.
    Возвращает число успешных продлений.
    """
    if not settings.subscription_autorenew_enabled:
        return 0

    base_plan = await get_base_subscription_plan(session)
    if base_plan is None:
        logger.warning("autorenew: нет активного плана «Базовый»")
        return 0

    price = base_plan.price_rub
    if price <= 0:
        return 0

    now = datetime.now(timezone.utc)
    window = timedelta(seconds=max(60, int(settings.subscription_autorenew_window_sec)))
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
    squads: list[str] | None = None
    if settings.remnawave_default_squad_uuid:
        squads = [settings.remnawave_default_squad_uuid.strip()]

    traffic_bytes = 0
    if base_plan.traffic_limit_gb is not None and base_plan.traffic_limit_gb > 0:
        traffic_bytes = int(base_plan.traffic_limit_gb) * (1024**3)

    renewed = 0
    for sub in candidates:
        user = sub.user
        if user is None:
            continue
        # повторная проверка окна после блокировки
        if not (sub.expires_at > now and sub.expires_at <= horizon):
            continue
        if user.balance < price:
            logger.info(
                "autorenew: skip user=%s sub=%s balance=%s need=%s",
                user.id,
                sub.id,
                user.balance,
                price,
            )
            continue
        if user.remnawave_uuid is None:
            logger.warning("autorenew: skip user=%s no remnawave_uuid", user.id)
            continue

        new_expires = sub.expires_at + timedelta(days=int(base_plan.duration_days))
        desc = build_remnawave_panel_description(user)
        try:
            await rw.update_user(
                str(user.remnawave_uuid),
                expire_at=new_expires,
                hwid_device_limit=sub.devices_count,
                traffic_limit_bytes=traffic_bytes,
                status="ACTIVE",
                description=desc,
                active_internal_squads=squads,
            )
        except RemnaWaveError as e:
            logger.warning("autorenew: RW failed user=%s: %s", user.id, e)
            continue

        user.balance -= price
        sub.expires_at = new_expires
        sub.plan_id = base_plan.id
        session.add(
            Transaction(
                user_id=user.id,
                type="subscription_autorenew",
                amount=price,
                currency="RUB",
                payment_provider="balance",
                payment_id=None,
                status="completed",
                description=f"Автопродление «{base_plan.name}» (+{base_plan.duration_days} дн.)",
                meta={
                    "plan_id": base_plan.id,
                    "subscription_id": sub.id,
                },
            )
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
