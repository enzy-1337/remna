"""Слияние результатов поиска: приоритет VK, дедупликация."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

from shared.services.media_search.providers.spotify import search_spotify
from shared.services.media_search.providers.vk import search_vk
from shared.services.media_search.providers.yandex import search_yandex
from shared.services.media_search.providers.ytdlp_search import search_soundcloud, search_youtube
from shared.services.media_search.types import MediaTrack

logger = logging.getLogger(__name__)


def _norm_key(track: MediaTrack) -> str:
    raw = f"{track.artist}|{track.title}".lower()
    raw = unicodedata.normalize("NFKD", raw)
    raw = re.sub(r"[^\w\s|]", " ", raw, flags=re.UNICODE)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def merge_tracks_vk_first(tracks: list[MediaTrack], *, max_items: int) -> list[MediaTrack]:
    vk: list[MediaTrack] = []
    other: list[MediaTrack] = []
    for t in tracks:
        if t.source == "vk":
            vk.append(t)
        else:
            other.append(t)
    seen: set[str] = set()
    merged: list[MediaTrack] = []
    for t in vk + other:
        key = _norm_key(t)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(t)
        if len(merged) >= max_items:
            break
    return merged


async def search_music_all(
    query: str,
    *,
    vk_token: str = "",
    spotify_client_id: str = "",
    spotify_client_secret: str = "",
    yandex_token: str = "",
    max_results: int = 80,
    per_provider_limit: int = 25,
) -> list[MediaTrack]:
    q = (query or "").strip()
    if not q:
        return []

    tasks = [
        search_vk(q, access_token=vk_token, limit=per_provider_limit),
        search_youtube(q, limit=per_provider_limit),
        search_soundcloud(q, limit=per_provider_limit),
        search_spotify(
            q,
            client_id=spotify_client_id,
            client_secret=spotify_client_secret,
            limit=per_provider_limit,
        ),
        search_yandex(q, token=yandex_token, limit=per_provider_limit),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    flat: list[MediaTrack] = []
    for batch in results:
        if isinstance(batch, Exception):
            logger.warning("music search provider error: %s", batch)
            continue
        flat.extend(batch)
    return merge_tracks_vk_first(flat, max_items=max_results)
