from __future__ import annotations

from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

import html

from tickets.config import config
from tickets.keyboards import rating_keyboard
from tickets.states import TicketStates
from tickets.services import (
    add_ticket_message,
    bump_ticket_activity,
    ensure_db_user,
    get_ticket_brief,
    set_ticket_status,
)

router = Router(name="tickets_admin")

def _extract_start_payload(message: Message) -> str | None:
    if not message.text:
        return None
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


def _is_admin(telegram_id: int | None) -> bool:
    if telegram_id is None:
        return False
    return telegram_id in (config.admin_ids or [])


@router.message(CommandStart(deep_link=True))
async def cmd_start_admin_entry(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return
    payload = _extract_start_payload(message)
    if not payload or not payload.startswith("reply_"):
        return
    if not _is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    try:
        tid = int(payload.split("_", 1)[1])
    except Exception:
        await message.answer("Неверный тикет.")
        return

    t = await get_ticket_brief(session, ticket_id=tid)
    if not t:
        await message.answer("Тикет не найден.")
        return
    if str(t.get("status") or "") == "closed":
        await message.answer(f"Тикет #{tid} уже закрыт.")
        return

    await ensure_db_user(session, message.from_user)
    await state.set_state(TicketStates.waiting_admin_reply_text)
    await state.update_data(reply_ticket_id=tid)
    await message.answer(f"Введите ответ на тикет #{tid}:")


@router.callback_query(F.data.startswith("tickets:status:"))
async def cb_status_set(cq: CallbackQuery, session: AsyncSession) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    data = (cq.data or "").split(":")
    if len(data) < 4:
        await cq.answer("Некорректные данные.", show_alert=True)
        return
    try:
        ticket_id = int(data[2])
    except Exception:
        await cq.answer("Некорректный ticket id.", show_alert=True)
        return
    status = data[3]
    if status not in {"in_progress"}:
        await cq.answer("Неподдерживаемый статус.", show_alert=True)
        return
    t = await get_ticket_brief(session, ticket_id=ticket_id)
    if not t:
        await cq.answer("Тикет не найден.", show_alert=True)
        return
    if str(t.get("status") or "") == "closed":
        await cq.answer("Тикет уже закрыт.", show_alert=True)
        return
    await set_ticket_status(session, ticket_id=ticket_id, status="in_progress", close_now=False)
    await cq.answer("Статус: в работе")
    if cq.message and cq.message.text:
        base = cq.message.text.split("\n\nСтатус:")[0]
        try:
            await cq.message.edit_text(base + "\n\nСтатус: 🔄 В работе", reply_markup=cq.message.reply_markup)
        except Exception:
            pass


@router.callback_query(F.data.startswith("tickets:close:"))
async def cb_close_ticket(cq: CallbackQuery, session: AsyncSession) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    data = (cq.data or "").split(":")
    if len(data) < 3:
        await cq.answer("Некорректные данные.", show_alert=True)
        return
    try:
        ticket_id = int(data[2])
    except Exception:
        await cq.answer("Некорректный ticket id.", show_alert=True)
        return
    t = await get_ticket_brief(session, ticket_id=ticket_id)
    if not t:
        await cq.answer("Тикет не найден.", show_alert=True)
        return
    if str(t.get("status") or "") == "closed":
        await cq.answer("Тикет уже закрыт.", show_alert=True)
        return

    await set_ticket_status(session, ticket_id=ticket_id, status="closed", close_now=True)

    # Архивируем/закрываем топик в группе.
    try:
        topic_id = int(t.get("topic_id") or 0)
    except Exception:
        topic_id = 0
    if topic_id:
        try:
            await cq.bot.close_forum_topic(chat_id=config.support_group_id, message_thread_id=topic_id)
        except Exception:
            pass

    # Уведомление пользователю + запрос оценки.
    try:
        user_tg = int(t.get("telegram_user_id") or 0)
    except Exception:
        user_tg = 0
    if user_tg:
        await cq.bot.send_message(chat_id=user_tg, text=f"Ваш тикет #{ticket_id} был закрыт администратором")
        await cq.bot.send_message(
            chat_id=user_tg,
            text=f"Оцените работу поддержки по тикету #{ticket_id}:",
            reply_markup=rating_keyboard(ticket_id),
        )

    await cq.answer("Тикет закрыт")
    if cq.message and cq.message.text:
        base = cq.message.text.split("\n\nСтатус:")[0]
        try:
            await cq.message.edit_text(base + "\n\nСтатус: ✅ Закрыт", reply_markup=None)
        except Exception:
            pass


@router.callback_query(F.data.startswith("tickets:reply:"))
async def cb_reply_stub(cq: CallbackQuery) -> None:
    # В норме тут будет deep link (шаг 6). Если URL-кнопка не сгенерилась — просто отвечаем.
    await cq.answer("Откройте тикет-бота для ответа.", show_alert=True)


@router.message(TicketStates.waiting_admin_reply_text, F.text)
async def msg_admin_reply(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return
    if not _is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    data = await state.get_data()
    tid = data.get("reply_ticket_id")
    try:
        ticket_id = int(tid)
    except Exception:
        await state.clear()
        await message.answer("Тикет не выбран.")
        return

    txt = (message.text or "").strip()
    if not txt:
        return
    t = await get_ticket_brief(session, ticket_id=ticket_id)
    if not t:
        await state.clear()
        await message.answer("Тикет не найден.")
        return
    if str(t.get("status") or "") == "closed":
        await state.clear()
        await message.answer(f"Тикет #{ticket_id} уже закрыт.")
        return

    # Сохраняем сообщение админа.
    db_admin = await ensure_db_user(session, message.from_user)
    await add_ticket_message(
        session,
        ticket_id=ticket_id,
        sender_id=db_admin.id,
        sender_role="admin",
        sender_telegram_id=int(message.from_user.id),
        text_body=txt,
        is_internal=False,
    )
    await bump_ticket_activity(session, ticket_id=ticket_id, status_to_in_progress=True)

    # Пишем пользователю.
    user_tg = t.get("telegram_user_id")
    try:
        user_tg_id = int(user_tg)
    except Exception:
        user_tg_id = 0
    if user_tg_id:
        body = (
            f"<b>📨 Ответ от администратора | Тикет #{ticket_id}</b>\n\n"
            f"{html.escape(txt)}\n\n"
            "С уважением, Flux Network"
        )
        await message.bot.send_message(chat_id=user_tg_id, text=body, disable_web_page_preview=True)

    # Копия в топик группы.
    try:
        topic_id = int(t.get("topic_id") or 0)
    except Exception:
        topic_id = 0
    if topic_id:
        admin_name = (message.from_user.full_name or "Администратор").strip()
        cap = f"<b>💬 Ответ администратора</b> — {html.escape(admin_name)}\n\n{html.escape(txt)}"
        await message.bot.send_message(
            chat_id=config.support_group_id,
            message_thread_id=topic_id,
            text=cap,
            disable_web_page_preview=True,
        )

    await state.clear()
    await message.answer(f"✅ Ответ отправлен пользователю (тикет #{ticket_id}).")

