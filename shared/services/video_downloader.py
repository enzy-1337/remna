"""Скачивание коротких видео (Reels/Shorts/TikTok) через yt-dlp."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

_URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DownloadedVideo:
    path: Path
    platform: str
    duration_sec: int
    size_bytes: int
    original_url: str


def extract_first_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    return m.group(0) if m else None


def detect_platform(url: str) -> str:
    u = (url or "").lower()
    if "instagram.com" in u:
        return "Instagram Reels"
    if "tiktok.com" in u:
        return "TikTok"
    if "vk.com/clip" in u or "clips.vk.com" in u or "vk.ru/clip" in u:
        return "VK Clips"
    if (
        "vkvideo.ru" in u
        or "vk.com/video" in u
        or "m.vk.com/video" in u
        or "vk.ru/video" in u
        or "m.vk.ru/video" in u
    ):
        return "VK Видео"
    if "youtube.com/shorts/" in u:
        return "YouTube Shorts"
    if "youtu.be/" in u or "youtube.com/watch" in u or "youtube.com/" in u:
        return "YouTube"
    return "Unknown"


def is_supported_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        x in u
        for x in (
            "instagram.com",
            "tiktok.com",
            "youtube.com",
            "youtu.be",
            "vkvideo.ru",
            "vk.com/video",
            "vk.com/clip",
            "m.vk.com/video",
            "clips.vk.com",
            "vk.ru/video",
            "vk.ru/clip",
            "m.vk.ru/video",
        )
    )


def is_short_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        x in u
        for x in (
            "vk.cc/",
            "bit.ly/",
            "t.co/",
            "tinyurl.com/",
            "goo.su/",
            "clck.ru/",
            "cutt.ly/",
            "is.gd/",
            "tiny.one/",
        )
    )


async def resolve_short_url(url: str) -> str:
    if not is_short_url(url):
        return url
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            r = await client.get(url)
            final = str(r.url)
            return final or url
    except Exception:
        return url


def _download_sync(url: str, temp_dir: str) -> DownloadedVideo:
    outtmpl = str(Path(temp_dir) / "%(id)s.%(ext)s")
    ydl_opts: dict[str, Any] = {
        "format": "bestvideo+bestaudio/best",
        "noplaylist": True,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://vk.com/",
        },
    }
    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except DownloadError as e:
            msg = str(e)
            logger.warning("yt-dlp download failed for url=%s: %s", url, msg)
            if "Unsupported URL" in msg or "No video formats found" in msg:
                raise RuntimeError("Площадка не отдала видео по этой ссылке (возможно приватный ролик или ограничения доступа).")
            if "This video is private" in msg or "Login required" in msg:
                raise RuntimeError("Видео приватное или требует авторизацию.")
            raise RuntimeError(f"Ошибка скачивания: {msg[:300]}")
        if info is None:
            raise RuntimeError("Не удалось получить информацию о видео.")
        if "entries" in info and info["entries"]:
            info = info["entries"][0]
        file_path = Path(ydl.prepare_filename(info))
        if file_path.suffix.lower() != ".mp4":
            candidate = file_path.with_suffix(".mp4")
            if candidate.exists():
                file_path = candidate
        if not file_path.exists():
            raise RuntimeError("Файл видео не найден после скачивания.")
        return DownloadedVideo(
            path=file_path,
            platform=detect_platform(url),
            duration_sec=int(info.get("duration") or 0),
            size_bytes=int(file_path.stat().st_size),
            original_url=url,
        )


async def download_video(url: str) -> tuple[DownloadedVideo, tempfile.TemporaryDirectory[str]]:
    temp_dir = tempfile.TemporaryDirectory(prefix="tg-video-")
    try:
        result = await asyncio.to_thread(_download_sync, url, temp_dir.name)
        return result, temp_dir
    except Exception:
        temp_dir.cleanup()
        raise


async def compress_video_to_limit(
    *,
    source_path: Path,
    duration_sec: int,
    max_size_mb: int,
) -> Path | None:
    """
    Сжимает видео под лимит размера.
    Возвращает путь к сжатому файлу или None, если сжать не удалось.
    """
    if duration_sec <= 0 or max_size_mb <= 0:
        return None
    out_path = source_path.with_name(source_path.stem + "_compressed.mp4")
    max_bytes = max_size_mb * 1024 * 1024
    # Бюджет на аудио + контейнер, остальное отдаём видео.
    audio_kbps = 96
    total_kbps = int((max_bytes * 8) / max(duration_sec, 1) / 1000)
    video_kbps = max(total_kbps - audio_kbps - 32, 200)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-c:v",
        "libx264",
        "-b:v",
        f"{video_kbps}k",
        "-maxrate",
        f"{video_kbps}k",
        "-bufsize",
        f"{video_kbps * 2}k",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_kbps}k",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    logger.info("Compress video with ffmpeg: %s", shlex.join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("ffmpeg compression failed rc=%s err=%s", proc.returncode, (stderr or b"").decode("utf-8", "ignore")[:800])
        return None
    if not out_path.exists():
        return None
    if out_path.stat().st_size > max_bytes:
        logger.warning(
            "compressed video still too large size_mb=%.2f limit_mb=%s",
            out_path.stat().st_size / 1024 / 1024,
            max_size_mb,
        )
        return None
    return out_path
