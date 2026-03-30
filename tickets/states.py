from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class TicketStates(StatesGroup):
    waiting_problem_text = State()
    waiting_admin_reply_text = State()

