"""Кэш карточки пользователя для шапки тикета (Remnawave + БД)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.integrations.rw_traffic import is_rw_hwid_devices_unlimited
from shared.models.user import User
from shared.services.subscription_service import get_active_subscription

logger = logging.getLogger(__name__)
_MSK = ZoneInfo("Europe/Moscow")
_CACHE_TTL = 300


def _redis(settings: Settings) -> redis.Redis:
    return redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)


async def build_ticket_user_info(
    session: AsyncSession,
    *,
    db_user: User,
    settings: Settings,
    stale: bool = False,
) -> dict:
    sub = await get_active_subscription(session, db_user.id)
    now = datetime.now(timezone.utc)
    plan_name = "—"
    status = "нет"
    expires = "—"
    devices_line = "—"
    if sub is not None:
        plan_name = sub.plan.name if sub.plan else "—"
        status = "активна" if sub.expires_at > now and sub.status in ("active", "trial") else "истекла"
        exp = sub.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        expires = exp.astimezone(_MSK).strftime("%d.%m.%Y %H:%M") + " МСК"
        occupied = 0
        account_user = db_user
        from shared.services.family_service import resolve_account_user

        account_user = await resolve_account_user(session, db_user)
        if account_user.remnawave_uuid:
            rw = RemnaWaveClient(settings)
            try:
                devs = await rw.get_user_hwid_devices(str(account_user.remnawave_uuid))
                occupied = len(devs)
                uinf = await rw.get_user(str(account_user.remnawave_uuid))
                unlimited = is_rw_hwid_devices_unlimited(uinf)
                denom = "∞" if unlimited else str(sub.devices_count)
                devices_line = f"{occupied}/{denom}"
            except RemnaWaveError:
                devices_line = f"?/{sub.devices_count}"
                stale = True
        else:
            devices_line = f"0/{sub.devices_count}"

    profile_url = ""
    base = (settings.public_site_url or "").strip().rstrip("/")
    if base:
        profile_url = f"{base}/admin/users/{int(db_user.id)}"

    uname = f"@{db_user.username}" if db_user.username else "—"
    return {
        "telegram_id": int(db_user.telegram_id or 0),
        "username": uname,
        "subscription_status": status,
        "subscription_expires": expires,
        "plan_name": plan_name,
        "devices_line": devices_line,
        "profile_url": profile_url,
        "stale": stale,
    }


async def get_ticket_user_info_cached(
    session: AsyncSession,
    *,
    db_user: User,
    settings: Settings,
) -> dict:
    key = f"ticket_user_info:{db_user.id}"
    r = _redis(settings)
    try:
        raw = await r.get(key)
        if raw:
            return json.loads(raw)
    except Exception:
        logger.debug("ticket user info cache read failed", exc_info=True)

    info = await build_ticket_user_info(session, db_user=db_user, settings=settings)
    try:
        await r.setex(key, _CACHE_TTL, json.dumps(info, ensure_ascii=False))
    except Exception:
        logger.debug("ticket user info cache write failed", exc_info=True)
    return info


def format_ticket_user_info_html(info: dict) -> str:
    stale_note = ""
    if info.get("stale"):
        stale_note = "\n<i>⚠️ данные могут быть устаревшими (панель VPN недоступна)</i>"
    profile = ""
    if info.get("profile_url"):
        profile = f'\n<a href="{info["profile_url"]}">Профиль в админке</a>'
    return (
        f"<b>Клиент</b>\n"
        f"Telegram ID: <code>{info.get('telegram_id')}</code>\n"
        f"Username: {info.get('username')}\n"
        f"Подписка: {info.get('subscription_status')} · {info.get('plan_name')}\n"
        f"До: {info.get('subscription_expires')}\n"
        f"Устройства: {info.get('devices_line')}"
        f"{profile}"
        f"{stale_note}"
    )
