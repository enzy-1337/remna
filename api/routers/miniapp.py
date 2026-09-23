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
from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings, get_settings
from shared.database import get_session_factory
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.models.referral_reward import ReferralReward
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError, subscription_url_for_telegram
from shared.integrations.rw_traffic import extract_traffic_gb_from_rw_user
from shared.subscription_qr import subscription_url_qr_png
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
    set_subscription_auto_renew,
    unbind_hwid_device_keep_slot,
)
from shared.services.offers_service import (
    get_intro_plan,
    intro_offer_eligible,
    intro_offer_texts,
    quote_plan_price,
    winback_percent,
)
from shared.services.onboarding import onboarding_payload
from shared.services.topup_service import create_topup_payment
from shared.services.trial_service import activate_trial, has_active_subscription, trial_eligible
from shared.services.user_registration import get_user_by_telegram_id
from shared.services.referral_service import (
    count_invited_users,
    list_invited_users,
    list_referrer_rewards_with_referred,
    sum_referrer_bonus_rub,
)
from shared.services.promo_service import apply_promo_code_for_user_v2
from shared.telegram_webapp_auth import WebAppAuthError, validate_init_data
from tickets.services import add_ticket_message, get_active_ticket_id
from shared.tickets_db_compat import (
    ticket_messages_has_document_columns,
    ticket_messages_has_photo_file_id_column,
    ticket_messages_has_video_file_id_column,
    ticket_messages_has_voice_columns,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Транзакции этих типов списывают с баланса; всё остальное (топап, рефералка, бонусы, промо) — пополнение.
_DEBIT_TXN_TYPES = {
    "subscription",
    "subscription_autorenew",
    "manual_add",
    "device_slots_purchased",
    "purchase_plan",
}


def _lifetime_used_gb(uinf: dict) -> float | None:
    """Использовано «за всё время» (Σ) — приоритет lifetimeUsedTrafficBytes; иначе текущий период."""
    def to_gb(v):
        try:
            n = float(v)
            return round(n / (1024 ** 3), 2) if n >= 0 else None
        except (TypeError, ValueError):
            return None

    ut = uinf.get("userTraffic")
    if isinstance(ut, dict) and ut.get("lifetimeUsedTrafficBytes") is not None:
        g = to_gb(ut["lifetimeUsedTrafficBytes"])
        if g is not None:
            return g
    if uinf.get("lifetimeUsedTrafficBytes") is not None:
        g = to_gb(uinf["lifetimeUsedTrafficBytes"])
        if g is not None:
            return g
    used, _limit = extract_traffic_gb_from_rw_user(uinf)
    return used


def _txn_amount_label(t) -> str:
    """Подпись справа в истории. Для бонусов в днях/ГБ/устройствах — не «+0 ₽», а суть бонуса."""
    meta = t.meta or {}
    if t.type == "promo_extra_days":
        days = int(meta.get("days") or 0)
        return f"+{days} д." if days else ""
    if t.type == "promo_extra_gb":
        gb = int(meta.get("extra_gb") or 0)
        return f"+{gb} ГБ" if gb else ""
    if t.type == "promo_extra_devices":
        n = int(meta.get("extra_devices") or 0)
        return f"+{n} устр." if n else ""
    try:
        amt = float(t.amount or 0)
    except (TypeError, ValueError):
        amt = 0.0
    if amt == 0:
        return ""  # денежные нулёвки не показываем
    sign = "−" if t.type in _DEBIT_TXN_TYPES else "+"
    return f"{sign}{int(round(abs(amt)))} ₽"


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


async def _pending_topup_bonus_percent(session: AsyncSession, user_id: int) -> Decimal:
    """Активный (ещё не применённый) промокод типа topup_bonus_percent — % бонуса к будущему пополнению."""
    from shared.models.promo import PromoCode, PromoUsage

    row = (
        await session.execute(
            select(PromoCode.value)
            .join(PromoUsage, PromoUsage.promo_id == PromoCode.id)
            .where(
                PromoUsage.user_id == user_id,
                PromoCode.type == "topup_bonus_percent",
                PromoUsage.topup_bonus_applied_at.is_(None),
            )
            .order_by(PromoUsage.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return Decimal(str(row)) if row is not None else Decimal("0")


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
            # Та же цена, что спишется: тариф → персональная цена → промокод или скидка «вернись».
            q = await quote_plan_price(session, user, p, settings)
            final_price, original_price = q.final, q.original
            eff_discount = 0
            if original_price > 0 and final_price < original_price:
                eff_discount = int(round(float((original_price - final_price) / original_price * 100)))
            plans_out.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "duration_days": p.duration_days,
                    "price_rub": str(final_price),
                    "original_price_rub": str(original_price),
                    "discount_percent": str(eff_discount),
                    "traffic_limit_gb": p.traffic_limit_gb,
                    "device_limit": p.device_limit,
                    "monthly_gb_limit": p.monthly_gb_limit,
                }
            )

        intro_out = None
        if await intro_offer_eligible(session, user, settings):
            ip = await get_intro_plan(session, settings)
            it = intro_offer_texts(settings)
            intro_out = {"plan_id": ip.id, "title": it["title"], "price_rub": str(it["price"]), "old_price_rub": str(it["old_price"])}
            disc = int(round(float((it["old_price"] - it["price"]) / it["old_price"] * 100))) if it["old_price"] > 0 else 0
            plans_out.insert(0, {
                "id": ip.id,
                "name": "🔥 Акция: " + it["title"],
                "duration_days": ip.duration_days,
                "price_rub": str(it["price"]),
                "original_price_rub": str(it["old_price"]),
                "discount_percent": str(disc),
                "traffic_limit_gb": None,
                "device_limit": None,
                "monthly_gb_limit": None,
                "is_offer": True,
            })
        wb = winback_percent(user, settings)
        winback_out = (
            {"percent": str(wb.normalize()), "until": _to_iso(user.winback_offer_until)} if wb > 0 else None
        )

        pending_topup_bonus_pct = await _pending_topup_bonus_percent(session, user.id)
        referrals_count = await count_invited_users(session, user.id)
        referrals_earned = await sum_referrer_bonus_rub(session, user.id)

        devices_used = 0
        devices_total = sub.devices_count if sub else 0
        panel_uinf = None
        if user.remnawave_uuid is not None:
            try:
                panel_uinf, devices, _err = await fetch_panel_hwid_context(user, settings)
                devices_used = connected_devices_count(panel_uinf, devices)
            except Exception:
                devices_used = 0

        sub_out = None
        if sub is not None:
            traffic_used_gb = None
            traffic_limit_gb = None
            subscription_url = ""
            try:
                uinf = panel_uinf
                if uinf:
                    # Трафик лежит в userTraffic.* (вложенно). «За всё время» = lifetime.
                    traffic_used_gb = _lifetime_used_gb(uinf)
                    _used_cur, traffic_limit_gb = extract_traffic_gb_from_rw_user(uinf)
                    subscription_url = subscription_url_for_telegram(uinf.get("subscriptionUrl"), settings) or ""
            except Exception:
                pass
            is_lifetime = sub.expires_at is not None and sub.expires_at.year >= settings.billing_legacy_lifetime_cutoff_year
            sub_out = {
                "status": sub.status,
                "expires_at": _to_iso(sub.expires_at),
                "days_left": _days_left(sub.expires_at),
                "is_lifetime": is_lifetime,
                "auto_renew": bool(sub.auto_renew),
                "devices_count": sub.devices_count,
                "plan_id": sub.plan_id,
                "subscription_url": subscription_url,
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
                "included_device_slots": settings.subscription_included_device_slots,
                "pending_topup_bonus_percent": str(pending_topup_bonus_pct),
                "referrals_count": referrals_count,
                "referrals_earned_rub": str(referrals_earned),
                "plans": plans_out,
                "offers": {"intro": intro_out, "winback": winback_out},
                # подсказки «как подключиться», пока нет ни одного устройства
                "onboarding": (
                    onboarding_payload(settings, sub_out["subscription_url"])
                    if sub_out and panel_uinf is not None and devices_used == 0 and sub_out.get("subscription_url")
                    else None
                ),
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
                        "direction": "debit" if t.type in _DEBIT_TXN_TYPES else "credit",
                        "amount_label": _txn_amount_label(t),
                        "provider": t.payment_provider,
                        "description": t.description,
                        "created_at": _to_iso(t.created_at),
                    }
                    for t in rows
                ]
            }
        )


def _classify_device(d: dict) -> str:
    """Тип устройства по платформе/модели/UA: phone | tv | computer | unknown."""
    hay = " ".join(
        str(d.get(k) or "")
        for k in ("platform", "deviceModel", "device_model", "osVersion", "os", "userAgent", "user_agent")
    ).lower()
    if not hay.strip():
        return "unknown"
    # ТВ — проверяем раньше телефона (androidtv содержит android).
    if any(t in hay for t in ("tv", "appletv", "android tv", "androidtv", "smarttv", "smart-tv", "webos", "tizen", "google tv")):
        return "tv"
    if any(t in hay for t in ("iphone", "ipad", "ios", "android", "phone", "mobile", "harmonyos", "xiaomi", "samsung", "huawei", "redmi", "poco", "oneplus", "pixel")):
        return "phone"
    if any(t in hay for t in ("windows", "macos", "mac os", "macintosh", "darwin", "linux", "ubuntu", "debian", "desktop", "pc", "x86", "win32", "win64", "amd64")):
        return "computer"
    return "unknown"


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
                            "dtype": _classify_device(d),
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
        # Unbind keeps the slot — only the device is freed in the panel, the paid
        # slot stays so the user can bind a new device to it.
        ok, message = await unbind_hwid_device_keep_slot(
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


class AutoRenewIn(BaseModel):
    enabled: bool


@router.post("/api/subscription/auto-renew")
async def api_subscription_auto_renew(
    body: AutoRenewIn,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        ok, message = await set_subscription_auto_renew(session, user.id, body.enabled)
        await session.commit()
        return JSONResponse({"ok": ok, "message": _md2_to_plain(message)})


class PromoApplyIn(BaseModel):
    code: str


@router.post("/api/promo/apply")
async def api_promo_apply(
    body: PromoApplyIn,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        ok, message, meta = await apply_promo_code_for_user_v2(
            session, settings=settings, user=user, raw_code=body.code
        )
        await session.commit()
        return JSONResponse({"ok": ok, "message": _md2_to_plain(message), "meta": meta})


# --- Referrals ---------------------------------------------------------------

def _ref_name(u: User) -> str:
    return (u.first_name or u.username or f"ID {u.telegram_id}")


@router.get("/api/referrals")
async def api_referrals(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        total = await sum_referrer_bonus_rub(session, user.id)
        friends = await count_invited_users(session, user.id)
        # Все приглашённые — даже без пополнений (тогда суммарно 0 ₽).
        invited_users = await list_invited_users(session, user.id, limit=200)
        rewards = await list_referrer_rewards_with_referred(session, user.id, limit=500)
        per_user_bonus: dict[int, Decimal] = {}
        for reward, referred in rewards:
            per_user_bonus[referred.id] = per_user_bonus.get(referred.id, Decimal("0")) + Decimal(str(reward.bonus_rub or 0))
        bot_username = (settings.bot_username or "").strip().lstrip("@")
        link = f"https://t.me/{bot_username}?start=ref_{user.referral_code}" if bot_username else ""
        return JSONResponse(
            {
                "link": link,
                "code": user.referral_code,
                "total_earned_rub": str(total),
                "friends_count": friends,
                "percent": str(settings.referral_payment_percent),
                "invited": [
                    {
                        "id": ru.id,
                        "telegram_id": ru.telegram_id,
                        "name": _ref_name(ru),
                        "username": ru.username,
                        "bonus_rub": str(per_user_bonus.get(ru.id, Decimal("0"))),
                    }
                    for ru in invited_users
                ],
            }
        )


_REF_SOURCE_LABEL = {
    "referral_signup": "Бонус за регистрацию",
    "referral_signup_bonus": "Бонус за регистрацию",
    "referral_signup_invited": "Бонус за регистрацию",
    "referral_payment_percent": "Процент с пополнения",
}


@router.get("/api/referrals/{referred_id}")
async def api_referral_detail(
    referred_id: int,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        referred = (
            await session.execute(
                select(User).where(User.id == referred_id, User.referred_by == user.id)
            )
        ).scalar_one_or_none()
        if referred is None:
            raise HTTPException(status_code=404, detail="not found")
        # Начисления именно от этого друга.
        rows = (
            await session.execute(
                select(ReferralReward)
                .where(
                    ReferralReward.referrer_id == user.id,
                    ReferralReward.referred_id == referred.id,
                    ReferralReward.status == "applied",
                )
                .order_by(ReferralReward.id.desc())
                .limit(100)
            )
        ).scalars().all()
        total_brought = sum((Decimal(str(r.bonus_rub or 0)) for r in rows), Decimal("0"))
        # Сколько всего пополнил приглашённый.
        their_topups = (
            await session.execute(
                select(Transaction).where(
                    Transaction.user_id == referred.id,
                    Transaction.type == "topup",
                    Transaction.status == "completed",
                )
            )
        ).scalars().all()
        their_topups_total = sum((Decimal(str(t.amount or 0)) for t in their_topups), Decimal("0"))
        return JSONResponse(
            {
                "name": _ref_name(referred),
                "username": referred.username,
                "telegram_id": referred.telegram_id,
                "total_brought_rub": str(total_brought),
                "their_topups_rub": str(their_topups_total),
                "percent": str(settings.referral_payment_percent),
                "rewards": [
                    {
                        "title": _REF_SOURCE_LABEL.get(r.source, "Начисление"),
                        "source": r.source,
                        "bonus_rub": str(r.bonus_rub or 0),
                        "created_at": _to_iso(r.created_at),
                    }
                    for r in rows
                ],
            }
        )


# --- Управление подпиской -----------------------------------------------------

@router.post("/api/subscription/reissue")
async def api_subscription_reissue(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        sub = await _get_active_subscription(session, user.id)
        if sub is None or user.remnawave_uuid is None:
            return JSONResponse({"ok": False, "message": "Нет активной подписки"}, status_code=400)
        rw_uuid = str(user.remnawave_uuid)
    rw = RemnaWaveClient(settings)
    try:
        uinf = await rw.reset_user_subscription_credentials(rw_uuid, revoke_only_passwords=False)
    except RemnaWaveError as e:
        return JSONResponse({"ok": False, "message": str(e)[:200]}, status_code=502)
    new_url = subscription_url_for_telegram(uinf.get("subscriptionUrl"), settings) or ""
    return JSONResponse({"ok": True, "subscription_url": new_url})


@router.get("/api/subscription/qr")
async def api_subscription_qr(
    init: str = "",
    data: str = "",
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> Response:
    """QR подписки. initData и сама ссылка (data) передаются в query — для <img>, который не шлёт заголовки.
    Если data не передана — берём ссылку из панели."""
    settings = get_settings()
    init_header = _extract_init_data(authorization, x_telegram_init_data)
    init_data = init or init_header
    try:
        parsed = validate_init_data(init_data, settings.bot_token)
    except WebAppAuthError:
        raise HTTPException(status_code=401, detail="invalid init data")
    tg_id = (parsed.get("user") or {}).get("id")
    if not tg_id:
        raise HTTPException(status_code=401, detail="no user")
    url = (data or "").strip()
    if not url or len(url) > 2048:
        # Резервный путь — достаём из панели.
        factory = get_session_factory()
        async with factory() as session:
            user = await get_user_by_telegram_id(session, int(tg_id))
            if user is None or user.remnawave_uuid is None:
                raise HTTPException(status_code=404, detail="no subscription")
            try:
                uinf = await fetch_panel_hwid_context(user, settings)
                url = subscription_url_for_telegram((uinf[0] or {}).get("subscriptionUrl"), settings) or ""
            except Exception:
                url = ""
    if not url:
        raise HTTPException(status_code=404, detail="no subscription url")
    png = subscription_url_qr_png(url, scale=7)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


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
        has_voice = await ticket_messages_has_voice_columns(session)
        cols = "id,sender_role,text,created_at"
        if has_photo:
            cols += ",photo_file_id"
        if has_video:
            cols += ",video_file_id"
        if has_doc:
            cols += ",document_file_id,document_file_name"
        if has_voice:
            cols += ",voice_file_id,video_note_file_id,audio_file_id,audio_file_name"
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
                        "voice_file_id": r.get("voice_file_id") if has_voice else None,
                        "video_note_file_id": r.get("video_note_file_id") if has_voice else None,
                        "audio_file_id": r.get("audio_file_id") if has_voice else None,
                        "audio_file_name": r.get("audio_file_name") if has_voice else None,
                    }
                    for r in rows
                ],
            }
        )


async def _user_owns_ticket_message(session: AsyncSession, *, user_id: int, msg_id: int) -> int | None:
    """Возвращает ticket_id, если сообщение принадлежит тикету этого пользователя, иначе None."""
    row = (
        await session.execute(
            text(
                """
                SELECT tm.ticket_id FROM ticket_messages tm
                JOIN tickets t ON t.id = tm.ticket_id
                WHERE tm.id = :mid AND t.user_id = :uid
                """
            ),
            {"mid": msg_id, "uid": user_id},
        )
    ).scalar_one_or_none()
    return int(row) if row is not None else None


async def _proxy_support_media(
    auth: WebAppAuth, settings: Settings, msg_id: int, column: str, *, media_type: str, filename_col: str | None = None
) -> Response:
    from aiogram import Bot
    from tickets.config import config as tickets_config

    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        owned = await _user_owns_ticket_message(session, user_id=user.id, msg_id=msg_id)
        if owned is None:
            raise HTTPException(status_code=404, detail="not found")
        cols = column if not filename_col else f"{column}, {filename_col}"
        row = (
            await session.execute(
                text(f"SELECT {cols} FROM ticket_messages WHERE id=:mid"), {"mid": msg_id}
            )
        ).mappings().first()
    if not row or not row.get(column):
        raise HTTPException(status_code=404, detail="media not found")
    fid = str(row[column])
    tok = (tickets_config.bot_token or "").strip()
    if not tok:
        raise HTTPException(status_code=503, detail="bot not configured")
    async with Bot(token=tok) as bot:
        f = await bot.get_file(fid)
        fp = f.file_path
        if not fp:
            raise HTTPException(status_code=404, detail="file path unavailable")
    url = f"https://api.telegram.org/file/bot{tok}/{fp}"
    headers = {}
    if filename_col and row.get(filename_col):
        headers["Content-Disposition"] = f'attachment; filename="{row[filename_col]}"'
    import httpx

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return Response(content=r.content, media_type=media_type, headers=headers)


@router.get("/api/support/media/{msg_id}/photo")
async def api_support_media_photo(
    msg_id: int,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    return await _proxy_support_media(auth, settings, msg_id, "photo_file_id", media_type="image/jpeg")


@router.get("/api/support/media/{msg_id}/video")
async def api_support_media_video(
    msg_id: int,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    return await _proxy_support_media(auth, settings, msg_id, "video_file_id", media_type="video/mp4")


@router.get("/api/support/media/{msg_id}/voice")
async def api_support_media_voice(
    msg_id: int,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    return await _proxy_support_media(auth, settings, msg_id, "voice_file_id", media_type="audio/ogg")


@router.get("/api/support/media/{msg_id}/video-note")
async def api_support_media_video_note(
    msg_id: int,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    return await _proxy_support_media(auth, settings, msg_id, "video_note_file_id", media_type="video/mp4")


@router.get("/api/support/media/{msg_id}/audio")
async def api_support_media_audio(
    msg_id: int,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    return await _proxy_support_media(
        auth, settings, msg_id, "audio_file_id", media_type="audio/mpeg", filename_col="audio_file_name"
    )


@router.get("/api/support/media/{msg_id}/document")
async def api_support_media_document(
    msg_id: int,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    return await _proxy_support_media(
        auth, settings, msg_id, "document_file_id", media_type="application/octet-stream", filename_col="document_file_name"
    )


@router.post("/api/support/send")
async def api_support_send(
    text_value: str = Form(default="", alias="text"),
    kind: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    auth = await _auth(settings, authorization, x_telegram_init_data)
    msg = (text_value or "").strip()
    has_file = file is not None and bool(file.filename)
    if not msg and not has_file:
        raise HTTPException(status_code=400, detail="empty message")

    from api.routers.public_pages import (
        _get_active_support_ticket,
        _start_web_support_ticket,
        _web_support_topic_text,
    )
    from aiogram import Bot
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import BufferedInputFile
    from tickets.config import config as tickets_config

    factory = get_session_factory()
    async with factory() as session:
        user = await _get_or_create_db_user(session, auth)
        await session.commit()

    photo_fid = video_fid = voice_fid = vidnote_fid = audio_fid = doc_fid = None
    audio_fname = doc_fname = None

    if has_file:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="empty file")
        if len(raw) > int(tickets_config.media_max_mb) * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"file too large (max {tickets_config.media_max_mb} MB)")
        if not tickets_config.bot_token:
            raise HTTPException(status_code=503, detail="bot not configured")
        ctype = (file.content_type or "").lower()
        safe_name = (file.filename or "upload").strip() or "upload"
        uid = int(user.telegram_id)
        async with Bot(token=tickets_config.bot_token) as bot:
            upload = BufferedInputFile(file=raw, filename=safe_name)
            if kind == "voice" or (ctype.startswith("audio/") and kind != "document"):
                try:
                    sent = await bot.send_voice(chat_id=uid, voice=upload, caption=(msg[:1024] or None))
                    voice_fid = sent.voice.file_id if sent.voice else None
                except TelegramBadRequest:
                    upload2 = BufferedInputFile(file=raw, filename=safe_name)
                    sent = await bot.send_audio(chat_id=uid, audio=upload2, caption=(msg[:1024] or None))
                    audio_fid = sent.audio.file_id if sent.audio else None
                    audio_fname = safe_name
            elif kind == "photo" or ctype.startswith("image/"):
                try:
                    sent = await bot.send_photo(chat_id=uid, photo=upload, caption=(msg[:1024] or None))
                    photo_fid = sent.photo[-1].file_id if sent.photo else None
                except TelegramBadRequest as e:
                    err = str(e)
                    if "PHOTO_INVALID_DIMENSIONS" in err or "IMAGE_PROCESS_FAILED" in err:
                        upload2 = BufferedInputFile(file=raw, filename=safe_name)
                        sent = await bot.send_document(chat_id=uid, document=upload2, caption=(msg[:1024] or None))
                        doc_fid = sent.document.file_id if sent.document else None
                        doc_fname = safe_name
                    else:
                        raise
            elif kind == "video" or ctype.startswith("video/"):
                sent = await bot.send_video(chat_id=uid, video=upload, caption=(msg[:1024] or None))
                video_fid = sent.video.file_id if sent.video else None
            else:
                sent = await bot.send_document(chat_id=uid, document=upload, caption=(msg[:1024] or None))
                doc_fid = sent.document.file_id if sent.document else None
                doc_fname = safe_name

    active = await _get_active_support_ticket(db_user=user)
    is_new = active is None
    if is_new:
        ticket_id, topic_id = await _start_web_support_ticket(db_user=user, message_text=msg or "📎 Вложение")
        if has_file:
            async with factory() as session:
                user = await _get_or_create_db_user(session, auth)
                await add_ticket_message(
                    session,
                    ticket_id=ticket_id,
                    sender_id=user.id,
                    sender_role="user",
                    sender_telegram_id=int(user.telegram_id),
                    text_body="",
                    is_internal=False,
                    photo_file_id=photo_fid,
                    video_file_id=video_fid,
                    document_file_id=doc_fid,
                    document_file_name=doc_fname,
                    voice_file_id=voice_fid,
                    video_note_file_id=vidnote_fid,
                    audio_file_id=audio_fid,
                    audio_file_name=audio_fname,
                )
                await session.commit()
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
                photo_file_id=photo_fid,
                video_file_id=video_fid,
                document_file_id=doc_fid,
                document_file_name=doc_fname,
                voice_file_id=voice_fid,
                video_note_file_id=vidnote_fid,
                audio_file_id=audio_fid,
                audio_file_name=audio_fname,
            )
            await session.commit()

    if topic_id and tickets_config.bot_token and tickets_config.support_group_id:
        try:
            async with Bot(token=tickets_config.bot_token) as bot:
                cap = (msg[:1024] or None) if is_new else _web_support_topic_text(ticket_id=ticket_id, db_user=user, msg=msg)[:1024]
                pm = ParseMode.HTML if (cap and not is_new) else None
                if photo_fid:
                    await bot.send_photo(chat_id=tickets_config.support_group_id, message_thread_id=topic_id, photo=photo_fid, caption=cap, parse_mode=pm)
                elif video_fid:
                    await bot.send_video(chat_id=tickets_config.support_group_id, message_thread_id=topic_id, video=video_fid, caption=cap, parse_mode=pm)
                elif voice_fid:
                    await bot.send_voice(chat_id=tickets_config.support_group_id, message_thread_id=topic_id, voice=voice_fid, caption=cap, parse_mode=pm)
                elif audio_fid:
                    await bot.send_audio(chat_id=tickets_config.support_group_id, message_thread_id=topic_id, audio=audio_fid, caption=cap, parse_mode=pm)
                elif doc_fid:
                    await bot.send_document(chat_id=tickets_config.support_group_id, message_thread_id=topic_id, document=doc_fid, caption=cap, parse_mode=pm)
                elif not is_new:
                    await bot.send_message(
                        chat_id=tickets_config.support_group_id,
                        message_thread_id=topic_id,
                        text=_web_support_topic_text(ticket_id=ticket_id, db_user=user, msg=msg),
                        parse_mode=ParseMode.HTML,
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
    settings = get_settings()
    logo_url = (settings.admin_panel_logo_url or "").strip()
    if not logo_url.startswith(("http://", "https://")):
        logo_url = ""
    safe_logo_url = logo_url.replace("\\", "\\\\").replace('"', '\\"')
    bot_username = (settings.bot_username or "").strip().lstrip("@")
    bot_deeplink = f"https://t.me/{bot_username}" if bot_username else "https://t.me"
    # База API = префикс монтирования (/my или /miniapp), берём из пути запроса.
    base = (request.url.path or "/my").rstrip("/") or "/my"
    html = _SHELL_HTML.replace("__FLUX_LOGO_URL__", safe_logo_url)
    html = html.replace("__BOT_DEEPLINK__", bot_deeplink)
    html = html.replace("__MINIAPP_BASE__", base)
    return HTMLResponse(html)


_SHELL_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<title>Flux VPN</title>
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
.brandicon{width:38px;height:38px;border-radius:11px;background:linear-gradient(140deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;flex-shrink:0;}
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
@keyframes fadeInUp{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:translateY(0);}}
#app.revealed .header{animation:fadeInUp .3s ease both;}
#app.revealed .navitem,#app.revealed .card,#app.revealed .plan,#app.revealed .history-item,#app.revealed .preset,#app.revealed .payrow,#app.revealed .bottombar .btn,#app.revealed .empty-illustration{animation:fadeInUp .4s ease both;}
#app.revealed .navitem:nth-of-type(1){animation-delay:.03s;}
#app.revealed .navitem:nth-of-type(2){animation-delay:.07s;}
#app.revealed .navitem:nth-of-type(3){animation-delay:.11s;}
#app.revealed .navitem:nth-of-type(4){animation-delay:.15s;}
#app.revealed .plan:nth-of-type(1){animation-delay:.05s;}
#app.revealed .plan:nth-of-type(2){animation-delay:.1s;}
#app.revealed .plan:nth-of-type(3){animation-delay:.15s;}
#app.revealed .bottombar{animation:fadeInUp .35s ease .1s both;}
.spin{animation:spin 1.1s linear infinite;}
/* Иконка-маска: красится в цвет акцента (под цвет своей плашки). */
.mi{display:inline-block;background:var(--accent);-webkit-mask-position:center;mask-position:center;-webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;-webkit-mask-size:contain;mask-size:contain;}
.toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:#232E3C;color:#fff;padding:11px 18px;border-radius:12px;font:600 13px Manrope;box-shadow:0 10px 30px rgba(0,0,0,.4);z-index:999;opacity:0;transition:opacity .2s;pointer-events:none;max-width:86vw;text-align:center;}
.toast.show{opacity:1;}
.navgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px;}
.navitem{background:var(--card);border-radius:16px;padding:16px;display:flex;align-items:center;gap:12px;cursor:pointer;}
.navitem .ic{width:40px;height:40px;border-radius:11px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;color:var(--accent);flex-shrink:0;}
.navitem .lbl{font:700 14px Manrope;color:#fff;}
.navitem .sub{font:500 11px Manrope;color:var(--muted);margin-top:1px;}
.avatar-circle{width:44px;height:44px;border-radius:50%;background:linear-gradient(140deg,#3a4a5a,#222e3a);border:1px solid rgba(255,255,255,.08);display:flex;align-items:center;justify-content:center;font:800 16px Manrope;color:#fff;flex-shrink:0;background-size:cover;background-position:center;overflow:hidden;}
.greet-row{display:flex;align-items:center;gap:12px;padding:2px 2px 10px;}
.greet-row .hi{font:500 12px Manrope;color:var(--muted);}
.greet-row .nm{font:800 18px Manrope;color:#fff;letter-spacing:-.01em;}
.cta-renew{background:var(--accent);border-radius:16px;padding:16px 18px;display:flex;align-items:center;gap:13px;cursor:pointer;margin-top:14px;}
.cta-renew .ic{width:38px;height:38px;border-radius:11px;background:rgba(255,255,255,.18);display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.toggle{width:44px;height:26px;border-radius:16px;padding:3px;display:flex;cursor:pointer;transition:background .15s;}
.toggle .knob{width:20px;height:20px;border-radius:50%;background:#fff;transition:margin .15s;}
.toggle.on{background:var(--accent);justify-content:flex-end;}
.toggle.off{background:rgba(255,255,255,.12);justify-content:flex-start;}
.promo-row{display:flex;align-items:center;gap:10px;padding:11px 14px;}
.promo-input{flex:1;background:transparent;border:none;color:#fff;font:600 14px Manrope;outline:none;}
.copybtn{font:700 12px Manrope;color:var(--accent);background:rgba(123,92,255,.14);padding:8px 12px;border-radius:10px;display:flex;align-items:center;gap:6px;cursor:pointer;flex-shrink:0;}
.fade-in{animation:fadein .35s ease both;}
@keyframes fadein{from{opacity:0;transform:translateY(6px);}to{opacity:1;transform:translateY(0);}}
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
.bubble-text{white-space:pre-wrap;word-break:break-word;}
.chat-media-wrap+.bubble-text,.bubble audio.chat-media-audio+.bubble-text,.bubble video.chat-media-vidnote+.bubble-text{margin-top:6px;}
.inputbar{display:flex;align-items:center;gap:10px;background:var(--header);padding:10px 12px calc(10px + env(safe-area-inset-bottom));border-top:1px solid rgba(255,255,255,.05);position:sticky;bottom:0;}
.inputbar input{flex:1;background:var(--bg);border:none;border-radius:20px;padding:11px 16px;color:#fff;font:500 14px Manrope;outline:none;}
.sendbtn{color:var(--accent);cursor:pointer;flex-shrink:0;}
.iconbtn{width:24px;height:24px;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;opacity:.85;}
.iconbtn img{width:20px;height:20px;object-fit:contain;}
.iconbtn.recording img{filter:drop-shadow(0 0 0 #FF6B6B);}
.iconbtn.recording{background:rgba(255,107,107,.18);border-radius:50%;animation:recpulse 1.1s ease-in-out infinite;}
@keyframes recpulse{0%,100%{box-shadow:0 0 0 0 rgba(255,107,107,.35);}50%{box-shadow:0 0 0 6px rgba(255,107,107,.12);}}
.chat-attach-preview{display:flex;align-items:center;gap:10px;background:var(--header);padding:8px 14px;font:600 12px Manrope;color:#fff;border-top:1px solid rgba(255,255,255,.05);}
.chat-attach-remove{color:var(--danger);cursor:pointer;font-weight:700;margin-left:auto;}
.bubble img.chat-media-img{width:200px;max-width:100%;min-height:150px;max-height:240px;border-radius:12px;display:block;cursor:pointer;object-fit:cover;}
.bubble video.chat-media-video{width:200px;max-width:100%;min-height:150px;max-height:240px;border-radius:12px;display:block;}
.bubble video.chat-media-video.media-loaded{background:#000;}
.bubble video.chat-media-vidnote{width:112px;height:112px;border-radius:50%;object-fit:cover;display:block;margin-top:6px;}
.bubble video.chat-media-vidnote.media-loaded{background:#000;}
/* Скелетон-шиммер пока медиа не загрузилось (виден, т.к. opacity не скрываем). */
.bubble img.chat-media-img:not(.media-loaded),.bubble video.chat-media-video:not(.media-loaded),.bubble video.chat-media-vidnote:not(.media-loaded){background:linear-gradient(90deg,#1b2531 0%,#26323f 50%,#1b2531 100%);background-size:600px 100%;animation:shimmer 1.3s infinite linear;color:transparent;}
.media-loaded{animation:none;}
.chat-media-img,.chat-media-video,.chat-media-vidnote{cursor:zoom-in;}
.vid-play-ov{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;}
.vid-play-ov svg{width:46px;height:46px;background:rgba(0,0,0,.55);border-radius:50%;padding:11px;box-sizing:border-box;}
.lb{position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.92);padding:20px;}
.lb.open{display:flex;}
.lb-media{max-width:96vw;max-height:90vh;border-radius:10px;object-fit:contain;}
.lb-close{position:absolute;top:calc(14px + env(safe-area-inset-top));right:16px;width:40px;height:40px;border-radius:50%;background:rgba(255,255,255,.14);color:#fff;font-size:18px;display:flex;align-items:center;justify-content:center;cursor:pointer;}
.bubble audio.chat-media-audio{width:220px;margin-top:6px;}
.va-player{margin-top:6px;min-width:210px;max-width:270px;}
.va-name{font:600 12px Manrope;opacity:.85;margin-bottom:5px;word-break:break-word;}
.va-row{display:flex;align-items:center;gap:10px;background:rgba(0,0,0,.18);border-radius:14px;padding:8px 11px;}
.bubble.me .va-row{background:rgba(255,255,255,.16);}
.va-audio{display:none;}
.va-play{width:34px;height:34px;border-radius:50%;border:none;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;padding:0;}
.bubble.me .va-play{background:#fff;color:var(--accent);}
.va-play svg{width:16px;height:16px;display:block;}
.va-track{flex:1;height:4px;border-radius:3px;background:rgba(255,255,255,.28);position:relative;cursor:pointer;}
.bubble.op .va-track{background:rgba(255,255,255,.18);}
.va-fill{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:#fff;width:0%;}
.bubble.op .va-fill{background:var(--accent);}
.va-time{font-size:11px;opacity:.85;min-width:34px;text-align:right;}
.va-dl{width:22px;height:22px;display:flex;align-items:center;justify-content:center;cursor:pointer;opacity:.7;flex-shrink:0;}
.va-dl img{width:13px;height:13px;}
.bubble .chat-media-doc{display:flex;align-items:center;gap:8px;margin-top:6px;background:rgba(0,0,0,.15);border-radius:10px;padding:8px 12px;text-decoration:none;color:inherit;}
.chat-media-wrap{position:relative;display:inline-block;margin-top:6px;}
.chat-media-dl{position:absolute;top:6px;right:6px;width:26px;height:26px;border-radius:50%;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;cursor:pointer;}
.chat-media-dl img{width:13px;height:13px;}
.bubble .chat-media-doc img{width:16px;height:16px;}
.bubble.me .chat-media-doc{background:rgba(255,255,255,.15);}
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
#tg-gate a{display:inline-flex;align-items:center;gap:9px;margin-top:6px;background:var(--accent);color:#fff;text-decoration:none;padding:13px 26px;border-radius:12px;font:700 14px Manrope;}
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
  <p>Это мини-приложение Flux VPN работает только внутри Telegram. Откройте бота и нажмите «Открыть».</p>
  <a href="__BOT_DEEPLINK__"><img src="/assets/miniapp_icons/telegram-100.png" style="width:18px;height:18px;object-fit:contain;" alt="">Открыть в боте</a>
</div>
<div id="splash">
  <div class="brandicon"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg></div>
  <div style="font:800 16px Manrope;color:#fff;letter-spacing:-.01em;">Flux VPN</div>
  <div class="ring"></div>
</div>
<div id="app" style="display:none;">
  <div class="header">
    <div class="left">
      <div class="brandicon" id="brandicon">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg>
      </div>
      <div>
        <div class="title" id="view-title">Flux VPN</div>
        <div class="subtitle" id="view-subtitle">мини-приложение</div>
      </div>
    </div>
  </div>

  <!-- HOME: subscription + plans -->
  <div class="view active" id="view-home">
    <div class="greet-row">
      <div class="avatar-circle" id="home-avatar">F</div>
      <div><div class="hi">Добро пожаловать</div><div class="nm" id="home-greet-name">—</div></div>
    </div>
    <div id="home-offers"></div>
    <div id="home-onboarding"></div>
    <div id="home-sub-card"></div>
    <div class="card row" style="margin-top:10px;cursor:pointer;" onclick="showView('balance')">
      <div style="width:42px;height:42px;border-radius:12px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;flex-shrink:0;"><span class="mi" style="width:25px;height:25px;-webkit-mask-image:url(/assets/miniapp_icons/wallet-100.png);mask-image:url(/assets/miniapp_icons/wallet-100.png);"></span></div>
      <div class="spacer"><div class="muted" style="font:500 12px Manrope;">Баланс</div><div style="font:800 22px Manrope;color:#fff;letter-spacing:-.01em;" id="home-balance">0 <span style="font-size:15px;color:var(--muted);">₽</span></div></div>
      <div style="font:700 13px Manrope;color:var(--accent);background:rgba(123,92,255,.12);padding:9px 16px;border-radius:12px;" onclick="event.stopPropagation();showView('balance')">Пополнить</div>
    </div>
    <div class="sectiontitle">Быстрый доступ</div>
    <div class="navgrid">
      <div class="navitem" onclick="showView('referrals')">
        <div class="ic"><span class="mi" style="width:21px;height:21px;-webkit-mask-image:url(/assets/miniapp_icons/user-account-100.png);mask-image:url(/assets/miniapp_icons/user-account-100.png);"></span></div>
        <div><div class="lbl">Рефералы</div><div class="sub" id="home-ref-sub">—</div></div>
      </div>
      <div class="navitem" onclick="showView('support')">
        <div class="ic"><span class="mi" style="width:21px;height:21px;-webkit-mask-image:url(/assets/miniapp_icons/chat-100.png);mask-image:url(/assets/miniapp_icons/chat-100.png);"></span></div>
        <div><div class="lbl">Поддержка</div><div class="sub">напишите нам</div></div>
      </div>
      <div class="navitem" onclick="showView('devices')">
        <div class="ic"><span class="mi" style="width:21px;height:21px;-webkit-mask-image:url(/assets/miniapp_icons/monitor-100.png);mask-image:url(/assets/miniapp_icons/monitor-100.png);"></span></div>
        <div><div class="lbl">Устройства</div><div class="sub" id="home-dev-sub">—</div></div>
      </div>
      <div class="navitem" onclick="showView('history')">
        <div class="ic"><span class="mi" style="width:21px;height:21px;-webkit-mask-image:url(/assets/miniapp_icons/clock-100.png);mask-image:url(/assets/miniapp_icons/clock-100.png);"></span></div>
        <div><div class="lbl">История</div><div class="sub">операций</div></div>
      </div>
    </div>
    <div id="home-renew-cta"></div>
  </div>

  <!-- RENEWAL: subscription + plans -->
  <div class="view" id="view-renewal">
    <div id="renewal-sub-card"></div>
    <div class="sectiontitle" id="plans-title" style="display:none;">Выберите тариф</div>
    <div id="plans-list"></div>
    <div class="card" style="margin-top:16px;padding:6px 4px;">
      <div class="row" id="autorenew-row" style="padding:11px 14px;">
        <span style="font:600 14px Manrope;color:#fff;">Автопродление</span>
        <div class="spacer"></div>
        <div class="toggle off" id="autorenew-toggle" onclick="toggleAutoRenew()"><div class="knob"></div></div>
      </div>
      <div id="autorenew-divider" style="height:1px;background:rgba(255,255,255,.05);margin:0 14px;"></div>
      <div class="promo-row">
        <input class="promo-input" id="promo-input" placeholder="Промокод">
        <div class="copybtn" onclick="applyPromo()">Применить</div>
      </div>
      <div id="promo-active-note" style="display:none;padding:0 14px 10px;font:600 12px Manrope;color:var(--accent);"></div>
    </div>
  </div>

  <!-- REFERRALS -->
  <div class="view" id="view-referrals">
    <div class="card" style="text-align:center;padding:20px;background:linear-gradient(130deg, rgba(123,92,255,.18), rgba(123,92,255,.04));border:1px solid rgba(123,92,255,.24);">
      <div class="sectiontitle" style="margin:0;">Заработано всего</div>
      <div style="font:800 38px Manrope;color:#fff;margin-top:6px;letter-spacing:-.02em;" id="ref-total">0 <span style="font-size:22px;color:var(--accent);">₽</span></div>
      <div class="row" style="gap:24px;justify-content:center;margin-top:14px;">
        <div><div style="font:800 18px Manrope;color:#fff;" id="ref-friends">0</div><div class="muted" style="font:500 11px Manrope;">друзей</div></div>
        <div style="width:1px;background:rgba(255,255,255,.1);"></div>
        <div><div style="font:800 18px Manrope;color:#fff;" id="ref-percent">0%</div><div class="muted" style="font:500 11px Manrope;">с платежей</div></div>
      </div>
    </div>
    <div class="sectiontitle">Ваш реферальный код</div>
    <div class="card row">
      <span class="mono" style="font-size:14px;color:#fff;flex:1;letter-spacing:.01em;word-break:break-all;" id="ref-link">—</span>
      <div class="copybtn" onclick="copyReferralLink()"><span class="mi" style="width:17px;height:17px;-webkit-mask-image:url(/assets/miniapp_icons/documents-100.png);mask-image:url(/assets/miniapp_icons/documents-100.png);"></span>Копировать</div>
    </div>
    <div class="btn btn-primary" style="margin-top:10px;display:flex;align-items:center;justify-content:center;gap:8px;" onclick="shareReferralLink()">
      <img src="/assets/miniapp_icons/forward-arrow-100.png" style="width:19px;height:19px;object-fit:contain;" alt="">Поделиться в Telegram
    </div>
    <div class="sectiontitle" id="ref-invited-title">Приглашённые</div>
    <div class="card" style="padding:0;overflow:hidden;" id="ref-invited-list"></div>
  </div>

  <!-- REFERRAL DETAIL -->
  <div class="view" id="view-refdetail">
    <div class="card" style="background:linear-gradient(130deg, rgba(123,92,255,.16), rgba(123,92,255,.04));border:1px solid rgba(123,92,255,.22);padding:20px;">
      <div class="row" style="gap:14px;">
        <div class="avatar-circle" id="rd-avatar" style="width:56px;height:56px;font-size:22px;">?</div>
        <div class="spacer">
          <div style="font:800 19px Manrope;color:#fff;letter-spacing:-.01em;" id="rd-name">—</div>
          <div style="font:500 13px Manrope;color:#aab8c2;margin-top:1px;" id="rd-username"></div>
          <div class="mono" style="font-size:11px;color:var(--muted);margin-top:3px;" id="rd-id"></div>
        </div>
      </div>
      <div class="row" style="gap:10px;margin-top:16px;">
        <div style="flex:1;background:rgba(0,0,0,.2);border-radius:12px;padding:12px;text-align:center;"><div style="font:800 20px Manrope;color:var(--accent);" id="rd-brought">0 ₽</div><div class="muted" style="font:500 11px Manrope;margin-top:2px;">принёс вам</div></div>
        <div style="flex:1;background:rgba(0,0,0,.2);border-radius:12px;padding:12px;text-align:center;"><div style="font:800 20px Manrope;color:#fff;" id="rd-topups">0 ₽</div><div class="muted" style="font:500 11px Manrope;margin-top:2px;">его пополнений</div></div>
      </div>
    </div>
    <div class="row" style="justify-content:space-between;margin:18px 4px 4px;">
      <span class="sectiontitle" style="margin:0;">История начислений</span>
      <span class="muted" style="font:600 11px Manrope;" id="rd-percent">0% с платежа</span>
    </div>
    <div class="card" style="padding:0;overflow:hidden;" id="rd-rewards"></div>
  </div>

  <!-- SUBSCRIPTION MANAGEMENT -->
  <div class="view" id="view-submanage">
    <div class="card" style="background:linear-gradient(130deg, rgba(123,92,255,.16), rgba(123,92,255,.04));border:1px solid rgba(123,92,255,.22);display:flex;align-items:center;justify-content:space-between;">
      <div class="row" style="gap:9px;"><div style="width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent);"></div><span style="font:700 14px Manrope;color:#fff;" id="sm-status">Подписка активна</span></div>
      <span class="muted" style="font:600 12px Manrope;" id="sm-until"></span>
    </div>
    <div class="sectiontitle">Подключение по QR</div>
    <div class="card" style="text-align:center;">
      <img id="sm-qr" src="" alt="QR" style="width:170px;height:170px;border-radius:14px;background:#fff;padding:10px;box-sizing:border-box;display:inline-block;">
      <div class="muted" style="font:500 12px Manrope;margin-top:12px;line-height:1.4;">Отсканируйте код в приложении VPN<br>на другом устройстве</div>
    </div>
    <div class="sectiontitle">Ссылка на подписку</div>
    <div class="card row" style="gap:11px;">
      <span class="mono" id="sm-link" style="font-size:12px;color:#aab8c2;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">—</span>
      <div class="copybtn" onclick="copySubLink()" style="flex-shrink:0;"><span class="mi" style="width:16px;height:16px;-webkit-mask-image:url(/assets/miniapp_icons/documents-100.png);mask-image:url(/assets/miniapp_icons/documents-100.png);"></span>Копировать</div>
    </div>
    <div class="card row" style="margin-top:12px;background:rgba(245,181,68,.08);border-color:rgba(245,181,68,.22);cursor:pointer;" onclick="reissueKeys()">
      <div style="width:38px;height:38px;border-radius:11px;background:rgba(245,181,68,.14);display:flex;align-items:center;justify-content:center;flex-shrink:0;"><img src="/assets/miniapp_icons/reset-100.png" style="width:20px;height:20px;object-fit:contain;" alt=""></div>
      <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">Перевыпустить ключи</div><div class="muted" style="font:500 11px Manrope;">Старые ключи перестанут работать</div></div>
      <span class="mi" style="width:20px;height:20px;background:var(--muted);-webkit-mask-image:url(/assets/miniapp_icons/arrow-100.png);mask-image:url(/assets/miniapp_icons/arrow-100.png);"></span>
    </div>
  </div>
  <div class="bottombar" id="submanage-bar" style="display:none;">
    <div class="btn btn-primary" onclick="connectDevice()" style="display:flex;align-items:center;justify-content:center;gap:9px;">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/></svg>Подключить устройство
    </div>
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
    <div id="topup-bonus-note" style="display:none;margin-top:10px;background:linear-gradient(130deg, rgba(123,92,255,.16), rgba(123,92,255,.04));border:1px solid rgba(123,92,255,.25);border-radius:13px;padding:11px 14px;align-items:center;gap:9px;">
      <img src="/assets/miniapp_icons/gift-100.png" style="width:20px;height:20px;object-fit:contain;" alt="">
      <span id="topup-bonus-text" style="font:600 12px Manrope;color:var(--accent);"></span>
    </div>
    <div class="sectiontitle">Способ оплаты</div>
    <div style="display:flex;flex-direction:column;gap:10px;" id="paymethods">
      <div class="payrow selected" data-provider="platega">
        <div style="width:36px;height:36px;border-radius:10px;background:rgba(106,179,243,.16);display:flex;align-items:center;justify-content:center;"><img src="/assets/miniapp_icons/card-100.png" style="width:20px;height:20px;object-fit:contain;" alt=""></div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">Банковская карта</div><div class="muted" style="font:500 12px Manrope;">Platega · Visa, MIR, СБП</div></div>
        <div class="radio" style="border:6px solid var(--accent);background:#fff;"></div>
      </div>
      <div class="payrow" data-provider="cryptobot">
        <div style="width:36px;height:36px;border-radius:10px;background:rgba(247,147,26,.16);display:flex;align-items:center;justify-content:center;"><img src="/assets/miniapp_icons/bitcoin-100.png" style="width:20px;height:20px;object-fit:contain;" alt=""></div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">Криптовалюта</div><div class="muted" style="font:500 12px Manrope;">CryptoBot · USDT, TON, BTC</div></div>
        <div class="radio"></div>
      </div>
    </div>
    <div id="payment-waiting" style="display:none;margin-top:14px;"></div>
  </div>
  <div class="bottombar" id="balance-bar" style="display:none;">
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
      <div style="width:36px;height:36px;border-radius:10px;background:rgba(123,92,255,.16);display:flex;align-items:center;justify-content:center;"><img src="/assets/miniapp_icons/plus-100.png" style="width:20px;height:20px;object-fit:contain;" alt=""></div>
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
  <div id="chat-attach-preview" class="chat-attach-preview" style="display:none;">
    <span id="chat-attach-name"></span>
    <span class="chat-attach-remove" onclick="clearChatAttachment()">✕</span>
  </div>
  <div class="inputbar" id="support-bar" style="display:none;">
    <div class="iconbtn" id="attach-btn" onclick="document.getElementById('chat-file').click()"><img src="/assets/miniapp_icons/attach-100.png" alt=""></div>
    <input type="file" id="chat-file" style="display:none" accept="image/*,video/*,.pdf,.doc,.docx,.xls,.xlsx,.txt,.csv,.zip,.rar,.7z,.tar,.gz" onchange="onChatFileSelected(event)">
    <input id="chat-input" placeholder="Сообщение…" onkeydown="if(event.key==='Enter')sendChat()">
    <div class="iconbtn" id="mic-btn" onclick="toggleVoiceRecord()"><img src="/assets/miniapp_icons/microphone-100.png" alt=""></div>
    <div class="sendbtn" id="send-btn" onclick="sendChat()">
      <img src="/assets/miniapp_icons/sent-100.png" style="width:20px;height:20px;object-fit:contain;" alt="">
    </div>
  </div>

  <!-- EMPTY (no subscription) -->
  <div class="view" id="view-empty">
    <div style="text-align:center;padding:48px 10px 0;">
      <div class="empty-illustration">
        <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="#4a5d70" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg>
      </div>
      <div style="font:800 22px Manrope;color:#fff;margin-top:22px;">Подписки пока нет</div>
      <div class="muted" style="font:500 14px Manrope;margin-top:8px;padding:0 16px;line-height:1.45;">Оформите подписку, чтобы начать пользоваться VPN</div>
    </div>
  </div>
  <div class="bottombar" id="empty-bar" style="display:none;flex-direction:column;gap:10px;">
    <div class="btn btn-primary" onclick="showView('renewal')">Купить подписку</div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="lb" id="lb" onclick="closeLightbox(event)">
  <div class="lb-close" onclick="closeLightbox(event)">✕</div>
  <img id="lb-img" class="lb-media" style="display:none;" alt="">
  <video id="lb-video" class="lb-media" style="display:none;" controls playsinline></video>
</div>

<script>
const FLUX_LOGO_URL = "__FLUX_LOGO_URL__";
const API_BASE = "__MINIAPP_BASE__";
if (FLUX_LOGO_URL) {
  document.querySelectorAll('.brandicon').forEach(el => {
    el.innerHTML = '';
    el.style.background = 'linear-gradient(140deg,var(--accent),var(--accent2))';
    el.style.overflow = 'visible';
    el.style.display = 'flex';
    el.style.alignItems = 'center';
    el.style.justifyContent = 'center';
    const img = document.createElement('img');
    img.src = FLUX_LOGO_URL;
    img.style.width = 'calc(100% - 4px)';
    img.style.height = 'calc(100% - 4px)';
    img.style.objectFit = 'contain';
    img.style.borderRadius = '0';
    el.appendChild(img);
  });
}
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
  const r = await fetch(API_BASE + path, opts);
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch(e) {}
    throw new Error(detail || ('HTTP ' + r.status));
  }
  return r.json();
}
async function apiForm(path, formData) {
  const r = await fetch(API_BASE + path, {method:'POST', body: formData, headers: {'X-Telegram-Init-Data': initData()}});
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch(e) {}
    throw new Error(detail || ('HTTP ' + r.status));
  }
  return r.json();
}
const MEDIA_CACHE = {};      // key "msgId:kind" -> objectURL
const MEDIA_INFLIGHT = {};   // key -> Promise (дедупликация параллельных запросов)
async function fetchMediaBlobUrl(msgId, kind) {
  const key = msgId + ':' + kind;
  if (MEDIA_CACHE[key]) return MEDIA_CACHE[key];
  if (MEDIA_INFLIGHT[key]) return MEDIA_INFLIGHT[key];
  const p = (async () => {
    const r = await fetch(API_BASE + '/api/support/media/' + msgId + '/' + kind, {headers: {'X-Telegram-Init-Data': initData()}});
    if (!r.ok) throw new Error('media fetch failed');
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    MEDIA_CACHE[key] = url;
    delete MEDIA_INFLIGHT[key];
    return url;
  })();
  MEDIA_INFLIGHT[key] = p;
  try { return await p; } catch (e) { delete MEDIA_INFLIGHT[key]; throw e; }
}
async function hydrateChatMedia(root) {
  const els = root.querySelectorAll('[data-media-msg]');
  await Promise.all(Array.prototype.map.call(els, async (el) => {
    const msgId = el.getAttribute('data-media-msg');
    const kind = el.getAttribute('data-media-kind');
    try {
      const url = await fetchMediaBlobUrl(msgId, kind);
      el.addEventListener('load', () => el.classList.add('media-loaded'), {once:true});
      el.addEventListener('loadeddata', () => el.classList.add('media-loaded'), {once:true});
      if (el.tagName === 'A') { el.href = url; el.classList.add('media-loaded'); }
      else el.src = url;
      el.removeAttribute('data-media-msg');
    } catch (e) {}
  }));
}

const VIEW_TITLES = {
  home: ['Flux VPN', 'мини-приложение'],
  renewal: ['Продление', ''],
  referrals: ['Рефералы', ''],
  refdetail: ['Реферал', ''],
  submanage: ['Управление подпиской', ''],
  balance: ['Баланс', ''],
  devices: ['Устройства', ''],
  history: ['История операций', ''],
  support: ['Поддержка Flux', 'Ответ 30 мин – 6 часов'],
  empty: ['Flux VPN', 'мини-приложение'],
};

function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  const el = document.getElementById('view-' + name);
  el.classList.add('active');
  el.classList.remove('fade-in');
  requestAnimationFrame(() => el.classList.add('fade-in'));
  ['balance-bar','empty-bar','support-bar','home-buy-bar','submanage-bar'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.style.display = 'none';
  });
  // Чат поддержки опрашиваем в реальном времени только пока он открыт.
  if (name !== 'support') stopSupportPoll();
  if (name === 'balance') { document.getElementById('balance-bar').style.display = 'block'; updateTopupBtn(); }
  if (name === 'empty') document.getElementById('empty-bar').style.display = 'flex';
  if (name === 'support') { document.getElementById('support-bar').style.display = 'flex'; loadSupport(true); startSupportPoll(); }
  if (name === 'devices') loadDevices();
  if (name === 'history') loadHistory();
  if (name === 'referrals') loadReferrals();
  if (name === 'submanage') { document.getElementById('submanage-bar').style.display = 'block'; loadSubManage(); }
  currentView = name;
  if (name === 'renewal') injectBuyBar();
  const t = VIEW_TITLES[name] || ['Flux VPN', ''];
  document.getElementById('view-title').textContent = t[0];
  document.getElementById('view-subtitle').textContent = t[1];
  document.getElementById('brandicon').style.display = name === 'home' ? 'flex' : 'none';
  if (tg && tg.BackButton) {
    if (name === 'home') tg.BackButton.hide(); else tg.BackButton.show();
  }
}
function goHome() {
  // Детальные экраны возвращают к своему разделу.
  if (currentView === 'refdetail') { showView('referrals'); return; }
  if (currentView === 'submanage') { showView('home'); return; }
  showView('home');
}
if (tg && tg.BackButton) tg.BackButton.onClick(goHome);

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('ru-RU', {day:'numeric', month:'long'});
}

function daysLabel(sub) {
  if (sub.is_lifetime) return '∞';
  return sub.days_left + ' ' + (sub.days_left===1?'день':(sub.days_left>=2&&sub.days_left<=4?'дня':'дней'));
}
function untilLabel(sub) {
  return sub.is_lifetime ? 'навсегда' : ('до ' + fmtDate(sub.expires_at));
}
function subCardHtml(sub, clickable) {
  const statusLabel = sub.status === 'trial' ? 'Триал · активна' : 'Подписка · активна';
  const usedGb = sub.traffic_used_gb != null ? sub.traffic_used_gb : '—';
  const limitGb = sub.traffic_limit_gb != null ? sub.traffic_limit_gb : '∞';
  const pct = (sub.traffic_limit_gb && sub.traffic_used_gb != null) ? Math.min(100, Math.round(sub.traffic_used_gb / sub.traffic_limit_gb * 100)) : 0;
  const clickAttr = clickable ? ' onclick="showView(\'submanage\')" style="background:linear-gradient(130deg, rgba(123,92,255,.16), rgba(123,92,255,.04));border:1px solid rgba(123,92,255,.22);cursor:pointer;"' : ' style="background:linear-gradient(130deg, rgba(123,92,255,.16), rgba(123,92,255,.04));border:1px solid rgba(123,92,255,.22);"';
  const manageHint = clickable ? `
      <div class="row" style="justify-content:space-between;margin-top:14px;padding-top:13px;border-top:1px solid rgba(255,255,255,.08);">
        <span style="font:700 13px Manrope;color:#fff;">Управление подпиской</span>
        <span class="mi" style="width:20px;height:20px;background:var(--accent);-webkit-mask-image:url(/assets/miniapp_icons/arrow-100.png);mask-image:url(/assets/miniapp_icons/arrow-100.png);"></span>
      </div>` : '';
  return `
    <div class="card"${clickAttr}>
      <div class="row" style="justify-content:space-between;">
        <div style="font:700 15px Manrope;color:#fff;">${statusLabel}</div>
        <div style="font:700 11px Manrope;color:var(--accent);background:rgba(123,92,255,.14);padding:4px 9px;border-radius:9px;">${daysLabel(sub)}</div>
      </div>
      <div class="progress" style="margin-top:12px;"><div style="width:${pct}%;"></div></div>
      <div class="row" style="justify-content:space-between;margin-top:8px;">
        <span class="muted" style="font:500 12px Manrope;">${usedGb} / ${limitGb} ГБ использовано</span>
        <span class="muted" style="font:500 12px Manrope;">${untilLabel(sub)}</span>
      </div>${manageHint}
    </div>`;
}

const TG_AVATAR_COLORS = ['#e17076', '#eda86c', '#a695e7', '#7bc862', '#6ec9cb', '#65aadd', '#ee7aae'];
function applyAvatar(el, opts) {
  const photoUrl = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.photo_url) || '';
  if (photoUrl) {
    el.style.backgroundImage = `url("${photoUrl}")`;
    el.style.background = `url("${photoUrl}") center/cover`;
    el.textContent = '';
    return;
  }
  const tgId = Math.abs(parseInt(opts.telegramId) || 0);
  const idx = tgId % TG_AVATAR_COLORS.length;
  el.style.backgroundImage = 'none';
  el.style.background = TG_AVATAR_COLORS[idx];
  const letter = (opts.name || opts.username || '?').trim().charAt(0).toUpperCase();
  el.textContent = letter || '?';
}
function renderHome() {
  const sub = CTX.subscription;
  const u = CTX.user;
  applyAvatar(document.getElementById('home-avatar'), {telegramId: u.telegram_id, name: u.first_name, username: u.username});
  document.getElementById('home-greet-name').textContent = u.username ? `${u.first_name} · @${u.username}` : u.first_name;
  document.getElementById('home-balance').innerHTML = Math.round(parseFloat(u.balance_rub)) + ' <span style="font-size:15px;color:var(--muted);">₽</span>';
  document.getElementById('home-dev-sub').textContent = (CTX.devices_used || 0) + ' из ' + (CTX.devices_total || 0);
  const refCount = CTX.referrals_count || 0;
  document.getElementById('home-ref-sub').textContent = refCount > 0
    ? (refCount + ' · +' + Math.round(parseFloat(CTX.referrals_earned_rub || '0')) + ' ₽')
    : 'Пусто :(';
  renderRenewal(sub);
  renderOffers();
  renderOnboarding();
  if (!sub) {
    document.getElementById('home-sub-card').innerHTML = `
      <div class="card" style="display:flex;align-items:center;gap:13px;">
        <div style="width:44px;height:44px;border-radius:50%;background:rgba(255,255,255,.05);border:1px dashed rgba(255,255,255,.14);display:flex;align-items:center;justify-content:center;flex-shrink:0;">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#4a5d70" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg>
        </div>
        <div><div style="font:700 15px Manrope;color:#fff;">Нет активной подписки</div><div class="muted" style="font:500 12px Manrope;margin-top:2px;">Оформите подписку, чтобы пользоваться VPN</div></div>
      </div>`;
    document.getElementById('home-renew-cta').innerHTML = `
      <div class="cta-renew" onclick="showView('renewal')">
        <div class="ic"><img src="/assets/miniapp_icons/buying-100.png" style="width:20px;height:20px;object-fit:contain;" alt=""></div>
        <div class="spacer"><div style="font:800 15px Manrope;color:#fff;">Купить подписку</div><div style="font:600 12px Manrope;color:rgba(255,255,255,.75);">тарифы · промокод</div></div>
        <span class="mi" style="width:20px;height:20px;background:rgba(255,255,255,.85);-webkit-mask-image:url(/assets/miniapp_icons/arrow-100.png);mask-image:url(/assets/miniapp_icons/arrow-100.png);"></span>
      </div>`;
    return;
  }
  document.getElementById('home-sub-card').innerHTML = subCardHtml(sub, true);
  document.getElementById('home-renew-cta').innerHTML = `
    <div class="cta-renew" onclick="showView('renewal')">
      <div class="ic"><img src="/assets/miniapp_icons/reset-100.png" style="width:20px;height:20px;object-fit:contain;" alt=""></div>
      <div class="spacer"><div style="font:800 15px Manrope;color:#fff;">Продление</div><div style="font:600 12px Manrope;color:rgba(255,255,255,.75);">тарифы · промокод · автопродление</div></div>
      <span class="mi" style="width:20px;height:20px;background:rgba(255,255,255,.85);-webkit-mask-image:url(/assets/miniapp_icons/arrow-100.png);mask-image:url(/assets/miniapp_icons/arrow-100.png);"></span>
    </div>`;
}

function escH(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

function renderOffers() {
  const box = document.getElementById('home-offers');
  const o = CTX.offers || {};
  let html = '';
  if (o.intro) {
    html += `
      <div class="card" style="margin-bottom:10px;cursor:pointer;background:radial-gradient(120% 160% at 0% 0%,rgba(123,92,255,.35),var(--card) 60%);border:1px solid rgba(123,92,255,.45);"
           onclick="showView('renewal'); setTimeout(function(){ selectPlan(${o.intro.plan_id}); }, 50);">
        <div style="font:800 10px Manrope;color:#C8B6FF;letter-spacing:.08em;">РАЗОВАЯ АКЦИЯ ДЛЯ НОВЫХ</div>
        <div style="font:800 18px Manrope;color:#fff;margin-top:6px;">🔥 ${escH(o.intro.title)}
          <span style="font:600 13px Manrope;color:var(--muted);text-decoration:line-through;margin-left:4px;">${Math.round(parseFloat(o.intro.old_price_rub))} ₽</span></div>
        <div class="muted" style="font:500 12px Manrope;margin-top:4px;">Доступна один раз — до первой покупки подписки. Нажмите, чтобы забрать.</div>
      </div>`;
  }
  if (o.winback) {
    const until = o.winback.until ? new Date(o.winback.until).toLocaleDateString('ru-RU') : '';
    html += `
      <div class="card" style="margin-bottom:10px;cursor:pointer;border:1px solid rgba(79,210,160,.35);" onclick="showView('renewal')">
        <div style="font:800 20px Manrope;color:#4FD2A0;">−${escH(o.winback.percent)}% для вас</div>
        <div style="font:700 14px Manrope;color:#fff;margin-top:4px;">С возвращением! Скидка на любой тариф</div>
        <div class="muted" style="font:500 12px Manrope;margin-top:2px;">Одна покупка${until ? ' · до ' + until : ''}</div>
      </div>`;
  }
  box.innerHTML = html;
}

let obTimer = null;
function renderOnboarding() {
  const box = document.getElementById('home-onboarding');
  const ob = CTX.onboarding;
  if (!ob) { box.innerHTML = ''; if (obTimer) { clearInterval(obTimer); obTimer = null; } return; }
  const ua = navigator.userAgent || '';
  const os = /iPhone|iPad|iPod/.test(ua) ? 'ios' : /Android/.test(ua) ? 'android' : /Mac/.test(ua) ? 'macos' : /Win/.test(ua) ? 'windows' : '';
  const apps = (ob.apps || []).map(a => `<div onclick="openLink('${escH(a.url)}')" style="padding:9px 12px;border-radius:11px;font:700 12.5px Manrope;cursor:pointer;${a.os === os ? 'background:var(--accent);color:#fff;' : 'background:rgba(255,255,255,.06);color:#fff;'}">${escH(a.label)}</div>`).join('');
  const step = (n, t, b) => `<div style="display:flex;gap:12px;padding:12px 0;border-top:1px solid rgba(255,255,255,.06);">
      <div style="width:26px;height:26px;border-radius:8px;background:rgba(123,92,255,.2);color:#C8B6FF;font:800 12px Manrope;display:flex;align-items:center;justify-content:center;flex-shrink:0;">${n}</div>
      <div style="flex:1;min-width:0;"><div style="font:800 14px Manrope;color:#fff;">${t}</div><div style="margin-top:8px;">${b}</div></div></div>`;
  box.innerHTML = `
    <div class="card" style="margin-bottom:10px;border:1px solid rgba(123,92,255,.45);background:linear-gradient(160deg,rgba(123,92,255,.16),var(--card) 55%);">
      <div style="font:800 17px Manrope;color:#fff;">⚡ Подключитесь за 2 минуты</div>
      <div class="muted" style="font:500 12px Manrope;margin-top:3px;">Подписка активна, но ни одно устройство ещё не подключено</div>
      <div style="margin-top:10px;">
        ${step(1, 'Установите ' + escH(ob.app_name), `<div style="display:flex;gap:8px;flex-wrap:wrap;">${apps}</div>`)}
        ${step(2, 'Добавьте подписку', `<div style="display:flex;gap:8px;flex-wrap:wrap;">
            <div onclick="openLink('${escH(ob.import_url)}')" style="padding:9px 12px;border-radius:11px;background:var(--accent);color:#fff;font:700 12.5px Manrope;cursor:pointer;">＋ Добавить в ${escH(ob.app_name)}</div>
            <div id="ob-copy" style="padding:9px 12px;border-radius:11px;background:rgba(255,255,255,.06);color:#fff;font:700 12.5px Manrope;cursor:pointer;">Скопировать ключ</div></div>
            <div class="muted" style="font:500 11px Manrope;margin-top:6px;">Если приложение не открылось — скопируйте ключ и вставьте его в приложении через «+».</div>`)}
        ${step(3, 'Нажмите «Подключить»', '<div class="muted" style="font:500 12px Manrope;">Как только устройство подключится, этот блок исчезнет.</div>')}
      </div>
      <div onclick="showView('support')" style="font:700 12px Manrope;color:#C8B6FF;margin-top:6px;cursor:pointer;">Не получается? Напишите в поддержку →</div>
    </div>`;
  const cp = document.getElementById('ob-copy');
  if (cp) cp.onclick = function(){ try { navigator.clipboard.writeText(ob.subscription_url); cp.textContent = 'Скопировано ✓'; } catch (e) {} };
  if (!obTimer) {
    let tries = 0;
    obTimer = setInterval(async function(){
      if (document.hidden) return;
      if (++tries > 60) { clearInterval(obTimer); obTimer = null; return; }
      try {
        const r = await api('/api/context');
        if (r && r.devices_used > 0) { CTX = r; clearInterval(obTimer); obTimer = null; renderHome(); if (window.Telegram && Telegram.WebApp && Telegram.WebApp.HapticFeedback) Telegram.WebApp.HapticFeedback.notificationOccurred('success'); }
      } catch (e) {}
    }, 15000);
  }
}

function openLink(url) {
  if (!url) return;
  if (/^https?:/.test(url) && window.Telegram && Telegram.WebApp && Telegram.WebApp.openLink) Telegram.WebApp.openLink(url);
  else window.location.href = url;
}

function renderRenewal(sub) {
  const card = document.getElementById('renewal-sub-card');
  const arRow = document.getElementById('autorenew-row');
  const arDiv = document.getElementById('autorenew-divider');
  if (sub) {
    card.innerHTML = subCardHtml(sub);
    document.getElementById('autorenew-toggle').className = 'toggle ' + (sub.auto_renew ? 'on' : 'off');
    arRow.style.display = 'flex';
    arDiv.style.display = 'block';
  } else {
    card.innerHTML = `<div style="padding:6px 2px 2px;"><div style="font:800 18px Manrope;color:#fff;">Оформите подписку</div><div class="muted" style="font:500 13px Manrope;margin-top:4px;">Выберите тариф ниже и оплатите с баланса</div></div>`;
    arRow.style.display = 'none';
    arDiv.style.display = 'none';
  }
  renderPlans();
}

let selectedPlanId = null;
function renderPlans() {
  document.getElementById('plans-title').style.display = 'block';
  const list = document.getElementById('plans-list');
  list.innerHTML = CTX.plans.map(p => {
    const disc = parseFloat(p.discount_percent || '0');
    const hasDisc = disc > 0;
    const orig = Math.round(parseFloat(p.original_price_rub || p.price_rub));
    const final = Math.round(parseFloat(p.price_rub));
    return `
    <div class="plan" data-plan="${p.id}" style="margin-top:10px;" onclick="selectPlan(${p.id})">
      ${hasDisc ? `<div class="badge">−${disc}%</div>` : ''}
      <div class="radio"></div>
      <div class="spacer">
        <div style="font:700 15px Manrope;color:#fff;">${p.name}</div>
        <div class="muted" style="font:500 12px Manrope;">${p.traffic_limit_gb || p.monthly_gb_limit || '∞'} ГБ · ${p.device_limit || CTX.included_device_slots || 2} устройств</div>
      </div>
      <div style="text-align:right;">
        <div style="font:800 16px Manrope;color:#fff;">${final} ₽</div>
        ${(hasDisc && orig > final) ? `<div class="muted" style="font:500 11px Manrope;text-decoration:line-through;">${orig} ₽</div>` : ''}
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
  bar.style.display = currentView === 'renewal' ? 'block' : 'none';
  const verb = CTX.subscription ? 'Продлить' : 'Купить';
  bar.innerHTML = `<div class="btn btn-primary" id="buy-plan-btn" onclick="buyPlan()">${verb} · ${plan.name} — ${Math.round(parseFloat(plan.price_rub))} ₽</div>`;
}

let _autoRenewInFlight = false;
async function toggleAutoRenew() {
  if (_autoRenewInFlight || !CTX.subscription) return;
  _autoRenewInFlight = true;
  const next = !CTX.subscription.auto_renew;
  try {
    const r = await api('/api/subscription/auto-renew', {method:'POST', body: JSON.stringify({enabled: next})});
    if (r.ok) { CTX.subscription.auto_renew = next; document.getElementById('autorenew-toggle').className = 'toggle ' + (next ? 'on' : 'off'); }
    toast(r.message || (r.ok ? 'Сохранено' : 'Ошибка'));
  } catch (e) { toast('Ошибка: ' + e.message); }
  finally { _autoRenewInFlight = false; }
}

let _promoInFlight = false;
async function applyPromo() {
  if (_promoInFlight) return;
  const input = document.getElementById('promo-input');
  const code = input.value.trim();
  if (!code) return;
  _promoInFlight = true;
  try {
    const r = await api('/api/promo/apply', {method:'POST', body: JSON.stringify({code})});
    toast(r.message || (r.ok ? 'Промокод применён' : 'Не удалось применить'));
    if (r.ok) { input.value = ''; await loadContext(); }
  } catch (e) { toast('Ошибка: ' + e.message); }
  finally { _promoInFlight = false; }
}

// --- Скелетоны загрузки ---
function skelInline(w, h) { return `<span class="skel" style="display:inline-block;width:${w}px;height:${h||16}px;border-radius:6px;vertical-align:middle;"></span>`; }
function skelCardRows(n) {
  let h = '';
  for (let i = 0; i < (n || 3); i++) h += `<div class="card row" style="margin-top:10px;"><div class="skel" style="width:38px;height:38px;border-radius:11px;flex-shrink:0;"></div><div class="spacer"><div class="skel" style="width:55%;height:13px;border-radius:6px;"></div><div class="skel" style="width:32%;height:10px;border-radius:6px;margin-top:8px;"></div></div></div>`;
  return h;
}
function skelHistory(n) {
  let h = '';
  for (let i = 0; i < (n || 4); i++) h += `<div class="history-item"><div class="skel" style="width:38px;height:38px;border-radius:11px;flex-shrink:0;"></div><div class="spacer"><div class="skel" style="width:55%;height:13px;border-radius:6px;"></div><div class="skel" style="width:32%;height:10px;border-radius:6px;margin-top:8px;"></div></div><div class="skel" style="width:50px;height:14px;border-radius:6px;"></div></div>`;
  return h;
}

async function loadReferrals() {
  // Скелетон, пока грузятся данные.
  document.getElementById('ref-total').innerHTML = skelInline(90, 30);
  document.getElementById('ref-friends').innerHTML = skelInline(24, 18);
  document.getElementById('ref-percent').innerHTML = skelInline(36, 18);
  document.getElementById('ref-invited-list').innerHTML = skelHistory(3);
  try {
    const r = await api('/api/referrals');
    document.getElementById('ref-total').innerHTML = Math.round(parseFloat(r.total_earned_rub)) + ' <span style="font-size:22px;color:var(--accent);">₽</span>';
    document.getElementById('ref-friends').textContent = r.friends_count;
    document.getElementById('ref-percent').textContent = Math.round(parseFloat(r.percent)) + '%';
    document.getElementById('ref-link').textContent = r.link || 'недоступно';
    window._refLink = r.link || '';
    document.getElementById('ref-invited-title').textContent = 'Приглашённые · ' + r.friends_count;
    const list = document.getElementById('ref-invited-list');
    if (!r.invited.length) { list.innerHTML = '<div class="muted" style="padding:18px;">Пока никого не пригласили</div>'; return; }
    list.innerHTML = r.invited.map(f => {
      const initial = (f.name || '?').charAt(0).toUpperCase();
      const bonus = Math.round(parseFloat(f.bonus_rub || '0'));
      // Цвет фона аватарки — по telegram_id, как в Telegram (фото других юзеров недоступно в Mini App).
      const avColor = TG_AVATAR_COLORS[Math.abs(parseInt(f.telegram_id)||0) % TG_AVATAR_COLORS.length];
      // 0 ₽ показываем без плюса; больше нуля — с плюсом и акцентом.
      const amt = bonus > 0
        ? `<div style="font:800 14px Manrope;color:var(--accent);">+${bonus} ₽</div>`
        : `<div style="font:800 14px Manrope;color:var(--muted);">0 ₽</div>`;
      return `<div class="history-item" style="cursor:pointer;" onclick="openReferralDetail(${f.id})">
        <div class="avatar-circle" style="width:40px;height:40px;font-size:15px;background:${avColor};">${initial}</div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">${f.name}</div><div class="muted" style="font:500 11px Manrope;">${f.username ? '@'+f.username : ('ID '+f.telegram_id)}</div></div>
        ${amt}
        <span class="mi" style="width:18px;height:18px;margin-left:4px;background:var(--muted);-webkit-mask-image:url(/assets/miniapp_icons/arrow-100.png);mask-image:url(/assets/miniapp_icons/arrow-100.png);flex-shrink:0;"></span>
      </div>`;
    }).join('');
  } catch (e) { toast('Ошибка загрузки рефералов'); }
}
async function openReferralDetail(id) {
  showView('refdetail');
  // Скелетон шапки и истории.
  document.getElementById('rd-name').innerHTML = skelInline(120, 19);
  document.getElementById('rd-username').innerHTML = skelInline(80, 13);
  document.getElementById('rd-id').innerHTML = skelInline(90, 11);
  document.getElementById('rd-brought').innerHTML = skelInline(60, 20);
  document.getElementById('rd-topups').innerHTML = skelInline(60, 20);
  document.getElementById('rd-rewards').innerHTML = skelHistory(3);
  try {
    const d = await api('/api/referrals/' + id);
    const av = document.getElementById('rd-avatar');
    av.textContent = (d.name || '?').charAt(0).toUpperCase();
    av.style.background = TG_AVATAR_COLORS[Math.abs(parseInt(d.telegram_id)||0) % TG_AVATAR_COLORS.length];
    document.getElementById('rd-name').textContent = d.name;
    document.getElementById('rd-username').textContent = d.username ? '@'+d.username : '';
    document.getElementById('rd-id').textContent = 'ID ' + d.telegram_id;
    document.getElementById('rd-brought').textContent = '+' + Math.round(parseFloat(d.total_brought_rub)) + ' ₽';
    document.getElementById('rd-topups').textContent = Math.round(parseFloat(d.their_topups_rub)) + ' ₽';
    document.getElementById('rd-percent').textContent = Math.round(parseFloat(d.percent)) + '% с платежа';
    const list = document.getElementById('rd-rewards');
    if (!d.rewards.length) { list.innerHTML = '<div class="muted" style="padding:18px;">Пока нет начислений</div>'; return; }
    list.innerHTML = d.rewards.map(rw => `
      <div class="history-item">
        <div class="history-ic" style="background:rgba(123,92,255,.14);"><img src="/assets/miniapp_icons/up-arrow-100.png" style="width:20px;height:20px;object-fit:contain;" alt=""></div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">${rw.title}</div><div class="muted" style="font:500 11px Manrope;">${fmtDate(rw.created_at)}</div></div>
        <div style="font:800 14px Manrope;color:var(--accent);">+${Math.round(parseFloat(rw.bonus_rub))} ₽</div>
      </div>`).join('');
  } catch (e) { toast('Не удалось открыть реферала'); }
}
function copyReferralLink() {
  const link = window._refLink || '';
  if (!link) return;
  if (navigator.clipboard) navigator.clipboard.writeText(link);
  toast('Ссылка скопирована');
}
function shareReferralLink() {
  const link = window._refLink || '';
  if (!link) return;
  const url = 'https://t.me/share/url?url=' + encodeURIComponent(link);
  if (tg && tg.openTelegramLink) tg.openTelegramLink(url); else window.open(url, '_blank');
}

// --- Управление подпиской ---
function loadSubManage() {
  const sub = CTX && CTX.subscription;
  if (!sub) { showView('home'); return; }
  document.getElementById('sm-status').textContent = sub.status === 'trial' ? 'Триал активен' : 'Подписка активна';
  document.getElementById('sm-until').textContent = untilLabel(sub) + (sub.is_lifetime ? '' : (' · ' + daysLabel(sub)));
  const url = sub.subscription_url || '';
  window._subUrl = url;
  document.getElementById('sm-link').textContent = url || 'недоступно';
  loadQr(url);
}
function loadQr(url) {
  const qr = document.getElementById('sm-qr');
  if (!url) { qr.removeAttribute('src'); return; }
  // Скелетон, пока QR грузится с сервера.
  qr.style.background = 'linear-gradient(90deg,#1b2531 0%,#26323f 50%,#1b2531 100%)';
  qr.style.backgroundSize = '600px 100%';
  qr.style.animation = 'shimmer 1.3s infinite linear';
  qr.style.opacity = '0';
  qr.onload = () => { qr.style.background = '#fff'; qr.style.animation = 'none'; qr.style.transition = 'opacity .25s'; qr.style.opacity = '1'; };
  qr.onerror = () => { qr.style.animation = 'none'; qr.style.background = 'var(--card2)'; qr.style.opacity = '1'; };
  qr.src = API_BASE + '/api/subscription/qr?data=' + encodeURIComponent(url) + '&init=' + encodeURIComponent(initData());
}
function copySubLink() {
  const url = window._subUrl || '';
  if (!url) { toast('Ссылка недоступна'); return; }
  if (navigator.clipboard) navigator.clipboard.writeText(url);
  toast('Ссылка скопирована');
}
function connectDevice() {
  const url = window._subUrl || '';
  if (!url) { toast('Ссылка недоступна'); return; }
  if (tg && tg.openLink) tg.openLink(url); else window.open(url, '_blank');
}
let _reissueInFlight = false;
async function reissueKeys() {
  if (_reissueInFlight) return;
  if (!confirm('Перевыпустить ключи? Старые подключения перестанут работать.')) return;
  _reissueInFlight = true;
  try {
    const r = await api('/api/subscription/reissue', {method:'POST', body:'{}'});
    if (r.ok) {
      toast('Ключи перевыпущены ✅');
      if (CTX && CTX.subscription) CTX.subscription.subscription_url = r.subscription_url;
      window._subUrl = r.subscription_url;
      document.getElementById('sm-link').textContent = r.subscription_url || '';
      loadQr(r.subscription_url);
    } else toast(r.message || 'Не удалось перевыпустить');
  } catch (e) { toast('Ошибка: ' + e.message); }
  finally { _reissueInFlight = false; }
}
let _buyPlanInFlight = false;
async function buyPlan() {
  if (!selectedPlanId || _buyPlanInFlight) return;
  _buyPlanInFlight = true;
  const btn = document.getElementById('buy-plan-btn');
  if (btn) btn.dataset.busy = '1';
  try {
    const r = await api('/api/plan/buy', {method:'POST', body: JSON.stringify({plan_id: selectedPlanId, idempotency_key: genKey()})});
    if (r.ok) { toast('Подписка оформлена ✅'); await loadContext(); showView('home'); }
    else if (r.kind === 'insufficient') {
      // Денег не хватает — ведём на пополнение, после оплаты вернёмся к покупке.
      pendingBuyPlanId = selectedPlanId;
      toast('Недостаточно средств — пополните баланс');
      showView('balance');
    }
    else toast(r.message || 'Недостаточно средств');
  } catch (e) { toast('Ошибка: ' + e.message); }
  finally { _buyPlanInFlight = false; }
}
let pendingBuyPlanId = null;

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
  const pct = parseFloat((CTX && CTX.pending_topup_bonus_percent) || '0');
  const note = document.getElementById('topup-bonus-note');
  if (pct > 0 && selectedAmount > 0) {
    const bonus = Math.floor(selectedAmount * pct / 100);
    document.getElementById('topup-bonus-text').textContent = `Промокод: +${pct}% к пополнению — придёт ещё ${bonus} ₽`;
    note.style.display = 'flex';
  } else {
    note.style.display = 'none';
  }
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
    if (r.status === 'completed') {
      toast('Оплата получена ✅');
      await loadContext();
      document.getElementById('payment-waiting').style.display = 'none';
      // Если пополняли ради покупки тарифа — возвращаемся к нему.
      if (pendingBuyPlanId) { selectedPlanId = pendingBuyPlanId; pendingBuyPlanId = null; renderPlans(); showView('renewal'); }
    }
    else toast('Платёж пока не подтверждён');
  } catch (e) { toast('Ошибка проверки'); }
}

// --- Devices ---
const DEV_ICONS = {phone:'iphone-100.png', tv:'tv-100.png', computer:'workstation-100.png', unknown:'system-report-100.png'};
function devIcon(dtype) { return DEV_ICONS[dtype] || DEV_ICONS.unknown; }
let CTX_DEVICES = null;
async function loadDevices() {
  // Скелетон, пока грузится список из панели.
  document.getElementById('dev-count-text').innerHTML = skelInline(150, 18);
  document.getElementById('dev-count-sub').innerHTML = skelInline(90, 11);
  document.getElementById('devices-list').innerHTML = skelCardRows(3);
  document.getElementById('buy-slots-row').style.display = 'none';
  try {
    const d = await api('/api/devices');
    CTX_DEVICES = d;
    const pct = d.total ? Math.min(100, Math.round(d.used / d.total * 100)) : 0;
    document.getElementById('dev-ring').setAttribute('stroke-dashoffset', String(138 - 138 * pct / 100));
    document.getElementById('dev-count-text').textContent = d.unlimited ? (d.used + ' устройств (без лимита)') : (d.used + ' из ' + d.total + ' устройств');
    document.getElementById('dev-count-sub').textContent = d.used >= d.total && !d.unlimited ? 'Базовый лимит занят полностью' : 'Слоты свободны';
    document.getElementById('devices-list').innerHTML = d.devices.map(dev => `
      <div class="card row" style="margin-top:10px;">
        <div style="width:40px;height:40px;border-radius:11px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;flex-shrink:0;"><span class="mi" style="width:22px;height:22px;-webkit-mask-image:url(/assets/miniapp_icons/${devIcon(dev.dtype)});mask-image:url(/assets/miniapp_icons/${devIcon(dev.dtype)});"></span></div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">${dev.title}</div><div class="muted" style="font:500 12px Manrope;">${dev.platform || ''}</div></div>
        <img src="/assets/miniapp_icons/trash-100.png" style="width:20px;height:20px;object-fit:contain;cursor:pointer;flex-shrink:0;" onclick="unbindDevice('${dev.hwid}')" alt="Удалить">
      </div>`).join('') || '<div class="muted" style="padding:14px;">Нет подключённых устройств</div>';
    document.getElementById('buy-slots-row').style.display = d.available_to_buy > 0 ? 'flex' : 'none';
    document.getElementById('buy-slots-sub').textContent = '1 устройство · списывается ежемесячно';
    document.getElementById('buy-slots-price').textContent = d.extra_slot_price_rub + ' ₽/мес';
  } catch (e) { toast('Ошибка загрузки устройств'); }
}
let _buySlotInFlight = false;
async function buySlot() {
  const priceTxt = (CTX_DEVICES && CTX_DEVICES.extra_slot_price_rub) ? (CTX_DEVICES.extra_slot_price_rub + ' ₽/мес') : 'месячную плату';
  if (_buySlotInFlight || !confirm('Добавить 1 слот устройства за ' + priceTxt + '?\\nСумма списывается ежемесячно автоматически, пока слот активен.')) return;
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
  topup: ['bill-100.png', 'rgba(123,92,255,.14)'],
  subscription: ['buying-100.png', 'rgba(255,255,255,.06)'],
  subscription_autorenew: ['buying-100.png', 'rgba(255,255,255,.06)'],
  purchase_plan: ['buying-100.png', 'rgba(255,255,255,.06)'],
  manual_add: ['buying-100.png', 'rgba(255,255,255,.06)'],
  device_slots_purchased: ['buying-100.png', 'rgba(255,255,255,.06)'],
};
function histIconFor(t) {
  if (HIST_ICONS[t.type]) return HIST_ICONS[t.type];
  return t.direction === 'debit' ? ['buying-100.png', 'rgba(255,255,255,.06)'] : ['gift-100.png', 'rgba(123,92,255,.14)'];
}
async function loadHistory() {
  document.getElementById('history-list').innerHTML = skelHistory(4);
  try {
    const r = await api('/api/transactions');
    const list = document.getElementById('history-list');
    if (!r.transactions.length) { list.innerHTML = '<div class="muted" style="padding:18px;">Пока нет операций</div>'; return; }
    list.innerHTML = r.transactions.map(t => {
      const ic = histIconFor(t);
      const color = t.direction === 'debit' ? '#C8D2DA' : '#7B5CFF';
      // amount_label с бэка: «+3 д.», «+10 ГБ», «−65 ₽» или пусто.
      const amtHtml = t.amount_label ? `<div style="font:800 15px Manrope;color:${color};">${t.amount_label}</div>` : '';
      return `<div class="history-item">
        <div class="history-ic" style="background:${ic[1]};"><img src="/assets/miniapp_icons/${ic[0]}" style="width:20px;height:20px;object-fit:contain;" alt=""></div>
        <div class="spacer"><div style="font:700 14px Manrope;color:#fff;">${t.description || t.type}</div><div class="muted" style="font:500 11px Manrope;">${fmtDate(t.created_at)}</div></div>
        ${amtHtml}
      </div>`;
    }).join('');
  } catch (e) { toast('Ошибка загрузки истории'); }
}

// --- Support ---
function dlBtn(msgId, kind) {
  return `<span class="chat-media-dl" data-dl-msg="${msgId}" data-dl-kind="${kind}"><img src="/assets/miniapp_icons/down-arrow-100.png" alt=""></span>`;
}
// Если медиа уже в кэше — сразу подставляем src (без мигания); иначе data-атрибуты для hydrate.
// Класс media-loaded дописывается в существующий class элемента, чтобы не плодить дубль-атрибут.
function mCls(msgId, kind) {
  return MEDIA_CACHE[msgId + ':' + kind] ? ' media-loaded' : '';
}
function mSrc(msgId, kind, isAnchor) {
  const cached = MEDIA_CACHE[msgId + ':' + kind];
  if (cached) return isAnchor ? `href="${cached}"` : `src="${cached}"`;
  return `data-media-msg="${msgId}" data-media-kind="${kind}"`;
}
function voicePlayerHtml(id, kind, name) {
  const label = name ? `<div class="va-name">${name.replace(/</g,'&lt;')}</div>` : '';
  return `<div class="va-player">${label}<div class="va-row"><audio class="va-audio" ${mSrc(id,kind,false)} preload="metadata"></audio><button class="va-play" type="button"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></button><div class="va-track"><div class="va-fill"></div></div><span class="va-time mono">0:00</span><span class="va-dl" data-dl-msg="${id}" data-dl-kind="${kind}"><img src="/assets/miniapp_icons/down-arrow-100.png" alt=""></span></div></div>`;
}
function setupVoicePlayers(root) {
  root.querySelectorAll('.va-player').forEach(pl => {
    if (pl._wired) return; pl._wired = true;
    const audio = pl.querySelector('.va-audio');
    const playBtn = pl.querySelector('.va-play');
    const fill = pl.querySelector('.va-fill');
    const track = pl.querySelector('.va-track');
    const timeEl = pl.querySelector('.va-time');
    const icon = playBtn.querySelector('svg path');
    const fmt = (s) => { s = Math.floor(s||0); return Math.floor(s/60)+':'+('0'+(s%60)).slice(-2); };
    const PLAY = 'M8 5v14l11-7z', PAUSE = 'M7 5h3v14H7zM14 5h3v14h-3z';
    playBtn.addEventListener('click', () => {
      if (audio.paused) {
        document.querySelectorAll('.va-audio').forEach(a => { if (a !== audio) a.pause(); });
        audio.play();
      } else audio.pause();
    });
    audio.addEventListener('play', () => icon.setAttribute('d', PAUSE));
    audio.addEventListener('pause', () => icon.setAttribute('d', PLAY));
    audio.addEventListener('ended', () => { icon.setAttribute('d', PLAY); fill.style.width = '0%'; timeEl.textContent = fmt(audio.duration); });
    audio.addEventListener('timeupdate', () => { if (audio.duration) { fill.style.width = (audio.currentTime/audio.duration*100)+'%'; timeEl.textContent = fmt(audio.currentTime); } });
    audio.addEventListener('loadedmetadata', () => { if (audio.duration && isFinite(audio.duration)) timeEl.textContent = fmt(audio.duration); });
    track.addEventListener('click', (e) => { const rect = track.getBoundingClientRect(); if (audio.duration) audio.currentTime = ((e.clientX-rect.left)/rect.width)*audio.duration; });
  });
}
function chatMediaHtml(m) {
  let html = '';
  if (m.photo_file_id) {
    html += `<div class="chat-media-wrap" data-lb-msg="${m.id}" data-lb-kind="photo"><img class="chat-media-img${mCls(m.id,'photo')}" ${mSrc(m.id,'photo',false)} alt="">${dlBtn(m.id,'photo')}</div>`;
  }
  if (m.video_file_id) {
    html += `<div class="chat-media-wrap" data-lb-msg="${m.id}" data-lb-kind="video"><video class="chat-media-video${mCls(m.id,'video')}" ${mSrc(m.id,'video',false)} playsinline preload="metadata" muted></video><div class="vid-play-ov"><svg viewBox="0 0 24 24" fill="#fff"><path d="M8 5v14l11-7z"/></svg></div>${dlBtn(m.id,'video')}</div>`;
  }
  if (m.video_note_file_id) {
    html += `<video class="chat-media-vidnote${mCls(m.id,'video-note')}" ${mSrc(m.id,'video-note',false)} controls playsinline preload="metadata"></video>`;
  }
  if (m.voice_file_id) {
    html += voicePlayerHtml(m.id, 'voice', '');
  }
  if (m.audio_file_id) {
    html += voicePlayerHtml(m.id, 'audio', (m.audio_file_name || ''));
  }
  if (m.document_file_id) {
    const name = (m.document_file_name || 'Документ').replace(/</g,'&lt;');
    html += `<a class="chat-media-doc" ${mSrc(m.id,'document',true)} download="${name}"><img src="/assets/miniapp_icons/down-arrow-100.png" alt="">${name}</a>`;
  }
  return html;
}
const MEDIA_EXT = {photo:'.jpg', video:'.mp4', 'video-note':'.mp4', voice:'.ogg', audio:'.mp3', document:''};
async function downloadChatMedia(msgId, kind) {
  try {
    const url = await fetchMediaBlobUrl(msgId, kind);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'flux-' + kind + '-' + msgId + (MEDIA_EXT[kind] || '');
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 0);
  } catch (e) { toast('Не удалось скачать'); }
}
// Скачивание по иконке — приоритетнее, чем открытие лайтбокса.
document.addEventListener('click', function(e) {
  const btn = e.target.closest && e.target.closest('[data-dl-msg]');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  downloadChatMedia(btn.getAttribute('data-dl-msg'), btn.getAttribute('data-dl-kind'));
});
// Открытие фото/видео в лайтбоксе для детального просмотра.
document.addEventListener('click', function(e) {
  if (e.target.closest && e.target.closest('[data-dl-msg]')) return; // это была кнопка скачать
  const el = e.target.closest && e.target.closest('[data-lb-msg]');
  if (!el) return;
  openLightbox(el.getAttribute('data-lb-msg'), el.getAttribute('data-lb-kind'));
});
async function openLightbox(msgId, kind) {
  const lb = document.getElementById('lb');
  const img = document.getElementById('lb-img');
  const vid = document.getElementById('lb-video');
  try {
    const url = await fetchMediaBlobUrl(msgId, kind);
    if (kind === 'photo') {
      vid.style.display = 'none'; vid.pause();
      img.src = url; img.style.display = 'block';
    } else {
      img.style.display = 'none'; img.removeAttribute('src');
      vid.src = url; vid.style.display = 'block'; vid.play().catch(()=>{});
    }
    lb.classList.add('open');
  } catch (e) { toast('Не удалось открыть'); }
}
function closeLightbox(e) {
  // Закрываем только по фону или крестику, не по самому медиа.
  if (e && e.target && e.target.id !== 'lb' && !(e.target.classList && e.target.classList.contains('lb-close'))) return;
  const lb = document.getElementById('lb');
  const vid = document.getElementById('lb-video');
  const img = document.getElementById('lb-img');
  lb.classList.remove('open');
  if (vid) { vid.pause(); vid.removeAttribute('src'); try { vid.load(); } catch(_e){} }
  if (img) img.removeAttribute('src');
}
let _supportSig = '';
let _supportPoll = null;
async function loadSupport(force) {
  try {
    const r = await api('/api/support/messages');
    const wrap = document.getElementById('chat-messages');
    const msgs = r.messages || [];
    // Перерисовываем только при изменениях — чтобы поллинг не моргал и не сбрасывал прокрутку.
    const sig = JSON.stringify(msgs.map(m => [m.id, m.text, m.sender_role, m.photo_file_id, m.video_file_id, m.voice_file_id, m.video_note_file_id, m.audio_file_id, m.document_file_id]));
    if (!force && sig === _supportSig) return;
    _supportSig = sig;
    if (!msgs.length) { wrap.innerHTML = '<div class="muted" style="text-align:center;padding:20px;">Напишите нам, если есть вопросы</div>'; return; }
    const nearBottom = (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight) < 90;
    wrap.innerHTML = msgs.map(m => {
      const cls = m.sender_role === 'user' ? 'me' : 'op';
      const time = m.created_at ? new Date(m.created_at).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}) : '';
      const txt = (m.text||'').trim();
      const media = chatMediaHtml(m);
      const txtHtml = txt ? `<div class="bubble-text">${txt.replace(/</g,'&lt;')}</div>` : '';
      // Медиа сверху, текст-подпись под ним.
      return `<div class="bubble ${cls}">${media}${txtHtml}<div class="bubbletime">${time}</div></div>`;
    }).join('');
    setupVoicePlayers(wrap);
    hydrateChatMedia(wrap);
    if (force || nearBottom) wrap.scrollTop = wrap.scrollHeight;
  } catch (e) { if (force) toast('Ошибка загрузки чата'); }
}
function startSupportPoll() {
  stopSupportPoll();
  _supportPoll = setInterval(() => { if (!document.hidden) loadSupport(false); }, 3000);
}
function stopSupportPoll() {
  if (_supportPoll) { clearInterval(_supportPoll); _supportPoll = null; }
}

let selectedChatFile = null;
function onChatFileSelected(e) {
  const f = e.target.files && e.target.files[0];
  if (!f) return;
  selectedChatFile = f;
  document.getElementById('chat-attach-name').textContent = f.name;
  document.getElementById('chat-attach-preview').style.display = 'flex';
}
function clearChatAttachment() {
  selectedChatFile = null;
  document.getElementById('chat-file').value = '';
  document.getElementById('chat-attach-preview').style.display = 'none';
}
function fileKindOf(file) {
  const t = (file.type || '').toLowerCase();
  if (t.startsWith('image/')) return 'photo';
  if (t.startsWith('video/')) return 'video';
  if (t.startsWith('audio/')) return 'audio';
  return 'document';
}

let _sendChatInFlight = false;
async function sendChat() {
  if (_sendChatInFlight) return;
  const input = document.getElementById('chat-input');
  const val = input.value.trim();
  const file = selectedChatFile;
  if (!val && !file) return;
  input.value = '';
  clearChatAttachment();
  _sendChatInFlight = true;
  try {
    const fd = new FormData();
    fd.append('text', val);
    if (file) { fd.append('kind', fileKindOf(file)); fd.append('file', file); }
    await apiForm('/api/support/send', fd);
    await loadSupport(true);
  } catch (e) { toast('Ошибка отправки'); }
  finally { _sendChatInFlight = false; }
}

let mediaRecorder = null;
let recordedChunks = [];
let recordingTimer = null;
let recordingSeconds = 0;
async function toggleVoiceRecord() {
  const btn = document.getElementById('mic-btn');
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    const mime = ['audio/ogg;codecs=opus','audio/webm;codecs=opus','audio/webm','audio/mp4'].find(t => window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || '';
    mediaRecorder = mime ? new MediaRecorder(stream, {mimeType: mime}) : new MediaRecorder(stream);
    recordedChunks = [];
    recordingSeconds = 0;
    mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) recordedChunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      btn.classList.remove('recording');
      if (recordingTimer) { clearInterval(recordingTimer); recordingTimer = null; }
      const placeholder = document.getElementById('chat-input');
      placeholder.placeholder = 'Сообщение…';
      if (!recordedChunks.length) return;
      const blob = new Blob(recordedChunks, {type: mediaRecorder.mimeType || 'audio/webm'});
      if (blob.size < 500) return;
      const ext = (mediaRecorder.mimeType || '').includes('ogg') ? 'ogg' : (mediaRecorder.mimeType || '').includes('mp4') ? 'm4a' : 'webm';
      const file = new File([blob], 'voice.' + ext, {type: blob.type});
      _sendChatInFlight = true;
      try {
        const fd = new FormData();
        fd.append('text', '');
        fd.append('kind', 'voice');
        fd.append('file', file);
        await apiForm('/api/support/send', fd);
        await loadSupport(true);
      } catch (e) { toast('Ошибка отправки голосового'); }
      finally { _sendChatInFlight = false; }
    };
    mediaRecorder.start();
    btn.classList.add('recording');
    document.getElementById('chat-input').placeholder = 'Запись… нажмите 🎤 ещё раз';
    recordingTimer = setInterval(() => { recordingSeconds++; }, 1000);
  } catch (e) {
    toast('Нет доступа к микрофону');
  }
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
    const appEl = document.getElementById('app');
    appEl.style.display = 'flex';
    requestAnimationFrame(() => appEl.classList.add('revealed'));
  }
}
boot();
</script>
</body>
</html>
"""
