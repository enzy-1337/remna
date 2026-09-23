"""Почта пользователя → поле email в панели Remnawave (после привязки/смены/отвязки)."""

from __future__ import annotations

import logging

from shared.config import Settings, get_settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.user import User

logger = logging.getLogger(__name__)


def panel_email_for_user(user: User) -> str | None:
    """В панель пишем только подтверждённую почту."""
    email = (user.email or "").strip()
    return email if email and user.email_verified_at is not None else None


async def push_user_email_to_remnawave(user: User, settings: Settings | None = None) -> bool:
    """Best-effort: ошибки панели не ломают привязку почты (периодическая синхронизация дотянет позже)."""
    if user.remnawave_uuid is None:
        return False
    s = settings or get_settings()
    try:
        await RemnaWaveClient(s).update_user(str(user.remnawave_uuid), email=panel_email_for_user(user))
    except RemnaWaveError as e:
        logger.warning("RW email push failed user=%s: %s", user.id, e)
        return False
    except Exception:
        logger.exception("RW email push failed user=%s", user.id)
        return False
    return True
