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

from api.routers import webhooks
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
app.include_router(webhooks.router, prefix="/webhooks")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
