"""FSM для ввода суммы пополнения."""

from aiogram.fsm.state import State, StatesGroup


class TopupStates(StatesGroup):
    waiting_amount_rub = State()
