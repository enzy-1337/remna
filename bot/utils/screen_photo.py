"""Единый стиль экранов: фото + подпись + кнопки; удаление предыдущего сообщения."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, FSInputFile, InputFile, Message, URLInputFile

if TYPE_CHECKING:
    from shared.config import Settings

logger = logging.getLogger(__name__)

TELEGRAM_PHOTO_CAPTION_MAX = 1024


async def safe_callback_answer(cq: CallbackQuery, **kwargs: Any) -> None:
    """
    Ответ на callback_query. Игнорируем устаревший/повторный query — иначе падает весь хендлер и откатывается БД.
    """
    try:
        await cq.answer(**kwargs)
    except TelegramBadRequest as e:
        msg = (getattr(e, "message", None) or str(e)).lower()
        if any(
            part in msg
            for part in (
                "query is too old",
                "response timeout expired",
                "query id is invalid",
                "already been answered",
                "already answered",
            )
        ):
            logger.debug("callback answer skipped: %s", e)
            return
        raise


def truncate_caption(text: str, max_len: int = TELEGRAM_PHOTO_CAPTION_MAX) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def resolve_section_photo(settings: Settings) -> InputFile | None:
    """Локальный файл (приоритет) или URL; иначе None — только текст."""
    url = (settings.bot_section_photo_url or "").strip()
    if url:
        return URLInputFile(url)
    raw_path = (settings.bot_section_photo_path or "").strip()
    if raw_path:
        p = Path(raw_path)
        if p.is_file():
            return FSInputFile(p)
    default = Path(__file__).resolve().parent.parent / "assets" / "section_header.png"
    if default.is_file():
        return FSInputFile(default)
    return None


async def delete_message_safe(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("delete_message failed", exc_info=True)


async def send_profile_screen(
    bot: Bot,
    *,
    chat_id: int,
    caption: str,
    reply_markup,
    settings: Settings,
    delete_message: Message | None = None,
) -> Message:
    await delete_message_safe(delete_message)
    cap = truncate_caption(caption)
    photo = resolve_section_photo(settings)
    if photo is not None:
        return await bot.send_photo(
            chat_id,
            photo,
            caption=cap,
            reply_markup=reply_markup,
        )
    return await bot.send_message(chat_id, cap, reply_markup=reply_markup)


async def answer_callback_with_photo_screen(
    cq: CallbackQuery,
    *,
    caption: str,
    reply_markup,
    settings: Settings,
) -> Message | None:
    if cq.message is None or cq.bot is None:
        return None
    await safe_callback_answer(cq)
    return await send_profile_screen(
        cq.bot,
        chat_id=cq.message.chat.id,
        caption=caption,
        reply_markup=reply_markup,
        settings=settings,
        delete_message=cq.message,
    )
