"""
FastAPI: вебхуки платежей и (далее) публичные эндпоинты.

Запуск: uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from api.routers import public_pages, tickets_api, web_admin, webhooks
from shared.config import get_settings
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin_plain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    log = logging.getLogger("api")
    log.info(
        "API стартовал (cryptobot_stub=%s platega_stub=%s telegram_webhook=%s)",
        s.cryptobot_stub,
        s.platega_stub,
        s.telegram_webhook_enabled,
    )
    stop_event = asyncio.Event()
    bg_tasks: list[asyncio.Task] = []
    if s.telegram_webhook_enabled:
        from bot.background_loops import cancel_background_tasks, start_background_loops
        from bot.bootstrap_db import bootstrap_bot_database_schema
        from bot.factory import apply_ipv4_preferred_dns, create_bot_and_dispatcher, webhook_allowed_updates

        apply_ipv4_preferred_dns()
        await bootstrap_bot_database_schema()
        bot, dp = await create_bot_and_dispatcher(s)
        app.state.telegram_bot = bot
        app.state.telegram_dispatcher = dp
        app.state.telegram_feed_lock = asyncio.Lock()
        bg_tasks = start_background_loops(s, stop_event)
        url = (s.telegram_webhook_url or "").strip()
        secret = (s.telegram_webhook_secret or "").strip()
        allowed = webhook_allowed_updates(dp)
        sw_kwargs: dict[str, object] = {"url": url, "secret_token": secret}
        if allowed is not None:
            sw_kwargs["allowed_updates"] = allowed
        await bot.set_webhook(**sw_kwargs)
        log.info("Telegram setWebhook: %s", url)
        try:
            await notify_admin_plain(
                s,
                text=f"🌐 API: режим Telegram webhook\n{url}",
                topic=AdminLogTopic.BOOT,
                event_type="api_telegram_webhook_startup",
            )
        except Exception:
            log.debug("admin notify webhook startup", exc_info=True)
    try:
        yield
    finally:
        if s.telegram_webhook_enabled:
            stop_event.set()
            from bot.background_loops import cancel_background_tasks

            await cancel_background_tasks(bg_tasks)
            try:
                bot = getattr(app.state, "telegram_bot", None)
                if bot is not None:
                    await bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                log.exception("Telegram delete_webhook")


app = FastAPI(title="Remna VPN API", version="0.1.0", lifespan=lifespan)
settings = get_settings()
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.web_admin_session_secret,
    session_cookie="remna_web_admin_session",
    same_site="lax",
    https_only=False,
)
app.include_router(webhooks.router, prefix="/webhooks")
app.include_router(web_admin.router, prefix="/admin")
app.include_router(public_pages.router)
app.include_router(tickets_api.router, prefix="/api")

_assets_dir = _ROOT / "assets"
if _assets_dir.is_dir():
    class CacheStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            if isinstance(response, Response) and response.status_code == 200:
                # Версионированные ассеты браузер может держать долго.
                response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
            return response

    app.mount(
        "/assets",
        CacheStaticFiles(directory=str(_assets_dir)),
        name="assets",
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
