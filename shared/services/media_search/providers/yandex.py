"""Поиск Яндекс.Музыки (неофициальный API при наличии токена)."""

from __future__ import annotations

import logging

import httpx

from shared.services.media_search.types import MediaTrack

logger = logging.getLogger(__name__)


async def search_yandex(query: str, *, token: str, limit: int = 20) -> list[MediaTrack]:
    tok = (token or "").strip()
    if not tok:
        return []
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.get(
                "https://api.music.yandex.net/search",
                params={"text": query, "type": "track", "page": 0, "nocorrect": "false"},
                headers={"Authorization": f"OAuth {tok}"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        logger.exception("Yandex Music search failed")
        return []
    tracks_block = ((data.get("result") or {}).get("tracks") or {})
    items = tracks_block.get("results") or []
    out: list[MediaTrack] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        artists = item.get("artists") or []
        artist = ", ".join(
            str(a.get("name") or "").strip()
            for a in artists
            if isinstance(a, dict) and (a.get("name") or "").strip()
        ) or "Unknown"
        dur_ms = item.get("durationMs")
        duration_sec = int(dur_ms // 1000) if isinstance(dur_ms, int) else None
        track_id = item.get("id")
        album_id = None
        albums = item.get("albums") or []
        if albums and isinstance(albums[0], dict):
            album_id = albums[0].get("id")
        out.append(
            MediaTrack(
                source="ya",
                title=title,
                artist=artist,
                duration_sec=duration_sec,
                url=None,
                source_id=str(track_id) if track_id is not None else None,
                extra={"album_id": album_id},
            )
        )
        if len(out) >= limit:
            break
    return out
