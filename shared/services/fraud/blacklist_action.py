"""Немедленная блокировка пользователя, попавшего во внешний/ручной чёрный список Telegram ID.

Общий код для двух точек входа: регистрация нового пользователя
(shared/services/user_registration.py) и фоновый синк (blacklist_sync.py).
Внешний список — уже провалидированный источник (не эвристика), поэтому staged
rollout здесь не применяется: force_auto_block=True всегда.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import plain
from shared.models.user import User
from shared.services.fraud.incident_service import record_incident


async def apply_blacklist_block(
    session: AsyncSession,
    settings: Settings,
    user: User,
    *,
    source: str,
) -> None:
    await record_incident(
        session,
        settings,
        user=user,
        detector="blacklist",
        severity="hard_violation",
        confidence=1.0,
        reason_text=plain(f"Telegram ID найден во внешнем чёрном списке (источник: {source})."),
        evidence={"source": source, "telegram_id": user.telegram_id},
        force_auto_block=True,
    )
