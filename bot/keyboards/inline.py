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
) -> InlineKeyboardMarkup:
    """Главное меню по ТЗ (inline)."""
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
    if support_url:
        builder.row(
            InlineKeyboardButton(text="💬 Поддержка", url=support_url),
            InlineKeyboardButton(text="ℹ️ О сервисе", callback_data="menu:about"),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="💬 Поддержка", callback_data="menu:support"),
            InlineKeyboardButton(text="ℹ️ О сервисе", callback_data="menu:about"),
        )
    return builder.as_markup()


def submenu_back_keyboard() -> InlineKeyboardMarkup:
    """Только «⬅️ Главное меню» для заглушек разделов."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))
    return builder.as_markup()


def topup_amounts_keyboard() -> InlineKeyboardMarkup:
    """Быстрые суммы пополнения."""
    builder = InlineKeyboardBuilder()
    for a in (100, 200, 300, 500):
        builder.row(InlineKeyboardButton(text=f"{a} ₽", callback_data=f"topup:amt:{a}"))
    builder.row(InlineKeyboardButton(text="✏️ Другая сумма", callback_data="topup:custom"))
    builder.row(
        InlineKeyboardButton(text="⬅️ Профиль", callback_data="menu:main"),
    )
    return builder.as_markup()


def topup_invoice_done_keyboard() -> InlineKeyboardMarkup:
    """После создания счёта."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ К балансу", callback_data="menu:balance"))
    builder.row(InlineKeyboardButton(text="🏠 Профиль", callback_data="menu:main"))
    return builder.as_markup()


def topup_providers_keyboard(amount_rub: int) -> InlineKeyboardMarkup:
    """Выбор провайдера после суммы."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🪙 CryptoBot (крипто)",
            callback_data=f"topup:prov:cryptobot:{amount_rub}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="💳 Platega (карта / СБП)",
            callback_data=f"topup:prov:platega:{amount_rub}",
        )
    )
    builder.row(InlineKeyboardButton(text="⬅️ К суммам", callback_data="topup:back_amt"))
    return builder.as_markup()
