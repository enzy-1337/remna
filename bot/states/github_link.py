from aiogram.fsm.state import State, StatesGroup


class GithubLinkStates(StatesGroup):
    waiting_username = State()
