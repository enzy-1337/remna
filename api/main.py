"""
FastAPI: вебхуки платежей и (далее) публичные эндпоинты.

Запуск: uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.getLogger("api").info(
        "API стартовал (cryptobot_stub=%s platega_stub=%s)",
        s.cryptobot_stub,
        s.platega_stub,
    )
    yield


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
