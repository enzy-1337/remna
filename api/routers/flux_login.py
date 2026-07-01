"""Telegram login for the Flux desktop client (one-time code exchange)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from shared.config import get_settings
from shared.services.flux_login_service import bot_deeplink, new_login_code, pop_login_url

router = APIRouter()


@router.post("/api/flux/login/start")
async def flux_login_start() -> JSONResponse:
    """Start a login: issue a code + the bot deep link for the desktop to open."""
    settings = get_settings()
    code = new_login_code()
    url = bot_deeplink(code, settings)
    if not url:
        return JSONResponse({"ok": False, "error": "bot_unconfigured"}, status_code=503)
    return JSONResponse({"ok": True, "code": code, "bot_url": url})


@router.get("/api/flux/login/{code}")
async def flux_login_poll(code: str) -> JSONResponse:
    """Poll a login code. Returns the subscription URL once the bot has set it."""
    settings = get_settings()
    url = await pop_login_url(code, settings=settings)
    if url:
        return JSONResponse({"ok": True, "status": "ok", "subscription_url": url})
    return JSONResponse({"ok": True, "status": "pending"})
