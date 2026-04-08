"""Inline-клавиатуры (канал, общие паттерны)."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def channel_required_keyboard(channel_username: str) -> InlineKeyboardMarkup:
    """Кнопки: открыть канал и «Я подписался»."""
    username = channel_username.lstrip("@")
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📢 Подписаться на канал",
            url=f"https://t.me/{username}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✅ Я подписался",
            callback_data="channel:check",
        )
    )
    return builder.as_markup()


def main_menu_keyboard(
    *,
    show_trial: bool,
    support_url: str | None = None,
    is_admin: bool = False,
) -> InlineKeyboardMarkup:
    """Главное меню по ТЗ (inline). support_url/is_admin — для совместимости сигнатуры."""
    _ = (support_url, is_admin)
    builder = InlineKeyboardBuilder()
    if show_trial:
        builder.row(
            InlineKeyboardButton(
                text="🎁 Активировать триал (3 дня / 1 ГБ)",
                callback_data="trial:activate",
            )
        )
    builder.row(
        InlineKeyboardButton(text="🔑 Моя подписка", callback_data="menu:subscription"),
        InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"),
    )
    builder.row(
        InlineKeyboardButton(text="🖥️ Устройства", callback_data="menu:devices"),
        InlineKeyboardButton(text="👥 Рефералы", callback_data="menu:referrals"),
    )
    builder.row(
        InlineKeyboardButton(text="🎁 Промокод", callback_data="menu:promo"),
        InlineKeyboardButton(text="📖 Инструкции", callback_data="menu:instructions"),
    )
    builder.row(InlineKeyboardButton(text="ℹ️ Информация", callback_data="menu:info"))
    return builder.as_markup()


def submenu_back_keyboard() -> InlineKeyboardMarkup:
    """Только «⬅️ Главное меню» для заглушек разделов."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))
    return builder.as_markup()


def topup_amounts_keyboard() -> InlineKeyboardMarkup:
    """Быстрые суммы пополнения: сетка 100|200, 300|500, затем свои строки."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="10 ₽", callback_data="topup:amt:10"),
        InlineKeyboardButton(text="100 ₽", callback_data="topup:amt:100"),
    )
    builder.row(
        InlineKeyboardButton(text="200 ₽", callback_data="topup:amt:200"),
        InlineKeyboardButton(text="300 ₽", callback_data="topup:amt:300"),
    )
    builder.row(
        InlineKeyboardButton(text="500 ₽", callback_data="topup:amt:500"),
        InlineKeyboardButton(text="1000 ₽", callback_data="topup:amt:1000"),
    )
    builder.row(InlineKeyboardButton(text="✏️ Другая сумма", callback_data="topup:custom"))
    builder.row(InlineKeyboardButton(text="⬅️ Профиль", callback_data="menu:main"))
    return builder.as_markup()


def topup_invoice_keyboard(pay_url: str, *, txn_id: int) -> InlineKeyboardMarkup:
    """После создания счёта: оплата по URL, ручная проверка и возврат к балансу."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Открыть страницу оплаты", url=pay_url))
    builder.row(
        InlineKeyboardButton(
            text="🔎 Проверить зачисление",
            callback_data=f"topup:check:{txn_id}",
        )
    )
    builder.row(InlineKeyboardButton(text="⬅️ К балансу", callback_data="menu:balance"))
    return builder.as_markup()


def topup_providers_keyboard(amount_rub: int) -> InlineKeyboardMarkup:
    """Выбор провайдера после суммы."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🪙 CryptoBot (Крипта)",
            callback_data=f"topup:prov:cryptobot:{amount_rub}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="💳 Platega (СБП)",
            callback_data=f"topup:prov:platega:{amount_rub}",
        )
    )
    builder.row(InlineKeyboardButton(text="⬅️ К суммам", callback_data="topup:back_amt"))
    return builder.as_markup()
