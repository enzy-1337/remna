"""Redis-сессии результатов поиска музыки (пагинация, выбор трека)."""

from __future__ import annotations

import json
import secrets
from typing import Any

import redis.asyncio as redis

from shared.services.media_search.types import MediaTrack

_SESSION_TTL_SEC = 3600
_KEY_PREFIX = "music:session:"


def _client(redis_url: str) -> redis.Redis:
    return redis.from_url(redis_url, encoding="utf-8", decode_responses=True)


def new_session_id() -> str:
    return secrets.token_urlsafe(9)[:12]


async def save_search_session(
    *,
    redis_url: str,
    session_id: str,
    query: str,
    tracks: list[MediaTrack],
) -> None:
    payload = {
        "query": query,
        "tracks": [t.to_dict() for t in tracks],
    }
    r = _client(redis_url)
    await r.setex(f"{_KEY_PREFIX}{session_id}", _SESSION_TTL_SEC, json.dumps(payload, ensure_ascii=False))


async def load_search_session(redis_url: str, session_id: str) -> dict[str, Any] | None:
    r = _client(redis_url)
    raw = await r.get(f"{_KEY_PREFIX}{session_id}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    tracks_raw = data.get("tracks")
    if not isinstance(tracks_raw, list):
        return None
    data["tracks"] = [MediaTrack.from_dict(x) for x in tracks_raw if isinstance(x, dict)]
    return data


async def get_track_from_session(
    redis_url: str,
    session_id: str,
    global_index: int,
) -> tuple[str, MediaTrack] | None:
    data = await load_search_session(redis_url, session_id)
    if data is None:
        return None
    tracks: list[MediaTrack] = data.get("tracks") or []
    if global_index < 0 or global_index >= len(tracks):
        return None
    query = str(data.get("query") or "")
    return query, tracks[global_index]
