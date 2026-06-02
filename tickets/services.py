from __future__ import annotations

import html
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.enums import ParseMode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings, get_settings
from shared.models.user import User
from shared.services.billing_v2.detail_service import format_hybrid_billing_today_for_support_topic
from tickets.config import config
from tickets.keyboards import topic_ticket_keyboard
from shared.services.user_registration import get_user_by_telegram_id, register_user
from shared.tickets_db_compat import (
    ticket_messages_has_photo_file_id_column,
    ticket_messages_has_video_file_id_column,
)


async def ensure_db_user(session: AsyncSession, tg_user) -> User:
    """Получить/создать пользователя в общей БД (users)."""
    u = await get_user_by_telegram_id(session, int(tg_user.id))
    if u is not None:
        return u
    user, _, _ = await register_user(session, tg_user, None)  # type: ignore[misc]
    return user


async def get_active_ticket_id(session: AsyncSession, *, user_id: int) -> int | None:
    """Активный тикет = status != closed (open/in_progress)."""
    q = await session.execute(
        text(
            """
            SELECT id
            FROM tickets
            WHERE user_id = :uid
              AND status IN ('open','in_progress')
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"uid": user_id},
    )
    row = q.first()
    return int(row[0]) if row else None


async def create_ticket(
    session: AsyncSession,
    *,
    user: User,
    telegram_user_id: int,
    text_body: str,
) -> int:
    """Создать тикет и первое сообщение пользователя. topic_id временно 0, обновим после create_forum_topic."""
    now = datetime.now(timezone.utc)
    r = await session.execute(
        text(
            """
            INSERT INTO tickets (user_id, telegram_user_id, status, topic_id, created_at, updated_at, last_activity)
            VALUES (:uid, :tg, 'open', 0, :now, :now, :now)
            RETURNING id
            """
        ),
        {"uid": user.id, "tg": telegram_user_id, "now": now},
    )
    tid = int(r.scalar_one())
    await session.execute(
        text(
            """
            INSERT INTO ticket_messages (ticket_id, sender_id, sender_role, sender_telegram_id, text, created_at, is_internal)
            VALUES (:tid, :sid, 'user', :stg, :txt, :now, false)
            """
        ),
        {"tid": tid, "sid": user.id, "stg": telegram_user_id, "txt": text_body, "now": now},
    )
    return tid


async def set_ticket_topic(session: AsyncSession, *, ticket_id: int, topic_id: int) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            """
            UPDATE tickets
            SET topic_id = :tp, updated_at = :now
            WHERE id = :tid
            """
        ),
        {"tp": topic_id, "now": now, "tid": ticket_id},
    )


async def get_ticket_brief(session: AsyncSession, *, ticket_id: int) -> dict | None:
    r = await session.execute(
        text(
            """
            SELECT id, status, topic_id, user_id, telegram_user_id,
                   operator_id, telegram_assigned_admin_id, rating_requested
            FROM tickets
            WHERE id = :tid
            """
        ),
        {"tid": ticket_id},
    )
    row = r.mappings().first()
    return dict(row) if row else None


async def get_ticket_by_topic(session: AsyncSession, *, topic_id: int) -> dict | None:
    r = await session.execute(
        text(
            """
            SELECT id, status, topic_id, user_id, telegram_user_id,
                   operator_id, telegram_assigned_admin_id, rating_requested
            FROM tickets
            WHERE topic_id = :tp
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"tp": topic_id},
    )
    row = r.mappings().first()
    return dict(row) if row else None


async def add_ticket_message(
    session: AsyncSession,
    *,
    ticket_id: int,
    sender_id: int | None,
    sender_role: str,
    sender_telegram_id: int | None,
    text_body: str,
    is_internal: bool,
    photo_file_id: str | None = None,
    video_file_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    has_photo = await ticket_messages_has_photo_file_id_column(session)
    has_video = await ticket_messages_has_video_file_id_column(session)
    if has_photo and has_video:
        await session.execute(
            text(
                """
                INSERT INTO ticket_messages (ticket_id, sender_id, sender_role, sender_telegram_id, text, created_at, is_internal, photo_file_id, video_file_id)
                VALUES (:tid, :sid, :role, :stg, :txt, :now, :internal, :photo, :video)
                """
            ),
            {
                "tid": ticket_id,
                "sid": sender_id,
                "role": sender_role,
                "stg": sender_telegram_id,
                "txt": text_body,
                "now": now,
                "internal": bool(is_internal),
                "photo": photo_file_id,
                "video": video_file_id,
            },
        )
    elif has_photo:
        await session.execute(
            text(
                """
                INSERT INTO ticket_messages (ticket_id, sender_id, sender_role, sender_telegram_id, text, created_at, is_internal, photo_file_id)
                VALUES (:tid, :sid, :role, :stg, :txt, :now, :internal, :photo)
                """
            ),
            {
                "tid": ticket_id,
                "sid": sender_id,
                "role": sender_role,
                "stg": sender_telegram_id,
                "txt": text_body,
                "now": now,
                "internal": bool(is_internal),
                "photo": photo_file_id,
            },
        )
    else:
        await session.execute(
            text(
                """
                INSERT INTO ticket_messages (ticket_id, sender_id, sender_role, sender_telegram_id, text, created_at, is_internal)
                VALUES (:tid, :sid, :role, :stg, :txt, :now, :internal)
                """
            ),
            {
                "tid": ticket_id,
                "sid": sender_id,
                "role": sender_role,
                "stg": sender_telegram_id,
                "txt": text_body,
                "now": now,
                "internal": bool(is_internal),
            },
        )


async def bump_ticket_activity(
    session: AsyncSession,
    *,
    ticket_id: int,
    status_to_in_progress: bool = False,
) -> None:
    now = datetime.now(timezone.utc)
    if status_to_in_progress:
        await session.execute(
            text(
                """
                UPDATE tickets
                SET status = CASE WHEN status = 'open' THEN 'in_progress' ELSE status END,
                    updated_at = :now,
                    last_activity = :now
                WHERE id = :tid
                """
            ),
            {"now": now, "tid": ticket_id},
        )
    else:
        await session.execute(
            text(
                """
                UPDATE tickets
                SET updated_at = :now, last_activity = :now
                WHERE id = :tid
                """
            ),
            {"now": now, "tid": ticket_id},
        )


async def set_ticket_status(
    session: AsyncSession,
    *,
    ticket_id: int,
    status: str,
    close_now: bool = False,
) -> None:
    now = datetime.now(timezone.utc)
    if close_now:
        await session.execute(
            text(
                """
                UPDATE tickets
                SET status = :st,
                    updated_at = :now,
                    last_activity = :now,
                    closed_at = :now
                WHERE id = :tid
                """
            ),
            {"st": status, "now": now, "tid": ticket_id},
        )
    else:
        await session.execute(
            text(
                """
                UPDATE tickets
                SET status = :st,
                    updated_at = :now,
                    last_activity = :now
                WHERE id = :tid
                """
            ),
            {"st": status, "now": now, "tid": ticket_id},
        )


async def assign_ticket_admin(
    session: AsyncSession,
    *,
    ticket_id: int,
    operator_id: int | None,
    admin_telegram_id: int,
) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            """
            UPDATE tickets
            SET operator_id = :oid,
                telegram_assigned_admin_id = :atg,
                updated_at = :now
            WHERE id = :tid
            """
        ),
        {"oid": operator_id, "atg": admin_telegram_id, "now": now, "tid": ticket_id},
    )


async def build_ticket_topic_open_html(
    session: AsyncSession,
    *,
    ticket_id: int,
    db_user: User,
    message_text: str,
    settings: Settings,
    display_name: str,
    telegram_user_id: int,
    username: str | None = None,
) -> str:
    """HTML-текст первого сообщения тикета в теме форума (как в боте поддержки)."""
    created_line = "Дата: " + datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    disp = (display_name or "Пользователь").strip()
    user_line = f'<a href="tg://user?id={int(telegram_user_id)}">{html.escape(disp)}</a>'
    un = ("@" + username) if username else ""
    from shared.services.ticket_user_info_service import (
        format_ticket_user_info_html,
        get_ticket_user_info_cached,
    )

    info = await get_ticket_user_info_cached(session, db_user=db_user, settings=settings)
    info_html = format_ticket_user_info_html(info)
    billing_html = await format_hybrid_billing_today_for_support_topic(
        session, user=db_user, settings=settings
    )
    body = html.escape((message_text or "").strip())
    cap = (
        f"<b>🎫 Тикет #{ticket_id}</b>\n"
        f"Пользователь: {user_line} {html.escape(un)}\n"
        f"{created_line}\n\n"
        f"{info_html}\n\n"
        f"<blockquote>{body}</blockquote>"
    )
    if billing_html:
        cap += "\n\n" + billing_html
    return cap


async def open_ticket_forum_topic(
    bot: Bot,
    session: AsyncSession,
    *,
    ticket_id: int,
    db_user: User,
    message_text: str,
    display_name: str,
    telegram_user_id: int,
    username: str | None = None,
    settings: Settings | None = None,
) -> int:
    """
    Создать тему форума и отправить открывающее сообщение с клавиатурой админа.
    Возвращает topic_id.
    """
    if config.support_group_id == 0:
        raise RuntimeError("SUPPORT_GROUP_ID не настроен")
    s = settings or get_settings()
    title = f"Тикет #{ticket_id} — {(display_name or 'Пользователь').strip()}"[:128]
    topic = await bot.create_forum_topic(chat_id=config.support_group_id, name=title)
    topic_id = int(topic.message_thread_id)
    await set_ticket_topic(session, ticket_id=ticket_id, topic_id=topic_id)
    me = await bot.get_me()
    base_url = (s.public_site_url or "").strip().rstrip("/")
    web_admin_ticket_url = f"{base_url}/admin/tickets/{ticket_id}" if base_url else ""
    kb = topic_ticket_keyboard(
        bot_username=me.username or "",
        ticket_id=ticket_id,
        web_admin_url=web_admin_ticket_url,
    )
    cap = await build_ticket_topic_open_html(
        session,
        ticket_id=ticket_id,
        db_user=db_user,
        message_text=message_text,
        settings=s,
        display_name=display_name,
        telegram_user_id=telegram_user_id,
        username=username,
    )
    await bot.send_message(
        chat_id=config.support_group_id,
        message_thread_id=topic_id,
        text=cap,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    return topic_id


async def save_ticket_rating(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int,
    operator_id: int | None,
    value: int,
) -> bool:
    """Возвращает True, если оценка добавлена впервые."""
    check = await session.execute(
        text("SELECT id FROM ticket_ratings WHERE ticket_id = :tid LIMIT 1"),
        {"tid": ticket_id},
    )
    if check.first() is not None:
        return False
    await session.execute(
        text(
            """
            INSERT INTO ticket_ratings (ticket_id, operator_id, user_id, value, created_at)
            VALUES (:tid, :oid, :uid, :val, :now)
            """
        ),
        {
            "tid": ticket_id,
            "oid": operator_id,
            "uid": user_id,
            "val": int(value),
            "now": datetime.now(timezone.utc),
        },
    )
    return True


async def mark_rating_requested(session: AsyncSession, *, ticket_id: int) -> None:
    await session.execute(
        text("UPDATE tickets SET rating_requested = true WHERE id = :tid"),
        {"tid": ticket_id},
    )


async def request_ticket_rating_if_needed(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_telegram_id: int,
    bot: Bot,
) -> None:
    row = await session.execute(
        text("SELECT rating_requested FROM tickets WHERE id = :tid"),
        {"tid": ticket_id},
    )
    first = row.first()
    if first is not None and bool(first[0]):
        return
    from tickets.keyboards import rating_keyboard

    await bot.send_message(
        chat_id=user_telegram_id,
        text=f"Оцените работу поддержки по тикету #{ticket_id}:",
        reply_markup=rating_keyboard(ticket_id),
    )
    await mark_rating_requested(session, ticket_id=ticket_id)

