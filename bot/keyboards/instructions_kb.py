"""Клавиатура экрана «Инструкции» (Telegra.ph + fallback по платформам)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shared.config import Settings


def build_instructions_markup(
    settings: Settings,
    *,
    back_callback: str,
    back_text: str,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    ph = (settings.instruction_telegraph_phone_url or "").strip()
    pc = (settings.instruction_telegraph_pc_url or "").strip()
    if ph and pc:
        b.row(
            InlineKeyboardButton(text="📱 Телефон (Telegra.ph)", url=ph),
            InlineKeyboardButton(text="💻 Компьютер (Telegra.ph)", url=pc),
        )
    else:
        if ph:
            b.row(InlineKeyboardButton(text="📱 Телефон (Telegra.ph)", url=ph))
        if pc:
            b.row(InlineKeyboardButton(text="💻 Компьютер (Telegra.ph)", url=pc))
    if not ph and not pc:
        if settings.instruction_android_url:
            b.row(InlineKeyboardButton(text="🤖 Android", url=settings.instruction_android_url))
        if settings.instruction_ios_url:
            b.row(InlineKeyboardButton(text="🍎 iOS", url=settings.instruction_ios_url))
        if settings.instruction_windows_url:
            b.row(InlineKeyboardButton(text="🪟 Windows", url=settings.instruction_windows_url))
        if settings.instruction_macos_url:
            b.row(InlineKeyboardButton(text="💻 macOS", url=settings.instruction_macos_url))
    b.row(InlineKeyboardButton(text=back_text, callback_data=back_callback))
    return b.as_markup()
