"""Доступ к web-admin для админов из .env."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.config import Settings
from shared.models.user import User
from shared.services.admin_rbac_service import (
    can_access_web_admin,
    is_env_superadmin_telegram,
    is_legacy_env_admin_telegram,
)


def test_env_superadmin_and_legacy_admin_flags() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        bot_token="x",
        superadmin_telegram_id=111,
        admin_telegram_ids_csv="222,333",
    )
    assert is_env_superadmin_telegram(settings, 111)
    assert not is_env_superadmin_telegram(settings, 222)
    assert is_legacy_env_admin_telegram(settings, 222)
    assert is_legacy_env_admin_telegram(settings, 111)


@pytest.mark.asyncio
async def test_can_access_web_admin_for_env_admin_without_db_row() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        bot_token="x",
        admin_telegram_ids_csv="4242",
    )
    user = User(id=1, telegram_id=4242)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    assert await can_access_web_admin(session, settings, user=user)
