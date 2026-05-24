"""Поиск и скачивание VK Music (audio.search)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from shared.services.media_search.types import MediaTrack

logger = logging.getLogger(__name__)
_VK_API = "https://api.vk.com/method"


async def search_vk(query: str, *, access_token: str, limit: int = 30) -> list[MediaTrack]:
    token = (access_token or "").strip()
    if not token:
        return []
    params = {
        "q": query,
        "count": min(max(limit, 1), 200),
        "access_token": token,
        "v": "5.199",
        "auto_complete": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.get(f"{_VK_API}/audio.search", params=params)
            data = r.json()
    except Exception:
        logger.exception("VK audio.search failed")
        return []
    if "error" in data:
        logger.warning("VK API error: %s", data.get("error"))
        return []
    items = (data.get("response") or {}).get("items") or []
    out: list[MediaTrack] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        artist = str(item.get("artist") or item.get("subtitle") or "Unknown").strip()
        owner_id = item.get("owner_id")
        audio_id = item.get("id")
        source_id = f"{owner_id}_{audio_id}" if owner_id is not None and audio_id is not None else None
        out.append(
            MediaTrack(
                source="vk",
                title=title,
                artist=artist,
                duration_sec=int(item["duration"]) if item.get("duration") is not None else None,
                url=str(item["url"]) if item.get("url") else None,
                source_id=source_id,
                extra={"access_key": item.get("access_key")},
            )
        )
    return out


async def _vk_audio_get_url_sync(access_token: str, source_id: str, access_key: str | None) -> str | None:
    owner_s, _, aud_s = source_id.partition("_")
    if not owner_s or not aud_s:
        return None
    params: dict[str, Any] = {
        "audios": f"{owner_s}_{aud_s}",
        "access_token": access_token,
        "v": "5.199",
    }
    if access_key:
        params["audios"] = f"{owner_s}_{aud_s}_{access_key}"
    try:
        with httpx.Client(timeout=25.0) as client:
            r = client.get(f"{_VK_API}/audio.getById", params=params)
            data = r.json()
        items = (data.get("response") or [])
        if items and isinstance(items[0], dict) and items[0].get("url"):
            return str(items[0]["url"])
    except Exception:
        logger.exception("VK audio.getById failed id=%s", source_id)
    return None


async def resolve_vk_download_url(
    track: MediaTrack,
    *,
    access_token: str,
) -> str | None:
    if track.url:
        return track.url
    if not track.source_id:
        return None
    access_key = (track.extra or {}).get("access_key") if track.extra else None
    return await asyncio.to_thread(
        _vk_audio_get_url_sync,
        access_token,
        track.source_id,
        str(access_key) if access_key else None,
    )


async def download_vk_to_path(
    track: MediaTrack,
    dest: Path,
    *,
    access_token: str,
) -> Path:
    url = await resolve_vk_download_url(track, access_token=access_token)
    if not url:
        raise RuntimeError("Не удалось получить ссылку VK для скачивания.")
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest
