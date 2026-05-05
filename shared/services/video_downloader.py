"""Скачивание коротких видео (Reels/Shorts/TikTok) через yt-dlp."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import tempfile
import urllib.request
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
    photo_paths: list[Path] | None = None


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
    if "youtube.com/shorts/" in u:
        return "YouTube Shorts"
    return "Unknown"


def is_supported_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        x in u
        for x in (
            "instagram.com",
            "tiktok.com",
            "youtube.com/shorts/",
            "vk.com/clip",
            "clips.vk.com",
            "vk.ru/clip",
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
        images = info.get("images") or []
        if images:
            photo_paths: list[Path] = []
            for idx, image in enumerate(images, start=1):
                image_url = ""
                if isinstance(image, dict):
                    # Для TikTok image-post обычно URL без водяного знака лежит в url_list.
                    url_list = image.get("url_list") or []
                    if isinstance(url_list, list):
                        for candidate in url_list:
                            c = str(candidate or "").strip()
                            if c:
                                image_url = c
                                break
                    if not image_url:
                        for key in ("display_image_url", "image_url", "url"):
                            c = str(image.get(key) or "").strip()
                            if c:
                                image_url = c
                                break
                if not image_url:
                    continue
                photo_path = Path(temp_dir) / f"{info.get('id') or 'item'}_{idx:02d}.jpg"
                with urllib.request.urlopen(image_url, timeout=20) as resp:
                    photo_path.write_bytes(resp.read())
                if photo_path.exists() and photo_path.stat().st_size > 0:
                    photo_paths.append(photo_path)
            if photo_paths:
                first = photo_paths[0]
                return DownloadedVideo(
                    path=first,
                    platform=detect_platform(url),
                    duration_sec=0,
                    size_bytes=sum(int(p.stat().st_size) for p in photo_paths),
                    original_url=url,
                    photo_paths=photo_paths,
                )
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
