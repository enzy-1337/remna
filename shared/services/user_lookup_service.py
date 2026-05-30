"""Поиск пользователя бота по Telegram ID или @username."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.user import User


async def find_user_by_tg_or_username(session: AsyncSession, raw: str) -> User | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        text = text[1:].strip()
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return (
            await session.execute(select(User).where(User.telegram_id == int(text)).limit(1))
        ).scalar_one_or_none()
    uname = text.lower()
    return (
        await session.execute(
            select(User).where(func.lower(User.username) == uname).limit(1)
        )
    ).scalar_one_or_none()


def user_card_label(user: User) -> str:
    name = (user.first_name or "").strip()
    un = f"@{user.username}" if user.username else ""
    parts = [p for p in (name, un, f"ID {user.telegram_id}") if p]
    return " · ".join(parts) if parts else f"ID {user.telegram_id}"
