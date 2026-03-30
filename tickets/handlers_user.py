from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

import html
from datetime import datetime, timezone

from tickets.keyboards import rating_keyboard, start_keyboard, topic_ticket_keyboard
from tickets.states import TicketStates
from tickets.services import create_ticket, ensure_db_user, get_active_ticket_id, save_ticket_rating, set_ticket_topic
from tickets.config import config

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


@router.message(TicketStates.waiting_problem_text, F.text)
async def msg_problem_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if message.from_user is None:
        return
    raw = (message.text or "").strip()
    if not raw:
        return
    db_user = await ensure_db_user(session, message.from_user)
    active_id = await get_active_ticket_id(session, user_id=db_user.id)
    if active_id is not None:
        await state.clear()
        await message.answer(f"У вас уже есть активный тикет #{active_id}.")
        return

    ticket_id = await create_ticket(
        session,
        user=db_user,
        telegram_user_id=int(message.from_user.id),
        text_body=raw,
    )

    # Создаём топик в супергруппе (forum topics).
    disp = (message.from_user.full_name or "Пользователь").strip()
    title = f"Тикет #{ticket_id} — {disp}"
    title = title[:128]
    topic = await message.bot.create_forum_topic(chat_id=config.support_group_id, name=title)
    await set_ticket_topic(session, ticket_id=ticket_id, topic_id=int(topic.message_thread_id))

    me = await message.bot.get_me()
    kb = topic_ticket_keyboard(bot_username=me.username or "", ticket_id=ticket_id)
    created_line = "Дата: " + datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    user_line = f"<a href=\"tg://user?id={int(message.from_user.id)}\">{disp}</a>"
    un = ("@" + message.from_user.username) if message.from_user.username else ""
    cap = (
        f"<b>🎫 Тикет #{ticket_id}</b>\n"
        f"Пользователь: {user_line} {un}\n"
        f"{created_line}\n\n"
        f"{html.escape(raw)}"
    )
    await message.bot.send_message(
        chat_id=config.support_group_id,
        message_thread_id=int(topic.message_thread_id),
        text=cap,
        reply_markup=kb,
        disable_web_page_preview=True,
    )

    await state.clear()
    await message.answer(f"✅ Ваш тикет #{ticket_id} создан. Ожидайте ответа от администратора.")


@router.callback_query(F.data.startswith("tickets:rate:"))
async def cb_rate_ticket(cq: CallbackQuery, session: AsyncSession) -> None:
    if cq.from_user is None:
        await cq.answer()
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 4:
        await cq.answer("Некорректные данные.", show_alert=True)
        return
    try:
        ticket_id = int(parts[2])
        rating_bool = parts[3] == "1"
    except Exception:
        await cq.answer("Некорректные данные.", show_alert=True)
        return

    ok = await save_ticket_rating(session, ticket_id=ticket_id, rating=rating_bool)
    if not ok:
        await cq.answer("Оценка уже сохранена.", show_alert=True)
        return

    await cq.answer("Спасибо за оценку!")
    if cq.message:
        label = "👍" if rating_bool else "👎"
        try:
            await cq.message.edit_text(f"Спасибо! Ваша оценка по тикету #{ticket_id}: {label}", reply_markup=None)
        except Exception:
            pass

