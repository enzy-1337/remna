"""Массовая рассылка сообщений пользователям бота (Telegram лимиты)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.broadcast_md2_convert import draft_to_markdown_v2
from shared.config import Settings
from shared.database import get_session_factory
from shared.models.broadcast_mailing import BroadcastHistory, ScheduledBroadcast
from shared.models.user import User

logger = logging.getLogger(__name__)

# ~25 сообщений/сек разным чатам — запас к лимитам Telegram
DEFAULT_DELAY_SEC = 0.05
MAX_MESSAGE_LEN = 4096

def apply_simple_formatting_for_broadcast(text: str) -> str:
    """Совместимость: черновик → MarkdownV2 (для старых вызовов)."""
    return draft_to_markdown_v2(text)


def broadcast_html_preview_fragment(text: str) -> str:
    """HTML-предпросмотр черновика (MarkdownV2-подобная разметка)."""
    from shared.broadcast_md2_convert import draft_to_preview_html

    return draft_to_preview_html(text)


async def collect_recipient_telegram_ids(
    session: AsyncSession,
    *,
    skip_blocked: bool = True,
) -> list[int]:
    q = select(User.telegram_id).where(User.telegram_id.is_not(None))
    if skip_blocked:
        q = q.where(User.is_blocked.is_(False))
    q = q.order_by(User.id)
    r = await session.execute(q)
    return [int(row[0]) for row in r.all()]


async def broadcast_to_users(
    bot: Bot,
    text: str,
    *,
    skip_blocked: bool = True,
    delay_sec: float = DEFAULT_DELAY_SEC,
    parse_mode: str | None = ParseMode.MARKDOWN_V2,
) -> tuple[int, int]:
    """
    Отправляет сообщение всем пользователям из БД.
    Черновик админки конвертируется в MarkdownV2 (см. shared.broadcast_md2_convert).
    parse_mode=None — только обычный текст без разметки.
    """
    draft = (text or "").strip()
    if not draft:
        return 0, 0
    body = draft_to_markdown_v2(draft)
    body = body[:MAX_MESSAGE_LEN]

    factory = get_session_factory()
    async with factory() as session:
        ids = await collect_recipient_telegram_ids(session, skip_blocked=skip_blocked)

    n = len(ids)
    logger.info("broadcast: старт, получателей=%s (skip_blocked=%s)", n, skip_blocked)

    ok = 0
    failed = 0

    async def _send(chat_id: int) -> None:
        try:
            await bot.send_message(chat_id, body, parse_mode=parse_mode)
        except TelegramBadRequest:
            if parse_mode:
                await bot.send_message(chat_id, draft[:MAX_MESSAGE_LEN], parse_mode=None)
            else:
                raise

    for tid in ids:
        try:
            await _send(tid)
            ok += 1
        except TelegramRetryAfter as e:
            wait = float(getattr(e, "retry_after", None) or 1)
            logger.warning("broadcast flood wait %s s for chat=%s", wait, tid)
            await asyncio.sleep(wait + 0.5)
            try:
                await _send(tid)
                ok += 1
            except Exception:
                logger.warning("broadcast retry failed chat=%s", tid, exc_info=True)
                failed += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:
            logger.warning("broadcast send failed chat=%s", tid, exc_info=True)
            failed += 1

        if delay_sec > 0:
            await asyncio.sleep(delay_sec)

    logger.info("broadcast: завершено ok=%s failed=%s (всего в выборке %s)", ok, failed, n)
    return ok, failed


async def send_broadcast_to_channel(
    bot: Bot,
    text: str,
    *,
    chat_id: int,
    parse_mode: str | None = ParseMode.MARKDOWN_V2,
) -> bool:
    """Одно сообщение в канал/супергруппу."""
    draft = (text or "").strip()
    if not draft:
        return False
    body = draft_to_markdown_v2(draft)
    body = body[:MAX_MESSAGE_LEN]
    try:
        await bot.send_message(chat_id, body, parse_mode=parse_mode)
        return True
    except TelegramBadRequest:
        if parse_mode:
            try:
                await bot.send_message(chat_id, draft[:MAX_MESSAGE_LEN], parse_mode=None)
                return True
            except Exception:
                logger.warning("broadcast channel send failed chat=%s", chat_id, exc_info=True)
                return False
        return False
    except Exception:
        logger.warning("broadcast channel send failed chat=%s", chat_id, exc_info=True)
        return False


async def save_broadcast_history(
    *,
    body_draft: str,
    recipients_ok: int,
    recipients_failed: int,
    source: str,
) -> None:
    src = (source or "mass")[:32]
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            BroadcastHistory(
                body_text=body_draft[:12000],
                recipients_ok=max(0, int(recipients_ok)),
                recipients_failed=max(0, int(recipients_failed)),
                source=src,
            )
        )
        await session.commit()


async def tick_scheduled_broadcast_queue(settings: Settings) -> None:
    """
    Одноразовая проверка отложенных рассылок (вызывать из фонового цикла API).
    """
    tok = (settings.bot_token or "").strip()
    if not tok:
        return
    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(ScheduledBroadcast)
                .where(
                    ScheduledBroadcast.status == "pending",
                    ScheduledBroadcast.scheduled_at <= datetime.now(timezone.utc),
                )
                .order_by(ScheduledBroadcast.scheduled_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.status = "processing"
        await session.commit()
        job_id = int(row.id)
        body = (row.body_text or "").strip()

    if not body:
        async with factory() as session:
            r2 = await session.get(ScheduledBroadcast, job_id)
            if r2 is not None:
                r2.status = "failed"
                r2.error_text = "empty body"
                r2.sent_at = datetime.now(timezone.utc)
                await session.commit()
        return

    send_u = bool(getattr(row, "send_to_users", True))
    send_ch = bool(getattr(row, "send_to_channel", False))
    if not send_u and not send_ch:
        async with factory() as session:
            r2 = await session.get(ScheduledBroadcast, job_id)
            if r2 is not None:
                r2.status = "failed"
                r2.error_text = "no targets selected"
                r2.sent_at = datetime.now(timezone.utc)
                await session.commit()
        return

    try:
        ok = failed = 0
        ch_sent = False
        async with Bot(token=tok) as bot:
            if send_u:
                ok, failed = await broadcast_to_users(bot, body)
            if send_ch:
                cid = getattr(settings, "broadcast_main_channel_id", None)
                if cid:
                    ch_sent = await send_broadcast_to_channel(bot, body, chat_id=int(cid))
        if send_u:
            await save_broadcast_history(
                body_draft=body,
                recipients_ok=ok,
                recipients_failed=failed,
                source="scheduled",
            )
        if send_ch:
            await save_broadcast_history(
                body_draft=body,
                recipients_ok=1 if ch_sent else 0,
                recipients_failed=0 if ch_sent else 1,
                source="channel",
            )
        async with factory() as session:
            r3 = await session.get(ScheduledBroadcast, job_id)
            if r3 is not None:
                r3.status = "sent"
                r3.sent_at = datetime.now(timezone.utc)
                r3.error_text = None
                await session.commit()
    except Exception as e:
        logger.exception("scheduled broadcast job_id=%s failed", job_id)
        async with factory() as session:
            r4 = await session.get(ScheduledBroadcast, job_id)
            if r4 is not None:
                r4.status = "failed"
                r4.sent_at = datetime.now(timezone.utc)
                r4.error_text = str(e)[:2000]
                await session.commit()
