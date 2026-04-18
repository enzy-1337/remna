"""Массовая рассылка сообщений пользователям бота (Telegram лимиты)."""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.models.broadcast_mailing import ScheduledBroadcast
from shared.models.user import User

logger = logging.getLogger(__name__)

# ~25 сообщений/сек разным чатам — запас к лимитам Telegram
DEFAULT_DELAY_SEC = 0.05
MAX_MESSAGE_LEN = 4096

_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_UNDER = re.compile(r"__(.+?)__", re.DOTALL)


def _bold_segments(raw: str) -> str:
    parts: list[str] = []
    last = 0
    for m in _RE_BOLD.finditer(raw):
        parts.append(html_lib.escape(raw[last : m.start()]))
        parts.append("<b>" + html_lib.escape(m.group(1)) + "</b>")
        last = m.end()
    parts.append(html_lib.escape(raw[last:]))
    return "".join(parts)


def _underline_segments(htmlish: str) -> str:
    parts: list[str] = []
    last = 0
    for m in _RE_UNDER.finditer(htmlish):
        parts.append(htmlish[last : m.start()])
        parts.append("<u>" + html_lib.escape(m.group(1)) + "</u>")
        last = m.end()
    parts.append(htmlish[last:])
    return "".join(parts)


def apply_simple_formatting_for_broadcast(text: str) -> str:
    """
    Упрощённая разметка для массовой рассылки (HTML в Telegram):
    - **жирный** → <b>жирный</b>
    - __подчёркнутый__ → <u>подчёркнутый</u>
    Фрагменты вне разметки экранируются; внутри ** и __ — безопасное экранирование содержимого.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    return _underline_segments(_bold_segments(raw))


def broadcast_html_preview_fragment(text: str) -> str:
    """Фрагмент HTML для предпросмотра в админке (как для Telegram HTML)."""
    return apply_simple_formatting_for_broadcast(text)


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
    parse_mode: str | None = ParseMode.HTML,
) -> tuple[int, int]:
    """
    Отправляет сообщение всем пользователям из БД.
    По умолчанию HTML: <b>, <i>, <u>, <s>, <code>, <pre>, <a href="">, эмодзи как есть.
    parse_mode=None — только обычный текст без разметки.
    """
    body = (text or "").strip()
    if not body:
        return 0, 0
    body = apply_simple_formatting_for_broadcast(body)
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
            # Часто ломается разметка HTML — повтор без parse_mode (как обычный текст)
            if parse_mode:
                await bot.send_message(chat_id, body, parse_mode=None)
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

    try:
        async with Bot(token=tok) as bot:
            await broadcast_to_users(bot, body)
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
