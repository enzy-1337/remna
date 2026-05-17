"""Строки для уведомлений о действиях в web-admin (в админ-чат Telegram)."""

from __future__ import annotations

from shared.md2 import link, plain
from shared.models.user import User
from shared.services.admin_notify import format_user_line
from shared.config import Settings

# Главный администратор — единственное имя в логах web-admin.
ENZY_TELEGRAM_ID = 883400626
ENZY_LABEL = "Enzy"


def web_admin_actor_notify_line() -> str:
    return plain("Кто: ") + link(ENZY_LABEL, f"tg://user?id={ENZY_TELEGRAM_ID}")


def web_admin_target_user_line(settings: Settings, user: User) -> str:
    return format_user_line(settings, user)
