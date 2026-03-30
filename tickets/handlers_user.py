from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from tickets.keyboards import start_keyboard
from tickets.states import TicketStates
from tickets.services import ensure_db_user, get_active_ticket_id

router = Router(name="tickets_user")


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    db_user = await ensure_db_user(session, message.from_user)
    active_id = await get_active_ticket_id(session, user_id=db_user.id)
    kb = start_keyboard(has_active_ticket=active_id is not None, active_ticket_id=active_id)
    text = (
        "👋 Добро пожаловать в поддержку Flux Network.\n\n"
        "Здесь вы можете создать тикет и получить ответ администратора."
    )
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "tickets:noop")
async def cb_noop(cq: CallbackQuery) -> None:
    await cq.answer()


@router.callback_query(F.data == "tickets:create")
async def cb_create_ticket(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if cq.from_user is None:
        await cq.answer()
        return
    db_user = await ensure_db_user(session, cq.from_user)
    active_id = await get_active_ticket_id(session, user_id=db_user.id)
    if active_id is not None:
        await cq.answer(f"У вас уже есть активный тикет #{active_id}.", show_alert=True)
        return

    await state.set_state(TicketStates.waiting_problem_text)
    await cq.answer()
    if cq.message:
        prompt = (
            "🎫 Создание тикета\n\n"
            "Опишите проблему одним сообщением. Это сообщение будет основным управляющим экраном."
        )
        await cq.message.answer(prompt)

