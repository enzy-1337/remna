from aiogram.fsm.state import State, StatesGroup


class PromoStates(StatesGroup):
    waiting_code = State()
