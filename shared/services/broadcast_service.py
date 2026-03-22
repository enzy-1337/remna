"""Массовая рассылка сообщений пользователям бота (Telegram лимиты)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import get_session_factory
from shared.models.user import User

logger = logging.getLogger(__name__)

# ~25 сообщений/сек разным чатам — запас к лимитам Telegram
DEFAULT_DELAY_SEC = 0.05
MAX_MESSAGE_LEN = 4096


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
    body = body[:MAX_MESSAGE_LEN]

    factory = get_session_factory()
    async with factory() as session:
        ids = await collect_recipient_telegram_ids(session, skip_blocked=skip_blocked)

    ok = 0
    failed = 0

    async def _send(chat_id: int) -> None:
        await bot.send_message(chat_id, body, parse_mode=parse_mode)

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
                logger.debug("broadcast retry failed chat=%s", tid, exc_info=True)
                failed += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:
            logger.debug("broadcast send failed chat=%s", tid, exc_info=True)
            failed += 1

        if delay_sec > 0:
            await asyncio.sleep(delay_sec)

    return ok, failed
