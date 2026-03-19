"""Уведомления в админ-чат (шаг 12) + запись в notifications_log."""

from __future__ import annotations

import html
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.models.notification_log import NotificationLog
from shared.models.user import User
from shared.services.telegram_notify import send_telegram_message

logger = logging.getLogger(__name__)


def format_user_line(user: User) -> str:
    un = html.escape(f"@{user.username}") if user.username else "—"
    return f"👤 <b>#{user.id}</b> · tg <code>{user.telegram_id}</code> · {un}"


def _admin_chat_configured(settings: Settings) -> bool:
    c = settings.admin_log_chat_id
    if c is None:
        return False
    if isinstance(c, str) and not c.strip():
        return False
    return True


async def _persist_log(
    *,
    user_id: int,
    event_type: str,
    message_text: str,
    status: str,
    session: AsyncSession | None,
) -> None:
    row = NotificationLog(
        user_id=user_id,
        type=f"admin:{event_type}",
        message_text=message_text[:8000] if message_text else None,
        status=status,
    )
    if session is not None:
        session.add(row)
        return
    factory = get_session_factory()
    async with factory() as s:
        s.add(row)
        await s.commit()


async def notify_admin(
    settings: Settings,
    *,
    title: str,
    lines: list[str],
    event_type: str,
    subject_user: User | None = None,
    subject_user_id: int | None = None,
    session: AsyncSession | None = None,
) -> None:
    """
    Отправка HTML в ADMIN_LOG_CHAT_ID (опционально ADMIN_LOG_TOPIC_ID для форумов).
    Дублирует событие в notifications_log для subject-пользователя (если известен id).
    """
    uid = subject_user.id if subject_user is not None else subject_user_id
    chunks: list[str] = []
    if subject_user is not None:
        chunks.append(format_user_line(subject_user))
    chunks.append(title)
    chunks.extend(lines)
    body = "\n".join(chunks)

    if not _admin_chat_configured(settings):
        if uid is not None:
            await _persist_log(
                user_id=uid,
                event_type=event_type,
                message_text=body,
                status="skipped_no_admin_chat",
                session=session,
            )
        return

    chat_id = settings.admin_log_chat_id
    assert chat_id is not None
    thread = settings.admin_log_topic_id

    ok = await send_telegram_message(
        chat_id,
        body,
        message_thread_id=thread,
        settings=settings,
    )
    log_status = "sent" if ok else "failed"
    if not ok:
        logger.warning("admin notify failed event=%s", event_type)

    if uid is not None:
        await _persist_log(
            user_id=uid,
            event_type=event_type,
            message_text=body,
            status=log_status,
            session=session,
        )
