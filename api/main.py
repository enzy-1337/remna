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

from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from api.middleware.web_admin_rbac import WebAdminRbacMiddleware
from api.routers import miniapp, public_pages, tickets_api, web_admin, web_admin_rbac_pages, webhooks
from shared.config import get_settings
from shared.database import get_session_factory
from shared.models.user import User
from shared.services.web_admin_session_service import (
    browser_fingerprint,
    restore_wauth_from_snapshot,
    touch_browser_session,
    try_get_browser_session,
)
from shared.services.admin_error_log_handler import install_admin_error_log_handler
from shared.services.admin_log_topics import AdminLogTopic
from shared.md2 import bold, plain
from shared.services.admin_notify import notify_admin
from shared.services.broadcast_service import tick_scheduled_broadcast_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
install_admin_error_log_handler()


class WebAdminSessionValidationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path or ""
        if path.startswith("/admin") and not path.startswith("/admin/login"):
            uid_raw = request.session.get("wauth_user_id")
            token = str(request.session.get("wauth_session_token") or "").strip()
            exp_raw = request.session.get("wauth_session_exp")
            auth = request.session.get("wauth")
            session_handles = uid_raw is not None and token and exp_raw is not None
            invalid = False
            if auth or session_handles:
                try:
                    uid = int(uid_raw)
                    exp = int(exp_raw)
                except (TypeError, ValueError):
                    invalid = True
                else:
                    now_ts = int(datetime.now(timezone.utc).timestamp())
                    if not token or exp <= now_ts:
                        invalid = True
                    else:
                        factory = get_session_factory()
                        fp = browser_fingerprint(request)
                        async with factory() as session:
                            row = await try_get_browser_session(session, token=token, fingerprint_hash=fp)
                            if row is None:
                                user = await session.get(User, uid)
                                if (
                                    user is None
                                    or (user.web_admin_session_token or "") != token
                                    or user.web_admin_session_expires_at is None
                                ):
                                    invalid = True
                                else:
                                    db_exp = user.web_admin_session_expires_at
                                    if db_exp.tzinfo is None:
                                        db_exp = db_exp.replace(tzinfo=timezone.utc)
                                    if db_exp <= datetime.now(timezone.utc):
                                        invalid = True
                            else:
                                if int(row.user_id) != uid:
                                    invalid = True
                                else:
                                    db_exp = row.expires_at
                                    if db_exp.tzinfo is None:
                                        db_exp = db_exp.replace(tzinfo=timezone.utc)
                                    now_utc = datetime.now(timezone.utc)
                                    if db_exp <= now_utc:
                                        invalid = True
                                    elif not invalid:
                                        new_exp = await touch_browser_session(session, row)
                                        request.session["wauth_session_exp"] = int(new_exp.timestamp())
                                        snap = row.auth_snapshot
                                        if isinstance(snap, dict) and snap and not request.session.get("wauth"):
                                            request.session["wauth"] = restore_wauth_from_snapshot(snap)
                if invalid:
                    path = request.url.path or ""
                    q = request.url.query or ""
                    cand = path + (("?" + q) if q else "")
                    next_q = ""
                    if cand.startswith("/admin") and not cand.startswith("/admin/login") and not cand.startswith("//"):
                        cand = cand.split("#", 1)[0][:2048]
                        next_q = "?next=" + url_quote(cand, safe="")
                    request.session.clear()
                    return RedirectResponse("/admin/login" + next_q, status_code=303)
        return await call_next(request)


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
        boot_ts = (
            datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M:%S | %d-%m-%Y | МСК")
        )
        mode = "webhook" if s.telegram_webhook_enabled else "polling"
        boot_lines = [plain("Режим Telegram: ") + bold(mode)]
        if s.telegram_webhook_enabled:
            wh_url = (s.telegram_webhook_url or "").strip()
            if wh_url:
                boot_lines.append(plain("URL: ") + bold(wh_url))
        boot_lines.append(plain(boot_ts))
        await notify_admin(
            s,
            title="🚀 " + bold("Сайт (API) запущен"),
            lines=boot_lines,
            topic=AdminLogTopic.BOOT,
            event_type="api_startup",
        )
        log.info("Уведомление о запуске сайта отправлено в админ-чат (тема BOOT).")
    except Exception:
        log.debug("admin notify api startup", exc_info=True)

    async def broadcast_schedule_tick() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await tick_scheduled_broadcast_queue(s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduled broadcast tick")

    sched_task = asyncio.create_task(broadcast_schedule_tick())
    try:
        yield
    finally:
        sched_task.cancel()
        try:
            await sched_task
        except asyncio.CancelledError:
            pass
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


class Utf8HtmlCharsetMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if ct.startswith("text/html") and "charset" not in ct:
            response.headers["content-type"] = "text/html; charset=utf-8"
        return response


app = FastAPI(title="Remna VPN API", version="0.1.0", lifespan=lifespan)
settings = get_settings()
app.add_middleware(Utf8HtmlCharsetMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(WebAdminSessionValidationMiddleware)
app.add_middleware(WebAdminRbacMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.web_admin_session_secret,
    session_cookie="remna_web_admin_session",
    same_site="lax",
    https_only=False,
    max_age=86400 * 14,
)


app.include_router(webhooks.router, prefix="/webhooks")
app.include_router(web_admin.router, prefix="/admin")
app.include_router(web_admin_rbac_pages.router, prefix="/admin")
app.include_router(public_pages.router)
app.include_router(tickets_api.router, prefix="/api")
app.include_router(miniapp.router, prefix="/my")
# Совместимость со старым путём (на случай уже выданных ссылок/закладок).
app.include_router(miniapp.router, prefix="/miniapp")

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


@app.exception_handler(404)
async def not_found_handler(request: Request, _exc):
    path = request.url.path or "/"
    if path.startswith("/api") or path.startswith("/webhooks"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return public_pages.render_not_found_page(path)
