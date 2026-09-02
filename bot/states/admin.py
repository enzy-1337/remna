"""FSM для админ-панели."""

from aiogram.fsm.state import State, StatesGroup


class AdminFindUserStates(StatesGroup):
    waiting_telegram_id = State()


class AdminSubscriptionStates(StatesGroup):
    waiting_add_months = State()
    waiting_add_days = State()
    waiting_add_balance = State()
    waiting_custom_month_price = State()
    waiting_personal_discount = State()
    waiting_rw_lookup_query = State()
    waiting_manual_bind_subscription_id = State()
    waiting_grant_days = State()


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


class AdminMassGrantStates(StatesGroup):
    """Массовая выдача баланса/дней: тип → аудитория → фильтр по подписке → сумма → подтверждение."""

    waiting_audience = State()
    waiting_filter_days = State()
    waiting_amount = State()
    waiting_confirm = State()


class AdminLogsStates(StatesGroup):
    """Поиск по вкладке "Логи" (app_error_logs или notifications_log — см. logs_tab в FSM data)."""

    waiting_query = State()
