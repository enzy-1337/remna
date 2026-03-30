from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.user import User
from shared.services.user_registration import get_user_by_telegram_id, register_user


async def ensure_db_user(session: AsyncSession, tg_user) -> User:
    """Получить/создать пользователя в общей БД (users)."""
    u = await get_user_by_telegram_id(session, int(tg_user.id))
    if u is not None:
        return u
    user, _, _ = await register_user(session, tg_user, None)  # type: ignore[misc]
    return user


async def get_active_ticket_id(session: AsyncSession, *, user_id: int) -> int | None:
    """Активный тикет = status != closed (open/in_progress)."""
    q = await session.execute(
        text(
            """
            SELECT id
            FROM tickets
            WHERE user_id = :uid
              AND status IN ('open','in_progress')
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"uid": user_id},
    )
    row = q.first()
    return int(row[0]) if row else None

