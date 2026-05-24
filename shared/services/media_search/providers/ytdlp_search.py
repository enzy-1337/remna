"""Поиск через yt-dlp (YouTube, SoundCloud)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared.services.media_search.types import MediaTrack

logger = logging.getLogger(__name__)


def _extract_entries(info: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not info:
        return []
    entries = info.get("entries")
    if entries is None and info.get("id"):
        return [info]
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _ytdlp_search_sync(prefix: str, query: str, limit: int) -> list[MediaTrack]:
    import yt_dlp

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    out: list[MediaTrack] = []
    source = "yt" if prefix.startswith("yt") else "sc"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)
        for entry in _extract_entries(info if isinstance(info, dict) else None):
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            artist = str(entry.get("uploader") or entry.get("channel") or entry.get("artist") or "").strip()
            url = entry.get("url") or entry.get("webpage_url")
            if not url and entry.get("id"):
                if source == "sc":
                    url = f"https://soundcloud.com/track/{entry['id']}"
                else:
                    url = f"https://www.youtube.com/watch?v={entry['id']}"
            dur = entry.get("duration")
            duration_sec = int(dur) if dur is not None else None
            out.append(
                MediaTrack(
                    source=source,
                    title=title,
                    artist=artist or "Unknown",
                    duration_sec=duration_sec,
                    url=str(url) if url else None,
                    source_id=str(entry.get("id") or "") or None,
                )
            )
    except Exception:
        logger.exception("ytdlp search failed prefix=%s query=%r", prefix, query[:80])
    return out


async def search_youtube(query: str, *, limit: int = 25) -> list[MediaTrack]:
    return await asyncio.to_thread(_ytdlp_search_sync, "ytsearch", query, limit)


async def search_soundcloud(query: str, *, limit: int = 25) -> list[MediaTrack]:
    return await asyncio.to_thread(_ytdlp_search_sync, "scsearch", query, limit)
