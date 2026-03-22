"""Массовая рассылка сообщений пользователям бота (Telegram лимиты)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
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


async def broadcast_plain_text(
    bot: Bot,
    text: str,
    *,
    skip_blocked: bool = True,
    delay_sec: float = DEFAULT_DELAY_SEC,
) -> tuple[int, int]:
    """
    Отправляет одинаковый текст всем пользователям из БД.
    Возвращает (успешно, ошибок).
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

    for tid in ids:
        try:
            await bot.send_message(chat_id=tid, text=body)
            ok += 1
        except TelegramRetryAfter as e:
            wait = float(getattr(e, "retry_after", None) or 1)
            logger.warning("broadcast flood wait %s s for chat=%s", wait, tid)
            await asyncio.sleep(wait + 0.5)
            try:
                await bot.send_message(chat_id=tid, text=body)
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
