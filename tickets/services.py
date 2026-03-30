from __future__ import annotations

from datetime import datetime, timezone

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


async def create_ticket(
    session: AsyncSession,
    *,
    user: User,
    telegram_user_id: int,
    text_body: str,
) -> int:
    """Создать тикет и первое сообщение пользователя. topic_id временно 0, обновим после create_forum_topic."""
    now = datetime.now(timezone.utc)
    r = await session.execute(
        text(
            """
            INSERT INTO tickets (user_id, telegram_user_id, status, topic_id, created_at, updated_at, last_activity)
            VALUES (:uid, :tg, 'open', 0, :now, :now, :now)
            RETURNING id
            """
        ),
        {"uid": user.id, "tg": telegram_user_id, "now": now},
    )
    tid = int(r.scalar_one())
    await session.execute(
        text(
            """
            INSERT INTO ticket_messages (ticket_id, sender_id, sender_role, sender_telegram_id, text, created_at, is_internal)
            VALUES (:tid, :sid, 'user', :stg, :txt, :now, false)
            """
        ),
        {"tid": tid, "sid": user.id, "stg": telegram_user_id, "txt": text_body, "now": now},
    )
    return tid


async def set_ticket_topic(session: AsyncSession, *, ticket_id: int, topic_id: int) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            """
            UPDATE tickets
            SET topic_id = :tp, updated_at = :now
            WHERE id = :tid
            """
        ),
        {"tp": topic_id, "now": now, "tid": ticket_id},
    )

