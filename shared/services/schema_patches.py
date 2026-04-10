"""Идемпотентные правки схемы БД без отдельной миграции (PostgreSQL)."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_PG_EXPIRY_NOTIFY_DDL = (
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expiry_notified_24h boolean NOT NULL DEFAULT false",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expiry_notified_3h boolean NOT NULL DEFAULT false",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expiry_notify_anchor_at TIMESTAMP WITH TIME ZONE NULL",
)


_PG_USER_NOTIFY_MSG_DDL = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_bonus_message_id bigint NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS device_notify_message_id bigint NULL",
)


_PG_PROMO_DDL = (
    # Награда для промокодов типа "дни с фолбэком на деньги"
    "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS fallback_value_rub numeric(12, 2) NULL",
    # Кто создал промокод (админ в боте — это тоже пользователь)
    "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS created_by_user_id integer NULL",
    # Отметка: бонус к первому пополнению уже начислен
    "ALTER TABLE promo_usages ADD COLUMN IF NOT EXISTS topup_bonus_applied_at TIMESTAMP WITH TIME ZONE NULL",
)


async def ensure_subscription_expiry_notify_columns(session: AsyncSession) -> None:
    """
    Колонки для напоминаний об окончании подписки.
    На PostgreSQL выполняется при старте бота; без этого ORM падает на SELECT.
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    for stmt in _PG_EXPIRY_NOTIFY_DDL:
        await session.execute(text(stmt))
    logger.info("schema_patches: проверены колонки expiry_notified_* в subscriptions")


async def ensure_user_bot_message_id_columns(session: AsyncSession) -> None:
    """Колонки для замены последних сервисных сообщений бота (рефералка, устройства)."""
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    for stmt in _PG_USER_NOTIFY_MSG_DDL:
        await session.execute(text(stmt))
    logger.info("schema_patches: проверены колонки referral_bonus_message_id / device_notify_message_id")


async def ensure_promo_columns(session: AsyncSession) -> None:
    """
    Колонки для расширенной логики промокодов:
    - fallback_value_rub / created_by_user_id в promo_codes
    - topup_bonus_applied_at в promo_usages
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    for stmt in _PG_PROMO_DDL:
        await session.execute(text(stmt))
    logger.info("schema_patches: проверены колонки promo_*")
