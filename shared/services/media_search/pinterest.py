"""Pinterest (заготовка для Reels-бота)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def fetch_pinterest_media(url: str) -> None:
    """Будущая интеграция с Reels-ботом."""
    _ = url
    raise NotImplementedError("Pinterest: интеграция запланирована в shared/services/media_search")
