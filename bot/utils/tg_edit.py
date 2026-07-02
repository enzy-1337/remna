"""Safe message-edit helpers.

Editing a callback's message can fail when that message was deleted, is too old,
or is unchanged — Telegram answers with ``TelegramBadRequest``. These helpers
swallow those benign cases so a stale button tap never crashes an update handler.
"""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

logger = logging.getLogger(__name__)

# Telegram error substrings that mean "nothing to do / message gone" — safe to ignore.
_BENIGN = (
    "message is not modified",
    "message to edit not found",
    "message can't be edited",
    "message to be edited not found",
)


def _is_benign(err: TelegramBadRequest) -> bool:
    msg = str(err).lower()
    return any(b in msg for b in _BENIGN)


async def safe_edit(
    message: Message | None,
    text: str,
    *,
    reply_markup=None,
    **kwargs,
) -> bool:
    """Edit a message's caption (if it has a photo) or text, ignoring benign errors.

    Returns True if the edit went through, False if it was skipped.
    """
    if message is None:
        return False
    try:
        if message.photo:
            await message.edit_caption(caption=text, reply_markup=reply_markup, **kwargs)
        else:
            await message.edit_text(text, reply_markup=reply_markup, **kwargs)
        return True
    except TelegramBadRequest as e:
        if _is_benign(e):
            logger.debug("safe_edit skipped: %s", e)
            return False
        raise


async def safe_edit_markup(message: Message | None, reply_markup=None) -> bool:
    """Edit only a message's inline keyboard, ignoring benign errors."""
    if message is None:
        return False
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
        return True
    except TelegramBadRequest as e:
        if _is_benign(e):
            logger.debug("safe_edit_markup skipped: %s", e)
            return False
        raise
