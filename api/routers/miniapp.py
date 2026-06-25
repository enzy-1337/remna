"""Telegram Mini App — пользовательский интерфейс (без админки, без кнопки подключения).

Дизайн: handoff "Flux Network VPN" (Claude Design), секция D — Telegram Mini App.
Один HTML-шелл (SPA) + JSON API, авторизация через Telegram WebApp initData.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram.types import User as TgUser
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings, get_settings
from shared.database import get_session_factory
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.device_slots_pricing import device_slot_cap, slots_available_to_buy
from shared.services.hwid_devices_service import (
    connected_devices_count,
    device_display_title,
    fetch_panel_hwid_context,
    panel_devices_unlimited,
)
from shared.services.subscription_service import (
    add_paid_device_slots,
    list_paid_plans,
    purchase_plan_with_balance,
    remove_hwid_device_from_panel,
    resolve_user_plan_price_rub,
)
from shared.services.topup_service import create_topup_payment
from shared.services.trial_service import activate_trial, has_active_subscription, trial_eligible
from shared.services.user_registration import get_user_by_telegram_id
from shared.telegram_webapp_auth import WebAppAuthError, validate_init_data
from tickets.services import add_ticket_message, get_active_ticket_id
from shared.tickets_db_compat import (
    ticket_messages_has_document_columns,
    ticket_messages_has_photo_file_id_column,
    ticket_messages_has_video_file_id_column,
)

logger = logging.getLogger(__name__)
router = APIRouter()


_MD2_ESCAPED_CHAR_RE = re.compile(r"\\([_*\[\]()~`>#+\-=|{}.!\\])")
_MD2_MARKER_RE = re.compile(r"[*_~`]")


def _md2_to_plain(text_value: str | None) -> str | None:
    """Сервисные функции возвращают сообщения, экранированные под Telegram MarkdownV2
    (bold()/plain() из shared.md2). Для отображения в мини-аппе (обычный текст, не Telegram)
    нужно снять экранирование и убрать маркеры разметки."""
    if not text_value:
        return text_value
    s = _MD2_ESCAPED_CHAR_RE.sub(r"\1", text_value)
    s = _MD2_MARKER_RE.sub("", s)
    return s


# ---------------------------------------------------------------------------
# Авторизация через Telegram WebApp initData
# ---------------------------------------------------------------------------

@dataclass
class WebAppAuth:
    telegram_id: int
    first_name: str
    last_name: str | None
    username: str | None


def _extract_init_data(authorization: str | None, x_init_data: str | None) -> str:
    raw = (x_init_data or "").strip()
    if raw:
        return raw
    auth = (authorization or "").strip()
    if auth.lower().startswith("tma "):
        return auth[4:].strip()
    return ""


async def _auth(
    settings: Settings,
    authorization: str | None,
    x_init_data: str | None,
) -> WebAppAuth:
    init_data = _extract_init_data(authorization, x_init_data)
    if not init_data:
        raise HTTPException(status_code=401, detail="missing init data")
    try:
        parsed = validate_init_data(init_data, settings.bot_token)
    except WebAppAuthError as e:
        raise HTTPException(status_code=401, detail=f"invalid init data: {e}") from e
    user = parsed.get("user") or {}
    tg_id = user.get("id")
    if not tg_id:
        raise HTTPException(status_code=401, detail="no telegram user in init data")
    return WebAppAuth(
        telegram_id=int(tg_id),
        first_name=str(user.get("first_name") or "").strip() or "Пользователь",
        last_name=(user.get("last_name") or None),
        username=(user.get("username") or None),
    )


async def _get_or_create_db_user(session: AsyncSession, auth: WebAppAuth) -> User:
    db_user = await get_user_by_telegram_id(session, auth.telegram_id)
    if db_user is not None:
        return db_user
    from shared.services.user_registration import register_user

    tg_user = TgUser(
        id=auth.telegram_id,
        is_bot=False,
        first_name=auth.first_name,
        last_name=auth.last_name,
        username=auth.username,
    )
    db_user, _created, _ = await register_user(session, tg_user, None)
    return db_user


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _days_left(exp: datetime | None) -> int:
    if exp is None:
        return 0
    now = datetime.now(timezone.utc)
    e = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
    return max(0, (e - now).days)


async def _get_active_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    now = datetime.now(timezone.utc)
    r = await session.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(("active", "trial")),
            Subscription.expires_at > now,
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    return r.scalar_one_or_none()


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/context")
async def api_context(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        sub = await _get_active_subscription(session, user.id)
        is_bot_admin = int(user.telegram_id) in set(settings.admin_telegram_ids)
        has_active = sub is not None
        trial_ok = trial_eligible(user, has_active)

        plans = await list_paid_plans(session)
        plans_out = []
        for p in plans:
            price = await resolve_user_plan_price_rub(session, user, p)
            plans_out.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "duration_days": p.duration_days,
                    "price_rub": str(price),
                    "catalog_price_rub": str(p.price_rub),
                    "discount_percent": str(p.discount_percent or 0),
                    "traffic_limit_gb": p.traffic_limit_gb,
                    "device_limit": p.device_limit,
                    "monthly_gb_limit": p.monthly_gb_limit,
                }
            )

        devices_used = 0
        devices_total = sub.devices_count if sub else 0
        if user.remnawave_uuid is not None:
            try:
                uinf, devices, _err = await fetch_panel_hwid_context(user, settings)
                devices_used = connected_devices_count(uinf, devices)
            except Exception:
                devices_used = 0

        sub_out = None
        if sub is not None:
            traffic_used_gb = None
            traffic_limit_gb = None
            try:
                uinf, _devices, _err = await fetch_panel_hwid_context(user, settings)
                if uinf:
                    used_bytes = uinf.get("usedTrafficBytes") or uinf.get("usedTraffic")
                    limit_bytes = uinf.get("trafficLimitBytes") or uinf.get("trafficLimit")
                    if used_bytes is not None:
                        traffic_used_gb = round(int(used_bytes) / (1024 ** 3), 1)
                    if limit_bytes is not None and int(limit_bytes) > 0:
                        traffic_limit_gb = round(int(limit_bytes) / (1024 ** 3), 1)
            except Exception:
                pass
            sub_out = {
                "status": sub.status,
                "expires_at": _to_iso(sub.expires_at),
                "days_left": _days_left(sub.expires_at),
                "auto_renew": bool(sub.auto_renew),
                "devices_count": sub.devices_count,
                "plan_id": sub.plan_id,
                "traffic_used_gb": traffic_used_gb,
                "traffic_limit_gb": traffic_limit_gb,
            }

        return JSONResponse(
            {
                "user": {
                    "telegram_id": user.telegram_id,
                    "first_name": auth.first_name,
                    "username": user.username,
                    "balance_rub": str(user.balance),
                    "referral_code": user.referral_code,
                },
                "subscription": sub_out,
                "devices_used": devices_used,
                "devices_total": devices_total,
                "trial_available": trial_ok,
                "trial_duration_days": settings.trial_duration_days,
                "trial_traffic_gb": settings.trial_traffic_gb,
                "plans": plans_out,
            }
        )


class BuyPlanIn(BaseModel):
    plan_id: int
    idempotency_key: str | None = None


@router.post("/api/plan/buy")
async def api_plan_buy(
    body: BuyPlanIn,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        ok, message, kind = await purchase_plan_with_balance(
            session,
            user=user,
            plan_id=body.plan_id,
            telegram_id=auth.telegram_id,
            settings=settings,
            idempotency_key=body.idempotency_key,
        )
        await session.commit()
        return JSONResponse({"ok": ok, "message": _md2_to_plain(message), "kind": kind})


class TopupIn(BaseModel):
    amount_rub: str
    provider: str


@router.post("/api/topup")
async def api_topup(
    body: TopupIn,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    try:
        amount = Decimal(body.amount_rub)
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="bad amount")
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        try:
            txn, pay_url = await create_topup_payment(
                session,
                user=user,
                telegram_id=auth.telegram_id,
                amount_rub=amount,
                provider_name=body.provider,
                settings=settings,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except KeyError as e:
            raise HTTPException(status_code=400, detail=f"unknown provider: {e}") from e
        await session.commit()
        return JSONResponse(
            {
                "ok": True,
                "transaction_id": txn.id,
                "pay_url": pay_url,
                "amount_rub": str(amount),
                "provider": body.provider,
            }
        )


@router.get("/api/topup/status/{txn_id}")
async def api_topup_status(
    txn_id: int,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        row = (
            await session.execute(
                select(Transaction).where(Transaction.id == txn_id, Transaction.user_id == user.id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse({"status": row.status, "amount_rub": str(row.amount)})


@router.get("/api/transactions")
async def api_transactions(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        r = await session.execute(
            select(Transaction)
            .where(Transaction.user_id == user.id, Transaction.status == "completed")
            .order_by(Transaction.created_at.desc())
            .limit(50)
        )
        rows = list(r.scalars().all())
        return JSONResponse(
            {
                "transactions": [
                    {
                        "id": t.id,
                        "type": t.type,
                        "amount_rub": str(t.amount),
                        "provider": t.payment_provider,
                        "description": t.description,
                        "created_at": _to_iso(t.created_at),
                    }
                    for t in rows
                ]
            }
        )


@router.get("/api/devices")
async def api_devices(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        sub = await _get_active_subscription(session, user.id)
        is_bot_admin = int(user.telegram_id) in set(settings.admin_telegram_ids)
        devices_out: list[dict] = []
        used = 0
        unlimited = False
        if user.remnawave_uuid is not None:
            try:
                uinf, devices, _err = await fetch_panel_hwid_context(user, settings)
                used = connected_devices_count(uinf, devices)
                unlimited = panel_devices_unlimited(uinf, is_bot_admin=is_bot_admin)
                for i, d in enumerate(devices):
                    devices_out.append(
                        {
                            "hwid": d.get("hwid") or d.get("hwId") or "",
                            "title": device_display_title(d, i),
                            "platform": d.get("platform"),
                            "updated_at": d.get("updatedAt") or d.get("createdAt"),
                        }
                    )
            except Exception:
                pass
        cap = device_slot_cap(user, settings, is_bot_admin=is_bot_admin)
        total = sub.devices_count if sub else 0
        available_to_buy = slots_available_to_buy(total, cap)
        return JSONResponse(
            {
                "devices": devices_out,
                "used": used,
                "total": total,
                "unlimited": unlimited,
                "available_to_buy": available_to_buy,
                "extra_slot_price_rub": str(settings.extra_device_price_rub),
            }
        )


class DeviceUnbindIn(BaseModel):
    hwid: str


@router.post("/api/devices/unbind")
async def api_devices_unbind(
    body: DeviceUnbindIn,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        ok, message = await remove_hwid_device_from_panel(
            session, user=user, hwid=body.hwid, settings=settings, initiator="miniapp"
        )
        await session.commit()
        return JSONResponse({"ok": ok, "message": _md2_to_plain(message)})


class BuySlotsIn(BaseModel):
    quantity: int = 1
    idempotency_key: str | None = None


@router.post("/api/devices/buy-slots")
async def api_devices_buy_slots(
    body: BuySlotsIn,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        ok, message = await add_paid_device_slots(
            session,
            user=user,
            settings=settings,
            quantity=max(1, int(body.quantity)),
            idempotency_key=body.idempotency_key,
        )
        await session.commit()
        return JSONResponse({"ok": ok, "message": _md2_to_plain(message)})


@router.post("/api/trial/activate")
async def api_trial_activate(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        tg_user = TgUser(
            id=auth.telegram_id,
            is_bot=False,
            first_name=auth.first_name,
            last_name=auth.last_name,
            username=auth.username,
        )
        try:
            sub, sub_url = await activate_trial(session, user=user, tg_user=tg_user, settings=settings)
        except ValueError as e:
            return JSONResponse({"ok": False, "message": str(e)}, status_code=400)
        await session.commit()
        return JSONResponse({"ok": True, "subscription_url": sub_url, "days_left": _days_left(sub.expires_at)})


# --- Support chat -----------------------------------------------------------

@router.get("/api/support/messages")
async def api_support_messages(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        active_id = await get_active_ticket_id(session, user_id=user.id)
        if active_id is None:
            return JSONResponse({"ticket_id": None, "messages": []})
        has_photo = await ticket_messages_has_photo_file_id_column(session)
        has_video = await ticket_messages_has_video_file_id_column(session)
        has_doc = await ticket_messages_has_document_columns(session)
        cols = "id,sender_role,text,created_at"
        if has_photo:
            cols += ",photo_file_id"
        if has_video:
            cols += ",video_file_id"
        if has_doc:
            cols += ",document_file_id,document_file_name"
        rows = (
            await session.execute(
                text(
                    f"SELECT {cols} FROM ticket_messages WHERE ticket_id=:tid "
                    "AND COALESCE(is_internal,false)=false ORDER BY id ASC"
                ),
                {"tid": int(active_id)},
            )
        ).mappings().all()
        return JSONResponse(
            {
                "ticket_id": int(active_id),
                "messages": [
                    {
                        "id": int(r["id"]),
                        "sender_role": r["sender_role"],
                        "text": r.get("text"),
                        "created_at": _to_iso(r.get("created_at")),
                        "photo_file_id": r.get("photo_file_id") if has_photo else None,
                        "video_file_id": r.get("video_file_id") if has_video else None,
                        "document_file_id": r.get("document_file_id") if has_doc else None,
                        "document_file_name": r.get("document_file_name") if has_doc else None,
                    }
                    for r in rows
                ],
            }
        )


class SupportSendIn(BaseModel):
    text: str


@router.post("/api/support/send")
async def api_support_send(
    body: SupportSendIn,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    msg = (body.text or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="empty message")

    from api.routers.public_pages import (
        _get_active_support_ticket,
        _start_web_support_ticket,
        _web_support_topic_text,
    )
    from aiogram import Bot
    from aiogram.enums import ParseMode
    from tickets.config import config as tickets_config

    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        await session.commit()

    active = await _get_active_support_ticket(db_user=user)
    is_new = active is None
    if is_new:
        ticket_id, topic_id = await _start_web_support_ticket(db_user=user, message_text=msg)
    else:
        ticket_id, topic_id = active
        async with factory() as session:
            user = await _get_or_create_db_user(session, auth)
            await add_ticket_message(
                session,
                ticket_id=ticket_id,
                sender_id=user.id,
                sender_role="user",
                sender_telegram_id=int(user.telegram_id),
                text_body=msg,
                is_internal=False,
            )
            await session.commit()

    if topic_id and tickets_config.bot_token and tickets_config.support_group_id:
        try:
            async with Bot(token=tickets_config.bot_token) as bot:
                if is_new:
                    cap = msg
                else:
                    cap = _web_support_topic_text(ticket_id=ticket_id, db_user=user, msg=msg)
                await bot.send_message(
                    chat_id=tickets_config.support_group_id,
                    message_thread_id=topic_id,
                    text=cap,
                    parse_mode=ParseMode.HTML if not is_new else None,
                    disable_web_page_preview=True,
                )
        except Exception:
            logger.warning("miniapp support: failed to post to topic", exc_info=True)

    return JSONResponse({"ok": True, "ticket_id": ticket_id})


# ---------------------------------------------------------------------------
# Shell HTML (SPA)
# ---------------------------------------------------------------------------

@router.get("")
@router.get("/")
async def miniapp_shell(request: Request) -> HTMLResponse:
    return HTMLResponse(_SHELL_HTML)


_SHELL_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<title>Flux Network</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0E1621; --header:#17212B; --card:#232E3C; --muted:#708499; --icon:#aab8c2;
  --accent:#7B5CFF; --accent2:#5436C9; --link:#6AB3F3; --text:#ffffff;
  --danger:#FF6B6B; --warn:#F5B544;
}
*{box-sizing:border-box;}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:'Manrope',sans-serif;-webkit-font-smoothing:antialiased;overscroll-behavior:none;}
.mono{font-family:'JetBrains Mono',monospace;}
#app{min-height:100vh;display:flex;flex-direction:column;}
.header{background:var(--header);border-bottom:1px solid rgba(255,255,255,.05);display:flex;align-items:center;justify-content:space-between;padding:calc(14px + env(safe-area-inset-top)) 16px 14px;position:sticky;top:0;z-index:10;}
.header .left{display:flex;align-items:center;gap:10px;}
.back{width:28px;height:28px;display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--link);}
.brandicon{width:30px;height:30px;border-radius:9px;background:linear-gradient(140deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.title{font:700 16px Manrope;color:#fff;}
.subtitle{font:500 11px Manrope;color:var(--muted);margin-top:1px;}
.view{display:none;padding:14px 14px 28px;flex:1;}
.view.active{display:block;}
.card{background:var(--card);border-radius:16px;padding:15px 16px;}
.card+.card{margin-top:10px;}
.sectiontitle{font:700 12px Manrope;color:var(--muted);letter-spacing:.04em;margin:18px 4px 8px;text-transform:uppercase;}
.btn{border-radius:12px;padding:15px;text-align:center;font:800 15px Manrope;cursor:pointer;user-select:none;}
.btn-primary{background:var(--accent);color:#fff;}
.btn-primary:active{opacity:.85;}
.btn-ghost{background:rgba(123,92,255,.12);border:1px solid rgba(123,92,255,.25);color:var(--accent);}
.btn-secondary{background:var(--card);border:1px solid rgba(255,255,255,.08);color:#fff;}
.btn[disabled]{opacity:.45;pointer-events:none;}
.bottombar{position:sticky;bottom:0;background:var(--header);padding:12px 14px calc(14px + env(safe-area-inset-bottom));border-top:1px solid rgba(255,255,255,.05);}
.row{display:flex;align-items:center;gap:12px;}
.spacer{flex:1;}
.muted{color:var(--muted);}
.skel{background:linear-gradient(90deg,#1b2531 0%,#26323f 50%,#1b2531 100%);background-size:600px 100%;animation:shimmer 1.3s infinite linear;border-radius:12px;}
@keyframes shimmer{0%{background-position:-300px 0;}100%{background-position:300px 0;}}
@keyframes spin{to{transform:rotate(360deg);}}
.spin{animation:spin 1.1s linear infinite;}
.toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:#232E3C;color:#fff;padding:11px 18px;border-radius:12px;font:600 13px Manrope;box-shadow:0 10px 30px rgba(0,0,0,.4);z-index:999;opacity:0;transition:opacity .2s;pointer-events:none;max-width:86vw;text-align:center;}
.toast.show{opacity:1;}
.navgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px;}
.navitem{background:var(--card);border-radius:16px;padding:16px 14px;display:flex;flex-direction:column;gap:8px;cursor:pointer;}
.navitem .ic{width:34px;height:34px;border-radius:10px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;color:var(--accent);}
.navitem .lbl{font:700 14px Manrope;color:#fff;}
.plan{border-radius:14px;padding:14px 16px;display:flex;align-items:center;gap:13px;background:var(--card);border:1px solid rgba(255,255,255,.05);position:relative;cursor:pointer;}
.plan.selected{border:1.5px solid var(--accent);}
.radio{width:20px;height:20px;border-radius:50%;border:2px solid #4a5d70;flex-shrink:0;}
.plan.selected .radio{border:6px solid var(--accent);background:#fff;}
.badge{position:absolute;top:-9px;right:14px;font:800 10px Manrope;color:#fff;background:var(--accent);padding:3px 9px;border-radius:8px;}
.progress{height:6px;background:rgba(0,0,0,.3);border-radius:4px;overflow:hidden;}
.progress > div{height:100%;background:var(--accent);border-radius:4px;}
.chatwrap{display:flex;flex-direction:column;gap:10px;}
.bubble{max-width:78%;border-radius:16px;padding:11px 14px;font:500 14px Manrope;line-height:1.4;}
.bubble.op{align-self:flex-start;background:var(--card);border-radius:16px 16px 16px 4px;}
.bubble.me{align-self:flex-end;background:var(--accent);color:#fff;border-radius:16px 16px 4px 16px;}
.bubbletime{font:500 10px Manrope;opacity:.6;text-align:right;margin-top:4px;}
.inputbar{display:flex;align-items:center;gap:10px;background:var(--header);padding:10px 12px calc(10px + env(safe-area-inset-bottom));border-top:1px solid rgba(255,255,255,.05);position:sticky;bottom:0;}
.inputbar input{flex:1;background:var(--bg);border:none;border-radius:20px;padding:11px 16px;color:#fff;font:500 14px Manrope;outline:none;}
.sendbtn{color:var(--accent);cursor:pointer;}
.history-item{display:flex;align-items:center;gap:13px;padding:14px 16px;}
.history-item+.history-item{border-top:1px solid rgba(255,255,255,.05);}
.history-ic{width:38px;height:38px;border-radius:11px;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.preset{flex:1;background:var(--card);border:1px solid rgba(255,255,255,.05);border-radius:13px;padding:14px 0;text-align:center;font:800 16px Manrope;color:#fff;cursor:pointer;}
.preset.selected{background:rgba(123,92,255,.1);border:1.5px solid var(--accent);color:var(--accent);}
.payrow{background:var(--card);border:1px solid rgba(255,255,255,.05);border-radius:13px;padding:14px 16px;display:flex;align-items:center;gap:13px;cursor:pointer;}
.payrow.selected{border:1.5px solid var(--accent);}
.empty-illustration{width:100px;height:100px;margin:0 auto;border-radius:50%;background:rgba(255,255,255,.04);border:1px dashed rgba(255,255,255,.12);display:flex;align-items:center;justify-content:center;}
input.amount{width:100%;background:var(--card);border:1px solid rgba(255,255,255,.08);border-radius:13px;padding:14px 16px;color:#fff;font:700 16px Manrope;outline:none;margin-top:10px;}
#tg-gate{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:30px;text-align:center;z-index:9999;}
#tg-gate .brandicon{width:54px;height:54px;border-radius:16px;}
#tg-gate h1{font:800 20px Manrope;margin:0;}
#tg-gate p{font:500 14px Manrope;color:var(--muted);margin:0;max-width:280px;line-height:1.5;}
#tg-gate a{display:inline-block;margin-top:6px;background:var(--accent);color:#fff;text-decoration:none;padding:13px 26px;border-radius:12px;font:700 14px Manrope;}
#splash{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;z-index:9998;}
#splash .brandicon{width:56px;height:56px;border-radius:18px;animation:pulse 1.6s ease-in-out infinite;}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.7;transform:scale(.94);}}
#splash .ring{width:28px;height:28px;border-radius:50%;border:3px solid rgba(123,92,255,.18);border-top-color:var(--accent);animation:spin 0.9s linear infinite;}
.btn[data-busy="1"]{opacity:.55;pointer-events:none;}
</style>
</head>
<body>
<div id="tg-gate" style="display:none;">
  <div class="brandicon"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg></div>
  <h1>Откройте в Telegram</h1>
  <p>Это мини-приложение Flux Network работает только внутри Telegram. Откройте бота и нажмите «Открыть приложение».</p>
  <a href="https://t.me">Открыть Telegram</a>
</div>
<div id="splash">
  <div class="brandicon"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg></div>
  <div class="ring"></div>
</div>
<div id="app" style="display:none;">
  <div class="header">
    <div class="left">
      <div class="back" id="btn-back" style="display:none;" onclick="goHome()">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></svg>
      </div>
      <div class="brandicon" id="brandicon">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg>
      </div>
      <div>
        <div class="title" id="view-title">Flux Network</div>
        <div class="subtitle" id="view-subtitle">мини-приложение</div>
      </div>
    </div>
  </div>

  <!-- HOME: subscription + plans -->
  <div class="view active" id="view-home">
    <div id="home-sub-card"></div>
    <div class="navgrid">
      <div class="navitem" onclick="showView('balance')">
        <div class="ic"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg></div>
        <div class="lbl">Баланс</div>
      </div>
      <div class="navitem" onclick="showView('devices')">
        <div class="ic"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="11" rx="2"/><path d="M2 20h20"/></svg></div>
        <div class="lbl">Устройства</div>
      </div>
      <div class="navitem" onclick="showView('history')">
        <div class="ic"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M6 13l6 6 6-6"/></svg></div>
        <div class="lbl">История</div>
      </div>
      <div class="navitem" onclick="showView('support')">
        <div class="ic"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-2a4 4 0 0 1 4-4h2M21 18v-2a4 4 0 0 0-4-4h-2M9 12a3 3 0 1 0 6 0M5 12V9a7 7 0 0 1 14 0v3"/></svg></div>
        <div class="lbl">Поддержка</div>
      </div>
    </div>
    <div class="sectiontitle" id="plans-title" style="display:none;">Выберите тариф</div>
    <div id="plans-list"></div>
  </div>

  <!-- BALANCE -->
  <div class="view" id="view-balance">
    <div class="card" style="text-align:center;padding:22px 20px;">
      <div class="sectiontitle" style="margin:0;">Текущий баланс</div>
      <div style="font:800 42px Manrope;margin-top:8px;letter-spacing:-.02em;" id="bal-amount">0 <span style="font-size:26px;color:var(--muted);">₽</span></div>
    </div>
    <div class="sectiontitle">Пополнить</div>
    <div class="row" id="presets">
      <div class="preset" data-amount="100">100 ₽</div>
      <div class="preset" data-amount="300">300 ₽</div>
      <div class="preset" data-amount="500">500 ₽</div>
      <div class="preset" data-amount="custom">Своя</div>
    </div>
    <input class="amount" id="custom-amount" placeholder="Введите сумму, ₽" style="display:none;" inputmode="numeric">
    <div class="sectiontitle">Способ оплаты</div>
    <div style="display:flex;flex-direction:column;gap:10px;" id="paymethods">
      <div class="payrow selected" data-provider="platega">
        <div style="width:36px;height:36px;border-radius:10px;background:rgba(106,179,243,.16);display:flex;align-items:center;justify-content:center;"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#6AB3F3" stroke-width="2"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg></div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">Банковская карта</div><div class="muted" style="font:500 12px Manrope;">Platega · Visa, MIR, СБП</div></div>
        <div class="radio" style="border:6px solid var(--accent);background:#fff;"></div>
      </div>
      <div class="payrow" data-provider="cryptobot">
        <div style="width:36px;height:36px;border-radius:10px;background:rgba(247,147,26,.16);display:flex;align-items:center;justify-content:center;"><svg width="20" height="20" viewBox="0 0 24 24" fill="#F7931A"><circle cx="12" cy="12" r="10"/></svg></div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">Криптовалюта</div><div class="muted" style="font:500 12px Manrope;">CryptoBot · USDT, TON, BTC</div></div>
        <div class="radio"></div>
      </div>
    </div>
    <div id="payment-waiting" style="display:none;margin-top:14px;"></div>
  </div>
  <div class="bottombar" id="balance-bar">
    <div class="btn btn-primary" id="btn-topup" onclick="doTopup()">Пополнить на 300 ₽</div>
  </div>

  <!-- DEVICES -->
  <div class="view" id="view-devices">
    <div class="card row">
      <div style="position:relative;width:52px;height:52px;flex-shrink:0;">
        <svg width="52" height="52" viewBox="0 0 52 52"><circle cx="26" cy="26" r="22" fill="none" stroke="rgba(255,255,255,.1)" stroke-width="5"/><circle cx="26" cy="26" r="22" fill="none" stroke="#7B5CFF" stroke-width="5" stroke-linecap="round" id="dev-ring" stroke-dasharray="138" stroke-dashoffset="138" transform="rotate(-90 26 26)"/></svg>
      </div>
      <div class="spacer"><div style="font:800 17px Manrope;color:#fff;" id="dev-count-text">— из — устройств</div><div class="muted" style="font:500 12px Manrope;margin-top:2px;" id="dev-count-sub"></div></div>
    </div>
    <div class="sectiontitle">Подключено</div>
    <div id="devices-list"></div>
    <div class="card row" style="margin-top:16px;background:linear-gradient(130deg, rgba(123,92,255,.14), rgba(123,92,255,.03));border:1px solid rgba(123,92,255,.2);cursor:pointer;" id="buy-slots-row" onclick="buySlot()">
      <div style="width:36px;height:36px;border-radius:10px;background:rgba(123,92,255,.16);display:flex;align-items:center;justify-content:center;"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#7B5CFF" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></div>
      <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">Добавить слот</div><div class="muted" style="font:500 12px Manrope;" id="buy-slots-sub">1 устройство</div></div>
      <span style="font:700 13px Manrope;color:var(--accent);" id="buy-slots-price"></span>
    </div>
  </div>

  <!-- HISTORY -->
  <div class="view" id="view-history">
    <div class="card" style="padding:0;overflow:hidden;" id="history-list"></div>
  </div>

  <!-- SUPPORT -->
  <div class="view" id="view-support">
    <div class="chatwrap" id="chat-messages"></div>
  </div>
  <div class="inputbar" id="support-bar" style="display:none;">
    <input id="chat-input" placeholder="Сообщение…" onkeydown="if(event.key==='Enter')sendChat()">
    <div class="sendbtn" onclick="sendChat()">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3zM6 11a6 6 0 0 0 12 0M12 17v4"/></svg>
    </div>
  </div>

  <!-- EMPTY (no subscription) -->
  <div class="view" id="view-empty">
    <div style="text-align:center;padding:30px 10px 0;">
      <div class="empty-illustration">
        <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="#4a5d70" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg>
      </div>
      <div style="font:800 22px Manrope;color:#fff;margin-top:22px;">Подписки пока нет</div>
      <div class="muted" style="font:500 14px Manrope;margin-top:8px;padding:0 16px;line-height:1.45;">Активируйте бесплатный триал или оформите подписку</div>
    </div>
    <div class="card" id="trial-card" style="margin-top:26px;background:linear-gradient(130deg, rgba(123,92,255,.18), rgba(123,92,255,.04));border:1px solid rgba(123,92,255,.3);">
      <div class="row">
        <div style="width:36px;height:36px;border-radius:10px;background:rgba(123,92,255,.18);display:flex;align-items:center;justify-content:center;"><svg width="20" height="20" viewBox="0 0 24 24" fill="#7B5CFF"><path d="M13 2L4 14h6l-1 8 9-12h-6z"/></svg></div>
        <div><div style="font:800 16px Manrope;color:#fff;">Бесплатный триал</div><div style="font:600 12px Manrope;color:var(--accent);">разовое предложение</div></div>
      </div>
      <div class="row" style="margin-top:14px;gap:10px;" id="trial-stats"></div>
    </div>
  </div>
  <div class="bottombar" id="empty-bar" style="display:none;flex-direction:column;gap:10px;">
    <div class="btn btn-primary" id="btn-activate-trial" onclick="activateTrial()">Активировать триал</div>
    <div style="text-align:center;font:700 14px Manrope;color:var(--link);padding:4px;" onclick="showView('home')">Купить подписку</div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;
function initData() { return (tg && tg.initData) || ''; }

let CTX = null;
let currentView = 'home';

function genKey() {
  try { return crypto.randomUUID(); } catch (e) { return 'k' + Date.now() + Math.random().toString(36).slice(2); }
}
function withBusy(btn, fn) {
  return async function() {
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    try { await fn(); } finally { btn.dataset.busy = '0'; }
  };
}

const TG_OK = !!(tg && initData());
if (!TG_OK) {
  document.getElementById('splash').style.display = 'none';
  document.getElementById('tg-gate').style.display = 'flex';
} else {
  tg.ready();
  tg.expand();
  try { tg.requestFullscreen && tg.requestFullscreen(); } catch (e) {}
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData()}, opts.headers || {});
  const r = await fetch('/miniapp' + path, opts);
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch(e) {}
    throw new Error(detail || ('HTTP ' + r.status));
  }
  return r.json();
}

const VIEW_TITLES = {
  home: ['Flux Network', 'мини-приложение'],
  balance: ['Баланс', ''],
  devices: ['Устройства', ''],
  history: ['История операций', ''],
  support: ['Поддержка Flux', 'отвечает быстро'],
  empty: ['Flux Network', 'мини-приложение'],
};

function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  ['balance-bar','empty-bar','support-bar'].forEach(id => document.getElementById(id).style.display = 'none');
  if (name === 'balance') document.getElementById('balance-bar').style.display = 'block';
  if (name === 'empty') document.getElementById('empty-bar').style.display = 'flex';
  if (name === 'support') { document.getElementById('support-bar').style.display = 'flex'; loadSupport(); }
  if (name === 'devices') loadDevices();
  if (name === 'history') loadHistory();
  const t = VIEW_TITLES[name] || ['Flux Network', ''];
  document.getElementById('view-title').textContent = t[0];
  document.getElementById('view-subtitle').textContent = t[1];
  document.getElementById('btn-back').style.display = name === 'home' ? 'none' : 'flex';
  document.getElementById('brandicon').style.display = name === 'home' ? 'flex' : 'none';
  currentView = name;
  if (tg && tg.BackButton) {
    if (name === 'home') tg.BackButton.hide(); else tg.BackButton.show();
  }
}
function goHome() { showView(CTX && CTX.subscription ? 'home' : 'empty'); }
if (tg && tg.BackButton) tg.BackButton.onClick(goHome);

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('ru-RU', {day:'numeric', month:'long'});
}

function renderHome() {
  const sub = CTX.subscription;
  const box = document.getElementById('home-sub-card');
  if (!sub) {
    showView('empty');
    const ts = document.getElementById('trial-stats');
    ts.innerHTML = `
      <div style="flex:1;background:rgba(0,0,0,.2);border-radius:11px;padding:11px;text-align:center;"><div style="font:800 19px Manrope;color:#fff;">${CTX.trial_duration_days}</div><div class="muted" style="font:500 11px Manrope;">дня</div></div>
      <div style="flex:1;background:rgba(0,0,0,.2);border-radius:11px;padding:11px;text-align:center;"><div style="font:800 19px Manrope;color:#fff;">${CTX.trial_traffic_gb}</div><div class="muted" style="font:500 11px Manrope;">ГБ трафика</div></div>
    `;
    document.getElementById('btn-activate-trial').style.display = CTX.trial_available ? 'block' : 'none';
    return;
  }
  const statusLabel = sub.status === 'trial' ? 'Триал · активна' : 'Премиум · активна';
  const usedGb = sub.traffic_used_gb != null ? sub.traffic_used_gb : '—';
  const limitGb = sub.traffic_limit_gb != null ? sub.traffic_limit_gb : '∞';
  const pct = (sub.traffic_limit_gb && sub.traffic_used_gb != null) ? Math.min(100, Math.round(sub.traffic_used_gb / sub.traffic_limit_gb * 100)) : 0;
  box.innerHTML = `
    <div class="card" style="background:linear-gradient(130deg, rgba(123,92,255,.16), rgba(123,92,255,.04));border:1px solid rgba(123,92,255,.22);">
      <div class="row" style="justify-content:space-between;">
        <div style="font:700 15px Manrope;color:#fff;">${statusLabel}</div>
        <div style="font:700 11px Manrope;color:var(--accent);background:rgba(123,92,255,.14);padding:4px 9px;border-radius:9px;">${sub.days_left} ${sub.days_left===1?'день':'дней'}</div>
      </div>
      <div class="progress" style="margin-top:12px;"><div style="width:${pct}%;"></div></div>
      <div class="row" style="justify-content:space-between;margin-top:8px;">
        <span class="muted" style="font:500 12px Manrope;">${usedGb} / ${limitGb} ГБ использовано</span>
        <span class="muted" style="font:500 12px Manrope;">до ${fmtDate(sub.expires_at)}</span>
      </div>
    </div>
  `;
  renderPlans();
}

let selectedPlanId = null;
function renderPlans() {
  document.getElementById('plans-title').style.display = 'block';
  const list = document.getElementById('plans-list');
  list.innerHTML = CTX.plans.map(p => {
    const disc = parseFloat(p.discount_percent || '0');
    const hasDisc = disc > 0;
    return `
    <div class="plan" data-plan="${p.id}" style="margin-top:10px;" onclick="selectPlan(${p.id})">
      ${hasDisc ? `<div class="badge">−${disc}%</div>` : ''}
      <div class="radio"></div>
      <div class="spacer">
        <div style="font:700 15px Manrope;color:#fff;">${p.name}</div>
        <div class="muted" style="font:500 12px Manrope;">${p.traffic_limit_gb || p.monthly_gb_limit || '∞'} ГБ · ${p.device_limit || '—'} устройств</div>
      </div>
      <div style="text-align:right;">
        <div style="font:800 16px Manrope;color:#fff;">${Math.round(parseFloat(p.price_rub))} ₽</div>
        ${hasDisc ? `<div class="muted" style="font:500 11px Manrope;text-decoration:line-through;">${Math.round(parseFloat(p.catalog_price_rub))} ₽</div>` : ''}
      </div>
    </div>`;
  }).join('');
  if (CTX.plans.length && !selectedPlanId) selectPlan(CTX.plans[0].id);
  injectBuyBar();
}
function selectPlan(id) {
  selectedPlanId = id;
  document.querySelectorAll('.plan').forEach(el => el.classList.toggle('selected', parseInt(el.dataset.plan) === id));
  injectBuyBar();
}
function injectBuyBar() {
  let bar = document.getElementById('home-buy-bar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'home-buy-bar';
    bar.className = 'bottombar';
    document.getElementById('app').appendChild(bar);
  }
  const plan = CTX.plans.find(p => p.id === selectedPlanId);
  if (!plan) { bar.style.display = 'none'; return; }
  bar.style.display = currentView === 'home' ? 'block' : 'none';
  const verb = CTX.subscription ? 'Продлить' : 'Купить';
  bar.innerHTML = `<div class="btn btn-primary" id="buy-plan-btn" onclick="buyPlan()">${verb} · ${plan.name} — ${Math.round(parseFloat(plan.price_rub))} ₽</div>`;
}
let _buyPlanInFlight = false;
async function buyPlan() {
  if (!selectedPlanId || _buyPlanInFlight) return;
  _buyPlanInFlight = true;
  const btn = document.getElementById('buy-plan-btn');
  if (btn) btn.dataset.busy = '1';
  try {
    const r = await api('/api/plan/buy', {method:'POST', body: JSON.stringify({plan_id: selectedPlanId, idempotency_key: genKey()})});
    if (r.ok) { toast('Подписка оформлена ✅'); await loadContext(); }
    else toast(r.message || 'Недостаточно средств');
  } catch (e) { toast('Ошибка: ' + e.message); }
  finally { _buyPlanInFlight = false; }
}

let _trialInFlight = false;
async function activateTrial() {
  if (_trialInFlight) return;
  _trialInFlight = true;
  try {
    const r = await api('/api/trial/activate', {method:'POST', body:'{}'});
    if (r.ok) { toast('Триал активирован ✅'); await loadContext(); }
    else toast(r.message || 'Не удалось активировать');
  } catch (e) { toast('Ошибка: ' + e.message); }
  finally { _trialInFlight = false; }
}

// --- Balance ---
let selectedAmount = 300;
let selectedProvider = 'platega';
document.querySelectorAll('.preset').forEach(el => el.addEventListener('click', () => {
  document.querySelectorAll('.preset').forEach(x => x.classList.remove('selected'));
  el.classList.add('selected');
  const customInput = document.getElementById('custom-amount');
  if (el.dataset.amount === 'custom') {
    customInput.style.display = 'block';
    customInput.focus();
    selectedAmount = parseInt(customInput.value) || 0;
  } else {
    customInput.style.display = 'none';
    selectedAmount = parseInt(el.dataset.amount);
  }
  updateTopupBtn();
}));
document.getElementById('custom-amount').addEventListener('input', (e) => {
  selectedAmount = parseInt(e.target.value) || 0;
  updateTopupBtn();
});
document.querySelectorAll('.payrow').forEach(el => el.addEventListener('click', () => {
  document.querySelectorAll('.payrow').forEach(x => { x.classList.remove('selected'); x.querySelector('.radio').style.border = '2px solid #4a5d70'; x.querySelector('.radio').style.background = 'none'; });
  el.classList.add('selected');
  el.querySelector('.radio').style.border = '6px solid var(--accent)';
  el.querySelector('.radio').style.background = '#fff';
  selectedProvider = el.dataset.provider;
}));
document.querySelector('.preset[data-amount="300"]').classList.add('selected');
function updateTopupBtn() {
  document.getElementById('btn-topup').textContent = 'Пополнить на ' + (selectedAmount || 0) + ' ₽';
}
let _topupInFlight = false;
async function doTopup() {
  if (_topupInFlight) return;
  if (!selectedAmount || selectedAmount <= 0) { toast('Укажите сумму'); return; }
  _topupInFlight = true;
  try {
    const r = await api('/api/topup', {method:'POST', body: JSON.stringify({amount_rub: String(selectedAmount), provider: selectedProvider})});
    if (tg && tg.openLink) tg.openLink(r.pay_url); else window.open(r.pay_url, '_blank');
    document.getElementById('payment-waiting').style.display = 'block';
    document.getElementById('payment-waiting').innerHTML = `
      <div class="card" style="text-align:center;">
        <div class="muted" style="font:600 13px Manrope;">Ожидаем оплату ${r.amount_rub} ₽…</div>
        <div class="btn btn-ghost" style="margin-top:12px;" onclick="checkTopup(${r.transaction_id})">Проверить платёж вручную</div>
      </div>`;
  } catch (e) { toast('Ошибка: ' + e.message); }
  finally { _topupInFlight = false; }
}
async function checkTopup(id) {
  try {
    const r = await api('/api/topup/status/' + id);
    if (r.status === 'completed') { toast('Оплата получена ✅'); await loadContext(); document.getElementById('payment-waiting').style.display = 'none'; }
    else toast('Платёж пока не подтверждён');
  } catch (e) { toast('Ошибка проверки'); }
}

// --- Devices ---
async function loadDevices() {
  try {
    const d = await api('/api/devices');
    const pct = d.total ? Math.min(100, Math.round(d.used / d.total * 100)) : 0;
    document.getElementById('dev-ring').setAttribute('stroke-dashoffset', String(138 - 138 * pct / 100));
    document.getElementById('dev-count-text').textContent = d.unlimited ? (d.used + ' устройств (без лимита)') : (d.used + ' из ' + d.total + ' устройств');
    document.getElementById('dev-count-sub').textContent = d.used >= d.total && !d.unlimited ? 'Базовый лимит занят полностью' : 'Слоты свободны';
    document.getElementById('devices-list').innerHTML = d.devices.map(dev => `
      <div class="card row" style="margin-top:10px;">
        <div style="width:38px;height:38px;border-radius:11px;background:#2b3947;display:flex;align-items:center;justify-content:center;flex-shrink:0;"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#aab8c2" stroke-width="1.7" stroke-linecap="round"><rect x="3" y="5" width="18" height="11" rx="2"/><path d="M2 20h20"/></svg></div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">${dev.title}</div><div class="muted" style="font:500 12px Manrope;">${dev.platform || ''}</div></div>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#FF6B6B" stroke-width="1.8" stroke-linecap="round" style="cursor:pointer;" onclick="unbindDevice('${dev.hwid}')"><path d="M6 7h12M9 7V5h6v2M8 7l1 13h6l1-13"/></svg>
      </div>`).join('') || '<div class="muted" style="padding:14px;">Нет подключённых устройств</div>';
    document.getElementById('buy-slots-row').style.display = d.available_to_buy > 0 ? 'flex' : 'none';
    document.getElementById('buy-slots-sub').textContent = '1 устройство · доступно ещё ' + d.available_to_buy;
    document.getElementById('buy-slots-price').textContent = d.extra_slot_price_rub + ' ₽';
  } catch (e) { toast('Ошибка загрузки устройств'); }
}
let _buySlotInFlight = false;
async function buySlot() {
  if (_buySlotInFlight || !confirm('Добавить 1 слот устройства?')) return;
  _buySlotInFlight = true;
  try {
    const r = await api('/api/devices/buy-slots', {method:'POST', body: JSON.stringify({quantity:1, idempotency_key: genKey()})});
    toast(r.message || (r.ok ? 'Слот добавлен' : 'Ошибка'));
    if (r.ok) await loadDevices();
  } catch (e) { toast('Ошибка: ' + e.message); }
  finally { _buySlotInFlight = false; }
}
let _unbindInFlight = false;
async function unbindDevice(hwid) {
  if (_unbindInFlight || !confirm('Отвязать устройство?')) return;
  _unbindInFlight = true;
  try {
    const r = await api('/api/devices/unbind', {method:'POST', body: JSON.stringify({hwid})});
    toast(r.ok ? 'Устройство отвязано' : (r.message || 'Ошибка'));
    await loadDevices();
  } catch (e) { toast('Ошибка: ' + e.message); }
  finally { _unbindInFlight = false; }
}

// --- History ---
const HIST_ICONS = {
  topup: ['<path d="M12 19V5M6 11l6-6 6 6"/>', 'rgba(123,92,255,.14)', '#7B5CFF'],
  subscription: ['<path d="M12 5v14M6 13l6 6 6-6"/>', 'rgba(255,255,255,.06)', '#C8D2DA'],
  default: ['<circle cx="12" cy="12" r="9"/>', 'rgba(255,255,255,.06)', '#aab8c2'],
};
async function loadHistory() {
  try {
    const r = await api('/api/transactions');
    const list = document.getElementById('history-list');
    if (!r.transactions.length) { list.innerHTML = '<div class="muted" style="padding:18px;">Пока нет операций</div>'; return; }
    list.innerHTML = r.transactions.map(t => {
      const ic = HIST_ICONS[t.type] || HIST_ICONS.default;
      const sign = t.type === 'topup' ? '+' : '−';
      const color = t.type === 'topup' ? '#7B5CFF' : '#C8D2DA';
      return `<div class="history-item">
        <div class="history-ic" style="background:${ic[1]};"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="${ic[2]}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ic[0]}</svg></div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">${t.description || t.type}</div><div class="muted" style="font:500 11px Manrope;">${fmtDate(t.created_at)}</div></div>
        <div style="font:800 15px Manrope;color:${color};">${sign}${Math.round(parseFloat(t.amount_rub))} ₽</div>
      </div>`;
    }).join('');
  } catch (e) { toast('Ошибка загрузки истории'); }
}

// --- Support ---
async function loadSupport() {
  try {
    const r = await api('/api/support/messages');
    const wrap = document.getElementById('chat-messages');
    if (!r.messages.length) { wrap.innerHTML = '<div class="muted" style="text-align:center;padding:20px;">Напишите нам, если есть вопросы</div>'; return; }
    wrap.innerHTML = r.messages.map(m => {
      const cls = m.sender_role === 'user' ? 'me' : 'op';
      const time = m.created_at ? new Date(m.created_at).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}) : '';
      return `<div class="bubble ${cls}">${(m.text||'').replace(/</g,'&lt;')}<div class="bubbletime">${time}</div></div>`;
    }).join('');
    wrap.scrollTop = wrap.scrollHeight;
  } catch (e) { toast('Ошибка загрузки чата'); }
}
let _sendChatInFlight = false;
async function sendChat() {
  if (_sendChatInFlight) return;
  const input = document.getElementById('chat-input');
  const val = input.value.trim();
  if (!val) return;
  input.value = '';
  _sendChatInFlight = true;
  try {
    await api('/api/support/send', {method:'POST', body: JSON.stringify({text: val})});
    await loadSupport();
  } catch (e) { toast('Ошибка отправки'); }
  finally { _sendChatInFlight = false; }
}

async function loadContext() {
  CTX = await api('/api/context');
  document.getElementById('bal-amount').innerHTML = Math.round(parseFloat(CTX.user.balance_rub)) + ' <span style="font-size:26px;color:var(--muted);">₽</span>';
  renderHome();
}

async function boot() {
  if (!TG_OK) return;
  try {
    await loadContext();
  } catch (e) {
    toast('Ошибка загрузки: ' + e.message);
  } finally {
    document.getElementById('splash').style.display = 'none';
    document.getElementById('app').style.display = 'flex';
  }
}
boot();
</script>
</body>
</html>
"""
