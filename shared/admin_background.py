"""Фон web-admin: общий для сайта и баннеров экранов админ-меню в боте."""

from __future__ import annotations

from pathlib import Path

from aiogram.types import FSInputFile, InputFile, URLInputFile

from shared.config import Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADMIN_ASSETS_DIR = _REPO_ROOT / "assets"
_BOT_WEB_ADMIN_BANNER = _REPO_ROOT / "assets" / "banners" / "web_admin.png"


def admin_assets_dir() -> Path:
    return _ADMIN_ASSETS_DIR


def _is_image_asset(name: str) -> bool:
    return name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def admin_background_local_path(settings: Settings) -> Path | None:
    """Локальный файл фона (asset или fallback web_admin.png)."""
    src = (settings.admin_background_source or "default").strip().lower()
    if src == "asset":
        raw = (settings.admin_background_asset or "").strip()
        if raw:
            candidate = Path(raw).name
            if _is_image_asset(candidate):
                p = _ADMIN_ASSETS_DIR / candidate
                if p.is_file():
                    return p
    if src == "default" and _BOT_WEB_ADMIN_BANNER.is_file():
        return _BOT_WEB_ADMIN_BANNER
    return None


def admin_background_url(settings: Settings) -> str | None:
    src = (settings.admin_background_source or "default").strip().lower()
    if src != "url":
        return None
    u = (settings.admin_background_url or "").strip()
    if u.startswith(("http://", "https://")):
        return u
    return None


def resolve_admin_background_photo(settings: Settings) -> InputFile | None:
    """Картинка для Telegram: тот же источник, что фон web-admin."""
    url = admin_background_url(settings)
    if url:
        return URLInputFile(url)
    path = admin_background_local_path(settings)
    if path is not None:
        return FSInputFile(path)
    return None


def admin_bot_screen_uses_web_background(photo_key: str) -> bool:
    """
    Экраны «меню админ-панели» — общий фон web-admin.
    Карточки пользователей, поиск, рассылка, метрики — свои баннеры.
    """
    key = (photo_key or "").strip().lower()
    if not key.startswith("admin:"):
        return False
    if key.startswith(("admin:users:", "admin:u:", "admin:subs:")):
        return False
    if key.startswith("admin:find"):
        return False
    if key.startswith("admin:broadcast"):
        return False
    if key.startswith(("admin:metrics", "admin:calc_payg", "admin:transition_calc")):
        return False
    if key.startswith("admin:promos"):
        return False
    return True
