"""Вебхуки платёжных систем."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response

from shared.config import get_settings
from shared.database import get_session_factory
from shared.payments.cryptobot import CryptoBotProvider
from shared.payments.platega import PlategaProvider
from shared.services.topup_service import apply_topup_from_webhook, notify_topup_success

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


def _lower_headers(request: Request) -> dict[str, str]:
    return {k.lower(): v for k, v in request.headers.items()}


@router.post("/cryptobot")
async def webhook_cryptobot(request: Request) -> Response:
    settings = get_settings()
    body = await request.body()
    headers = _lower_headers(request)
    prov = CryptoBotProvider(settings)
    if not settings.cryptobot_stub and not prov.verify_webhook(body=body, headers=headers):
        logger.warning("CryptoBot webhook: неверная подпись")
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    parsed = prov.parse_topup_webhook(data)
    if not parsed:
        return Response(status_code=200)

    factory = get_session_factory()
    async with factory() as session:
        status, tg_id, amount, user_id, promo_bonus_rub = await apply_topup_from_webhook(
            session,
            provider_name="cryptobot",
            parsed=parsed,
            settings=settings,
        )
        await session.commit()

    if status == "completed" and tg_id and amount is not None:
        await notify_topup_success(
            telegram_id=tg_id,
            amount_rub=amount,
            promo_bonus_rub=promo_bonus_rub,
            settings=settings,
            user_id=user_id,
            provider_name="cryptobot",
        )
    elif status == "duplicate":
        logger.info("CryptoBot webhook: дубликат invoice txn=%s", parsed.internal_transaction_id)
    elif status in ("rejected", "not_found"):
        logger.warning("CryptoBot webhook: %s txn=%s", status, parsed.internal_transaction_id)

    return Response(status_code=200)


@router.post("/platega")
async def webhook_platega(request: Request) -> Response:
    settings = get_settings()
    body = await request.body()
    headers = _lower_headers(request)
    prov = PlategaProvider(settings)
    if not settings.platega_stub and not prov.verify_webhook(body=body, headers=headers):
        logger.warning("Platega webhook: проверка не пройдена")
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    parsed = prov.parse_topup_webhook(data)
    if not parsed:
        return Response(status_code=200)

    factory = get_session_factory()
    async with factory() as session:
        status, tg_id, amount, user_id, promo_bonus_rub = await apply_topup_from_webhook(
            session,
            provider_name="platega",
            parsed=parsed,
            settings=settings,
        )
        await session.commit()

    if status == "completed" and tg_id and amount is not None:
        await notify_topup_success(
            telegram_id=tg_id,
            amount_rub=amount,
            promo_bonus_rub=promo_bonus_rub,
            settings=settings,
            user_id=user_id,
            provider_name="platega",
        )
    elif status == "duplicate":
        logger.info("Platega webhook: дубликат txn=%s", parsed.internal_transaction_id)

    return Response(status_code=200)
