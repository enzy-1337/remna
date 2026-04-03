"""Совместимость схемы ticket_messages (колонка photo_file_id после миграции 0005)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def ticket_messages_has_photo_file_id_column(session: AsyncSession) -> bool:
    """True, если в БД уже есть колонка photo_file_id (миграция применена)."""
    r = await session.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'ticket_messages'
                  AND column_name = 'photo_file_id'
            )
            """
        )
    )
    val = r.scalar()
    return bool(val)
