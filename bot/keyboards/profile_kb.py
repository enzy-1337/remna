"""Главный экран — профиль."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def profile_main_keyboard(
    *,
    has_active_sub: bool,
    can_buy_sub: bool,
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
        if can_buy_sub:
            b.row(InlineKeyboardButton(text="🛒 Купить подписку", callback_data="sub:plans"))
    b.row(
        InlineKeyboardButton(text="👥 Рефералы", callback_data="menu:referrals"),
        InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"),
    )
    if is_admin:
        if support_url:
            b.row(
                InlineKeyboardButton(text="💬 Поддержка", url=support_url),
                InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:panel"),
            )
        else:
            b.row(
                InlineKeyboardButton(text="💬 Поддержка", callback_data="menu:support"),
                InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:panel"),
            )
    else:
        if support_url:
            b.row(InlineKeyboardButton(text="💬 Поддержка", url=support_url))
        else:
            b.row(InlineKeyboardButton(text="💬 Поддержка", callback_data="menu:support"))
    return b.as_markup()
