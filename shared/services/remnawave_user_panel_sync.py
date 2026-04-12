"""Обновление пользователя в панели Remnawave без циклических импортов с subscription_service."""

from __future__ import annotations

from typing import Any

from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.integrations.rw_traffic import should_apply_hwid_device_limit_to_panel


async def update_rw_user_respecting_hwid_limit(
    rw: RemnaWaveClient,
    user_uuid: str,
    *,
    devices_limit_for_panel: int | None = None,
    **kwargs: Any,
) -> None:
    """
    PATCH пользователя в панели. ``hwidDeviceLimit`` добавляется только если в панели
    для этого пользователя не отключён лимит HWID (см. ``should_apply_hwid_device_limit_to_panel``).
    """
    uinf: dict[str, Any] | None = None
    try:
        uinf = await rw.get_user(user_uuid)
    except RemnaWaveError:
        pass
    if devices_limit_for_panel is not None and should_apply_hwid_device_limit_to_panel(uinf):
        kwargs["hwid_device_limit"] = devices_limit_for_panel
    if not kwargs:
        return
    await rw.update_user(user_uuid, **kwargs)
