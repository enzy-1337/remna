"""Поиск метаданных Spotify (Client Credentials). Скачивание — через URL из других источников."""

from __future__ import annotations

import logging
import time

import httpx

from shared.services.media_search.types import MediaTrack

logger = logging.getLogger(__name__)

_token_cache: tuple[str, float] | None = None


async def _spotify_token(client_id: str, client_secret: str) -> str | None:
    global _token_cache
    now = time.time()
    if _token_cache and _token_cache[1] > now + 30:
        return _token_cache[0]
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
            )
            r.raise_for_status()
            data = r.json()
        token = str(data.get("access_token") or "")
        expires = int(data.get("expires_in") or 3600)
        if token:
            _token_cache = (token, now + expires)
            return token
    except Exception:
        logger.exception("Spotify token failed")
    return None


async def search_spotify(
    query: str,
    *,
    client_id: str,
    client_secret: str,
    limit: int = 20,
) -> list[MediaTrack]:
    cid = (client_id or "").strip()
    sec = (client_secret or "").strip()
    if not cid or not sec:
        return []
    token = await _spotify_token(cid, sec)
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                "https://api.spotify.com/v1/search",
                params={"q": query, "type": "track", "limit": min(limit, 50)},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        logger.exception("Spotify search failed")
        return []
    items = ((data.get("tracks") or {}).get("items")) or []
    out: list[MediaTrack] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or "").strip()
        if not title:
            continue
        artists = item.get("artists") or []
        artist = ", ".join(
            str(a.get("name") or "").strip()
            for a in artists
            if isinstance(a, dict) and (a.get("name") or "").strip()
        ) or "Unknown"
        dur_ms = item.get("duration_ms")
        duration_sec = int(dur_ms // 1000) if isinstance(dur_ms, int) else None
        ext_url = (item.get("external_urls") or {}).get("spotify")
        out.append(
            MediaTrack(
                source="sf",
                title=title,
                artist=artist,
                duration_sec=duration_sec,
                url=str(ext_url) if ext_url else None,
                source_id=str(item.get("id") or "") or None,
            )
        )
    return out
