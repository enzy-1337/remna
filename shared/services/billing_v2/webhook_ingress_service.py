from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.models.remnawave_webhook_event import RemnawaveWebhookEvent
from shared.models.user import User
from shared.services.billing_v2.device_service import add_device_history_event
from shared.services.billing_v2.rating_service import charge_daily_device_once, charge_gb_step


def telegram_id_from_payload(payload: dict) -> int | None:
    raw = payload.get("telegram_id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        val = raw.strip()
        if val.isdigit():
            try:
                return int(val)
            except ValueError:
                return None
    return None


def verify_remnawave_signature(
    *,
    body: bytes,
    ts_header: str,
    signature_header: str,
    settings: Settings,
) -> bool:
    secret = (settings.remnawave_webhook_secret or "").encode("utf-8")
    if not secret:
        return False
    try:
        ts = int(ts_header)
    except (TypeError, ValueError):
        return False
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if abs(now_ts - ts) > settings.remnawave_webhook_signature_ttl_sec:
        return False
    payload_to_sign = f"{ts_header}.".encode("utf-8") + body
    digest = hmac.new(secret, payload_to_sign, hashlib.sha256).hexdigest()
    sig = signature_header.strip()
    expected_prefixed = f"sha256={digest}"
    if hmac.compare_digest(expected_prefixed, sig):
        return True
    # Панель может отдавать только hex без префикса (см. docs.rw webhooks).
    return hmac.compare_digest(digest, sig)


async def store_raw_webhook_event(
    session: AsyncSession,
    *,
    event_id: str,
    event_type: str,
    payload: dict,
    headers: dict[str, str],
    signature_valid: bool,
) -> tuple[RemnawaveWebhookEvent, bool]:
    existing = (
        await session.execute(
            select(RemnawaveWebhookEvent).where(RemnawaveWebhookEvent.event_id == event_id).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status != "duplicate":
            existing.status = "duplicate"
            await session.flush()
        return existing, True
    row = RemnawaveWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
        headers=headers,
        signature_valid=signature_valid,
        status="received",
    )
    session.add(row)
    await session.flush()
    return row, False


async def process_remnawave_event(session: AsyncSession, *, row: RemnawaveWebhookEvent, settings: Settings) -> None:
    payload = row.payload or {}
    event_type = (row.event_type or "").strip().lower()
    user_tg_id = telegram_id_from_payload(payload)
    if user_tg_id is None:
        row.status = "ignored"
        row.processed_at = datetime.now(timezone.utc)
        return

    user = (
        await session.execute(select(User).where(User.telegram_id == user_tg_id).limit(1))
    ).scalar_one_or_none()
    if user is None:
        row.status = "ignored"
        row.processed_at = datetime.now(timezone.utc)
        return

    now = datetime.now(timezone.utc)
    if event_type == "traffic.gb_step":
        is_mobile = bool(payload.get("mobile_internet", False))
        ok = await charge_gb_step(
            session,
            user=user,
            event_id=row.event_id,
            event_ts=now,
            is_mobile_internet=is_mobile,
            settings=settings,
        )
        row.status = "processed" if ok else "rejected"
    elif event_type in ("device.attached", "device.detached"):
        hwid = str(payload.get("device_hwid") or "").strip()
        if not hwid:
            row.status = "ignored"
        else:
            is_active = event_type == "device.attached"
            await add_device_history_event(
                session,
                user_id=user.id,
                subscription_id=None,
                device_hwid=hwid,
                event_type=event_type,
                event_ts=now,
                is_active=is_active,
                meta={"source_event_id": row.event_id},
            )
            if is_active:
                await charge_daily_device_once(
                    session,
                    user=user,
                    device_hwid=hwid,
                    day=now.date(),
                    settings=settings,
                )
            row.status = "processed"
    elif event_type == "subscription.status":
        row.status = "processed"
    else:
        row.status = "ignored"

    row.processed_at = datetime.now(timezone.utc)
    await session.flush()


def parse_webhook_payload(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def event_id_from_payload(payload: dict, *, fallback: str) -> str:
    raw = payload.get("event_id") or payload.get("id") or fallback
    event_id = str(raw).strip() or str(fallback).strip() or "fallback"
    # Ограничиваем до размера колонки БД (String(128)), чтобы не ловить ошибки на flush/commit.
    return event_id[:128]


async def process_remnawave_event_by_id(*, event_id: str, settings: Settings) -> None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(RemnawaveWebhookEvent).where(RemnawaveWebhookEvent.event_id == event_id).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return
        try:
            await process_remnawave_event(session, row=row, settings=settings)
        except Exception as exc:
            row.status = "error"
            row.error = str(exc)
            row.processed_at = datetime.now(timezone.utc)
        await session.commit()
