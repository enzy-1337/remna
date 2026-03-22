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
