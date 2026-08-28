"""Блокировка/разблокировка пользователя по решению антифрода.

Отдельно от admin_disable_subscription_record/admin_enable_subscription_record
(bot-админка): там подписка уходит в status="cancelled" (ручной тумблер админа),
здесь — в status="blocked" (антифрод), чтобы блок-лист явно отличал причину
и unblock точно знал, что восстанавливает именно фрод-блок, а не чей-то
самостоятельный cancel.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.subscription import Subscription
from shared.models.user import User
from shared.services.subscription_service import get_active_subscription

logger = logging.getLogger(__name__)


async def _blocked_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    r = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(Subscription.user_id == user_id, Subscription.status == "blocked")
        .order_by(Subscription.expires_at.desc())
    )
    return r.scalars().first()


async def block_user_for_fraud(
    session: AsyncSession,
    settings: Settings,
    user: User,
    *,
    reason: str,
) -> None:
    """reason — машиночитаемый префикс вида "fraud:{detector}:incident_{id}" (User.block_reason),
    парсится веб-админкой на странице блок-листа для отображения источника."""
    user.is_blocked = True
    user.block_reason = reason
    sub = await get_active_subscription(session, user.id, account_scope=False)
    if sub is not None:
        sub.status = "blocked"
    if user.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            await rw.update_user(str(user.remnawave_uuid), status="DISABLED")
        except RemnaWaveError as e:
            logger.warning("block_user_for_fraud: RW disable failed user_id=%s: %s", user.id, e)


async def unblock_user_for_fraud(session: AsyncSession, settings: Settings, user: User) -> None:
    user.is_blocked = False
    user.block_reason = None
    sub = await _blocked_subscription(session, user.id)
    if sub is not None:
        is_trial = sub.plan is not None and sub.plan.name == "Триал"
        sub.status = "trial" if is_trial else "active"
        if user.remnawave_uuid is not None and not settings.remnawave_stub:
            rw = RemnaWaveClient(settings)
            try:
                await rw.update_user(str(user.remnawave_uuid), status="ACTIVE")
            except RemnaWaveError as e:
                logger.warning("unblock_user_for_fraud: RW enable failed user_id=%s: %s", user.id, e)
