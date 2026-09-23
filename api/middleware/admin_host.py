"""Домен админки (ADMIN_SITE_URL, например weba.flux-network.store) отдаёт только web-admin.

Один backend отвечает на оба домена (nginx проксирует всё в :8000), поэтому без этого фильтра
на домене админки открывались клиентский лендинг, /login и личный кабинет сайта.
"""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from shared.config import get_settings

# Что остаётся доступным на домене админки
_ADMIN_HOST_ALLOWED_PREFIXES = (
    "/admin",
    "/assets",
    "/webhooks",  # вебхуки платёжек/Remnawave/Telegram могли быть настроены на этот домен
    "/api",
    "/health",
    "/favicon.ico",
    "/robots.txt",
)


def _host(url: str | None) -> str:
    return (urlparse((url or "").strip()).hostname or "").lower()


def _request_host(request: Request) -> str:
    raw = (request.headers.get("host") or "").strip().lower()
    return raw.split(":", 1)[0]


class AdminHostMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        admin_host = _host(s.admin_site_url)
        public_host = _host(s.public_site_url)
        if not admin_host or admin_host == public_host or _request_host(request) != admin_host:
            return await call_next(request)
        path = request.url.path or "/"
        if any(path == p or path.startswith(p + "/") for p in _ADMIN_HOST_ALLOWED_PREFIXES):
            return await call_next(request)
        if request.method in ("GET", "HEAD"):
            return RedirectResponse("/admin", status_code=302)
        return PlainTextResponse("Not Found", status_code=404)
