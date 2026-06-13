"""Совместимость схемы ticket_messages (доп. колонки вложений)."""

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


async def ticket_messages_has_video_file_id_column(session: AsyncSession) -> bool:
    """True, если в БД уже есть колонка video_file_id (миграция 0006)."""
    r = await session.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'ticket_messages'
                  AND column_name = 'video_file_id'
            )
            """
        )
    )
    val = r.scalar()
    return bool(val)


async def ticket_messages_has_voice_columns(session: AsyncSession) -> bool:
    """True, если есть voice_file_id / video_note_file_id / audio_file_id (миграция 0036)."""
    r = await session.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'ticket_messages'
                  AND column_name = 'voice_file_id'
            )
            """
        )
    )
    val = r.scalar()
    return bool(val)


async def ticket_messages_has_document_columns(session: AsyncSession) -> bool:
    """True, если есть document_file_id/document_file_name (миграция 0022)."""
    r = await session.execute(
        text(
            """
            SELECT (
                EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'ticket_messages'
                      AND column_name = 'document_file_id'
                )
                AND
                EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'ticket_messages'
                      AND column_name = 'document_file_name'
                )
            )
            """
        )
    )
    val = r.scalar()
    return bool(val)
