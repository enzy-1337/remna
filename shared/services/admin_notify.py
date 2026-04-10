"""Уведомления в админ-чат (темы форума) + запись в notifications_log (MarkdownV2)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.md2 import bold, code, esc, join_lines
from shared.models.notification_log import NotificationLog
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.telegram_notify import send_telegram_document, send_telegram_message

logger = logging.getLogger(__name__)


def format_user_line(user: User) -> str:
    un = esc(f"@{user.username}") if user.username else "—"
    return join_lines(
        "👤 "
        + bold(f"#{user.id}")
        + " · tg "
        + code(str(user.telegram_id))
        + " · "
        + un
    )


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
    topic: AdminLogTopic = AdminLogTopic.GENERAL,
    subject_user: User | None = None,
    subject_user_id: int | None = None,
    session: AsyncSession | None = None,
) -> None:
    """
    MarkdownV2 в тему форума по типу события (ADMIN_LOG_TOPIC_* или общий ADMIN_LOG_TOPIC_ID).
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
    thread = settings.admin_log_thread_for(topic)

    mid = await send_telegram_message(
        chat_id,
        body,
        message_thread_id=thread,
        parse_mode="MarkdownV2",
        settings=settings,
    )
    ok = mid is not None
    log_status = "sent" if ok else "failed"
    if not ok:
        logger.warning("admin notify failed event=%s topic=%s", event_type, topic.value)

    if uid is not None:
        await _persist_log(
            user_id=uid,
            event_type=event_type,
            message_text=body,
            status=log_status,
            session=session,
        )


async def notify_admin_plain(
    settings: Settings,
    *,
    text: str,
    topic: AdminLogTopic,
    event_type: str = "plain",
) -> bool:
    """
    Текст без parse_mode (эмодзи, хэштеги, многострочные шаблоны бэкапов и отчётов).
    """
    if not _admin_chat_configured(settings):
        logger.debug("admin plain notify skipped: no chat event=%s", event_type)
        return False
    chat_id = settings.admin_log_chat_id
    assert chat_id is not None
    thread = settings.admin_log_thread_for(topic)
    mid = await send_telegram_message(
        chat_id,
        text[:12000],
        message_thread_id=thread,
        parse_mode=None,
        settings=settings,
    )
    return mid is not None


async def notify_admin_document(
    settings: Settings,
    *,
    document_path: str,
    caption: str | None,
    topic: AdminLogTopic,
    event_type: str = "document",
) -> bool:
    if not _admin_chat_configured(settings):
        logger.debug("admin document notify skipped: no chat event=%s", event_type)
        return False
    chat_id = settings.admin_log_chat_id
    assert chat_id is not None
    thread = settings.admin_log_thread_for(topic)
    return await send_telegram_document(
        chat_id,
        document_path,
        caption=caption,
        message_thread_id=thread,
        settings=settings,
    )
