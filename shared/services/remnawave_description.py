"""Текст поля «description» пользователя в панели Remnawave (читаемо + маркер tg_id)."""

from __future__ import annotations

import re

from shared.models.user import User

# Для поиска в find_user_by_telegram_id — подстрока должна сохраняться
_TG_MARKER = "tg_id:{}"


def build_remnawave_panel_description(user: User, *, max_len: int = 900) -> str:
    """
    Многострочное описание: кто это в боте / Telegram.
    В конце всегда строка tg_id:<число> (нужна для поиска и сопоставления).
    """
    lines: list[str] = [
        "Бот",
        f"ID в боте: #{user.id}",
        f"Telegram ID: {user.telegram_id}",
    ]
    un = (user.username or "").strip().lstrip("@")
    lines.append(f"Username: @{un}" if un else "Username: —")

    fn = (user.first_name or "").strip()
    ln = (user.last_name or "").strip()
    parts_name = [x for x in (fn, ln) if x]
    lines.append(f"Имя и фамилия: {' '.join(parts_name)}" if parts_name else "Имя и фамилия: —")

    ph = (user.phone or "").strip()
    if ph:
        lines.append(f"Телефон: {ph}")

    marker = _TG_MARKER.format(user.telegram_id)
    body = "\n".join(lines)
    full = f"{body}\n{marker}"
    if len(full) <= max_len:
        return full
    reserve = len(marker) + 1
    cap = max(24, max_len - reserve)
    trimmed = body[:cap].rstrip()
    if len(trimmed) < len(body):
        trimmed = trimmed[:-1].rstrip() + "…"
    return f"{trimmed}\n{marker}"


def normalize_remnawave_description(text: str) -> str:
    """Сравнение «есть ли смысл пушить в API заново»."""
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t
