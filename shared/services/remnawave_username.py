"""Формирование username для Remnawave (3–36 символов, [a-zA-Z0-9_-])."""

from __future__ import annotations

import re

from aiogram.types import User as TgUser


def build_remnawave_username_from_db_user(user) -> str:
    """Username для Remnawave из записи БД (без объекта Telegram User)."""
    # user: shared.models.user.User
    if user.username:
        u = user.username.strip().lower()
        u = re.sub(r"[^a-z0-9_-]", "_", u)
        u = u.strip("_") or f"u{user.telegram_id}"
        if len(u) < 3:
            u = f"{u}_{user.telegram_id}"[:36]
        return u[:36]
    return f"tg_{user.telegram_id}"[-36:]


def build_remnawave_username(tg_user: TgUser) -> str:
    """
    По ТЗ: @username → phone_* → tg_{id}.
    Remnawave: 3–36 символов, только буквы/цифры/_/- .
    """
    if tg_user.username:
        u = tg_user.username.strip().lower()
        u = re.sub(r"[^a-z0-9_-]", "_", u)
        u = u.strip("_") or f"u{tg_user.id}"
        if len(u) < 3:
            u = f"{u}_{tg_user.id}"[:36]
        return u[:36]

    # Телефон в aiogram User обычно недоступен без запроса contact — опускаем
    return f"tg_{tg_user.id}"[-36:]
