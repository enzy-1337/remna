"""Маршруты web-admin → permission, пункты меню."""

from __future__ import annotations

from shared.models.admin_role import ADMIN_PERMISSIONS

# (href, icon, label, permission | None = любой админ, "superadmin" = только супер-админ)
ADMIN_NAV_ITEMS: tuple[tuple[str, str, str, str | None], ...] = (
    ("/admin/dashboard", "fa-solid fa-chart-pie", "Дашборд", "view_stats"),
    ("/admin/topups", "fa-solid fa-money-bill-transfer", "Пополнения", "view_stats"),
    ("/admin/status", "fa-solid fa-heart-pulse", "Статус", "view_stats"),
    ("/admin/users", "fa-solid fa-users", "Пользователи", "view_users"),
    ("/admin/tickets", "fa-solid fa-headset", "Тикеты", "manage_tickets"),
    ("/admin/subscriptions", "fa-solid fa-clock-rotate-left", "Подписки", "edit_subscriptions"),
    ("/admin/tariffs", "fa-solid fa-tags", "Тарифы", "edit_subscriptions"),
    ("/admin/promos", "fa-solid fa-ticket", "Промокоды", "edit_subscriptions"),
    ("/admin/broadcast", "fa-solid fa-bullhorn", "Рассылка", "edit_subscriptions"),
    ("/admin/admins", "fa-solid fa-user-shield", "Администраторы", "superadmin"),
    ("/admin/roles", "fa-solid fa-key", "Роли", "superadmin"),
    ("/admin/settings", "fa-solid fa-gear", "Настройки", "superadmin"),
)

ADMIN_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/admin/login",
)

ADMIN_OPEN_PATHS: frozenset[str] = frozenset(
    {
        "/admin",
        "/admin/profile",
    }
)


def permission_labels() -> dict[str, str]:
    return {
        "view_users": "Просмотр пользователей",
        "edit_subscriptions": "Изменение подписок",
        "manage_tickets": "Управление тикетами",
        "view_stats": "Просмотр статистики",
        "manage_admins": "Управление администраторами",
    }


def required_permission_for_request(method: str, path: str) -> str | None:
    """None — доступ любому авторизованному админу."""
    p = (path or "").rstrip("/") or "/"
    for prefix in ADMIN_PUBLIC_PREFIXES:
        if p.startswith(prefix):
            return None
    if p in ADMIN_OPEN_PATHS or p.startswith("/admin/profile"):
        return None

    m = (method or "GET").upper()
    if m != "GET" and p.startswith("/admin/users"):
        return "edit_subscriptions"
    if m != "GET" and (
        p.startswith("/admin/subscriptions")
        or p.startswith("/admin/tariffs")
        or p.startswith("/admin/promos")
        or p.startswith("/admin/broadcast")
    ):
        return "edit_subscriptions"

    rules: tuple[tuple[str, str], ...] = (
        ("/admin/admins", "superadmin"),
        ("/admin/roles", "superadmin"),
        ("/admin/settings", "superadmin"),
        ("/admin/users", "view_users"),
        ("/admin/tickets", "manage_tickets"),
        ("/admin/subscriptions", "edit_subscriptions"),
        ("/admin/tariffs", "edit_subscriptions"),
        ("/admin/promos", "edit_subscriptions"),
        ("/admin/broadcast", "edit_subscriptions"),
        ("/admin/dashboard", "view_stats"),
        ("/admin/topups", "view_stats"),
        ("/admin/status", "view_stats"),
    )
    for prefix, perm in rules:
        if p == prefix or p.startswith(prefix + "/"):
            return perm
    return "view_stats"


def has_nav_access(perm: str | None, *, permissions: set[str], is_superadmin: bool) -> bool:
    if perm is None:
        return True
    if perm == "superadmin":
        return is_superadmin
    if is_superadmin:
        return True
    return perm in permissions


def session_has_permission(
    session_data: dict,
    permission: str | None,
    *,
    is_superadmin: bool,
    permissions: set[str],
) -> bool:
    if permission is None:
        return True
    if permission == "superadmin":
        return is_superadmin
    if is_superadmin:
        return True
    return permission in permissions
