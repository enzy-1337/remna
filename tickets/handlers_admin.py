from __future__ import annotations

from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

import html

from tickets.config import config
from tickets.keyboards import closed_ticket_keyboard, rating_keyboard, topic_ticket_keyboard
from tickets.states import TicketStates
from tickets.services import (
    add_ticket_message,
    assign_ticket_admin,
    bump_ticket_activity,
    ensure_db_user,
    get_ticket_by_topic,
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
    """Быстрая проверка по ADMIN_IDS из .env."""
    if telegram_id is None:
        return False
    return telegram_id in (config.admin_ids or [])


async def _can_manage_tickets(session: AsyncSession, telegram_id: int | None) -> bool:
    """
    Может ли пользователь управлять тикетами:
    1. Есть в ADMIN_IDS (legacy env-список)
    2. ИЛИ имеет permission manage_tickets / is_superadmin в таблице admin_users
    """
    if telegram_id is None:
        return False
    if _is_admin(telegram_id):
        return True
    try:
        row = (
            await session.execute(
                text(
                    """
                    SELECT au.is_superadmin,
                           au.extra_permissions,
                           ar.permissions AS role_permissions
                    FROM users u
                    JOIN admin_users au ON au.user_id = u.id
                    LEFT JOIN admin_roles ar ON ar.id = au.role_id
                    WHERE u.telegram_id = :tg
                    LIMIT 1
                    """
                ),
                {"tg": int(telegram_id)},
            )
        ).mappings().first()
        if row is None:
            return False
        if row["is_superadmin"]:
            return True
        extra = row["extra_permissions"] or []
        role_perms = row["role_permissions"] or []
        all_perms = set(extra) | set(role_perms)
        return "manage_tickets" in all_perms
    except Exception:
        return False


def _within_media_limit(size_bytes: int | None) -> bool:
    if not size_bytes:
        return True
    return size_bytes <= int(config.media_max_mb) * 1024 * 1024


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
    if not await _can_manage_tickets(session, message.from_user.id):
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
    if cq.from_user is None or not await _can_manage_tickets(session, cq.from_user.id):
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
    if status not in {"open", "in_progress"}:
        await cq.answer("Неподдерживаемый статус.", show_alert=True)
        return
    t = await get_ticket_brief(session, ticket_id=ticket_id)
    if not t:
        await cq.answer("Тикет не найден.", show_alert=True)
        return
    if str(t.get("status") or "") == "closed":
        await cq.answer("Тикет уже закрыт.", show_alert=True)
        return
    db_admin = await ensure_db_user(session, cq.from_user)
    from shared.services.admin_rbac_service import resolve_operator_id

    operator_id = await resolve_operator_id(session, user_id=db_admin.id)
    await assign_ticket_admin(
        session,
        ticket_id=ticket_id,
        operator_id=operator_id,
        admin_telegram_id=int(cq.from_user.id),
    )
    await set_ticket_status(session, ticket_id=ticket_id, status=status, close_now=False)
    if status == "open":
        try:
            topic_id = int(t.get("topic_id") or 0)
        except Exception:
            topic_id = 0
        if topic_id:
            try:
                await cq.bot.reopen_forum_topic(chat_id=config.support_group_id, message_thread_id=topic_id)
            except Exception:
                pass
    await cq.answer("Статус обновлён")
    if cq.message:
        # Используем html_text чтобы сохранить форматирование (ссылки, цитаты, жирный текст)
        src = cq.message.html_text or cq.message.text or ""
        if src:
            base = src.split("\n\nСтатус:")[0]
            try:
                badge = "🟢 Открыт" if status == "open" else "🔄 В работе"
                await cq.message.edit_text(
                    base + f"\n\nСтатус: {badge}",
                    reply_markup=cq.message.reply_markup,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass


@router.callback_query(F.data.startswith("tickets:status_info:"))
async def cb_status_info(cq: CallbackQuery, session: AsyncSession) -> None:
    if cq.from_user is None or not await _can_manage_tickets(session, cq.from_user.id):
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
    st = str(t.get("status") or "open")
    ru = {"open": "Открыт", "in_progress": "В работе", "closed": "Закрыт"}.get(st, st)
    await cq.answer(f"Статус тикета #{ticket_id}: {ru}", show_alert=True)


@router.callback_query(F.data.startswith("tickets:close:"))
async def cb_close_ticket(cq: CallbackQuery, session: AsyncSession) -> None:
    if cq.from_user is None or not await _can_manage_tickets(session, cq.from_user.id):
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
        from tickets.services import request_ticket_rating_if_needed

        await request_ticket_rating_if_needed(
            session, ticket_id=ticket_id, user_telegram_id=user_tg, bot=cq.bot
        )

    await cq.answer("Тикет закрыт")
    if cq.message:
        src = cq.message.html_text or cq.message.text or ""
        base = src.split("\n\nСтатус:")[0]
        try:
            await cq.message.edit_text(
                base + "\n\nСтатус: ✅ Закрыт",
                reply_markup=closed_ticket_keyboard(ticket_id),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("tickets:reopen:"))
async def cb_reopen_ticket(cq: CallbackQuery, session: AsyncSession) -> None:
    """Возобновление закрытого тикета администратором."""
    if cq.from_user is None or not await _can_manage_tickets(session, cq.from_user.id):
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
    if str(t.get("status") or "") != "closed":
        await cq.answer("Тикет не закрыт.", show_alert=True)
        return

    # Возобновляем — ставим in_progress
    await set_ticket_status(session, ticket_id=ticket_id, status="in_progress", close_now=False)

    # Назначаем текущего администратора
    db_admin = await ensure_db_user(session, cq.from_user)
    from shared.services.admin_rbac_service import resolve_operator_id
    operator_id = await resolve_operator_id(session, user_id=db_admin.id)
    await assign_ticket_admin(
        session,
        ticket_id=ticket_id,
        operator_id=operator_id,
        admin_telegram_id=int(cq.from_user.id),
    )

    # Открываем топик обратно
    try:
        topic_id = int(t.get("topic_id") or 0)
    except Exception:
        topic_id = 0
    if topic_id:
        try:
            await cq.bot.reopen_forum_topic(
                chat_id=config.support_group_id, message_thread_id=topic_id
            )
        except Exception:
            pass

    # Уведомляем пользователя (без повторного запроса оценки)
    try:
        user_tg = int(t.get("telegram_user_id") or 0)
    except Exception:
        user_tg = 0
    if user_tg:
        try:
            await cq.bot.send_message(
                chat_id=user_tg,
                text=(
                    f"🔄 Администратор возобновил диалог по тикету #{ticket_id}.\n"
                    "Вы можете продолжить общение — просто напишите сообщение."
                ),
            )
        except Exception:
            pass

    await cq.answer("Диалог возобновлён ✅")

    # Обновляем сообщение в топике — показываем полную клавиатуру управления
    if cq.message and cq.bot:
        try:
            bot_me = await cq.bot.get_me()
            bot_username = bot_me.username or ""
        except Exception:
            bot_username = ""
        web_admin_url = ""
        from tickets.config import config as _cfg
        root = (_cfg.web_admin_url or "").strip().rstrip("/")
        if root:
            web_admin_url = f"{root}/admin/tickets/{ticket_id}"
        src = cq.message.html_text or cq.message.text or ""
        base = src.split("\n\nСтатус:")[0]
        try:
            await cq.message.edit_text(
                base + "\n\nСтатус: 🔄 В работе",
                reply_markup=topic_ticket_keyboard(
                    bot_username=bot_username,
                    ticket_id=ticket_id,
                    web_admin_url=web_admin_url,
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("tickets:reply:"))
async def cb_reply_stub(cq: CallbackQuery) -> None:
    # В норме тут будет deep link (шаг 6). Если URL-кнопка не сгенерилась — просто отвечаем.
    await cq.answer("Откройте тикет-бота для ответа.", show_alert=True)


@router.message(TicketStates.waiting_admin_reply_text)
async def msg_admin_reply(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return
    if not await _can_manage_tickets(session, message.from_user.id):
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

    txt = (message.text or message.caption or "").strip()
    photo_fid: str | None = message.photo[-1].file_id if message.photo else None
    video_fid: str | None = message.video.file_id if message.video else None
    voice_fid: str | None = message.voice.file_id if message.voice else None
    vidnote_fid: str | None = message.video_note.file_id if message.video_note else None
    audio_fid: str | None = message.audio.file_id if message.audio else None
    audio_fname: str | None = (message.audio.file_name if message.audio else None) or None
    doc_fid: str | None = None
    doc_fname: str | None = None
    if message.document:
        doc_fid = message.document.file_id
        doc_fname = message.document.file_name or "file"

    has_any_media = bool(photo_fid or video_fid or voice_fid or vidnote_fid or audio_fid or doc_fid)
    media_size = None
    if message.photo:
        media_size = int(message.photo[-1].file_size or 0)
    elif message.video:
        media_size = int(message.video.file_size or 0)
    elif message.voice:
        media_size = int(message.voice.file_size or 0)
    elif message.video_note:
        media_size = int(message.video_note.file_size or 0)
    elif message.audio:
        media_size = int(message.audio.file_size or 0)
    elif message.document:
        media_size = int(message.document.file_size or 0)
    if has_any_media and not _within_media_limit(media_size):
        await message.answer(f"Файл слишком большой. Максимум: {config.media_max_mb} МБ.")
        return
    if not txt and not has_any_media:
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
    from shared.services.admin_rbac_service import resolve_operator_id

    operator_id = await resolve_operator_id(session, user_id=db_admin.id)
    await assign_ticket_admin(
        session,
        ticket_id=ticket_id,
        operator_id=operator_id,
        admin_telegram_id=int(message.from_user.id),
    )
    await add_ticket_message(
        session,
        ticket_id=ticket_id,
        sender_id=db_admin.id,
        sender_role="admin",
        sender_telegram_id=int(message.from_user.id),
        text_body=txt,
        is_internal=False,
        photo_file_id=photo_fid,
        video_file_id=video_fid,
        document_file_id=doc_fid,
        document_file_name=doc_fname,
        voice_file_id=voice_fid,
        video_note_file_id=vidnote_fid,
        audio_file_id=audio_fid,
        audio_file_name=audio_fname,
    )
    await bump_ticket_activity(session, ticket_id=ticket_id, status_to_in_progress=True)

    # Пишем пользователю.
    user_tg = t.get("telegram_user_id")
    try:
        user_tg_id = int(user_tg)
    except Exception:
        user_tg_id = 0
    if user_tg_id:
        body = f"📨 Ответ от администратора | Тикет #{ticket_id}"
        if txt:
            body += f"\n\n{html.escape(txt)}"
        body += "\n\nС уважением, Flux Network"
        if photo_fid:
            await message.bot.send_photo(chat_id=user_tg_id, photo=photo_fid, caption=body)
        elif video_fid:
            await message.bot.send_video(chat_id=user_tg_id, video=video_fid, caption=body)
        elif voice_fid:
            await message.bot.send_voice(chat_id=user_tg_id, voice=voice_fid, caption=body)
        elif vidnote_fid:
            await message.bot.send_video_note(chat_id=user_tg_id, video_note=vidnote_fid)
            if body:
                await message.bot.send_message(chat_id=user_tg_id, text=body, disable_web_page_preview=True)
        elif audio_fid:
            await message.bot.send_audio(chat_id=user_tg_id, audio=audio_fid, caption=body)
        elif doc_fid:
            await message.bot.send_document(chat_id=user_tg_id, document=doc_fid, caption=body)
        else:
            await message.bot.send_message(chat_id=user_tg_id, text=body, disable_web_page_preview=True)

    # Копия в топик группы.
    try:
        topic_id = int(t.get("topic_id") or 0)
    except Exception:
        topic_id = 0
    if topic_id:
        admin_name = (message.from_user.full_name or "Администратор").strip()
        cap = f"<b>💬 Ответ администратора</b> — {html.escape(admin_name)}"
        if txt:
            cap += f"\n\n<blockquote>{html.escape(txt)}</blockquote>"
        if photo_fid:
            await message.bot.send_photo(chat_id=config.support_group_id, message_thread_id=topic_id, photo=photo_fid, caption=cap[:1024], parse_mode="HTML")
        elif video_fid:
            await message.bot.send_video(chat_id=config.support_group_id, message_thread_id=topic_id, video=video_fid, caption=cap[:1024], parse_mode="HTML")
        elif voice_fid:
            await message.bot.send_voice(chat_id=config.support_group_id, message_thread_id=topic_id, voice=voice_fid, caption=cap[:1024], parse_mode="HTML")
        elif vidnote_fid:
            await message.bot.send_video_note(chat_id=config.support_group_id, message_thread_id=topic_id, video_note=vidnote_fid)
        elif audio_fid:
            await message.bot.send_audio(chat_id=config.support_group_id, message_thread_id=topic_id, audio=audio_fid, caption=cap[:1024], parse_mode="HTML")
        elif doc_fid:
            await message.bot.send_document(chat_id=config.support_group_id, message_thread_id=topic_id, document=doc_fid, caption=cap[:1024], parse_mode="HTML")
        else:
            await message.bot.send_message(chat_id=config.support_group_id, message_thread_id=topic_id, text=cap, disable_web_page_preview=True)

    await state.clear()
    await message.answer(f"✅ Ответ отправлен пользователю (тикет #{ticket_id}).")


@router.message(F.chat.id == config.support_group_id, F.message_thread_id)
async def msg_admin_in_topic_to_user(message: Message, session: AsyncSession) -> None:
    is_anonymous_group_admin = (
        message.from_user is None
        and message.sender_chat is not None
        and int(message.sender_chat.id) == int(config.support_group_id)
    )
    if message.from_user is not None and message.from_user.is_bot:
        return
    tg_id = message.from_user.id if message.from_user else None
    if not is_anonymous_group_admin and not await _can_manage_tickets(session, tg_id):
        return
    try:
        topic_id = int(message.message_thread_id or 0)
    except Exception:
        return
    if topic_id <= 0:
        return
    t = await get_ticket_by_topic(session, topic_id=topic_id)
    if not t:
        return
    # Закрытый тикет автоматически переоткрывается когда админ пишет в топик
    txt = (message.text or message.caption or "").strip()
    photo_fid: str | None = message.photo[-1].file_id if message.photo else None
    video_fid: str | None = message.video.file_id if message.video else None
    voice_fid: str | None = message.voice.file_id if message.voice else None
    vidnote_fid: str | None = message.video_note.file_id if message.video_note else None
    audio_fid: str | None = message.audio.file_id if message.audio else None
    audio_fname: str | None = (message.audio.file_name if message.audio else None) or None
    doc_fid: str | None = None
    doc_fname: str | None = None
    if message.document:
        doc_fid = message.document.file_id
        doc_fname = message.document.file_name or "file"
    has_any_media = bool(photo_fid or video_fid or voice_fid or vidnote_fid or audio_fid or doc_fid)
    media_size = None
    if message.photo:
        media_size = int(message.photo[-1].file_size or 0)
    elif message.video:
        media_size = int(message.video.file_size or 0)
    elif message.voice:
        media_size = int(message.voice.file_size or 0)
    elif message.video_note:
        media_size = int(message.video_note.file_size or 0)
    elif message.audio:
        media_size = int(message.audio.file_size or 0)
    elif message.document:
        media_size = int(message.document.file_size or 0)
    if has_any_media and not _within_media_limit(media_size):
        await message.reply(f"Файл слишком большой. Максимум: {config.media_max_mb} МБ.")
        return
    if not txt and not has_any_media:
        return

    ticket_id_int = int(t["id"])
    ticket_was_closed = str(t.get("status") or "") == "closed"

    # Если тикет закрыт — автоматически возобновляем
    if ticket_was_closed:
        await set_ticket_status(session, ticket_id=ticket_id_int, status="in_progress", close_now=False)
        # Уведомляем пользователя о возобновлении
        try:
            _user_tg_reopen = int(t.get("telegram_user_id") or 0)
        except Exception:
            _user_tg_reopen = 0
        if _user_tg_reopen:
            try:
                await message.bot.send_message(
                    chat_id=_user_tg_reopen,
                    text=(
                        f"🔄 Администратор возобновил диалог по тикету #{ticket_id_int}.\n"
                        "Вы можете продолжить общение — просто напишите сообщение."
                    ),
                )
            except Exception:
                pass

    db_admin = None
    if message.from_user is not None:
        db_admin = await ensure_db_user(session, message.from_user)
        from shared.services.admin_rbac_service import resolve_operator_id

        operator_id = await resolve_operator_id(session, user_id=db_admin.id)
        await assign_ticket_admin(
            session,
            ticket_id=ticket_id_int,
            operator_id=operator_id,
            admin_telegram_id=int(message.from_user.id),
        )
    await add_ticket_message(
        session,
        ticket_id=ticket_id_int,
        sender_id=(db_admin.id if db_admin is not None else None),
        sender_role="admin",
        sender_telegram_id=(int(message.from_user.id) if message.from_user is not None else None),
        text_body=txt,
        is_internal=False,
        photo_file_id=photo_fid,
        video_file_id=video_fid,
        document_file_id=doc_fid,
        document_file_name=doc_fname,
        voice_file_id=voice_fid,
        video_note_file_id=vidnote_fid,
        audio_file_id=audio_fid,
        audio_file_name=audio_fname,
    )
    await bump_ticket_activity(session, ticket_id=ticket_id_int, status_to_in_progress=True)
    try:
        user_tg_id = int(t.get("telegram_user_id") or 0)
    except Exception:
        user_tg_id = 0
    if user_tg_id:
        body = f"📨 Ответ от администратора | Тикет #{ticket_id_int}"
        if txt:
            body += f"\n\n{html.escape(txt)}"
        body += "\n\nС уважением, Flux Network"
        try:
            if photo_fid:
                await message.bot.send_photo(chat_id=user_tg_id, photo=photo_fid, caption=body)
            elif video_fid:
                await message.bot.send_video(chat_id=user_tg_id, video=video_fid, caption=body)
            elif voice_fid:
                await message.bot.send_voice(chat_id=user_tg_id, voice=voice_fid, caption=body)
            elif vidnote_fid:
                await message.bot.send_video_note(chat_id=user_tg_id, video_note=vidnote_fid)
                if body:
                    await message.bot.send_message(chat_id=user_tg_id, text=body, disable_web_page_preview=True)
            elif audio_fid:
                await message.bot.send_audio(chat_id=user_tg_id, audio=audio_fid, caption=body)
            elif doc_fid:
                await message.bot.send_document(chat_id=user_tg_id, document=doc_fid, caption=body)
            else:
                await message.bot.send_message(chat_id=user_tg_id, text=body, disable_web_page_preview=True)
        except Exception:
            pass

