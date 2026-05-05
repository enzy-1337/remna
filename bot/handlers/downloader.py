"""Downloader-режим бота: /start и скачивание ссылок Reels/Shorts/TikTok."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
import logging
import secrets
import string

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from downloader.config import get_downloader_settings
from shared.md2 import bold, esc, join_lines, plain
from shared.models.downloader_user_topic import DownloaderUserTopic
from shared.models.user import User
from shared.services.video_downloader import (
    download_video,
    extract_first_url,
    is_supported_url,
    resolve_short_url,
)

router = Router(name="downloader")
_user_locks: dict[int, asyncio.Lock] = {}
logger = logging.getLogger(__name__)


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


async def _generate_unique_referral_code(session: AsyncSession) -> str:
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        q: Select[tuple[int]] = select(User.id).where(User.referral_code == code)
        if (await session.execute(q)).scalar_one_or_none() is None:
            return code
    raise RuntimeError("Не удалось сгенерировать referral_code")


async def _get_or_create_user(session: AsyncSession, message: Message) -> User:
    tg = message.from_user
    if tg is None:
        raise RuntimeError("Пользователь Telegram не найден")
    existing = (
        await session.execute(select(User).where(User.telegram_id == tg.id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    user = User(
        telegram_id=tg.id,
        username=tg.username,
        first_name=tg.first_name,
        last_name=tg.last_name,
        language_code=tg.language_code,
        referral_code=await _generate_unique_referral_code(session),
        is_subscribed_channel=True,
        billing_mode="legacy",
    )
    session.add(user)
    await session.flush()
    return user


async def _ensure_user_topic(
    *,
    session: AsyncSession,
    message: Message,
    db_user: User,
) -> DownloaderUserTopic:
    settings = get_downloader_settings()
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
        "👋 " + bold("Привет!"),
        "",
        plain("Пришли ссылку — скачаю и отправлю видео:"),
        "",
        plain("• Instagram Reels"),
        plain("• YouTube Shorts и обычные YouTube видео"),
        plain("• TikTok"),
        plain("• VK Видео"),
        plain("• VK Clips"),
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
) -> None:
    settings = get_downloader_settings()
    tg = message.from_user
    if tg is None:
        return
    db_user = await _get_or_create_user(session, message)
    if db_user.is_blocked:
        await message.answer(plain("Ваш аккаунт заблокирован. Обратитесь в поддержку."))
        return
    await _ensure_user_topic(session=session, message=message, db_user=db_user)
    await message.answer(_start_caption())


@router.message(F.text)
async def handle_download_link(
    message: Message,
    session: AsyncSession,
) -> None:
    settings = get_downloader_settings()
    tg = message.from_user
    if tg is None or tg.is_bot:
        return
    db_user = await _get_or_create_user(session, message)
    if db_user.is_blocked:
        await message.answer(plain("Ваш аккаунт заблокирован. Обратитесь в поддержку."))
        return

    url = extract_first_url(message.text or "")
    if not url:
        return
    resolved_url = await resolve_short_url(url)
    if resolved_url != url:
        logger.info("Short URL resolved: %s -> %s", url, resolved_url)
    if not is_supported_url(resolved_url):
        await message.answer(
            plain("Поддерживаются ссылки: Instagram Reels, YouTube Shorts/YouTube, TikTok, VK Видео и VK Clips.")
        )
        return

    topic = await _ensure_user_topic(session=session, message=message, db_user=db_user)
    lock = _user_locks.setdefault(tg.id, asyncio.Lock())
    if lock.locked():
        await message.answer(plain("⏳ Предыдущее видео ещё загружается. Дождитесь завершения."))
        return

    progress_msg = await message.answer(plain("⏳ Загружаем видео, подождите..."))
    async with lock:
        try:
            video, temp_dir = await download_video(resolved_url)
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
                try:
                    await message.answer_video(
                        FSInputFile(video.path),
                        reply_markup=kb,
                    )
                except TelegramBadRequest as e:
                    msg = str(e).lower()
                    if "file is too big" in msg or "request entity too large" in msg:
                        await message.answer(
                            plain(
                                "Видео скачалось, но Telegram не принял файл по размеру. "
                                "Попробуйте другую ссылку или более короткий ролик."
                            )
                        )
                        logger.warning(
                            "Telegram rejected video size user_id=%s size_mb=%s url=%s err=%s",
                            tg.id,
                            _format_size_mb(video.size_bytes),
                            resolved_url,
                            e,
                        )
                        return
                    raise

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
        except Exception as e:
            logger.exception(
                "Downloader failed user_id=%s url=%s resolved_url=%s",
                tg.id,
                url,
                resolved_url,
            )
            reason = str(e).strip()
            reason_line = f"\nПричина: {reason[:220]}" if reason else ""
            try:
                await progress_msg.edit_text(
                    plain("Не удалось скачать видео. Проверьте ссылку и попробуйте снова." + reason_line)
                )
            except Exception:
                await message.answer(plain("Не удалось скачать видео. Проверьте ссылку и попробуйте снова." + reason_line))
