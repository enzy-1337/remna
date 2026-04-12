"""Оптимизированный маршрут: squad в Remnawave + надбавка за ГБ (billing v2)."""

from __future__ import annotations

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient
from shared.models.user import User
from shared.services.subscription_service import update_rw_user_respecting_hwid_limit


def remnawave_squads_for_db_user(settings: Settings, user: User) -> list[str] | None:
    """Список `activeInternalSquads` для панели по флагу пользователя и .env."""
    opt = (settings.remnawave_optimized_squad_uuid or "").strip()
    dft = (settings.remnawave_default_squad_uuid or "").strip()
    if user.optimized_route_enabled and opt:
        return [opt]
    if dft:
        return [dft]
    return None


def optimized_route_panel_ready(settings: Settings) -> bool:
    """Для переключения в боте нужны оба UUID (обычный и оптимизированный squad)."""
    return bool((settings.remnawave_default_squad_uuid or "").strip()) and bool(
        (settings.remnawave_optimized_squad_uuid or "").strip()
    )


async def sync_user_optimized_route_to_panel(
    *,
    user: User,
    settings: Settings,
) -> None:
    """Применить текущий флаг пользователя к `activeInternalSquads` в панели."""
    if user.remnawave_uuid is None or settings.remnawave_stub:
        return
    squads = remnawave_squads_for_db_user(settings, user)
    rw = RemnaWaveClient(settings)
    await update_rw_user_respecting_hwid_limit(
        rw,
        str(user.remnawave_uuid),
        active_internal_squads=squads,
    )
