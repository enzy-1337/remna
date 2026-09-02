"""Массовая рассылка сообщений пользователям бота (Telegram лимиты)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, FSInputFile
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
MAX_CAPTION_LEN = 1024

# Директория для временного хранения медиафайлов рассылки
_MEDIA_DIR = Path(__file__).resolve().parent.parent.parent / "uploads" / "broadcast_media"

# Префикс для file_id, хранящихся локально
_LOCAL_PREFIX = "local:"


def get_broadcast_media_dir() -> Path:
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return _MEDIA_DIR


def save_broadcast_media_file(data: bytes, filename: str) -> str:
    """Сохранить файл рассылки на диск, вернуть local-ссылку вида 'local:<uuid>:<filename>'."""
    media_dir = get_broadcast_media_dir()
    file_uuid = str(uuid.uuid4())
    # Сохраняем оригинальное имя файла в ссылке (через двоеточие)
    safe_name = filename.replace(":", "_").replace("/", "_").replace("\\", "_")
    dest = media_dir / f"{file_uuid}_{safe_name}"
    dest.write_bytes(data)
    return f"{_LOCAL_PREFIX}{file_uuid}:{safe_name}"


def resolve_broadcast_media(media_file_id: str) -> FSInputFile | str | None:
    """
    Если media_file_id начинается с 'local:' — читаем файл с диска и возвращаем FSInputFile.
    Иначе — Telegram file_id, возвращаем как строку.
    """
    if not media_file_id:
        return None
    if not media_file_id.startswith(_LOCAL_PREFIX):
        return media_file_id  # уже telegram file_id
    rest = media_file_id[len(_LOCAL_PREFIX):]
    # формат: <uuid>:<filename>
    colon_idx = rest.find(":")
    if colon_idx < 0:
        file_uuid = rest
        safe_name = "file"
    else:
        file_uuid = rest[:colon_idx]
        safe_name = rest[colon_idx + 1:]
    media_dir = get_broadcast_media_dir()
    path = media_dir / f"{file_uuid}_{safe_name}"
    if not path.is_file():
        logger.warning("broadcast media file not found: %s", path)
        return None
    return FSInputFile(path, filename=safe_name)


def delete_broadcast_media_file(media_file_id: str) -> None:
    """Удалить локальный файл после рассылки (если это local:-ссылка)."""
    if not media_file_id or not media_file_id.startswith(_LOCAL_PREFIX):
        return
    rest = media_file_id[len(_LOCAL_PREFIX):]
    colon_idx = rest.find(":")
    if colon_idx < 0:
        file_uuid = rest
        safe_name = "file"
    else:
        file_uuid = rest[:colon_idx]
        safe_name = rest[colon_idx + 1:]
    media_dir = get_broadcast_media_dir()
    path = media_dir / f"{file_uuid}_{safe_name}"
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.debug("delete broadcast media failed: %s", path, exc_info=True)


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


async def _send_one_message(
    bot: Bot,
    chat_id: int,
    body_md: str,
    draft: str,
    *,
    parse_mode: str | None,
    media_type: str | None = None,
    media_file_id: str | None = None,
) -> None:
    """Отправить одно сообщение с опциональным медиа (фото/документ)."""
    if media_type and media_file_id:
        input_file = resolve_broadcast_media(media_file_id)
        if input_file is None:
            # Файл не найден — отправим только текст
            if body_md:
                await bot.send_message(chat_id, body_md, parse_mode=parse_mode)
            return
        caption = body_md[:MAX_CAPTION_LEN] if body_md else None
        pm = parse_mode if caption else None
        try:
            if media_type == "photo":
                await bot.send_photo(chat_id, input_file, caption=caption, parse_mode=pm)
            else:
                await bot.send_document(chat_id, input_file, caption=caption, parse_mode=pm)
        except TelegramBadRequest:
            if pm and caption:
                plain_cap = draft[:MAX_CAPTION_LEN] if draft else None
                # Пересоздаём input_file — FSInputFile нельзя читать дважды
                input_file2 = resolve_broadcast_media(media_file_id)
                if input_file2 is None:
                    return
                if media_type == "photo":
                    await bot.send_photo(chat_id, input_file2, caption=plain_cap, parse_mode=None)
                else:
                    await bot.send_document(chat_id, input_file2, caption=plain_cap, parse_mode=None)
            else:
                raise
    else:
        try:
            await bot.send_message(chat_id, body_md, parse_mode=parse_mode)
        except TelegramBadRequest:
            if parse_mode:
                await bot.send_message(chat_id, draft[:MAX_MESSAGE_LEN], parse_mode=None)
            else:
                raise


async def broadcast_to_users(
    bot: Bot,
    text: str,
    *,
    skip_blocked: bool = True,
    delay_sec: float = DEFAULT_DELAY_SEC,
    parse_mode: str | None = ParseMode.MARKDOWN_V2,
    media_type: str | None = None,
    media_file_id: str | None = None,
) -> tuple[int, int]:
    """
    Отправляет сообщение всем пользователям из БД.
    Черновик админки конвертируется в MarkdownV2 (см. shared.broadcast_md2_convert).
    parse_mode=None — только обычный текст без разметки.
    media_type/media_file_id — опциональное фото или документ (caption = text).
    """
    draft = (text or "").strip()
    has_media = bool(media_type and media_file_id)
    if not draft and not has_media:
        return 0, 0
    body = draft_to_markdown_v2(draft) if draft else ""
    if not has_media:
        body = body[:MAX_MESSAGE_LEN]
    else:
        body = body[:MAX_CAPTION_LEN]

    factory = get_session_factory()
    async with factory() as session:
        ids = await collect_recipient_telegram_ids(session, skip_blocked=skip_blocked)

    n = len(ids)
    logger.info("broadcast: старт, получателей=%s (skip_blocked=%s, media=%s)", n, skip_blocked, media_type)

    ok = 0
    failed = 0

    for tid in ids:
        try:
            await _send_one_message(
                bot, tid, body, draft,
                parse_mode=parse_mode,
                media_type=media_type,
                media_file_id=media_file_id,
            )
            ok += 1
        except TelegramRetryAfter as e:
            wait = float(getattr(e, "retry_after", None) or 1)
            logger.warning("broadcast flood wait %s s for chat=%s", wait, tid)
            await asyncio.sleep(wait + 0.5)
            try:
                await _send_one_message(
                    bot, tid, body, draft,
                    parse_mode=parse_mode,
                    media_type=media_type,
                    media_file_id=media_file_id,
                )
                ok += 1
            except Exception:
                logger.warning("broadcast retry failed chat=%s", tid, exc_info=True)
                failed += 1
        except TelegramForbiddenError:
            failed += 1
        except TelegramBadRequest as e:
            # Ожидаемые случаи для мёртвых/удалённых аккаунтов — не льём трейсбек в лог на
            # каждого такого получателя, иначе консоль/Logs тонет в шуме при рассылке на базу.
            logger.info("broadcast send failed chat=%s: %s", tid, e)
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
    media_type: str | None = None,
    media_file_id: str | None = None,
) -> bool:
    """Одно сообщение в канал/супергруппу."""
    draft = (text or "").strip()
    has_media = bool(media_type and media_file_id)
    if not draft and not has_media:
        return False
    body = draft_to_markdown_v2(draft) if draft else ""
    if not has_media:
        body = body[:MAX_MESSAGE_LEN]
    else:
        body = body[:MAX_CAPTION_LEN]
    try:
        await _send_one_message(
            bot, chat_id, body, draft,
            parse_mode=parse_mode,
            media_type=media_type,
            media_file_id=media_file_id,
        )
        return True
    except Exception:
        logger.warning("broadcast channel send failed chat=%s", chat_id, exc_info=True)
        return False


async def save_broadcast_history(
    *,
    body_draft: str,
    recipients_ok: int,
    recipients_failed: int,
    source: str,
    media_type: str | None = None,
    media_file_id: str | None = None,
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
                media_type=media_type or None,
                media_file_id=media_file_id or None,
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
        media_type = row.media_type or None
        media_file_id = row.media_file_id or None

    has_media = bool(media_type and media_file_id)
    if not body and not has_media:
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
                ok, failed = await broadcast_to_users(
                    bot, body, media_type=media_type, media_file_id=media_file_id
                )
            if send_ch:
                cid = getattr(settings, "broadcast_main_channel_id", None)
                if cid:
                    ch_sent = await send_broadcast_to_channel(
                        bot, body, chat_id=int(cid),
                        media_type=media_type, media_file_id=media_file_id,
                    )
        if send_u:
            await save_broadcast_history(
                body_draft=body,
                recipients_ok=ok,
                recipients_failed=failed,
                source="scheduled",
                media_type=media_type,
                media_file_id=media_file_id,
            )
        if send_ch:
            await save_broadcast_history(
                body_draft=body,
                recipients_ok=1 if ch_sent else 0,
                recipients_failed=0 if ch_sent else 1,
                source="channel",
                media_type=media_type,
                media_file_id=media_file_id,
            )
        # Удаляем локальный файл после успешной отправки
        if media_file_id:
            delete_broadcast_media_file(media_file_id)
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
