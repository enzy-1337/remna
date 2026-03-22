"""Полное удаление пользователя из БД и (опционально) из Remnawave."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.user import User


async def delete_user_from_app(
    session: AsyncSession,
    *,
    user_id: int,
    settings: Settings,
) -> tuple[bool, str]:
    """
    Удаляет пользователя из PostgreSQL (CASCADE по подпискам, транзакциям и т.д.).
    Сначала пытается удалить учётку в Remnawave, если есть uuid и не REMNAWAVE_STUB.
    """
    user = await session.get(User, user_id)
    if user is None:
        return False, "Пользователь не найден."

    if user.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            await rw.delete_panel_user(str(user.remnawave_uuid))
        except RemnaWaveError as e:
            return (
                False,
                "Не удалось удалить пользователя в Remnawave. Исправьте ошибку или отключите "
                f"учётную запись в панели вручную.\n\n{e}",
            )

    await session.delete(user)
    await session.flush()
    return True, "Пользователь удалён."
