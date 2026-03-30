from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def start_keyboard(*, has_active_ticket: bool, active_ticket_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_active_ticket and active_ticket_id is not None:
        b.row(
            InlineKeyboardButton(
                text=f"У вас уже есть активный тикет #{active_ticket_id}",
                callback_data="tickets:noop",
            )
        )
    else:
        b.row(InlineKeyboardButton(text="📩 Создать тикет", callback_data="tickets:create"))
    return b.as_markup()


def ticket_cancel_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="tickets:home"))
    return b.as_markup()

