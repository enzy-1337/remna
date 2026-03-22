"""Полная очистка данных приложения в БД (осторожно)."""

from __future__ import annotations

import logging

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.device import Device
from shared.models.notification_log import NotificationLog
from shared.models.plan import Plan
from shared.models.promo import PromoCode, PromoUsage
from shared.models.referral_reward import ReferralReward
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User

logger = logging.getLogger(__name__)


async def wipe_all_application_data(session: AsyncSession) -> None:
    """
    Удаляет все строки во всех таблицах приложения (порядок с учётом FK).
    После вызова у текущей сессии не должно оставаться «живых» ORM-экземпляров User.
    """
    await session.execute(delete(Device))
    await session.execute(delete(Subscription))
    await session.execute(delete(Transaction))
    await session.execute(delete(ReferralReward))
    await session.execute(delete(PromoUsage))
    await session.execute(delete(NotificationLog))
    await session.execute(delete(User))
    await session.execute(delete(PromoCode))
    await session.execute(delete(Plan))
    await session.flush()
    session.expunge_all()
    logger.warning("factory reset: all application tables wiped")
