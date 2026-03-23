"""FSM для админ-управления промокодами."""

from aiogram.fsm.state import State, StatesGroup


class AdminPromoStates(StatesGroup):
    # Create flow
    create_waiting_code = State()
    create_waiting_type = State()
    create_waiting_value = State()
    create_waiting_fallback = State()
    create_waiting_expires_at = State()
    create_waiting_max_uses = State()
    create_waiting_active = State()

    # Edit flow
    edit_choosing_field = State()
    edit_waiting_type = State()
    edit_waiting_value = State()
    edit_waiting_fallback = State()
    edit_waiting_expires_at = State()
    edit_waiting_max_uses = State()

