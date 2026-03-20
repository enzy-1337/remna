"""FSM для админ-панели."""

from aiogram.fsm.state import State, StatesGroup


class AdminFindUserStates(StatesGroup):
    waiting_telegram_id = State()


class AdminSubscriptionStates(StatesGroup):
    waiting_add_days = State()
    waiting_add_balance = State()
