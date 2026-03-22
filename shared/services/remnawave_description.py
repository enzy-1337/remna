"""Текст поля «description» пользователя в панели Remnawave."""

from __future__ import annotations

import re

from shared.models.user import User


def build_remnawave_panel_description(user: User, *, max_len: int = 900) -> str:
    """Многострочное описание: кто это в боте / Telegram (ID уже в строке «Telegram ID: …»)."""
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

    body = "\n".join(lines)
    if len(body) <= max_len:
        return body
    cap = max(24, max_len)
    trimmed = body[:cap].rstrip()
    if len(trimmed) < len(body):
        trimmed = trimmed[:-1].rstrip() + "…"
    return trimmed


def normalize_remnawave_description(text: str) -> str:
    """Сравнение «есть ли смысл пушить в API заново»."""
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t
