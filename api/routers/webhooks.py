"""Вебхуки платёжных систем."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from shared.config import get_settings
from shared.database import get_session_factory
from shared.models.remnawave_webhook_event import RemnawaveWebhookEvent
from shared.payments.cryptobot import CryptoBotProvider
from shared.payments.platega import PlategaProvider
from shared.services.topup_service import apply_topup_from_webhook, notify_topup_success
from shared.services.billing_v2.webhook_ingress_service import (
    event_id_from_payload,
    parse_webhook_payload,
    process_remnawave_event,
    process_remnawave_event_by_id,
    store_raw_webhook_event,
    verify_remnawave_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


def _invalid_event_id() -> str:
    # У invalid-signature событий нет доверенного event_id, поэтому храним всегда уникальный ключ.
    return f"invalid:{uuid4().hex}"


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

    if status == "completed" and amount is not None:
        await notify_topup_success(
            telegram_id=tg_id,
            amount_rub=amount,
            promo_bonus_rub=promo_bonus_rub,
            settings=settings,
            user_id=user_id,
            provider_name="cryptobot",
            internal_transaction_id=parsed.internal_transaction_id,
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

    if status == "completed" and amount is not None:
        await notify_topup_success(
            telegram_id=tg_id,
            amount_rub=amount,
            promo_bonus_rub=promo_bonus_rub,
            settings=settings,
            user_id=user_id,
            provider_name="platega",
            internal_transaction_id=parsed.internal_transaction_id,
        )
    elif status == "duplicate":
        logger.info("Platega webhook: дубликат txn=%s", parsed.internal_transaction_id)

    return Response(status_code=200)


@router.post("/remnawave")
async def webhook_remnawave(request: Request, background_tasks: BackgroundTasks) -> Response:
    settings = get_settings()
    if not settings.remnawave_webhooks_enabled:
        logger.info("Remnawave webhook ignored: feature disabled")
        return Response(status_code=404)

    body = await request.body()
    headers = _lower_headers(request)
    ts_header = headers.get("x-remnawave-timestamp") or headers.get("x-timestamp") or ""
    sig_header = headers.get("x-remnawave-signature") or headers.get("x-signature") or ""
    signature_ok = verify_remnawave_signature(
        body=body,
        ts_header=ts_header,
        signature_header=sig_header,
        settings=settings,
    )
    if not signature_ok:
        logger.warning("Remnawave webhook invalid signature")
        try:
            payload = {}
            try:
                payload = parse_webhook_payload(body)
            except Exception:
                payload = {"raw": body.decode("utf-8", errors="ignore")[:4000]}
            event_id = event_id_from_payload(payload, fallback=str(uuid4()))
            async with get_session_factory()() as session:
                session.add(
                    RemnawaveWebhookEvent(
                        event_id=_invalid_event_id(),
                        event_type=str(payload.get("event_type") or "unknown"),
                        status="invalid_signature",
                        signature_valid=False,
                        payload=payload,
                        headers=headers,
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("Failed to persist invalid remnawave webhook")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = parse_webhook_payload(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    event_id = event_id_from_payload(payload, fallback=str(uuid4()))
    event_type = str(payload.get("event") or payload.get("event_type") or "unknown")

    factory = get_session_factory()
    async with factory() as session:
        row, is_duplicate = await store_raw_webhook_event(
            session,
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            headers=headers,
            signature_valid=True,
        )
        if not is_duplicate:
            if settings.remnawave_webhook_background_process:
                background_tasks.add_task(process_remnawave_event_by_id, event_id=event_id, settings=settings)
                logger.info("Remnawave webhook queued event_id=%s type=%s", event_id, event_type)
            else:
                try:
                    await process_remnawave_event(session, row=row, settings=settings)
                    logger.info("Remnawave webhook processed event_id=%s type=%s", event_id, event_type)
                except Exception as exc:
                    row.status = "error"
                    row.error = str(exc)
                    row.processed_at = datetime.now(timezone.utc)
                    logger.exception("Remnawave webhook process failed event_id=%s", event_id)
        else:
            logger.info("Remnawave webhook duplicate event_id=%s", event_id)
        await session.commit()
    return Response(status_code=200)
