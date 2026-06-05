"""Скачивание пинов Pinterest (видео и фото) для Reels-бота."""

from __future__ import annotations

import json
import logging
import re
import secrets
import shlex
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PINTEREST_HOST_RE = re.compile(
    r"(?:https?://)?(?:[^/]+\.)?pinterest\.(?:"
    r"com|fr|de|ch|jp|cl|ca|it|co\.uk|nz|ru|com\.au|at|pt|co\.kr|es|com\.mx|"
    r"dk|ph|th|com\.uy|co|nl|info|kr|ie|vn|com\.vn|ec|mx|in|pe|co\.at|hu|"
    r"co\.in|co\.nz|id|com\.ec|com\.py|tw|be|uk|com\.bo|com\.pe)",
    re.I,
)
_PIN_ID_RE = re.compile(r"/pin/(?:[\w-]+--)?(\d+)", re.I)
_PIN_IT_RE = re.compile(r"pin\.it/", re.I)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_PREFERRED_MP4 = ("V_720P", "V_540P", "V_EXP7", "V_EXP6", "V_EXP5")
_PREFERRED_HLS = ("V_HLSV4", "V_HLSV3_WEB", "V_HLSV3_MOBILE", "V_HLSV3")


@dataclass(slots=True)
class PinterestDownload:
    path: Path
    duration_sec: int
    size_bytes: int
    photo_paths: list[Path] | None = None
    is_gif: bool = False


class PinterestEmbedPin(Exception):
    """Пин с внешним embed — нужен yt-dlp."""


def is_pinterest_url(url: str) -> bool:
    u = (url or "").lower()
    if _PIN_IT_RE.search(u):
        return True
    return bool(_PINTEREST_HOST_RE.search(u) and "/pin/" in u)


def extract_pin_id(url: str) -> str | None:
    m = _PIN_ID_RE.search(url or "")
    return m.group(1) if m else None


async def resolve_pinterest_url(url: str) -> str:
    u = (url or "").strip()
    if not _PIN_IT_RE.search(u):
        return u
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            r = await client.get(u, headers={"User-Agent": _USER_AGENT})
            final = str(r.url).strip()
            return final or u
    except Exception:
        logger.exception("Failed to resolve pin.it URL: %s", u)
        return u


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
        u = str(raw or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _call_pin_api(pin_id: str) -> dict[str, Any]:
    csrf = secrets.token_hex(16)
    source_url = f"/pin/{pin_id}/"
    params = {
        "source_url": source_url,
        "data": json.dumps(
            {
                "options": {
                    "id": pin_id,
                    "field_set_key": "unauth_react_main_pin",
                }
            },
            separators=(",", ":"),
        ),
    }
    query = urllib.parse.urlencode(params)
    api_url = f"https://www.pinterest.com/resource/PinResource/get/?{query}"
    handlers = (
        f"www/pin/{pin_id}.js",
        "www/[username].js",
        f"pin/{pin_id}.js",
    )
    last_error: Exception | None = None
    for handler in handlers:
        req = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "X-Pinterest-PWS-Handler": handler,
                "X-Pinterest-Source-Url": source_url,
                "User-Agent": _USER_AGENT,
                "Cookie": f"csrftoken={csrf}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", "ignore"))
            data = payload.get("resource_response", {}).get("data")
            if isinstance(data, dict):
                return data
        except Exception as exc:
            last_error = exc
            logger.debug("Pinterest API handler=%s failed pin=%s: %s", handler, pin_id, exc)
    if last_error is not None:
        raise RuntimeError(f"Pinterest API недоступен для pin {pin_id}: {last_error}") from last_error
    raise RuntimeError(f"Pinterest API не вернул данные для pin {pin_id}")


def _video_lists_from_pin(pin: dict[str, Any]) -> list[dict[str, Any]]:
    lists: list[dict[str, Any]] = []
    videos = pin.get("videos")
    if isinstance(videos, dict):
        video_list = videos.get("video_list")
        if isinstance(video_list, dict):
            lists.append(video_list)
    story = pin.get("story_pin_data")
    if isinstance(story, dict):
        for page in story.get("pages") or []:
            if not isinstance(page, dict):
                continue
            for block in page.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                video = block.get("video")
                if isinstance(video, dict):
                    video_list = video.get("video_list")
                    if isinstance(video_list, dict):
                        lists.append(video_list)
    return lists


def _pick_video(video_lists: list[dict[str, Any]]) -> tuple[str | None, int]:
    for video_list in video_lists:
        for key in _PREFERRED_MP4:
            fmt = video_list.get(key)
            if isinstance(fmt, dict):
                url = str(fmt.get("url") or "").strip()
                if url and "m3u8" not in url.lower():
                    duration = int((fmt.get("duration") or 0) / 1000)
                    return url, duration
        for fmt in video_list.values():
            if not isinstance(fmt, dict):
                continue
            url = str(fmt.get("url") or "").strip()
            if url and "m3u8" not in url.lower():
                duration = int((fmt.get("duration") or 0) / 1000)
                return url, duration
    for video_list in video_lists:
        for key in _PREFERRED_HLS:
            fmt = video_list.get(key)
            if isinstance(fmt, dict):
                url = str(fmt.get("url") or "").strip()
                if url:
                    duration = int((fmt.get("duration") or 0) / 1000)
                    return url, duration
        for fmt in video_list.values():
            if not isinstance(fmt, dict):
                continue
            url = str(fmt.get("url") or "").strip()
            if url:
                duration = int((fmt.get("duration") or 0) / 1000)
                return url, duration
    return None, 0


def _image_url_from_block(block: dict[str, Any], page: dict[str, Any]) -> str | None:
    image = block.get("image")
    if isinstance(image, dict):
        images = image.get("images") or {}
        for key in ("originals", "orig", "736x", "564x"):
            item = images.get(key)
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                if url:
                    return url
    sig = str(block.get("image_signature") or page.get("image_signature") or "").strip()
    if len(sig) >= 6:
        return f"https://i.pinimg.com/originals/{sig[0:2]}/{sig[2:4]}/{sig[4:6]}/{sig}.jpg"
    return None


def _collect_image_urls(pin: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    carousel = pin.get("carousel_data")
    if isinstance(carousel, dict):
        for slot in carousel.get("carousel_slots") or []:
            if not isinstance(slot, dict):
                continue
            images = slot.get("images") or {}
            for key in ("orig", "736x", "564x"):
                item = images.get(key)
                if isinstance(item, dict):
                    url = str(item.get("url") or "").strip()
                    if url:
                        urls.append(url.replace(f"/{key}/", "/originals/", 1) if key != "orig" else url)
                        break
    if urls:
        return _dedupe_urls(urls)

    story = pin.get("story_pin_data")
    if isinstance(story, dict):
        for page in story.get("pages") or []:
            if not isinstance(page, dict):
                continue
            for block in page.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")
                if block_type == "story_pin_image_block" or block.get("image"):
                    url = _image_url_from_block(block, page)
                    if url:
                        urls.append(url)
    if urls:
        return _dedupe_urls(urls)

    images = pin.get("images") or {}
    if isinstance(images, dict):
        for key in ("orig", "736x", "564x"):
            item = images.get(key)
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                if url:
                    urls.append(url)
                    break
    return _dedupe_urls(urls)


def _mp4_has_no_audio(path: Path) -> bool:
    """Return True if the mp4 has no audio stream (typical for GIF-converted-to-mp4)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True,
            timeout=10,
        )
        return proc.stdout.strip() == b""
    except Exception:
        return False


def _download_file(url: str, dest: Path) -> None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Referer": "https://www.pinterest.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        dest.write_bytes(resp.read())


def _download_hls_to_mp4(m3u8_url: str, dest: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        m3u8_url,
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        str(dest),
    ]
    logger.info("Pinterest HLS download: %s", shlex.join(cmd))
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
        return
    stderr = (proc.stderr or b"").decode("utf-8", "ignore")[:300]
    cmd_reencode = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        m3u8_url,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        str(dest),
    ]
    proc2 = subprocess.run(cmd_reencode, capture_output=True, timeout=300)
    if proc2.returncode != 0 or not dest.exists() or dest.stat().st_size <= 0:
        err2 = (proc2.stderr or b"").decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"Не удалось скачать видео Pinterest (HLS): {stderr or err2}")


def _download_images(urls: list[str], temp_dir: str, pin_id: str) -> list[Path]:
    photo_paths: list[Path] = []
    for idx, image_url in enumerate(urls[:10], start=1):
        ext = ".jpg"
        low = image_url.lower()
        if ".png" in low:
            ext = ".png"
        elif ".webp" in low:
            ext = ".webp"
        photo_path = Path(temp_dir) / f"pinterest_{pin_id}_{idx:02d}{ext}"
        try:
            _download_file(image_url, photo_path)
        except Exception:
            logger.exception("Pinterest image download failed url=%s", image_url)
            continue
        if photo_path.exists() and photo_path.stat().st_size > 0:
            photo_paths.append(photo_path)
    if not photo_paths:
        raise RuntimeError("Pinterest не отдал изображения по этой ссылке.")
    return photo_paths


def download_pinterest_sync(url: str, temp_dir: str) -> PinterestDownload:
    pin_id = extract_pin_id(url)
    if not pin_id:
        raise RuntimeError("Не удалось определить ID Pinterest-пина.")

    pin = _call_pin_api(pin_id)

    pin_type = str(pin.get("type") or "").lower()
    is_gif = pin_type == "gif" or bool(pin.get("is_gif"))

    # Сначала пробуем видео — оно важнее embed-статуса
    video_url, duration_sec = _pick_video(_video_lists_from_pin(pin))
    if video_url:
        dest = Path(temp_dir) / f"pinterest_{pin_id}.mp4"
        if "m3u8" in video_url.lower():
            _download_hls_to_mp4(video_url, dest)
        else:
            _download_file(video_url, dest)
        if not dest.exists() or dest.stat().st_size <= 0:
            raise RuntimeError("Файл видео Pinterest не найден после скачивания.")
        # Treat silent short clips as GIF (Pinterest stores GIFs as mp4)
        if not is_gif and duration_sec > 0 and duration_sec <= 15:
            is_gif = _mp4_has_no_audio(dest)
        return PinterestDownload(
            path=dest,
            duration_sec=duration_sec,
            size_bytes=int(dest.stat().st_size),
            is_gif=is_gif,
        )

    # Пробуем изображения — даже для embed-пинов они могут быть в API
    image_urls = _collect_image_urls(pin)
    if image_urls:
        photo_paths = _download_images(image_urls, temp_dir, pin_id)
        first = photo_paths[0]
        return PinterestDownload(
            path=first,
            duration_sec=0,
            size_bytes=sum(int(p.stat().st_size) for p in photo_paths),
            photo_paths=photo_paths,
        )

    # Если изображений нет — проверяем embed (для yt-dlp fallback)
    domain = str(pin.get("domain") or "")
    embed = pin.get("embed")
    embed_src = embed.get("src") if isinstance(embed, dict) else None
    if domain.lower() != "uploaded by user" and embed_src:
        raise PinterestEmbedPin(str(embed_src))

    raise RuntimeError("На этом пине нет доступного видео или изображений.")
