"""Async SQLAlchemy: движок и фабрика сессий.

Поддерживается PostgreSQL (asyncpg). Переход на MariaDB — заглушка и чеклист:
docs/mariadb-migration-future.md, shared/db_mariadb_future.py
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.config import get_settings
from shared.db_mariadb_future import require_postgres_database_url

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        require_postgres_database_url(settings.database_url)
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory
