from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

import html
from datetime import datetime, timezone

from tickets.keyboards import active_ticket_keyboard, rating_keyboard, start_keyboard, topic_ticket_keyboard
from tickets.states import TicketStates
from tickets.services import create_ticket, ensure_db_user, get_active_ticket_id, get_ticket_brief, save_ticket_rating, set_ticket_status, set_ticket_topic
from tickets.config import config

router = Router(name="tickets_user")


@router.message(CommandStart(deep_link=False))
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


@router.callback_query(F.data == "tickets:home")
async def cb_home(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if cq.from_user is None:
        await cq.answer()
        return
    await state.clear()
    db_user = await ensure_db_user(session, cq.from_user)
    active_id = await get_active_ticket_id(session, user_id=db_user.id)
    kb = start_keyboard(has_active_ticket=active_id is not None, active_ticket_id=active_id)
    text = (
        "👋 Добро пожаловать в поддержку Flux Network.\n\n"
        "Здесь вы можете создать тикет и получить ответ администратора."
    )
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(text, reply_markup=kb)


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
    await message.answer(
        f"✅ Ваш тикет #{ticket_id} создан. Ожидайте ответа от администратора.",
        reply_markup=active_ticket_keyboard(ticket_id),
    )


@router.callback_query(F.data.startswith("tickets:user_close:"))
async def cb_user_close_ticket(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if cq.from_user is None:
        await cq.answer()
        return
    await state.clear()
    db_user = await ensure_db_user(session, cq.from_user)
    active_id = await get_active_ticket_id(session, user_id=db_user.id)
    if active_id is None:
        await cq.answer("Активных тикетов нет.", show_alert=True)
        return
    t = await get_ticket_brief(session, ticket_id=active_id)
    if not t:
        await cq.answer("Тикет не найден.", show_alert=True)
        return
    # Защита от закрытия чужого тикета.
    if int(t.get("telegram_user_id") or 0) != int(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if str(t.get("status") or "") == "closed":
        await cq.answer("Тикет уже закрыт.", show_alert=True)
        return

    await set_ticket_status(session, ticket_id=active_id, status="closed", close_now=True)
    topic_id = int(t.get("topic_id") or 0)
    if topic_id:
        try:
            await cq.bot.close_forum_topic(chat_id=config.support_group_id, message_thread_id=topic_id)
        except Exception:
            pass

    await cq.answer("Тикет закрыт")
    if cq.message:
        await cq.message.edit_text(
            f"Ваш тикет #{active_id} закрыт.\n\nОцените работу поддержки:",
            reply_markup=rating_keyboard(active_id),
        )


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


@router.message(F.text)
async def msg_user_to_active_ticket(message: Message, session: AsyncSession) -> None:
    """Любое новое сообщение пользователя в ЛС -> в топик тикета + в ЛС назначенному админу."""
    if message.from_user is None:
        return
    txt = (message.text or "").strip()
    if not txt:
        return
    if txt.startswith("/"):
        # Команды обрабатываются отдельными роутами.
        return

    db_user = await ensure_db_user(session, message.from_user)
    active_id = await get_active_ticket_id(session, user_id=db_user.id)
    if active_id is None:
        return
    t = await get_ticket_brief(session, ticket_id=active_id)
    if not t or str(t.get("status") or "") == "closed":
        return

    await add_ticket_message(
        session,
        ticket_id=active_id,
        sender_id=db_user.id,
        sender_role="user",
        sender_telegram_id=int(message.from_user.id),
        text_body=txt,
        is_internal=False,
    )
    await bump_ticket_activity(session, ticket_id=active_id, status_to_in_progress=False)

    disp = (message.from_user.full_name or "Пользователь").strip()
    user_line = f"<a href=\"tg://user?id={int(message.from_user.id)}\">{disp}</a>"
    topic_text = (
        f"<b>✉️ Новое сообщение в тикете #{active_id}</b>\n"
        f"От: {user_line}\n\n"
        f"{html.escape(txt)}"
    )

    # В топик тикета.
    try:
        topic_id = int(t.get("topic_id") or 0)
    except Exception:
        topic_id = 0
    if topic_id:
        await message.bot.send_message(
            chat_id=config.support_group_id,
            message_thread_id=topic_id,
            text=topic_text,
            disable_web_page_preview=True,
        )

    # В личку назначенному админу (если тикет уже кто-то взял).
    try:
        admin_tg = int(t.get("telegram_assigned_admin_id") or 0)
    except Exception:
        admin_tg = 0
    if admin_tg:
        me = await message.bot.get_me()
        un = (me.username or "").lstrip("@")
        deep = f"https://t.me/{un}?start=reply_{active_id}" if un else ""
        dm = (
            f"<b>✉️ Пользователь написал в тикет #{active_id}</b>\n"
            f"От: {user_line}\n\n"
            f"{html.escape(txt)}"
            + (f"\n\n<a href=\"{deep}\">Ответить</a>" if deep else "")
        )
        await message.bot.send_message(chat_id=admin_tg, text=dm, disable_web_page_preview=True)

