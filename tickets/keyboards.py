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
        b.row(InlineKeyboardButton(text="❌ Закрыть тикет", callback_data=f"tickets:user_close:{active_ticket_id}"))
    else:
        b.row(InlineKeyboardButton(text="📩 Создать тикет", callback_data="tickets:create"))
    return b.as_markup()


def ticket_cancel_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="tickets:home"))
    return b.as_markup()


def topic_ticket_keyboard(*, bot_username: str, ticket_id: int) -> InlineKeyboardMarkup:
    un = (bot_username or "").lstrip("@")
    deep = f"https://t.me/{un}?start=reply_{ticket_id}" if un else ""
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="💬 Ответить", url=deep) if deep else InlineKeyboardButton(text="💬 Ответить", callback_data=f"tickets:reply:{ticket_id}"),
    )
    b.row(
        InlineKeyboardButton(text="🔄 В работе", callback_data=f"tickets:status:{ticket_id}:in_progress"),
        InlineKeyboardButton(text="🟢 Открыт", callback_data=f"tickets:status:{ticket_id}:open"),
        InlineKeyboardButton(text="✅ Закрыть", callback_data=f"tickets:close:{ticket_id}"),
    )
    b.row(InlineKeyboardButton(text="📌 Статус", callback_data=f"tickets:status_info:{ticket_id}"))
    return b.as_markup()


def rating_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="👍", callback_data=f"tickets:rate:{ticket_id}:1"),
        InlineKeyboardButton(text="👎", callback_data=f"tickets:rate:{ticket_id}:0"),
    )
    return b.as_markup()


def active_ticket_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"🎫 Тикет #{ticket_id} активен", callback_data="tickets:noop"))
    b.row(InlineKeyboardButton(text="❌ Закрыть тикет", callback_data=f"tickets:user_close:{ticket_id}"))
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="tickets:home"))
    return b.as_markup()

