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
    show_plan_calculator: bool = False,
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
        b.row(InlineKeyboardButton(text="📋 Тарифы", callback_data="sub:plans"))
    else:
        b.row(InlineKeyboardButton(text="📋 Тарифы", callback_data="sub:plans"))
    if show_plan_calculator:
        b.row(InlineKeyboardButton(text="📊 Калькулятор тарифа", callback_data="menu:calc"))
    b.row(
        InlineKeyboardButton(text="👥 Рефералы", callback_data="menu:referrals"),
        InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"),
    )
    b.row(
        InlineKeyboardButton(text="🎁 Промокод", callback_data="menu:promo"),
        InlineKeyboardButton(text="ℹ️ О сервисе", callback_data="menu:info"),
    )
    if is_admin:
        b.row(InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:panel"))
    return b.as_markup()
