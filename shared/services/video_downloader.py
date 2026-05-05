"""Скачивание коротких видео (Reels/Shorts/TikTok) через yt-dlp."""

from __future__ import annotations

import asyncio
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from yt_dlp import YoutubeDL

_URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)


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
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
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
