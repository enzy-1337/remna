"""Единый текст и клавиатура карточки «Telegram ID» для ID-бота (MarkdownV2)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from shared.md2 import bold, code, esc, italic, join_lines, link, plain


def cta_keyboard(bot_username: str | None) -> InlineKeyboardMarkup | None:
    """Inline-кнопка в стиле основного бота: 💜 @<bot_username>."""
    uname = (bot_username or "").strip().lstrip("@")
    if not uname:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💜 @{uname}",
                    url=f"https://t.me/{uname}",
                    style="primary",
                )
            ],
        ]
    )


def format_user_telegram_card(
    *,
    name: str,
    username: str | None,
    user_id: int,
    chat_id: int | None = None,
) -> str:
    """Текст как у /start в личке: заголовок, имя, кликабельный тэг, ID в `code`, подсказка."""
    has_chat = chat_id is not None
    title = "Информация о чате" if has_chat else "Ваш Telegram"
    lines: list[str] = [
        "🪪 " + bold(title),
        "",
        plain("👤 ") + bold("Имя") + plain(": ") + esc(name),
    ]
    if username:
        u = username.strip().lstrip("@")
        tag_visible = f"@{u}"
        lines.append(plain("🏷 ") + bold("Тэг") + plain(": ") + link(tag_visible, f"https://t.me/{u}"))
    else:
        lines.append(plain("🏷 ") + bold("Тэг") + plain(": ") + plain("—"))
    lines.append(plain("🆔 ") + bold("Юзер ID") + plain(": ") + code(str(user_id)))
    if has_chat and chat_id is not None:
        lines.append(plain("💬 ") + bold("Чат ID") + plain(": ") + code(str(chat_id)))
    lines.extend(["", italic("Тапните по ID, чтобы скопировать.")])
    return join_lines(*lines)


def format_group_chat_peer_card(
    *,
    chat_id: int,
    title: str,
    chat_type: str,
    username: str | None,
) -> str:
    """Карточка для группы/канала из get_chat (не личный профиль)."""
    lines: list[str] = [
        "🪪 " + bold("Информация о чате"),
        "",
        plain("💬 ") + bold("Чат ID") + plain(": ") + code(str(chat_id)),
        plain("📛 ") + bold("Название") + plain(": ") + esc(title),
        plain("⚙️ ") + bold("Тип") + plain(": ") + esc(str(chat_type)),
    ]
    if username:
        u = username.strip().lstrip("@")
        lines.append(plain("🏷 ") + bold("Тэг") + plain(": ") + link(f"@{u}", f"https://t.me/{u}"))
    lines.extend(["", italic("Тапните по ID, чтобы скопировать.")])
    return join_lines(*lines)
