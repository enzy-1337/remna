"""Извлечение использованного/лимита трафика из ответа Remnawave (поля могут отличаться по версии панели)."""

from __future__ import annotations

from typing import Any


def _bytes_to_gb(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return round(n / (1024**3), 2)


def extract_traffic_gb_from_rw_user(u: dict[str, Any]) -> tuple[float | None, float | None]:
    """
    Возвращает (used_gb, limit_gb).

    По OpenAPI Remnawave (GetUserByUuidResponseDto): расход в ``userTraffic.usedTrafficBytes``,
    накопительно — ``userTraffic.lifetimeUsedTrafficBytes``; лимит — ``trafficLimitBytes`` (0 = без лимита).
    """
    used: float | None = None

    ut = u.get("userTraffic")
    if isinstance(ut, dict):
        # Текущий период / основной счётчик панели
        if ut.get("usedTrafficBytes") is not None:
            used = _bytes_to_gb(ut["usedTrafficBytes"])
        # Fallback: некоторые инсталлы отдают только lifetime
        if used is None and ut.get("lifetimeUsedTrafficBytes") is not None:
            used = _bytes_to_gb(ut["lifetimeUsedTrafficBytes"])

    if used is None:
        for key in (
            "usedTrafficBytes",
            "usedBytes",
            "userTrafficInBytes",
            "totalUsedBytes",
            "consumedTrafficBytes",
            "lifetimeUsedTrafficBytes",
        ):
            if key in u and u[key] is not None:
                used = _bytes_to_gb(u[key])
                if used is not None:
                    break

    if used is None:
        nested = u.get("trafficStatistics") or u.get("statistics") or {}
        if isinstance(nested, dict):
            for key in ("usedBytes", "usedTrafficBytes", "totalUsedBytes", "used", "lifetimeUsedTrafficBytes"):
                if key in nested and nested[key] is not None:
                    used = _bytes_to_gb(nested[key])
                    if used is not None:
                        break

    raw_limit = u.get("trafficLimitBytes")
    limit_gb = _bytes_to_gb(raw_limit) if raw_limit is not None else None
    if raw_limit is not None:
        try:
            if int(float(raw_limit)) == 0:
                limit_gb = None
        except (TypeError, ValueError):
            pass

    return used, limit_gb


def is_rw_traffic_unlimited(u: dict[str, Any]) -> bool:
    """``trafficLimitBytes == 0`` в OpenAPI Remnawave — без лимита трафика."""
    raw = u.get("trafficLimitBytes")
    if raw is None:
        return False
    try:
        return int(float(raw)) == 0
    except (TypeError, ValueError):
        return False


def traffic_limit_gb_for_display(u: dict[str, Any]) -> float | None:
    """
    Лимит трафика в ГБ для отображения «исп/макс».
    Возвращает None, если без лимита (0 байт) или поле отсутствует/некорректно.
    """
    if not u or is_rw_traffic_unlimited(u):
        return None
    return _bytes_to_gb(u.get("trafficLimitBytes"))


def is_rw_hwid_devices_unlimited(u: dict[str, Any]) -> bool:
    """
    ``hwidDeviceLimit`` nullable: null — без лимита устройств (HWID не ограничивает).
    0 также считаем «без лимита» на случай нестандартных ответов.
    """
    if not u:
        return False
    lim = u.get("hwidDeviceLimit")
    if lim is None:
        return True
    try:
        return int(lim) <= 0
    except (TypeError, ValueError):
        return True


def should_apply_hwid_device_limit_to_panel(uinf: dict[str, Any] | None) -> bool:
    """
    False, если в профиле пользователя панели отключён лимит устройств по HWID
    (``hwidDeviceLimit`` = null или ≤ 0): панель не ограничивает устройства по HWID.
    Тогда бот не передаёт ``hwidDeviceLimit`` в API, чтобы не включить ограничение снова.

    При ``uinf is None`` (не удалось получить профиль) — True: синхронизируем как обычно.
    """
    if uinf is None:
        return True
    return not is_rw_hwid_devices_unlimited(uinf)


def rw_hwid_device_max(u: dict[str, Any]) -> int | None:
    """Максимум устройств по панели; None = без лимита (∞)."""
    if not u or is_rw_hwid_devices_unlimited(u):
        return None
    try:
        return int(u["hwidDeviceLimit"])
    except (TypeError, ValueError, KeyError):
        return None


def extract_connected_devices_from_rw_user(u: dict[str, Any]) -> int | None:
    for key in (
        "connectedDevices",
        "connectedClients",
        "activeConnections",
        "usedDevices",
        "activeDevices",
        "clientsCount",
    ):
        val = u.get(key)
        if val is None:
            continue
        try:
            n = int(val)
            if n >= 0:
                return n
        except (TypeError, ValueError):
            continue
    nested = u.get("statistics") or u.get("trafficStatistics") or {}
    if isinstance(nested, dict):
        for key in ("connectedDevices", "activeConnections", "usedDevices"):
            val = nested.get(key)
            if val is None:
                continue
            try:
                n = int(val)
                if n >= 0:
                    return n
            except (TypeError, ValueError):
                continue
    return None
