"""Распознавание аудио (Shazam) и извлечение запроса из видео/файла."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


async def identify_audio_file(path: Path) -> str | None:
    """Shazam-подобное распознавание → строка «Artist Title» для поиска."""
    try:
        from shazamio import Shazam

        shazam = Shazam()
        result = await shazam.recognize_song(str(path))
        track = result.get("track") if isinstance(result, dict) else None
        if isinstance(track, dict):
            title = (track.get("title") or "").strip()
            subtitle = (track.get("subtitle") or "").strip()
            if title and subtitle:
                return f"{subtitle} {title}"
            if title:
                return title
    except Exception:
        logger.exception("Shazam identify failed")
    return _tags_from_file(path)


def _tags_from_file(path: Path) -> str | None:
    try:
        from mutagen import File as MutagenFile

        meta = MutagenFile(path)
        if meta is None:
            return None
        artist = ""
        title = ""
        if getattr(meta, "tags", None):
            tags = meta.tags
            artist = str(tags.get("TPE1") or tags.get("artist") or [""])[0] if hasattr(tags, "get") else ""
            title = str(tags.get("TIT2") or tags.get("title") or [""])[0] if hasattr(tags, "get") else ""
        if isinstance(artist, list):
            artist = artist[0] if artist else ""
        if isinstance(title, list):
            title = title[0] if title else ""
        artist = str(artist).strip()
        title = str(title).strip()
        if artist and title:
            return f"{artist} {title}"
        if title:
            return title
    except Exception:
        logger.debug("mutagen tags read failed", exc_info=True)
    return None


def _ytdlp_video_query_sync(url: str) -> str | None:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            return None
        title = str(info.get("title") or "").strip()
        uploader = str(info.get("uploader") or info.get("channel") or "").strip()
        if uploader and title:
            return f"{uploader} {title}"
        return title or None
    except Exception:
        logger.exception("yt-dlp video info failed url=%s", url[:120])
        return None


async def query_from_video_url(url: str) -> str | None:
    return await asyncio.to_thread(_ytdlp_video_query_sync, url)


def _extract_audio_from_video_sync(video_path: Path, out_path: Path) -> None:
    import subprocess

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "4",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


async def query_from_telegram_video_file(video_path: Path) -> str | None:
    """Из видеофайла: ffmpeg → mp3 → Shazam."""
    with tempfile.TemporaryDirectory(prefix="music_vid_") as td:
        audio = Path(td) / "clip.mp3"
        try:
            await asyncio.to_thread(_extract_audio_from_video_sync, video_path, audio)
        except Exception:
            logger.exception("ffmpeg extract audio failed")
            return None
        return await identify_audio_file(audio)


_URL_RE = re.compile(r"https?://\S+", re.I)


def extract_first_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(").,]") if m else None
