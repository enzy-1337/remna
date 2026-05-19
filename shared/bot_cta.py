"""Рекламная inline-кнопка «перейти в основной бот» (💜 VPN · @username)."""

from __future__ import annotations

import os

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

DEFAULT_BOT_CTA_LABEL = "💜 VPN · @{username}"


def resolve_bot_cta_label(label_template: str | None = None) -> str:
    """Шаблон подписи кнопки: {username} — username бота без @."""
    if label_template is not None and label_template.strip():
        return label_template.strip()
    return (os.getenv("BOT_CTA_LABEL") or DEFAULT_BOT_CTA_LABEL).strip() or DEFAULT_BOT_CTA_LABEL


def normalize_bot_username(bot_username: str | None) -> str | None:
    uname = (bot_username or "").strip().lstrip("@")
    return uname or None


def format_bot_cta_label(
    bot_username: str | None,
    *,
    label_template: str | None = None,
) -> str | None:
    uname = normalize_bot_username(bot_username)
    if not uname:
        return None
    return resolve_bot_cta_label(label_template).format(username=uname)


def build_bot_cta_keyboard(
    bot_username: str | None,
    *,
    label_template: str | None = None,
) -> InlineKeyboardMarkup | None:
    uname = normalize_bot_username(bot_username)
    label = format_bot_cta_label(uname, label_template=label_template)
    if not uname or not label:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    url=f"https://t.me/{uname}",
                    style="primary",
                )
            ],
        ]
    )
