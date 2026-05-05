"""Downloader-режим бота: /start и скачивание ссылок Reels/Shorts/TikTok."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked
from shared.config import get_settings
from shared.md2 import bold, esc, join_lines, plain
from shared.models.downloader_user_topic import DownloaderUserTopic
from shared.models.user import User
from shared.services.user_registration import register_user
from shared.services.video_downloader import (
    download_video,
    extract_first_url,
    is_supported_url,
)

router = Router(name="downloader")
_user_locks: dict[int, asyncio.Lock] = {}


def _now_label() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def _format_size_mb(size_bytes: int) -> str:
    return f"{(size_bytes / 1024 / 1024):.2f}"


def _build_cta_keyboard(bot_username: str | None) -> InlineKeyboardMarkup | None:
    uname = (bot_username or "").strip().lstrip("@")
    if not uname:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"❤️ @{uname}", url=f"https://t.me/{uname}")],
        ]
    )


async def _ensure_user_topic(
    *,
    session: AsyncSession,
    message: Message,
    db_user: User,
) -> DownloaderUserTopic:
    settings = get_settings()
    forum_chat_id = settings.downloader_forum_chat_id
    if forum_chat_id is None:
        raise RuntimeError("Не задан DOWNLOADER_FORUM_CHAT_ID в .env")

    existing = (
        await session.execute(
            select(DownloaderUserTopic).where(
                DownloaderUserTopic.telegram_id == db_user.telegram_id,
                DownloaderUserTopic.forum_chat_id == forum_chat_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    tg = message.from_user
    if tg is None:
        raise RuntimeError("Пользователь Telegram не найден")
    title = (f"@{tg.username} | {tg.id}" if tg.username else f"{tg.id}")[:128]
    topic = await message.bot.create_forum_topic(chat_id=forum_chat_id, name=title)
    row = DownloaderUserTopic(
        telegram_id=tg.id,
        forum_chat_id=forum_chat_id,
        topic_id=topic.message_thread_id,
        user_id=db_user.id,
    )
    session.add(row)
    await session.flush()
    return row


async def _recreate_user_topic(
    *,
    session: AsyncSession,
    message: Message,
    db_user: User,
    existing: DownloaderUserTopic,
) -> DownloaderUserTopic:
    tg = message.from_user
    if tg is None:
        raise RuntimeError("Пользователь Telegram не найден")
    title = (f"@{tg.username} | {tg.id}" if tg.username else f"{tg.id}")[:128]
    topic = await message.bot.create_forum_topic(chat_id=existing.forum_chat_id, name=title)
    existing.topic_id = topic.message_thread_id
    existing.user_id = db_user.id
    await session.flush()
    return existing


def _start_caption() -> str:
    return join_lines(
        plain("Привет! 👋"),
        "",
        plain("Пришли ссылку — скачаю и отправлю видео:"),
        plain("• Instagram Reels"),
        plain("• YouTube Shorts"),
        plain("• TikTok"),
    )


def _meta_caption(*, platform: str, duration_sec: int, size_bytes: int, tg_id: int, username: str | None, url: str) -> str:
    tag = f"@{username}" if username else "без username"
    return join_lines(
        "📥 " + bold("Новое скачивание"),
        plain("Платформа: ") + bold(platform),
        plain("Размер: ") + bold(f"{_format_size_mb(size_bytes)} MB"),
        plain("Длительность: ") + bold(str(duration_sec)) + plain(" сек"),
        plain("Пользователь: ") + bold(tag) + plain(" | ") + bold(str(tg_id)),
        plain("Время: ") + bold(_now_label()),
        plain("Ссылка: ") + esc(url),
    )


@router.message(CommandStart())
async def cmd_start_downloader(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    tg = message.from_user
    if tg is None:
        return
    if db_user is None:
        db_user, _, _ = await register_user(session, tg, None)
    if await reject_if_blocked(message, db_user):
        return
    await _ensure_user_topic(session=session, message=message, db_user=db_user)
    await message.answer(_start_caption())


@router.message(F.text)
async def handle_download_link(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    settings = get_settings()
    tg = message.from_user
    if tg is None or tg.is_bot:
        return
    if db_user is None:
        db_user, _, _ = await register_user(session, tg, None)
    if await reject_if_blocked(message, db_user):
        return

    url = extract_first_url(message.text or "")
    if not url:
        return
    if not is_supported_url(url):
        await message.answer(plain("Поддерживаются только Instagram Reels, YouTube Shorts и TikTok ссылки."))
        return

    topic = await _ensure_user_topic(session=session, message=message, db_user=db_user)
    lock = _user_locks.setdefault(tg.id, asyncio.Lock())
    if lock.locked():
        await message.answer(plain("⏳ Предыдущее видео ещё загружается. Дождитесь завершения."))
        return

    progress_msg = await message.answer(plain("⏳ Загружаем видео, подождите..."))
    async with lock:
        try:
            video, temp_dir = await download_video(url)
            try:
                if settings.downloader_max_duration_sec > 0 and video.duration_sec > settings.downloader_max_duration_sec:
                    await progress_msg.edit_text(
                        plain(
                            f"Видео слишком длинное: {video.duration_sec} сек. Лимит: {settings.downloader_max_duration_sec} сек."
                        )
                    )
                    return
                if settings.downloader_max_file_mb > 0:
                    max_bytes = int(Decimal(settings.downloader_max_file_mb) * 1024 * 1024)
                    if video.size_bytes > max_bytes:
                        await progress_msg.edit_text(
                            plain(
                                f"Видео слишком большое: {_format_size_mb(video.size_bytes)} MB. Лимит: {settings.downloader_max_file_mb} MB."
                            )
                        )
                        return

                kb = _build_cta_keyboard(settings.bot_username)
                await progress_msg.delete()
                await message.answer_video(
                    FSInputFile(video.path),
                    reply_markup=kb,
                )

                meta = _meta_caption(
                    platform=video.platform,
                    duration_sec=video.duration_sec,
                    size_bytes=video.size_bytes,
                    tg_id=tg.id,
                    username=tg.username,
                    url=video.original_url,
                )
                await message.bot.send_message(
                    chat_id=topic.forum_chat_id,
                    message_thread_id=topic.topic_id,
                    text=meta,
                )
                await message.bot.send_video(
                    chat_id=topic.forum_chat_id,
                    message_thread_id=topic.topic_id,
                    video=FSInputFile(video.path),
                    reply_markup=kb,
                )
            except Exception as topic_exc:
                err = str(topic_exc).lower()
                if "message thread not found" not in err and "topic" not in err:
                    raise
                topic = await _recreate_user_topic(
                    session=session,
                    message=message,
                    db_user=db_user,
                    existing=topic,
                )
                await message.bot.send_message(
                    chat_id=topic.forum_chat_id,
                    message_thread_id=topic.topic_id,
                    text=meta,
                )
                await message.bot.send_video(
                    chat_id=topic.forum_chat_id,
                    message_thread_id=topic.topic_id,
                    video=FSInputFile(video.path),
                    reply_markup=kb,
                )
            finally:
                temp_dir.cleanup()
        except Exception:
            try:
                await progress_msg.edit_text(plain("Не удалось скачать видео. Проверьте ссылку и попробуйте снова."))
            except Exception:
                await message.answer(plain("Не удалось скачать видео. Проверьте ссылку и попробуйте снова."))
