"""FSM для админ-панели."""

from aiogram.fsm.state import State, StatesGroup


class AdminFindUserStates(StatesGroup):
    waiting_telegram_id = State()


class AdminSubscriptionStates(StatesGroup):
    waiting_add_months = State()
    waiting_add_balance = State()


class AdminSecurityStates(StatesGroup):
    waiting_totp_enable_code = State()
    waiting_totp_disable_code = State()


class AdminFactoryResetStates(StatesGroup):
    """Подтверждение полного сброса БД (три проверки по данным Telegram)."""

    waiting_first_name = State()
    waiting_username = State()
    waiting_telegram_numeric_id = State()


class AdminBroadcastStates(StatesGroup):
    """Общая рассылка: текст → подтверждение → отправка."""

    waiting_text = State()
    waiting_confirm = State()
