"""FSM для админ-панели."""

from aiogram.fsm.state import State, StatesGroup


class AdminFindUserStates(StatesGroup):
    waiting_telegram_id = State()
