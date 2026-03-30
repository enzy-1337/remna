from __future__ import annotations

from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

import html

from tickets.config import config
from tickets.states import TicketStates
from tickets.services import (
    add_ticket_message,
    bump_ticket_activity,
    ensure_db_user,
    get_ticket_brief,
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


@router.message(CommandStart())
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
async def cb_status_stub(cq: CallbackQuery) -> None:
    # Полная логика будет на шаге 7.
    await cq.answer("Ок.", show_alert=False)


@router.callback_query(F.data.startswith("tickets:close:"))
async def cb_close_stub(cq: CallbackQuery) -> None:
    # Полная логика будет на шаге 7.
    await cq.answer("Ок.", show_alert=False)


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

