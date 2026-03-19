"""Общие утилиты для разбора Telegram Update."""

from aiogram.types import Update, User


def user_from_update(update: Update) -> User | None:
    """Извлечь пользователя из апдейта (сообщение, колбэк и т.д.)."""
    if update.message and update.message.from_user:
        return update.message.from_user
    if update.edited_message and update.edited_message.from_user:
        return update.edited_message.from_user
    if update.callback_query and update.callback_query.from_user:
        return update.callback_query.from_user
    if update.inline_query and update.inline_query.from_user:
        return update.inline_query.from_user
    if update.chosen_inline_result and update.chosen_inline_result.from_user:
        return update.chosen_inline_result.from_user
    if update.shipping_query and update.shipping_query.from_user:
        return update.shipping_query.from_user
    if update.pre_checkout_query and update.pre_checkout_query.from_user:
        return update.pre_checkout_query.from_user
    if update.my_chat_member and update.my_chat_member.from_user:
        return update.my_chat_member.from_user
    if update.chat_member and update.chat_member.from_user:
        return update.chat_member.from_user
    return None
