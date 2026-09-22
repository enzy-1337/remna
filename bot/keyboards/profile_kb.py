"""Главный экран — профиль."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shared.config import get_settings


def profile_main_keyboard(
    *,
    show_trial: bool,
    support_url: str | None,
    is_admin: bool = False,
    show_welcome_topup: bool = False,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _settings = get_settings()
    miniapp_url = (_settings.miniapp_url or "").strip().rstrip("/")
    if not miniapp_url:
        site = (_settings.public_site_url or "").strip().rstrip("/")
        miniapp_url = f"{site}/my" if site else ""
    if miniapp_url:
        b.row(
            InlineKeyboardButton(
                text="🚀 Открыть приложение",
                web_app=WebAppInfo(url=miniapp_url),
            )
        )
    if show_welcome_topup:
        b.row(
            InlineKeyboardButton(
                text="💳 Пополнить баланс",
                callback_data="menu:balance",
            )
        )
    if show_trial:
        b.row(
            InlineKeyboardButton(
                text="🎁 Активировать триал",
                callback_data="trial:activate",
            )
        )
    b.row(
        InlineKeyboardButton(
            text="🔑 Моя подписка", callback_data="menu:sub_main"
        )
    )
    b.row(
        InlineKeyboardButton(text="👥 Рефералы", callback_data="menu:referrals"),
        InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"),
    )
    b.row(
        InlineKeyboardButton(text="🎁 Промокод", callback_data="menu:promo"),
        InlineKeyboardButton(text="ℹ️ О сервисе", callback_data="menu:info"),
    )
    b.row(
        InlineKeyboardButton(text="👨‍👩‍👧 Семейная подписка", callback_data="menu:family"),
    )
    b.row(
        InlineKeyboardButton(text="✉️ Почта для сайта", callback_data="menu:email"),
    )
    if is_admin:
        b.row(
            InlineKeyboardButton(
                text="🛠 Админ-панель", callback_data="admin:panel"
            )
        )
    return b.as_markup()
