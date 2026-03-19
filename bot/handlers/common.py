"""Общие проверки для хендлеров."""

from __future__ import annotations

from aiogram.types import CallbackQuery, Message

from shared.models.user import User


async def reject_if_blocked(message_or_cq: Message | CallbackQuery, db_user: User | None) -> bool:
    """True — прервать обработку (пользователь заблокирован)."""
    if db_user is None or not db_user.is_blocked:
        return False
    text = "⛔ Ваш аккаунт заблокирован. Обратитесь в поддержку."
    if isinstance(message_or_cq, Message):
        await message_or_cq.answer(text)
    else:
        await message_or_cq.answer(text, show_alert=True)
    return True


async def reject_if_no_user(message_or_cq: Message | CallbackQuery, db_user: User | None) -> bool:
    """True — нет записи в БД, нужен /start."""
    if db_user is not None:
        return False
    text = "Сначала нажмите /start."
    if isinstance(message_or_cq, Message):
        await message_or_cq.answer(text)
    else:
        await message_or_cq.answer(text, show_alert=True)
    return True


def support_telegram_url(username: str | None) -> str | None:
    if not username:
        return None
    u = username.strip().lstrip("@")
    return f"https://t.me/{u}"
