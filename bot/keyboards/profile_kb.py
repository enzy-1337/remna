"""Главный экран — профиль."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def profile_main_keyboard(
    *,
    show_trial: bool,
    support_url: str | None,
    is_admin: bool = False,
    show_welcome_topup: bool = False,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if show_welcome_topup:
        b.row(
            InlineKeyboardButton(
                text="💳 Оплатить 10 ₽",
                callback_data="topup:amt:10",
                style="success",
            )
        )
    if show_trial:
        b.row(
            InlineKeyboardButton(
                text="🎁 Активировать триал",
                callback_data="trial:activate",
                style="success",
            )
        )
    b.row(
        InlineKeyboardButton(
            text="🔑 Моя подписка", callback_data="menu:sub_main", style="primary"
        )
    )
    b.row(
        InlineKeyboardButton(text="👥 Рефералы", callback_data="menu:referrals", style="primary"),
        InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance", style="primary"),
    )
    b.row(
        InlineKeyboardButton(text="🎁 Промокод", callback_data="menu:promo", style="primary"),
        InlineKeyboardButton(text="ℹ️ О сервисе", callback_data="menu:info", style="primary"),
    )
    if is_admin:
        b.row(
            InlineKeyboardButton(
                text="🛠 Админ-панель", callback_data="admin:panel", style="primary"
            )
        )
    return b.as_markup()
