"""Статистика операторов поддержки."""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession


async def list_operator_stats(session: AsyncSession) -> list[dict]:
    rows = (
        await session.execute(
            text(
                """
                SELECT au.id AS operator_id,
                       u.id AS user_id,
                       u.first_name,
                       u.username,
                       u.telegram_id,
                       COALESCE(SUM(tr.value), 0) AS score,
                       COALESCE(SUM(CASE WHEN tr.value > 0 THEN 1 ELSE 0 END), 0) AS likes,
                       COALESCE(SUM(CASE WHEN tr.value < 0 THEN 1 ELSE 0 END), 0) AS dislikes,
                       (
                         SELECT COUNT(*)::int FROM tickets t2
                         WHERE t2.operator_id = au.id AND t2.status = 'closed'
                       ) AS closed_tickets
                FROM admin_users au
                JOIN users u ON u.id = au.user_id
                LEFT JOIN ticket_ratings tr ON tr.operator_id = au.id
                GROUP BY au.id, u.id, u.first_name, u.username, u.telegram_id
                ORDER BY score DESC, closed_tickets DESC, au.id ASC
                """
            )
        )
    ).mappings().all()
    return [dict(r) for r in rows]
