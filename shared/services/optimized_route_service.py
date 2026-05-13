"""Оптимизированный маршрут: squad в Remnawave + надбавка за ГБ (billing v2)."""

from __future__ import annotations

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient
from shared.models.user import User
from shared.services.remnawave_user_panel_sync import update_rw_user_respecting_hwid_limit

# Internal squad «sub-opt» в Remnawave: выдаётся всем в `activeInternalSquads`
# (раньше для «обычного» маршрута использовался REMNAWAVE_DEFAULT_SQUAD_UUID / «sub»).
SUB_OPT_INTERNAL_SQUAD_UUID = "e614816c-de57-49f5-b93f-c6e2cbe8ef56"


def remnawave_squads_for_db_user(settings: Settings, user: User) -> list[str] | None:
    """Список `activeInternalSquads`: один squad sub-opt для всех.

    Переключатель «оптимизированный маршрут» у пользователя влияет только на надбавку
    за ГБ в биллинге, не на состав сквада. Опционально: переопределение через
    REMNAWAVE_OPTIMIZED_SQUAD_UUID в .env.
    """
    env_opt = (settings.remnawave_optimized_squad_uuid or "").strip()
    if env_opt:
        return [env_opt]
    return [SUB_OPT_INTERNAL_SQUAD_UUID]


def optimized_route_panel_ready(settings: Settings) -> bool:
    """Можно переключать надбавку за оптимизированный маршрут (сквад в коде / .env)."""
    return not settings.remnawave_stub


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
