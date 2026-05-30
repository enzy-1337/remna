"""Единый биллинг-аккаунт (владелец семьи или сам пользователь)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.user import User
from shared.services.family_service import get_family_membership, resolve_account_user


async def resolve_billing_user(session: AsyncSession, user: User) -> User:
    """Баланс и списания — с аккаунта владельца семьи."""
    return await resolve_account_user(session, user)


async def is_family_member(session: AsyncSession, user_id: int) -> bool:
    return await get_family_membership(session, user_id=user_id) is not None
