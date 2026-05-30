"""FSM: семейная подписка и передача."""

from aiogram.fsm.state import State, StatesGroup


class FamilyStates(StatesGroup):
    waiting_bind_target = State()
    waiting_unbind_target = State()


class TransferStates(StatesGroup):
    waiting_recipient = State()
    confirm = State()
