"""Главный экран — профиль."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def profile_main_keyboard(
    *,
    has_active_sub: bool,
    show_trial: bool,
    support_url: str | None,
    is_admin: bool = False,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if show_trial:
        b.row(
            InlineKeyboardButton(
                text="🎁 Активировать триал",
                callback_data="trial:activate",
            )
        )
    if has_active_sub:
        b.row(InlineKeyboardButton(text="🔑 Моя подписка", callback_data="menu:sub_main"))
    else:
        b.row(InlineKeyboardButton(text="🛒 Купить подписку", callback_data="sub:plans"))
    b.row(
        InlineKeyboardButton(text="👥 Рефералы", callback_data="menu:referrals"),
        InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"),
    )
    if is_admin:
        b.row(
            InlineKeyboardButton(text="ℹ️ Информация", callback_data="menu:info"),
            InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:panel"),
        )
    else:
        b.row(InlineKeyboardButton(text="ℹ️ Информация", callback_data="menu:info"))
    return b.as_markup()
