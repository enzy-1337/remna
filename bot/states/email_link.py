from aiogram.fsm.state import State, StatesGroup


class EmailLinkStates(StatesGroup):
    waiting_email = State()
    waiting_code = State()
