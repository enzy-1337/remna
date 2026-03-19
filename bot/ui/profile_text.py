"""Тексты главного экрана «Профиль»."""

from __future__ import annotations

import html

from aiogram.types import User as TgUser

from shared.models.user import User


def profile_caption_html(db_user: User, tg_user: TgUser) -> str:
    name = html.escape(tg_user.first_name or db_user.first_name or "—")
    bal = html.escape(str(db_user.balance))
    return (
        "👤 <b>Профиль:</b>\n\n"
        f"📝 Имя: <b>{name}</b>\n"
        f"🆔 ID: <code>{tg_user.id}</code>\n"
        f"💳 Баланс: <b>{bal}</b> ₽"
    )
