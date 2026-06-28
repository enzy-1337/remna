"""Список HWID-устройств из панели Remnawave."""

from __future__ import annotations

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.integrations.rw_hwid_devices import (
    hwid_device_title,
    normalize_hwid_devices_list,
)
from shared.integrations.rw_traffic import extract_connected_devices_from_rw_user, is_rw_hwid_devices_unlimited
from shared.models.user import User


async def fetch_panel_hwid_context(
    user: User, settings: Settings
) -> tuple[dict | None, list[dict], str | None]:
    if user.remnawave_uuid is None:
        return None, [], "Remnawave не привязан к профилю."
    rw = RemnaWaveClient(settings)
    uinf: dict | None = None
    try:
        uinf = await rw.get_user(str(user.remnawave_uuid))
    except RemnaWaveError:
        uinf = None
    try:
        raw = await rw.get_user_hwid_devices(str(user.remnawave_uuid))
    except RemnaWaveError as e:
        return uinf, [], str(e)
    return uinf, normalize_hwid_devices_list(raw), None


def connected_devices_count(uinf: dict | None, devices: list[dict]) -> int:
    used = len(devices)
    if uinf is not None and used == 0:
        n_alt = extract_connected_devices_from_rw_user(uinf)
        if n_alt is not None:
            used = int(n_alt)
    return used


def panel_devices_unlimited(uinf: dict | None, *, is_bot_admin: bool) -> bool:
    if is_bot_admin:
        return True
    return uinf is not None and is_rw_hwid_devices_unlimited(uinf)


async def heal_legacy_unlimited_hwid_limit(
    user: User,
    settings: Settings,
    uinf: dict | None,
    devices_count: int,
) -> dict | None:
    """Чинит легаси-аккаунты: в панели ``hwidDeviceLimit`` бывает ``null`` (без лимита)
    у пользователей, заведённых до появления лимита по HWID — лимит для них никогда
    не выставлялся, а не был осознанно снят админом. Для обычных (не безлимитных)
    пользователей принудительно проставляем реальный лимит из подписки.
    """
    if uinf is None or not is_rw_hwid_devices_unlimited(uinf):
        return uinf
    if user.remnawave_uuid is None:
        return uinf
    rw = RemnaWaveClient(settings)
    try:
        await rw.update_user(str(user.remnawave_uuid), hwid_device_limit=int(devices_count))
    except RemnaWaveError:
        return uinf
    healed = dict(uinf)
    healed["hwidDeviceLimit"] = int(devices_count)
    return healed


def device_display_title(d: dict, index: int) -> str:
    return hwid_device_title(d, index)
