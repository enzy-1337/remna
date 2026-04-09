from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import html
from datetime import datetime, timezone

from tickets.keyboards import (
    active_ticket_keyboard,
    rating_keyboard,
    start_keyboard,
    ticket_cancel_keyboard,
    ticket_view_keyboard,
    topic_ticket_keyboard,
)
from tickets.states import TicketStates
from tickets.services import (
    add_ticket_message,
    bump_ticket_activity,
    create_ticket,
    ensure_db_user,
    get_active_ticket_id,
    get_ticket_brief,
    save_ticket_rating,
    set_ticket_status,
    set_ticket_topic,
)
from tickets.config import config

router = Router(name="tickets_user")


def _status_emoji(status: str) -> str:
    st = (status or "").lower()
    if st == "open":
        return "🟢"
    if st == "in_progress":
        return "🟡"
    if st == "closed":
        return "✅"
    return "⚪"


@router.message(CommandStart(deep_link=False))
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    db_user = await ensure_db_user(session, message.from_user)
    active_id = await get_active_ticket_id(session, user_id=db_user.id)
    active_label = None
    if active_id is not None:
        brief = await get_ticket_brief(session, ticket_id=active_id)
        st = str((brief or {}).get("status") or "open")
        active_label = f"{_status_emoji(st)} Тикет #{active_id}"
    kb = start_keyboard(
        has_active_ticket=active_id is not None,
        active_ticket_id=active_id,
        active_ticket_label=active_label,
    )
    if active_id is not None:
        text = (
            "📮 Активные тикеты:\n\n"
            f"• {active_label or f'Тикет #{active_id}'}\n\n"
            "Нажмите тикет, чтобы посмотреть статус или закрыть."
        )
    else:
        text = (
            "👋 Добро пожаловать в поддержку.\n\n"
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
    active_label = None
    if active_id is not None:
        brief = await get_ticket_brief(session, ticket_id=active_id)
        st = str((brief or {}).get("status") or "open")
        active_label = f"{_status_emoji(st)} Тикет #{active_id}"
    kb = start_keyboard(
        has_active_ticket=active_id is not None,
        active_ticket_id=active_id,
        active_ticket_label=active_label,
    )
    if active_id is not None:
        text = (
            "📮 Активные тикеты:\n\n"
            f"• {active_label or f'Тикет #{active_id}'}\n\n"
            "Нажмите тикет, чтобы посмотреть статус или закрыть."
        )
    else:
        text = (
            "👋 Добро пожаловать в поддержку.\n\n"
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
        try:
            await cq.message.delete()
        except Exception:
            pass
        prompt = (
            "🎫 Создание тикета\n\n"
            "Опишите проблему одним сообщением. Это сообщение будет основным управляющим экраном."
        )
        sent = await cq.message.answer(prompt, reply_markup=ticket_cancel_keyboard())
        await state.update_data(ticket_prompt_mid=sent.message_id)


@router.callback_query(F.data == "tickets:create_cancel")
async def cb_create_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cq.answer("Создание тикета отменено")
    if cq.message:
        await cq.message.edit_text(
            "Создание тикета отменено.",
            reply_markup=start_keyboard(has_active_ticket=False),
        )


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
        brief = await get_ticket_brief(session, ticket_id=active_id)
        st = str((brief or {}).get("status") or "open")
        label = f"{_status_emoji(st)} Тикет #{active_id}"
        await message.answer(
            f"У вас уже есть активный тикет #{active_id}.",
            reply_markup=active_ticket_keyboard(active_id, label=label),
        )
        return
    data = await state.get_data()
    prompt_mid = data.get("ticket_prompt_mid")
    if message.bot:
        try:
            await message.delete()
        except Exception:
            pass
        if isinstance(prompt_mid, int):
            try:
                await message.bot.delete_message(message.chat.id, prompt_mid)
            except Exception:
                pass

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
        f"<blockquote>{html.escape(raw)}</blockquote>"
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
        reply_markup=active_ticket_keyboard(ticket_id, label=f"🟢 Тикет #{ticket_id}"),
    )


@router.callback_query(F.data.startswith("tickets:view:"))
async def cb_view_ticket(cq: CallbackQuery, session: AsyncSession) -> None:
    if cq.from_user is None:
        await cq.answer()
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 3:
        await cq.answer("Некорректные данные", show_alert=True)
        return
    try:
        ticket_id = int(parts[2])
    except Exception:
        await cq.answer("Некорректные данные", show_alert=True)
        return
    t = await get_ticket_brief(session, ticket_id=ticket_id)
    if not t:
        await cq.answer("Тикет не найден", show_alert=True)
        return
    if int(t.get("telegram_user_id") or 0) != int(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return
    details = (
        await session.execute(
            text(
                """
                SELECT t.created_at,
                       t.status,
                       t.telegram_assigned_admin_id,
                       t.assigned_admin_id,
                       u.first_name AS admin_first_name,
                       u.username AS admin_username,
                       m.text AS initial_text
                FROM tickets t
                LEFT JOIN users u ON u.id = t.assigned_admin_id
                LEFT JOIN LATERAL (
                  SELECT text FROM ticket_messages
                  WHERE ticket_id = t.id AND sender_role = 'user' AND COALESCE(is_internal,false)=false
                  ORDER BY id ASC
                  LIMIT 1
                ) m ON TRUE
                WHERE t.id = :tid
                """
            ),
            {"tid": ticket_id},
        )
    ).mappings().first()
    if not details:
        await cq.answer("Тикет не найден", show_alert=True)
        return
    status = str(details.get("status") or "unknown")
    st_emoji = _status_emoji(status)
    opened = details.get("created_at")
    opened_s = opened.strftime("%d.%m.%Y %H:%M UTC") if opened is not None else "—"
    assigned_tg = int(details.get("telegram_assigned_admin_id") or 0)
    admin_db_id = details.get("assigned_admin_id")
    adm_first = str(details.get("admin_first_name") or "").strip()
    adm_un = str(details.get("admin_username") or "").strip()
    if admin_db_id:
        admin_line = f"#{int(admin_db_id)} {adm_first or ('@' + adm_un if adm_un else '')}".strip()
    elif assigned_tg > 0:
        admin_line = f"tg://user?id={assigned_tg}"
    else:
        admin_line = "не назначен"
    initial_text = str(details.get("initial_text") or "—").strip()
    text_view = (
        f"{st_emoji} <b>Тикет #{ticket_id}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Админ: {admin_line}\n"
        f"Открыт: {opened_s}\n\n"
        f"<b>Проблема:</b>\n<blockquote>{html.escape(initial_text[:1200])}</blockquote>"
    )
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(
            text_view,
            parse_mode="HTML",
            reply_markup=ticket_view_keyboard(ticket_id),
            disable_web_page_preview=True,
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


@router.message()
async def msg_user_to_active_ticket(message: Message, session: AsyncSession) -> None:
    """Любое новое сообщение пользователя в ЛС -> в топик тикета + в ЛС назначенному админу."""
    if message.from_user is None:
        return
    txt = (message.text or message.caption or "").strip()
    if not txt:
        if message.photo:
            txt = "📷 [Фото без подписи]"
        elif message.video:
            txt = "🎬 [Видео без подписи]"
        elif message.document:
            txt = "📎 [Файл без подписи]"
        elif message.voice:
            txt = "🎤 [Голосовое сообщение]"
        elif message.sticker:
            txt = "🧩 [Стикер]"
        else:
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

    photo_fid: str | None = message.photo[-1].file_id if message.photo else None
    await add_ticket_message(
        session,
        ticket_id=active_id,
        sender_id=db_user.id,
        sender_role="user",
        sender_telegram_id=int(message.from_user.id),
        text_body=txt,
        is_internal=False,
        photo_file_id=photo_fid,
    )
    await bump_ticket_activity(session, ticket_id=active_id, status_to_in_progress=False)

    disp = (message.from_user.full_name or "Пользователь").strip()
    user_line = f"<a href=\"tg://user?id={int(message.from_user.id)}\">{disp}</a>"
    topic_text = (
        f"<b>✉️ Новое сообщение в тикете #{active_id}</b>\n"
        f"От: {user_line}\n\n"
        f"<blockquote>{html.escape(txt)}</blockquote>"
    )

    # В топик тикета.
    try:
        topic_id = int(t.get("topic_id") or 0)
    except Exception:
        topic_id = 0
    if topic_id:
        if message.photo:
            await message.bot.send_photo(
                chat_id=config.support_group_id,
                message_thread_id=topic_id,
                photo=message.photo[-1].file_id,
                caption=topic_text[:1024],
                parse_mode="HTML",
            )
        else:
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
            f"✉️ Пользователь написал в тикет #{active_id}\n"
            f"От: {disp}\n\n"
            f"{txt}"
            + (f"\n\n<a href=\"{deep}\">Ответить</a>" if deep else "")
        )
        await message.bot.send_message(chat_id=admin_tg, text=dm, disable_web_page_preview=True)

