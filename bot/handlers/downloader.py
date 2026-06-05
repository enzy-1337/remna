"""Downloader-режим бота: /start и скачивание ссылок Reels/Shorts/TikTok."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
import logging
import secrets
import string
import tempfile
from pathlib import Path

import httpx
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge
from aiogram.types import FSInputFile, Message, InputMediaPhoto
from shared.bot_cta import build_bot_cta_keyboard
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from downloader.config import get_downloader_settings
from shared.md2 import bold, esc, join_lines, plain
from shared.models.downloader_user_topic import DownloaderUserTopic
from shared.models.user import User
from shared.services.marketplace_scraper import detect_marketplace, scrape_marketplace_reviews
from shared.services.video_downloader import (
    compress_video_to_limit,
    download_video,
    extract_first_url,
    is_supported_url,
    resolve_short_url,
)

router = Router(name="downloader")
_user_locks: dict[int, asyncio.Lock] = {}
logger = logging.getLogger(__name__)
_TELEGRAM_BOT_UPLOAD_MAX_MB = 49


def _now_label() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def _format_size_mb(size_bytes: int) -> str:
    return f"{(size_bytes / 1024 / 1024):.2f}"


async def _maybe_compress_for_telegram(
    *,
    video_path,
    duration_sec: int,
    size_bytes: int,
):
    telegram_limit_bytes = _TELEGRAM_BOT_UPLOAD_MAX_MB * 1024 * 1024
    if size_bytes <= telegram_limit_bytes:
        return video_path, size_bytes, False
    compressed = await compress_video_to_limit(
        source_path=video_path,
        duration_sec=duration_sec,
        max_size_mb=_TELEGRAM_BOT_UPLOAD_MAX_MB,
    )
    if compressed is None:
        return video_path, size_bytes, False
    return compressed, int(compressed.stat().st_size), True


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
        plain("Пришли ссылку — скачаю видео, фото или GIF:"),
        "",
        plain("• Instagram Reels, посты, истории"),
        plain("• TikTok (видео, фотопосты, истории)"),
        plain("• Pinterest (видео, GIF, фото)"),
        plain("• Telegram истории (t.me/канал/s/ID)"),
        plain("• YouTube Shorts"),
        plain("• VK Clips"),
        plain("• Wildberries, Ozon, Яндекс.Маркет (фото из отзывов)"),
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


async def _handle_marketplace_url(
    message: Message,
    session: AsyncSession,
    db_user,
    url: str,
) -> None:
    """Скачать фото из отзывов маркетплейса и отправить пользователю."""
    progress = await message.answer("⏳ " + bold("Загружаем фото из отзывов, подождите..."))
    try:
        result = await scrape_marketplace_reviews(url)
    except Exception as exc:
        logger.exception("Marketplace scrape error: %s", exc)
        await progress.edit_text(plain("Ошибка при скачивании. Попробуйте позже."))
        return

    if result.error:
        await progress.edit_text(plain(result.error))
        return

    photos = result.photo_urls
    if not photos:
        await progress.edit_text(plain("Фото из отзывов не найдены. Возможно, товар не имеет отзывов с фото."))
        return

    platform_labels = {
        "wildberries": "Wildberries",
        "ozon": "Ozon",
        "yandex_market": "Яндекс.Маркет",
    }
    platform_label = platform_labels.get(result.platform, result.platform)
    await progress.delete()

    # Отправляем фото группами по 10
    sent_count = 0
    batch: list = []
    temp_files: list[Path] = []

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
        for i, photo_url in enumerate(photos[:_MAX_PHOTOS_SEND]):
            try:
                resp = await http.get(photo_url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                suffix = ".jpg"
                ct = resp.headers.get("content-type", "")
                if "png" in ct:
                    suffix = ".png"
                elif "webp" in ct:
                    suffix = ".webp"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                tmp.write(resp.content)
                tmp.close()
                p = Path(tmp.name)
                temp_files.append(p)
                batch.append(FSInputFile(p))
            except Exception:
                continue

            if len(batch) == 10 or i == len(photos[:_MAX_PHOTOS_SEND]) - 1:
                if batch:
                    media_group = [InputMediaPhoto(media=f) for f in batch]
                    if sent_count == 0:
                        media_group[0] = InputMediaPhoto(
                            media=batch[0],
                            caption=plain(f"📸 Фото из отзывов {platform_label}\nТовар: {result.product_name}\nВсего фото: {len(photos)}"),
                        )
                    try:
                        await message.answer_media_group(media_group)
                        sent_count += len(batch)
                    except Exception as exc:
                        logger.warning("Failed to send marketplace photo group: %s", exc)
                    batch = []

    for tmp_file in temp_files:
        try:
            tmp_file.unlink(missing_ok=True)
        except Exception:
            pass

    if sent_count == 0:
        await message.answer(plain("Не удалось загрузить изображения. Попробуйте позже."))


_MAX_PHOTOS_SEND = 50


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

    # Проверяем маркетплейсы до видео-загрузчика
    if detect_marketplace(resolved_url):
        await _handle_marketplace_url(message, session, db_user, resolved_url)
        return

    if not is_supported_url(resolved_url):
        await message.answer(
            plain("Поддерживаются: Instagram, TikTok, Pinterest, Telegram истории (t.me/канал/s/ID), YouTube Shorts, VK Clips, Wildberries, Ozon, Яндекс.Маркет.")
        )
        return

    topic = await _ensure_user_topic(session=session, message=message, db_user=db_user)
    lock = _user_locks.setdefault(tg.id, asyncio.Lock())
    if lock.locked():
        await message.answer(join_lines("⏳ " + bold("Предыдущее видео ещё загружается."), plain("Дождитесь завершения.")))
        return

    progress_msg = await message.answer("⏳ " + bold("Загружаем, подождите..."))
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
                kb = build_bot_cta_keyboard(settings.bot_username, label_template=settings.bot_cta_label)
                sent_user_video: Message | None = None
                sent_user_photos: list[Message] = []
                delivered_size = video.size_bytes
                if video.photo_paths:
                    await progress_msg.delete()
                    media = [InputMediaPhoto(media=FSInputFile(p)) for p in video.photo_paths[:10]]
                    sent_user_photos = await message.answer_media_group(media)
                    delivered_size = sum(int(p.stat().st_size) for p in video.photo_paths[:10])
                    if kb is not None:
                        await message.answer(plain("💜 Поддержать бота"), reply_markup=kb)
                else:
                    send_path, send_size, was_compressed = await _maybe_compress_for_telegram(
                        video_path=video.path,
                        duration_sec=video.duration_sec,
                        size_bytes=video.size_bytes,
                    )
                    if was_compressed:
                        await message.answer(plain("🎞 Видео было автоматически сжато для отправки в Telegram."))
                    telegram_limit_bytes = _TELEGRAM_BOT_UPLOAD_MAX_MB * 1024 * 1024
                    if send_size > telegram_limit_bytes:
                        await progress_msg.delete()
                        await message.answer(
                            plain(
                                "Видео скачалось, но Telegram не принял размер даже после попытки сжатия. "
                                f"Размер: {_format_size_mb(send_size)} MB, лимит Telegram: {_TELEGRAM_BOT_UPLOAD_MAX_MB} MB."
                            )
                        )
                        return
                    delivered_size = send_size
                    await progress_msg.delete()
                    try:
                        if video.is_gif:
                            sent_user_video = await message.answer_animation(
                                FSInputFile(send_path),
                                reply_markup=kb,
                            )
                        else:
                            sent_user_video = await message.answer_video(
                                FSInputFile(send_path),
                                reply_markup=kb,
                            )
                    except TelegramBadRequest as e:
                        msg = str(e).lower()
                        if "file is too big" in msg or "request entity too large" in msg:
                            await message.answer(
                                join_lines(
                                    "❌ " + bold("Telegram не принял файл по размеру."),
                                    plain("Попробуйте другую ссылку или более короткий ролик."),
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
                    except TelegramEntityTooLarge:
                        await message.answer(
                            join_lines(
                                "❌ " + bold("Telegram не принял файл по размеру."),
                                plain(f"Лимит Telegram: {_TELEGRAM_BOT_UPLOAD_MAX_MB} MB."),
                            )
                        )
                        logger.warning(
                            "TelegramEntityTooLarge user_id=%s size_mb=%s url=%s",
                            tg.id,
                            _format_size_mb(video.size_bytes),
                            resolved_url,
                        )
                        return

                meta = _meta_caption(
                    platform=video.platform,
                    duration_sec=video.duration_sec,
                    size_bytes=delivered_size,
                    tg_id=tg.id,
                    username=tg.username,
                    url=video.original_url,
                )
                await message.bot.send_message(
                    chat_id=topic.forum_chat_id,
                    message_thread_id=topic.topic_id,
                    text=meta,
                )
                if sent_user_photos:
                    try:
                        for sent_photo in sent_user_photos:
                            await message.bot.forward_message(
                                chat_id=topic.forum_chat_id,
                                message_thread_id=topic.topic_id,
                                from_chat_id=sent_photo.chat.id,
                                message_id=sent_photo.message_id,
                            )
                    except TelegramBadRequest:
                        media_admin = []
                        for sent_photo in sent_user_photos[:10]:
                            if not sent_photo.photo:
                                continue
                            media_admin.append(InputMediaPhoto(media=sent_photo.photo[-1].file_id))
                        if media_admin:
                            await message.bot.send_media_group(
                                chat_id=topic.forum_chat_id,
                                message_thread_id=topic.topic_id,
                                media=media_admin,
                            )
                else:
                    if sent_user_video is None:
                        raise RuntimeError("Не удалось отправить видео пользователю.")
                    try:
                        await message.bot.forward_message(
                            chat_id=topic.forum_chat_id,
                            message_thread_id=topic.topic_id,
                            from_chat_id=sent_user_video.chat.id,
                            message_id=sent_user_video.message_id,
                        )
                    except TelegramBadRequest:
                        if sent_user_video.animation is not None:
                            await message.bot.send_animation(
                                chat_id=topic.forum_chat_id,
                                message_thread_id=topic.topic_id,
                                animation=sent_user_video.animation.file_id,
                            )
                        elif sent_user_video.video is not None:
                            await message.bot.send_video(
                                chat_id=topic.forum_chat_id,
                                message_thread_id=topic.topic_id,
                                video=sent_user_video.video.file_id,
                                reply_markup=kb,
                            )
                        else:
                            raise
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
                if sent_user_photos:
                    for sent_photo in sent_user_photos:
                        await message.bot.forward_message(
                            chat_id=topic.forum_chat_id,
                            message_thread_id=topic.topic_id,
                            from_chat_id=sent_photo.chat.id,
                            message_id=sent_photo.message_id,
                        )
                else:
                    try:
                        await message.bot.forward_message(
                            chat_id=topic.forum_chat_id,
                            message_thread_id=topic.topic_id,
                            from_chat_id=sent_user_video.chat.id,
                            message_id=sent_user_video.message_id,
                        )
                    except TelegramBadRequest:
                        if sent_user_video is None:
                            raise
                        if sent_user_video.animation is not None:
                            await message.bot.send_animation(
                                chat_id=topic.forum_chat_id,
                                message_thread_id=topic.topic_id,
                                animation=sent_user_video.animation.file_id,
                            )
                        elif sent_user_video.video is not None:
                            await message.bot.send_video(
                                chat_id=topic.forum_chat_id,
                                message_thread_id=topic.topic_id,
                                video=sent_user_video.video.file_id,
                                reply_markup=kb,
                            )
                        else:
                            raise
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
                    join_lines(
                        "❌ " + bold("Не удалось скачать видео."),
                        plain("Проверьте ссылку и попробуйте снова." + reason_line),
                    )
                )
            except Exception:
                await message.answer(
                    join_lines(
                        "❌ " + bold("Не удалось скачать видео."),
                        plain("Проверьте ссылку и попробуйте снова." + reason_line),
                    )
                )
