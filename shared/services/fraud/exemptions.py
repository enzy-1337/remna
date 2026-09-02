"""Исключения из антифрод-проверок.

Админы бота (env-суперадмин, ADMIN_TELEGRAM_IDS, роль с правом view_stats) и аккаунты
с lifetime-подпиской (expires_at.year >= BILLING_LEGACY_LIFETIME_CUTOFF_YEAR, конвенция
"вечной" подписки, см. billing_v2/transition_service.py) полностью игнорируются всеми
детекторами антифрода — вызывается из record_incident() до создания FraudIncident,
поэтому для таких пользователей не остаётся ни записи, ни алерта.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.subscription import Subscription
from shared.models.user import User
from shared.services.admin_rbac_service import telegram_has_permission


async def is_fraud_exempt(session: AsyncSession, settings: Settings, *, user: User) -> bool:
    if user.telegram_id is not None:
        tg_id = int(user.telegram_id)
        if tg_id in settings.admin_telegram_ids:
            return True
        if await telegram_has_permission(
            session, settings, telegram_id=tg_id, permission="view_stats"
        ):
            return True

    cutoff = datetime(settings.billing_legacy_lifetime_cutoff_year, 1, 1, tzinfo=timezone.utc)
    row = (
        await session.execute(
            select(Subscription.id)
            .where(Subscription.user_id == user.id, Subscription.expires_at >= cutoff)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None
