"""Music-бот: поиск и скачивание музыки."""

from __future__ import annotations

import logging
import secrets
import string
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram import Bot
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User as TgUser,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from musicbot.config import MusicBotSettings, get_musicbot_settings
from shared.bot_cta import build_bot_cta_keyboard
from shared.md2 import bold, esc, join_lines, plain
from shared.models.music_user_topic import MusicUserTopic
from shared.models.user import User
from shared.services.media_search import (
    MediaTrack,
    download_track_to_mp3,
    extract_first_url,
    get_track_from_session,
    identify_audio_file,
    load_search_session,
    new_session_id,
    query_from_telegram_video_file,
    query_from_video_url,
    save_search_session,
    search_music_all,
)

router = Router(name="music")
logger = logging.getLogger(__name__)


async def _generate_unique_referral_code(session: AsyncSession) -> str:
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        q: Select[tuple[int]] = select(User.id).where(User.referral_code == code)
        if (await session.execute(q)).scalar_one_or_none() is None:
            return code
    raise RuntimeError("Не удалось сгенерировать referral_code")


async def _get_or_create_user(session: AsyncSession, tg: TgUser) -> User:
    if tg.is_bot:
        raise RuntimeError("Нельзя создать пользователя для бота")
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
    bot: Bot,
    tg: TgUser,
    db_user: User,
    settings: MusicBotSettings,
) -> MusicUserTopic:
    forum_chat_id = settings.music_forum_chat_id
    if forum_chat_id is None:
        raise RuntimeError("MUSIC_FORUM_CHAT_ID не задан")
    if int(tg.id) != int(db_user.telegram_id):
        raise RuntimeError("telegram_id пользователя не совпадает с записью в БД")
    existing = (
        await session.execute(
            select(MusicUserTopic).where(
                MusicUserTopic.telegram_id == db_user.telegram_id,
                MusicUserTopic.forum_chat_id == forum_chat_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    title = (f"@{tg.username} | {tg.id}" if tg.username else f"{tg.id}")[:128]
    topic = await bot.create_forum_topic(chat_id=forum_chat_id, name=title)
    row = MusicUserTopic(
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
    bot: Bot,
    tg: TgUser,
    db_user: User,
    existing: MusicUserTopic,
) -> MusicUserTopic:
    title = (f"@{tg.username} | {tg.id}" if tg.username else f"{tg.id}")[:128]
    topic = await bot.create_forum_topic(chat_id=existing.forum_chat_id, name=title)
    existing.topic_id = topic.message_thread_id
    existing.user_id = db_user.id
    await session.flush()
    return existing


def _start_caption() -> str:
    return join_lines(
        plain("Привет! Чтобы найти нужную тебе музыку, отправь мне:"),
        "",
        plain("• Название песни или исполнителя"),
        plain("• Слова из песни"),
        plain("• Видео"),
        plain("• Аудио"),
    )


def _build_results_keyboard(session_id: str, page: int, total_pages: int, n_items: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for i in range(n_items):
        row.append(
            InlineKeyboardButton(
                text=str(i + 1),
                callback_data=f"msc:p:{session_id}:{page}:{i}",
            )
        )
        if len(row) >= 5:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"msc:g:{session_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="msc:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"msc:g:{session_id}:{page + 1}"))
    b.row(*nav)
    return b.as_markup()


def _results_caption(query: str, tracks: list[MediaTrack], page: int, per_page: int) -> str:
    start = page * per_page
    chunk = tracks[start : start + per_page]
    lines = [bold(esc(query)), ""]
    for i, t in enumerate(chunk, start=1):
        lines.append(plain(t.display_line(i)))
    return join_lines(*lines)


async def _run_search_and_reply(message: Message, query: str, settings: MusicBotSettings) -> None:
    q = (query or "").strip()
    if len(q) < 2:
        await message.answer(plain("Слишком короткий запрос. Напишите название, исполнителя или строку из песни."))
        return
    wait = await message.answer(plain("🔎 Ищу…"))
    max_items = settings.music_results_per_page * settings.music_max_pages
    tracks = await search_music_all(
        q,
        vk_token=settings.vk_access_token,
        spotify_client_id=settings.spotify_client_id,
        spotify_client_secret=settings.spotify_client_secret,
        yandex_token=settings.yandex_music_token,
        max_results=max_items,
    )
    if not tracks:
        await wait.edit_text(plain("Ничего не найдено. Попробуйте другой запрос."))
        return
    session_id = new_session_id()
    await save_search_session(
        redis_url=settings.redis_url,
        session_id=session_id,
        query=q,
        tracks=tracks,
    )
    per_page = settings.music_results_per_page
    total_pages = min(
        settings.music_max_pages,
        max(1, (len(tracks) + per_page - 1) // per_page),
    )
    cap = _results_caption(q, tracks, 0, per_page)
    kb = _build_results_keyboard(session_id, 0, total_pages, min(per_page, len(tracks)))
    await wait.edit_text(cap, reply_markup=kb)


async def _log_to_topic(
    *,
    bot: Bot,
    topic: MusicUserTopic,
    text: str,
    audio_message: Message | None = None,
) -> None:
    try:
        await bot.send_message(
            chat_id=topic.forum_chat_id,
            message_thread_id=topic.topic_id,
            text=text,
            parse_mode="MarkdownV2",
        )
        if audio_message is not None:
            await bot.forward_message(
                chat_id=topic.forum_chat_id,
                message_thread_id=topic.topic_id,
                from_chat_id=audio_message.chat.id,
                message_id=audio_message.message_id,
            )
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message thread not found" in err or "topic" in err:
            return
        raise


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, session: AsyncSession) -> None:
    settings = get_musicbot_settings()
    tg = message.from_user
    if tg is None:
        return
    db_user = await _get_or_create_user(session, tg)
    if db_user.is_blocked:
        await message.answer(plain("Ваш аккаунт заблокирован. Обратитесь в поддержку."))
        return
    assert message.bot is not None
    await _ensure_user_topic(
        session=session, bot=message.bot, tg=tg, db_user=db_user, settings=settings
    )
    await message.answer(_start_caption())


@router.callback_query(F.data == "msc:noop")
async def cb_noop(cq: CallbackQuery) -> None:
    await cq.answer()


@router.callback_query(F.data.startswith("msc:g:"))
async def cb_page(cq: CallbackQuery) -> None:
    parts = (cq.data or "").split(":")
    if len(parts) != 4:
        await cq.answer()
        return
    session_id, page_s = parts[2], parts[3]
    try:
        page = int(page_s)
    except ValueError:
        await cq.answer()
        return
    settings = get_musicbot_settings()
    data = await load_search_session(settings.redis_url, session_id)
    if data is None or cq.message is None:
        await cq.answer("Сессия устарела. Отправьте запрос снова.", show_alert=True)
        return
    tracks: list[MediaTrack] = data.get("tracks") or []
    query = str(data.get("query") or "")
    per_page = settings.music_results_per_page
    total_pages = min(
        settings.music_max_pages,
        max(1, (len(tracks) + per_page - 1) // per_page),
    )
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    n_items = min(per_page, len(tracks) - start)
    cap = _results_caption(query, tracks, page, per_page)
    kb = _build_results_keyboard(session_id, page, total_pages, n_items)
    try:
        await cq.message.edit_text(cap, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cq.answer()


@router.callback_query(F.data.startswith("msc:p:"))
async def cb_pick(
    cq: CallbackQuery,
    session: AsyncSession,
) -> None:
    parts = (cq.data or "").split(":")
    if len(parts) != 5:
        await cq.answer()
        return
    session_id, page_s, idx_s = parts[2], parts[3], parts[4]
    try:
        page = int(page_s)
        idx = int(idx_s)
    except ValueError:
        await cq.answer()
        return
    settings = get_musicbot_settings()
    global_index = page * settings.music_results_per_page + idx
    hit = await get_track_from_session(settings.redis_url, session_id, global_index)
    if hit is None or cq.message is None or cq.from_user is None or cq.bot is None:
        await cq.answer("Сессия устарела.", show_alert=True)
        return
    if cq.from_user.is_bot:
        await cq.answer()
        return
    query, track = hit
    await cq.answer("Скачиваю…")
    progress = await cq.message.answer(plain("⏳ Загружаю трек…"))
    db_user = await _get_or_create_user(session, cq.from_user)
    if db_user.is_blocked:
        await progress.edit_text(plain("Аккаунт заблокирован."))
        return
    topic = await _ensure_user_topic(
        session=session, bot=cq.bot, tg=cq.from_user, db_user=db_user, settings=settings
    )
    kb = build_bot_cta_keyboard(settings.bot_username, label_template=settings.bot_cta_label)
    title = f"{track.artist} — {track.title}"[:128]
    try:
        path, tmp = await download_track_to_mp3(track, vk_token=settings.vk_access_token)
        try:
            sent = await cq.message.answer_audio(
                FSInputFile(path),
                title=track.title[:64],
                performer=track.artist[:64],
                reply_markup=kb,
            )
        finally:
            tmp.cleanup()
        await progress.delete()
        meta = join_lines(
            "🎵 " + bold("Скачивание"),
            plain("Запрос: ") + esc(query),
            plain("Трек: ") + esc(title),
            plain("Источник: ") + bold(track.source_label),
            plain("Пользователь: ")
            + esc(f"@{cq.from_user.username}" if cq.from_user.username else str(cq.from_user.id)),
            plain("Время: ") + esc(datetime.now().strftime("%d.%m.%Y %H:%M:%S")),
        )
        await _log_to_topic(bot=cq.bot, topic=topic, text=meta, audio_message=sent)
    except Exception as e:
        logger.exception("music download failed")
        await progress.edit_text(join_lines(plain("Не удалось скачать."), esc(str(e)[:200])))


@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def handle_text(message: Message, session: AsyncSession) -> None:
    settings = get_musicbot_settings()
    tg = message.from_user
    if tg is None or tg.is_bot:
        return
    db_user = await _get_or_create_user(session, tg)
    if db_user.is_blocked:
        await message.answer(plain("Ваш аккаунт заблокирован."))
        return
    assert message.bot is not None
    await _ensure_user_topic(
        session=session, bot=message.bot, tg=tg, db_user=db_user, settings=settings
    )
    text = (message.text or "").strip()
    url = extract_first_url(text)
    if url:
        wait = await message.answer(plain("🎬 Анализирую видео…"))
        q = await query_from_video_url(url)
        if not q:
            await wait.edit_text(plain("Не удалось определить трек по ссылке. Отправьте название текстом."))
            return
        await wait.delete()
        await _run_search_and_reply(message, q, settings)
        return
    await _run_search_and_reply(message, text, settings)


async def _download_tg_file(message: Message, file_id: str, suffix: str) -> Path:
    assert message.bot is not None
    f = await message.bot.get_file(file_id)
    if f.file_path is None:
        raise RuntimeError("Пустой file_path")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    dest = Path(tmp.name)
    await message.bot.download_file(f.file_path, destination=dest)
    return dest


@router.message(F.chat.type == ChatType.PRIVATE, F.audio | F.voice)
async def handle_audio(message: Message, session: AsyncSession) -> None:
    settings = get_musicbot_settings()
    tg = message.from_user
    if tg is None:
        return
    db_user = await _get_or_create_user(session, tg)
    if db_user.is_blocked:
        return
    assert message.bot is not None
    await _ensure_user_topic(
        session=session, bot=message.bot, tg=tg, db_user=db_user, settings=settings
    )
    file_id = message.audio.file_id if message.audio else message.voice.file_id
    wait = await message.answer(plain("🎧 Распознаю аудио…"))
    path = await _download_tg_file(message, file_id, ".ogg" if message.voice else ".mp3")
    try:
        q = await identify_audio_file(path)
    finally:
        path.unlink(missing_ok=True)
    if not q:
        await wait.edit_text(plain("Не удалось распознать трек. Попробуйте отправить название текстом."))
        return
    await wait.delete()
    await _run_search_and_reply(message, q, settings)


@router.message(F.chat.type == ChatType.PRIVATE, F.video | F.video_note)
async def handle_video(message: Message, session: AsyncSession) -> None:
    settings = get_musicbot_settings()
    tg = message.from_user
    if tg is None:
        return
    db_user = await _get_or_create_user(session, tg)
    if db_user.is_blocked:
        return
    assert message.bot is not None
    await _ensure_user_topic(
        session=session, bot=message.bot, tg=tg, db_user=db_user, settings=settings
    )
    file_id = message.video.file_id if message.video else message.video_note.file_id
    wait = await message.answer(plain("🎬 Ищу трек в видео…"))
    vpath = await _download_tg_file(message, file_id, ".mp4")
    try:
        q = await query_from_telegram_video_file(vpath)
    finally:
        vpath.unlink(missing_ok=True)
    if not q:
        await wait.edit_text(plain("Не удалось распознать. Отправьте название или ссылку."))
        return
    await wait.delete()
    await _run_search_and_reply(message, q, settings)
