"""Скачивание трека в MP3 (yt-dlp / VK)."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from shared.services.media_search.providers.vk import download_vk_to_path
from shared.services.media_search.providers.ytdlp_search import search_youtube
from shared.services.media_search.types import MediaTrack

logger = logging.getLogger(__name__)


def _ytdlp_download_audio_sync(url: str, out_dir: Path) -> Path:
    import yt_dlp

    outtmpl = str(out_dir / "%(title).120B.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("yt-dlp не вернул данные.")
        base = Path(ydl.prepare_filename(info))
    mp3 = base.with_suffix(".mp3")
    if mp3.is_file():
        return mp3
    if base.is_file():
        return base
    for p in out_dir.iterdir():
        if p.suffix.lower() in (".mp3", ".m4a", ".opus", ".ogg"):
            return p
    raise RuntimeError("Файл после скачивания не найден.")


async def _resolve_download_url(track: MediaTrack, *, vk_token: str) -> str:
    if track.url and track.source in ("yt", "sc"):
        return track.url
    if track.source == "vk":
        from shared.services.media_search.providers.vk import resolve_vk_download_url

        url = await resolve_vk_download_url(track, access_token=vk_token)
        if url:
            return url
    # Spotify / Yandex / без прямой ссылки — ищем на YouTube
    q = f"{track.artist} {track.title}".strip()
    hits = await search_youtube(q, limit=3)
    for hit in hits:
        if hit.url:
            return hit.url
    raise RuntimeError("Не найден источник для скачивания.")


async def download_track_to_mp3(
    track: MediaTrack,
    *,
    vk_token: str = "",
) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory(prefix="music_dl_")
    out_dir = Path(tmp.name)
    if track.source == "vk":
        dest = out_dir / "track.mp3"
        await download_vk_to_path(track, dest, access_token=vk_token)
        return dest, tmp
    url = await _resolve_download_url(track, vk_token=vk_token)
    path = await asyncio.to_thread(_ytdlp_download_audio_sync, url, out_dir)
    return path, tmp
