"""Тексты новых фич бота (семья, передача подписки)."""

FAMILY_MENU_TITLE = "👨‍👩‍👧 Семейная подписка"
FAMILY_MENU_HINT = (
    "Общий доступ к подписке, балансу и устройствам владельца.\n"
    "Лимит участников зависит от числа устройств в тарифе (макс. 3 человека включая владельца)."
)
FAMILY_BTN_BIND = "➕ Привязать"
FAMILY_BTN_MEMBERS = "👥 Участники"
FAMILY_BTN_UNBIND = "➖ Отвязать"
FAMILY_BTN_LEAVE = "🚪 Выйти из семьи"
FAMILY_BTN_KICK = "❌ Исключить"

FAMILY_ASK_TARGET = "Введите Telegram ID или @username пользователя для привязки:"
FAMILY_ASK_UNBIND = "Введите Telegram ID или @username участника для отвязки:"
FAMILY_NOT_FOUND = "Пользователь не найден. Он должен хотя бы раз написать боту /start."
FAMILY_BIND_OK_OWNER = "✅ Пользователь {label} привязан к семейной подписке."
FAMILY_BIND_OK_MEMBER = "✅ Вас добавили в семейную подписку пользователя {label}."
FAMILY_UNBIND_OK_OWNER = "✅ Участник {label} отвязан от семейной подписки."
FAMILY_UNBIND_OK_MEMBER = "ℹ️ Вас отвязали от семейной подписки."
FAMILY_LEAVE_OK = "✅ Вы вышли из семейной подписки."

TRANSFER_MENU_BTN = "📤 Передать подписку"
TRANSFER_ASK_TARGET = "Введите Telegram ID или @username получателя подписки:"
TRANSFER_CONFIRM_HINT = (
    "Получатель: {label}\n"
    "Подписка до: {expires}\n"
    "Устройств: {devices}\n"
    "Все ваши устройства будут отвязаны. Продолжить?"
)
TRANSFER_OK_SENDER = "✅ Подписка передана пользователю {label}."
TRANSFER_OK_RECIPIENT = "✅ Вам передана подписка. {details}"
TRANSFER_ONLY_OWNER = "Передать подписку может только её владелец."
