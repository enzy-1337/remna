"""Web-admin: аналитика, пользователи и управление промокодами."""

from __future__ import annotations

import asyncio
import base64
import hmac
import html
import json
import logging
import os
import socket
import time
from calendar import monthrange
from pathlib import Path
from hashlib import sha256
from base64 import urlsafe_b64encode
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from secrets import token_urlsafe
from types import SimpleNamespace
from urllib.parse import quote as url_quote
from urllib.parse import quote_plus, urlparse
from uuid import UUID

import httpx
import pyotp
import re
import redis.asyncio as redis_async
import segno
from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import and_, desc, distinct, exists, func, or_, select, text
from sqlalchemy import case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from shared.admin_dotenv import WEB_ADMIN_ENV_SECTIONS, WEB_ADMIN_ENV_WHITELIST, patch_dotenv, read_whitelist_values
from shared.config import Settings, get_settings
from shared.database import get_session_factory
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError, subscription_url_for_telegram
from shared.integrations.rw_user_meta import rw_user_first_connected_at, rw_user_online_at
from shared.integrations.rw_traffic import (
    extract_connected_devices_from_rw_user,
    extract_traffic_gb_from_rw_user,
    is_rw_traffic_unlimited,
    traffic_limit_gb_for_display,
)
from shared.integrations.rw_hwid_devices import format_rw_device_datetime_local, hwid_device_title, normalize_hwid_devices_list
from shared.models.billing_daily_summary import BillingDailySummary
from shared.models.billing_ledger_entry import BillingLedgerEntry
from shared.models.billing_usage_event import BillingUsageEvent
from shared.models.device import Device
from shared.models.plan import Plan
from shared.models.promo import PromoCode, PromoCodeAllowedUser, PromoUsage
from shared.models.remnawave_webhook_event import RemnawaveWebhookEvent
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.broadcast_mailing import BroadcastHistory, BroadcastTemplate, ScheduledBroadcast
from shared.models.user import User
from shared.models.web_admin_browser_session import WebAdminBrowserSession
from shared.services.billing_calculator import (
    estimate_pay_per_use_30d_rub,
    plan_fields_for_ppu_estimate,
    transition_credit_for_remaining_legacy_rub,
)
from shared.services.admin_user_delete import delete_user_from_app
from shared.services.factory_reset_service import wipe_all_application_data
from shared.services.backup_service import run_backup_once
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.referral_service import count_invited_users, list_invited_users
from shared.services.billing_v2.billing_calendar import (
    billing_local_day_end_utc_exclusive,
    billing_local_day_start_utc,
    billing_today,
)
from shared.services.billing_v2.detail_service import (
    get_month_summaries,
    get_today_summary,
    month_bounds,
    summarize_month_total,
    usage_package_breakdown,
)
from shared.broadcast_md2_convert import draft_to_markdown_v2
from shared.md2 import bold, esc as md_esc, plain
from shared.services.broadcast_service import broadcast_html_preview_fragment, save_broadcast_history
from shared.services.telegram_notify import send_telegram_message
from shared.services.billing_v2.traffic_meter_poll_service import baseline_meter_at_hybrid_transition
from shared.services.admin_purchase_refund_service import admin_refund_purchase_transaction, txn_row_refund_eligible
from shared.services.feature_flags import set_tariff_purchases_enabled, tariff_purchases_enabled
from shared.services.admin_notify import notify_admin
from shared.services.web_admin_notify import (
    web_admin_actor_notify_line,
    web_admin_target_user_line,
)
from shared.services.web_admin_session_service import (
    auth_snapshot_from_wauth,
    browser_fingerprint,
    clear_login_hint_cookie,
    create_browser_session,
    find_active_browser_session_for_user_safe,
    get_browser_session,
    list_user_browser_sessions_safe,
    try_get_browser_session,
    login_hint_payload,
    read_login_hint,
    restore_wauth_from_snapshot,
    revoke_all_user_browser_sessions,
    revoke_browser_session,
    revoke_browser_session_by_id,
    set_login_hint_cookie,
    touch_browser_session,
)
from shared.services.remnawave_user_panel_sync import update_rw_user_respecting_hwid_limit
from shared.services.topup_service import apply_balance_credit_followups
from shared.services.subscription_service import (
    BASE_SUBSCRIPTION_PLAN_NAME,
    MIN_DEVICES,
    MAX_DEVICES,
    assign_plan_catalog_price,
    default_one_month_tariff_price_rub,
    get_one_month_reference_plan,
    is_one_month_duration,
    refresh_all_derived_plan_prices,
    resolve_plan_price_rub,
    admin_convert_monthly_subscriptions_to_payg_balance,
    admin_disable_subscription_record,
    admin_adjust_subscription_days,
    admin_adjust_subscription_device_slots,
    admin_enable_subscription_record,
    count_devices,
    get_active_subscription,
    get_admin_manageable_subscription,
    get_base_subscription_plan,
    monthly_extra_devices_rub_preview,
    remove_hwid_device_from_panel,
    remove_device_slot,
    resolve_legacy_transition_base_month_rub,
    set_subscription_auto_renew,
    unlink_hwid_device_keep_slots,
)
from shared.subscription_qr import subscription_url_qr_png
from tickets.config import config as tickets_config

_RESERVED_PLAN_NAMES = frozenset({BASE_SUBSCRIPTION_PLAN_NAME, "Триал"})

_TRANSACTION_TYPE_HINTS_RU: dict[str, str] = {
    "topup": "Пополнение баланса через платёжную систему (Platega, CryptoBot и т.д.)",
    "subscription": "Покупка тарифа или продление с баланса",
    "subscription_autorenew": "Автопродление PAYG-подписки (списание 0 ₽ в метаданных продления)",
    "manual_add": "Покупка дополнительного слота устройства с баланса",
    "admin_balance_add": "Ручное начисление суммы администратором из web-admin или бота",
    "admin_balance_reset": "Обнуление баланса администратором в web-admin",
    "billing_transition": "Техническая запись перехода на гибридный биллинг (legacy → hybrid)",
    "usage_charge": "Списание PAYG: трафик по ГБ или суточная плата за устройство (метаданные уточняют источник)",
    "promo_topup_bonus": "Бонусные рубли по промокоду при пополнении",
    "first_topup_balance_bonus": "Бонус при первом пополнении (% от суммы или акция)",
    "referral_signup": "Приветственное начисление по реферальной ссылке при регистрации",
    "referral_signup_invited": "Приветственное начисление приглашённому по реферальной ссылке",
    "referral_payment_percent": "Процент на баланс пригласившему от платежа приглашённого",
    "purchase_refund": "Возврат на баланс при отмене покупки тарифа или слота устройства администратором",
    "subscription_repeat_bonus": "Бонус на баланс при повторной покупке тарифа с баланса (2-я и далее)",
}


def _txn_history_action_cell(user_id: int, t: Transaction) -> str:
    if t.status == "refunded":
        return '<span class="text-xs opacity-60">отменено</span>'
    if txn_row_refund_eligible(t):
        msg = _esc_attr("Вернуть сумму на баланс и отменить эффект покупки (тариф или слот)?")
        return (
            f'<form method="post" action="/admin/users/{user_id}/transactions/{int(t.id)}/refund" class="inline" '
            f'data-remna-confirm-msg="{msg}">'
            '<button type="submit" class="btn btn-ghost btn-xs text-warning">Возврат</button></form>'
        )
    return '<span class="text-xs opacity-40">—</span>'


def _txn_type_hint_html(txn_type: str) -> str:
    tt = (txn_type or "").strip()
    hint = _TRANSACTION_TYPE_HINTS_RU.get(tt)
    inner = _esc(tt)
    if hint:
        return (
            f'<span class="cursor-help border-b border-dotted border-base-content/35" '
            f'title="{_esc_attr(hint)}">{inner}</span>'
        )
    return inner


logger = logging.getLogger(__name__)

router = APIRouter(tags=["web-admin"])

_MSK_TZ = ZoneInfo("Europe/Moscow")
_WEB_ADMIN_SESSION_TTL = timedelta(hours=24)
_LOGIN_NOTIFY_LAST: dict[int, float] = {}


def _add_calendar_months(dt: datetime, months: int) -> datetime:
    shifted = (dt.year * 12 + (dt.month - 1)) + months
    year = shifted // 12
    month = shifted % 12 + 1
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _fmt_dt_msk(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_MSK_TZ).strftime("%d.%m.%Y %H:%M") + " МСК"


def _fmt_relative_ru(dt: datetime | None, *, now: datetime | None = None) -> str:
    """Человекочитаемое «N часов назад» для ленты пополнений (UTC)."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if now is None:
        now = datetime.now(UTC)
    else:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    sec = int((now - dt).total_seconds())
    if sec < 0:
        return _fmt_dt_msk(dt)
    if sec < 60:
        return "только что"
    if sec < 3600:
        m = sec // 60
        return f"{m} мин назад"
    if sec < 86400:
        h = sec // 3600
        return f"{h} ч назад"
    if sec < 86400 * 14:
        d = sec // 86400
        return f"{d} дн назад"
    return _fmt_dt_msk(dt)


_AVATAR_CACHE: dict[int, tuple[float, bytes, str]] = {}
_AVATAR_TTL_SEC = 3600.0
_avatar_fetch_locks: dict[int, asyncio.Lock] = {}
_DASHBOARD_HTML_CACHE: tuple[float, str] | None = None
_DASHBOARD_HTML_TTL_SEC = 20.0
_USERS_HTML_CACHE: dict[tuple[str, int, str, str, str], tuple[float, str]] = {}
_USERS_HTML_TTL_SEC = 15.0
_INT32_MAX = 2_147_483_647
_INT64_MAX = 9_223_372_036_854_775_807
_LOGIN_BG_CANDIDATES = (
    Path("assets/login-bg.png"),
    Path(
        "C:/Users/admin/.cursor/projects/c-remna-remna/assets/"
        "c__Users_admin_AppData_Roaming_Cursor_User_workspaceStorage_"
        "60473e56cf1239dc65e9dcb102354c5b_images_image-4e2cf300-ba40-4fcb-9fe8-e034f29c8894.png"
    ),
)


def _avatar_fetch_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _avatar_fetch_locks:
        _avatar_fetch_locks[user_id] = asyncio.Lock()
    return _avatar_fetch_locks[user_id]


async def _load_telegram_profile_photo(user: User) -> tuple[bytes, str] | None:
    """Фото профиля Telegram по user_id через Bot API (без отдачи токена в браузер)."""
    token = (get_settings().bot_token or "").strip()
    if not token:
        return None
    try:
        tg = int(user.telegram_id)
    except (TypeError, ValueError):
        return None
    async with httpx.AsyncClient(timeout=22.0) as client:
        r = await client.get(
            f"https://api.telegram.org/bot{token}/getUserProfilePhotos",
            params={"user_id": tg, "limit": 1},
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("ok"):
            return None
        photos = data.get("result", {}).get("photos") or []
        if not photos or not photos[0]:
            return None
        sizes = photos[0]
        file_id = sizes[-1]["file_id"]
        r2 = await client.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
        )
        if r2.status_code != 200:
            return None
        d2 = r2.json()
        if not d2.get("ok"):
            return None
        path = d2.get("result", {}).get("file_path")
        if not path:
            return None
        r3 = await client.get(f"https://api.telegram.org/file/bot{token}/{path}")
        if r3.status_code != 200:
            return None
        raw_ct = (r3.headers.get("content-type") or "").split(";")[0].strip() or "image/jpeg"
        if not raw_ct.startswith("image/"):
            raw_ct = "image/jpeg"
        return (r3.content, raw_ct)


async def _fetch_telegram_public_userpic(username: str) -> tuple[bytes, str] | None:
    """Публичная картинка t.me/i/userpic (по @username), если не 1×1-пустышка."""
    un = (username or "").strip().lstrip("@")
    if not un or not re.match(r"^[A-Za-z0-9_]{3,64}$", un):
        return None
    url = f"https://t.me/i/userpic/320/{un}.jpg"
    async with httpx.AsyncClient(timeout=18.0, follow_redirects=True) as client:
        r = await client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; RemnaBot/1.0; +https://telegram.org)",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        if r.status_code != 200:
            return None
        body = r.content
        if len(body) < 400:
            return None
        ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if "text" in ct or "html" in ct:
            return None
        if not ct.startswith("image/"):
            if body[:2] not in (b"\xff\xd8", b"\x89P", b"GIF", b"RIFF"):
                return None
            ct = "image/jpeg"
        return (body, ct)


def _humanize_left_ru(exp: datetime, now: datetime) -> str:
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    left = exp - now
    if left.total_seconds() <= 0:
        return "истекла"
    d = left.days
    h = left.seconds // 3600
    if d >= 1:
        n = abs(int(d))
        if n % 10 == 1 and n % 100 != 11:
            return f"{n} день"
        if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
            return f"{n} дня"
        return f"{n} дней"
    if h >= 1:
        return f"{h} ч."
    m = left.seconds // 60
    if m >= 1:
        return f"{m} мин."
    return "меньше минуты"


def _esc(v: object) -> str:
    return html.escape(str(v))


def _esc_attr(v: object) -> str:
    return html.escape(str(v), quote=True)


_WAUTH_POST_LOGIN_PATH_KEY = "wauth_post_login_path"


def _safe_admin_next_path(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if not s.startswith("/") or s.startswith("//"):
        return None
    if not s.startswith("/admin"):
        return None
    if s.startswith("/admin/login"):
        return None
    s = s.split("#", 1)[0]
    return s[:2048] if len(s) > 2048 else s


def _remember_admin_next_from_query(request: Request) -> None:
    raw = (request.query_params.get("next") or "").strip()
    if not raw:
        return
    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            p = urlparse(raw)
            raw = (p.path or "") + (("?" + p.query) if p.query else "")
        except Exception:
            return
    safe = _safe_admin_next_path(raw)
    if safe:
        request.session[_WAUTH_POST_LOGIN_PATH_KEY] = safe


def _login_success_destination(request: Request, *, explicit: str | None = None) -> str:
    if explicit:
        request.session.pop(_WAUTH_POST_LOGIN_PATH_KEY, None)
        return explicit
    raw = request.session.pop(_WAUTH_POST_LOGIN_PATH_KEY, None)
    if isinstance(raw, str):
        safe = _safe_admin_next_path(raw)
        if safe:
            return safe
    return "/admin/dashboard"


def _admin_login_url_with_next(request: Request) -> str:
    path = request.url.path or ""
    q = request.url.query or ""
    cand = path + (("?" + q) if q else "")
    safe = _safe_admin_next_path(cand)
    if not safe:
        return "/admin/login"
    return "/admin/login?next=" + url_quote(safe, safe="")


def _pagination_bar(
    *,
    page: int,
    total_pages: int,
    base_path: str,
    query_extra: dict[str, str],
    htmx_target: str | None = None,
) -> str:
    """Центр: «1 | ‹ | текущая | › | N»."""

    def _url(p: int) -> str:
        seg = [f"page={p}"]
        for k, v in query_extra.items():
            vs = (v or "").strip()
            if vs:
                seg.append(f"{quote_plus(k)}={quote_plus(vs)}")
        return base_path + "?" + "&".join(seg)

    def _btn(href: str, label: str, *, disabled: bool = False, primary: bool = False) -> str:
        if disabled:
            return (
                f"<span class='btn btn-ghost btn-sm h-9 min-h-9 opacity-40 pointer-events-none' "
                f"aria-disabled='true'>{_esc(label)}</span>"
            )
        tw = "btn btn-primary btn-sm h-9 min-h-9" if primary else "btn btn-ghost btn-sm h-9 min-h-9"
        hx = ""
        if htmx_target and not disabled:
            hx = (
                f" hx-get=\"{_esc_attr(href)}\" hx-target=\"{_esc_attr(htmx_target)}\""
                ' hx-swap="innerHTML" hx-push-url="true"'
            )
        return f"<a class='{tw}' href='{_esc(href)}'{hx}>{_esc(label)}</a>"

    if total_pages <= 1:
        return "<div class='flex justify-center py-3'><span class='text-sm opacity-60'>Страница 1 из 1</span></div>"
    prev_p = max(1, page - 1)
    next_p = min(total_pages, page + 1)
    return f"""
    <div class="flex flex-col items-center gap-2 py-4">
      <span class="text-sm opacity-60">Страница {_esc(page)} из {_esc(total_pages)}</span>
      <div class="flex flex-wrap justify-center items-center gap-1">
        {_btn(_url(1), "1", disabled=page == 1)}
        {_btn(_url(prev_p), "‹", disabled=page == 1)}
        <span class="btn btn-primary btn-sm h-9 min-h-9 min-w-[2.5rem] pointer-events-none">{_esc(page)}</span>
        {_btn(_url(next_p), "›", disabled=page == total_pages)}
        {_btn(_url(total_pages), str(total_pages), disabled=page == total_pages)}
      </div>
    </div>
    """


def _auth_data(request: Request) -> dict:
    auth = request.session.get("wauth")
    if isinstance(auth, dict):
        return auth
    return {}


def _auth_label(request: Request) -> str | None:
    auth = _auth_data(request)
    return str(auth.get("label") or auth.get("username") or "") or None


def _auth_avatar(request: Request) -> str:
    uid_raw = request.session.get("wauth_user_id")
    try:
        uid = int(uid_raw) if uid_raw is not None else 0
    except (TypeError, ValueError):
        uid = 0
    if uid > 0:
        return f"/admin/users/{uid}/telegram-photo"
    auth = _auth_data(request)
    avatar_url = str(auth.get("avatar_url") or "").strip()
    if avatar_url:
        return avatar_url
    return "/assets/icon.png"


def _simple_avatar_markup(*, url: str, label: str, px: int = 36) -> str:
    raw = (label or "?").strip()
    ch = raw[0].upper() if raw else "?"
    if not ch.isalnum():
        ch = "?"
    return (
        f"<span class='relative flex h-[{px}px] w-[{px}px] items-center justify-center overflow-hidden rounded-full'>"
        f"<img src=\"{_esc(url)}\" alt=\"\" width=\"{px}\" height=\"{px}\" class=\"h-full w-full rounded-full object-cover remna-avatar-img\" "
        "loading=\"lazy\" decoding=\"async\" data-remna-avatar=\"1\" "
        "onerror=\"this.classList.add('hidden');this.nextElementSibling.classList.remove('hidden')\" />"
        f"<span class='hidden absolute inset-0 items-center justify-center rounded-full bg-primary/20 text-xs font-bold text-primary'>{_esc(ch)}</span>"
        "</span>"
    )


def _admin_assets_dir() -> Path:
    return Path("assets").resolve()


def _is_image_asset(name: str) -> bool:
    return name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def _resolve_admin_asset_url(name: str | None) -> str | None:
    raw = (name or "").strip()
    if not raw:
        return None
    candidate = Path(raw).name
    if not _is_image_asset(candidate):
        return None
    p = _admin_assets_dir() / candidate
    if not p.exists() or not p.is_file():
        return None
    return f"/assets/{url_quote(candidate)}"


_ENV_CHOICE_OPTIONS: dict[str, list[tuple[str, str]]] = {
    "PLATEGA_API_BASE_URL": [
        ("https://app.platega.io", "app.platega.io"),
        ("https://api.platega.io", "api.platega.io"),
    ],
    "BILLING_CALENDAR_TIMEZONE": [
        ("Europe/Moscow", "Europe/Moscow"),
        ("UTC", "UTC"),
    ],
}

_ENV_MULTI_CHOICE_OPTIONS: dict[str, list[tuple[str, str]]] = {
    "PLATEGA_PAYMENT_METHODS": [
        ("2", "СБП QR"),
        ("10", "Карты RUB"),
        ("11", "Эквайринг"),
        ("12", "International"),
        ("13", "Крипта"),
    ],
}

_ENV_BOOL_KEYS = {
    "REMNAWAVE_SYNC_ENABLED",
    "REMNAWAVE_SYNC_PUSH_DESCRIPTION",
    "REMNAWAVE_STUB",
    "TRIAL_ENABLED",
    "SUBSCRIPTION_AUTORENEW_ENABLED",
    "SUBSCRIPTION_EXPIRY_NOTIFY_ENABLED",
    "BILLING_FIRST_TOPUP_WELCOME_ENABLED",
    "BILLING_V2_ENABLED",
    "BILLING_TRAFFIC_RW_METER_ENABLED",
    "BILLING_NEGATIVE_NOTIFY_ENABLED",
    "ADMIN_REPORT_ENABLED",
    "TELEGRAM_WEBHOOK_ENABLED",
    "CRYPTOBOT_STUB",
    "PLATEGA_STUB",
    "PLATEGA_SKIP_WEBHOOK_AUTH",
    "PLATEGA_PAYER_CHOOSES_METHOD",
}


def _env_bool_state(raw: str) -> bool | None:
    val = (raw or "").strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    return None


def _admin_background_image_url(settings: Settings) -> str | None:
    src = (settings.admin_background_source or "default").strip().lower()
    if src == "url":
        u = (settings.admin_background_url or "").strip()
        if u.startswith(("http://", "https://")):
            return u
        return None
    if src == "asset":
        return _resolve_admin_asset_url(settings.admin_background_asset)
    return None


def _list_admin_image_assets() -> list[str]:
    d = _admin_assets_dir()
    if not d.exists() or not d.is_dir():
        return []
    out: list[str] = []
    for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and _is_image_asset(p.name):
            out.append(p.name)
    return out


def _head_common(title: str, *, favicon_url: str | None = None, background_url: str | None = None) -> str:
    fav = ""
    u = (favicon_url or "").strip()
    if u.startswith(("http://", "https://")):
        fav = f'  <link rel="icon" href="{_esc(u)}" />\n'
    bg_url = (background_url or "").strip()
    bg_with_img = (
        f"""
    body {{
      background-image:
        linear-gradient(145deg, rgba(18, 16, 36, .72), rgba(52, 28, 94, .62)),
        url('{_esc(bg_url)}');
      background-size: cover;
      background-position: center;
      background-repeat: no-repeat;
      background-attachment: fixed;
    }}
    html[data-theme='light'] body {{
      background-image:
        linear-gradient(145deg, rgba(240, 240, 248, .78), rgba(214, 206, 236, .58)),
        url('{_esc(bg_url)}');
    }}
    html[data-theme='night'] body {{
      background-image:
        linear-gradient(145deg, rgba(15, 12, 32, .78), rgba(41, 20, 78, .68)),
        url('{_esc(bg_url)}');
    }}
"""
        if bg_url
        else """
    body {
      background:
        linear-gradient(145deg, rgba(24, 18, 46, .96), rgba(58, 30, 102, .92));
      background-attachment: fixed;
    }
    html[data-theme='light'] body {
      background:
        linear-gradient(145deg, rgba(233, 227, 247, .94), rgba(210, 198, 236, .92));
    }
"""
    )
    return f"""  <meta charset="utf-8" />
  <script>try{{var t=localStorage.getItem('remna-admin-theme');if(t==='light'||t==='night')document.documentElement.setAttribute('data-theme',t);}}catch(e){{}}</script>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex, nofollow" />
  <meta name="googlebot" content="noindex, nofollow" />
  <meta name="description" content="Панель управления Remna VPN — администраторский интерфейс." />
  <meta name="theme-color" content="#0f0c20" />
  <meta property="og:title" content="{_esc(title)}" />
  <meta property="og:type" content="website" />
  <title>{_esc(title)}</title>
{fav}  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" crossorigin="anonymous" referrerpolicy="no-referrer" />
  <link href="https://cdn.jsdelivr.net/npm/daisyui@4.12.14/dist/full.min.css" rel="stylesheet" type="text/css" />
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {{
      theme: {{ extend: {{ fontFamily: {{ sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"] }} }} }}
    }};
  </script>
  <style>
    body {{
      font-family: Inter, ui-sans-serif, system-ui, sans-serif;
    }}
{bg_with_img}
    :root {{
      --remna-anim-fast: 180ms;
      --remna-anim-ease: cubic-bezier(.22, .61, .36, 1);
      --remna-anim-shadow: 0 12px 28px -16px color-mix(in oklab, var(--bc) 40%, transparent);
    }}
    .remna-page .btn,
    .remna-page a.btn,
    .remna-page button.btn {{
      transition:
        transform var(--remna-anim-fast) var(--remna-anim-ease),
        box-shadow var(--remna-anim-fast) var(--remna-anim-ease),
        filter var(--remna-anim-fast) var(--remna-anim-ease),
        border-color var(--remna-anim-fast) var(--remna-anim-ease),
        background-color var(--remna-anim-fast) var(--remna-anim-ease);
      will-change: transform;
    }}
    .remna-page .btn:hover,
    .remna-page a.btn:hover,
    .remna-page button.btn:hover {{
      transform: translateY(-1px);
      box-shadow: var(--remna-anim-shadow);
      filter: saturate(1.03);
    }}
    .remna-page .btn:active,
    .remna-page a.btn:active,
    .remna-page button.btn:active {{
      transform: translateY(0) scale(.985);
      box-shadow: none;
    }}
    .remna-page .btn:focus-visible,
    .remna-page a.btn:focus-visible,
    .remna-page button.btn:focus-visible {{
      outline: 0;
      box-shadow: 0 0 0 2px color-mix(in oklab, var(--p) 55%, transparent), var(--remna-anim-shadow);
    }}
    .remna-page .btn i {{
      transition: transform var(--remna-anim-fast) var(--remna-anim-ease), filter var(--remna-anim-fast) var(--remna-anim-ease);
    }}
    .remna-page .btn:hover i {{
      transform: translateY(-.5px) scale(1.04);
      filter: drop-shadow(0 0 5px color-mix(in oklab, var(--p) 50%, transparent));
    }}
    .remna-page .btn.btn-ghost:hover,
    .remna-page a.btn.btn-ghost:hover,
    .remna-page button.btn.btn-ghost:hover {{
      transform: translateY(-.5px);
      box-shadow: 0 8px 20px -16px color-mix(in oklab, var(--bc) 30%, transparent);
      filter: saturate(1.01);
    }}
    .remna-page .btn.btn-ghost:hover i {{
      transform: translateY(-.25px) scale(1.02);
      filter: drop-shadow(0 0 3px color-mix(in oklab, var(--bc) 30%, transparent));
    }}
    .remna-page .btn.btn-primary:hover,
    .remna-page a.btn.btn-primary:hover,
    .remna-page button.btn.btn-primary:hover {{
      transform: translateY(-1.5px);
      box-shadow:
        0 16px 30px -16px color-mix(in oklab, var(--bc) 42%, transparent),
        0 0 14px color-mix(in oklab, var(--p) 35%, transparent);
      filter: saturate(1.06);
    }}
    .remna-page .btn.btn-primary:hover i {{
      transform: translateY(-.5px) scale(1.06);
      filter: drop-shadow(0 0 7px color-mix(in oklab, var(--p) 60%, transparent));
    }}
    .remna-page .remna-interactive {{
      transition:
        transform var(--remna-anim-fast) var(--remna-anim-ease),
        box-shadow var(--remna-anim-fast) var(--remna-anim-ease),
        color var(--remna-anim-fast) var(--remna-anim-ease),
        background-color var(--remna-anim-fast) var(--remna-anim-ease);
    }}
    .remna-page .remna-interactive:hover {{
      transform: translateY(-1px);
      box-shadow: var(--remna-anim-shadow);
    }}
    .remna-page .btn:not(.btn-square):not(.btn-circle) {{
      position: relative;
      isolation: isolate;
      font-weight: 600;
      letter-spacing: 0.01em;
      text-rendering: geometricPrecision;
      -webkit-font-smoothing: antialiased;
      box-shadow: 0 10px 24px -20px color-mix(in oklab, var(--bc) 34%, transparent);
      transition:
        transform var(--remna-anim-fast) var(--remna-anim-ease),
        box-shadow var(--remna-anim-fast) var(--remna-anim-ease),
        border-color var(--remna-anim-fast) var(--remna-anim-ease),
        background-color var(--remna-anim-fast) var(--remna-anim-ease),
        color var(--remna-anim-fast) var(--remna-anim-ease);
    }}
    .remna-page .btn:not(.btn-square):not(.btn-circle):hover {{
      transform: translateY(-1px);
      box-shadow: 0 14px 30px -22px color-mix(in oklab, var(--bc) 42%, transparent);
    }}
    .remna-page .btn:not(.btn-square):not(.btn-circle):active {{
      transform: translateY(0);
    }}
    .remna-page {{
      position: relative;
    }}
    .remna-page::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        radial-gradient(circle at 20% 18%, rgba(168,85,247,.11), transparent 28%),
        radial-gradient(circle at 82% 24%, rgba(59,130,246,.08), transparent 24%),
        radial-gradient(circle at 50% 82%, rgba(236,72,153,.08), transparent 24%);
      z-index: 0;
    }}
    .remna-loading-overlay {{
      position: fixed;
      inset: 0;
      z-index: 200;
      display: none;
      align-items: center;
      justify-content: center;
      background: color-mix(in oklab, var(--b1) 84%, var(--bc) 16%);
      backdrop-filter: blur(4px);
    }}
    .remna-loading-overlay.is-active {{ display: flex; }}
    .remna-loading-box {{
      display: inline-flex;
      align-items: center;
      gap: .52rem;
      border: 1px solid color-mix(in oklab, var(--bc) 20%, transparent);
      background: color-mix(in oklab, var(--b1) 92%, transparent);
      border-radius: .9rem;
      padding: .72rem 1rem;
      box-shadow: 0 12px 36px rgba(0,0,0,.2);
    }}
    .remna-loading-main {{
      display: inline-flex;
      align-items: center;
      gap: .36rem;
    }}
    .remna-loading-side {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1.8rem;
      height: 1.8rem;
      border-radius: .6rem;
      border: 1px solid color-mix(in oklab, var(--bc) 12%, transparent);
      background: color-mix(in oklab, var(--b3) 74%, transparent);
      color: color-mix(in oklab, var(--p) 72%, var(--bc) 28%);
    }}
    .remna-hourglass-icon {{
      display: inline-block;
      transform-origin: 50% 50%;
      animation: remna-hourglass-rotate 2.2s infinite;
    }}
    .remna-spinner {{
      width: 1rem;
      height: 1rem;
      border: 2px solid color-mix(in oklab, var(--bc) 20%, transparent);
      border-top-color: var(--p);
      border-radius: 9999px;
      animation: remna-spin .8s linear infinite;
    }}
    @keyframes remna-hourglass-rotate {{
      /* Пол-оборота: плавный старт, ускорение, замедление */
      0%   {{ transform: rotate(0deg); animation-timing-function: cubic-bezier(.40, 0, .95, .32); }}
      18%  {{ transform: rotate(16deg); }}
      50%  {{ transform: rotate(180deg); animation-timing-function: cubic-bezier(.08, .64, .26, 1); }}
      68%  {{ transform: rotate(196deg); }}
      100% {{ transform: rotate(360deg); }}
    }}
    @keyframes remna-spin {{ to {{ transform: rotate(360deg); }} }}
    .remna-avatar-img {{
      opacity: 0;
      transition: opacity .18s ease-out;
      background: linear-gradient(90deg, color-mix(in oklab, var(--b3) 70%, transparent) 0%, color-mix(in oklab, var(--b2) 80%, transparent) 50%, color-mix(in oklab, var(--b3) 70%, transparent) 100%);
      background-size: 240% 100%;
      animation: remna-avatar-shimmer 1.2s linear infinite;
    }}
    .remna-avatar-img.is-loaded {{
      opacity: 1;
      background: transparent;
      animation: none;
    }}
    @keyframes remna-avatar-shimmer {{
      0% {{ background-position: 100% 0; }}
      100% {{ background-position: -100% 0; }}
    }}
    .remna-admin-avatar-ring .rounded-full {{
      aspect-ratio: 1 / 1;
    }}
    .remna-admin-avatar-ring.ring-emerald-500 {{
      box-shadow: 0 0 0 1px rgba(16, 185, 129, 0.45), 0 0 12px rgba(16, 185, 129, 0.35);
    }}
    .remna-admin-avatar-ring.ring-red-500 {{
      box-shadow: 0 0 0 1px rgba(239, 68, 68, 0.45), 0 0 12px rgba(239, 68, 68, 0.4);
    }}
    @keyframes remna-fade-in {{
      from {{ opacity: 0; transform: translateY(8px); }}
      to {{ opacity: 1; transform: none; }}
    }}
    .remna-page .card {{
      animation: remna-fade-in 0.42s ease-out both;
      transition: box-shadow 0.2s ease, transform 0.2s ease, border-color 0.2s ease;
      backdrop-filter: blur(1.5px);
    }}
    .remna-page .card:hover {{
      box-shadow: 0 18px 40px -18px color-mix(in oklab, var(--bc) 25%, transparent);
    }}
    tr.remna-row-link {{
      cursor: pointer;
      transition: background-color 0.15s ease;
    }}
    tr.remna-row-link:hover {{
      background-color: color-mix(in oklab, var(--p) 10%, transparent);
    }}
    #remna-toast-host {{
      position: fixed;
      top: 1rem;
      left: 50%;
      transform: translateX(-50%);
      z-index: 200;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.5rem;
      width: min(28rem, calc(100vw - 2rem));
      pointer-events: none;
    }}
    .remna-toast {{
      pointer-events: auto;
      position: relative;
      overflow: hidden;
      width: 100%;
      border-radius: 0.85rem;
      box-shadow: 0 14px 44px -12px color-mix(in oklab, var(--bc) 35%, transparent);
      display: flex;
      align-items: center;
      gap: 0.75rem;
      padding: 0.85rem 1rem 0.95rem;
      backdrop-filter: blur(7px) saturate(1.08);
      -webkit-backdrop-filter: blur(7px) saturate(1.08);
      animation: remna-toast-in 0.4s cubic-bezier(0.22, 1, 0.36, 1) both;
    }}
    .remna-toast.remna-toast--out {{ animation: remna-toast-out 0.32s ease-in forwards; }}
    .remna-toast--success {{
      background: color-mix(in oklab, #22c55e 33%, transparent);
      border: 1px solid color-mix(in oklab, #22c55e 42%, transparent);
    }}
    .remna-toast--error {{
      background: color-mix(in oklab, #ef4444 33%, transparent);
      border: 1px solid color-mix(in oklab, #ef4444 38%, transparent);
    }}
    .remna-toast--warning {{
      background: color-mix(in oklab, #eab308 33%, transparent);
      border: 1px solid color-mix(in oklab, #ca8a04 35%, transparent);
    }}
    .remna-toast--info {{
      background: color-mix(in oklab, var(--p) 33%, transparent);
      border: 1px solid color-mix(in oklab, var(--p) 35%, transparent);
    }}
    .remna-toast__icon {{
      flex-shrink: 0;
      width: 2.35rem;
      height: 2.35rem;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 9999px;
      font-size: 1.05rem;
      background: color-mix(in oklab, var(--bc) 8%, transparent);
    }}
    .remna-toast__text {{
      flex: 1;
      text-align: center;
      font-size: 0.9rem;
      font-weight: 500;
      line-height: 1.4;
    }}
    .remna-toast__bar {{
      position: absolute;
      bottom: 0;
      left: 0;
      height: 3px;
      border-radius: 0 2px 2px 0;
      animation: remna-toast-progress linear forwards;
    }}
    @keyframes remna-toast-in {{
      from {{ opacity: 0; transform: translateY(-120%); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @keyframes remna-toast-out {{
      to {{ opacity: 0; transform: translateY(-130%); }}
    }}
    @keyframes remna-toast-progress {{
      from {{ width: 100%; }}
      to {{ width: 0%; }}
    }}
  </style>"""


def _brand_logo_mark(settings: Settings, *, compact: bool = False) -> str:
    url = (settings.admin_panel_logo_url or "").strip()
    box = "max-h-8 max-w-8 h-8 w-8" if compact else "max-h-9 max-w-9 h-9 w-9"
    w = "32" if compact else "36"
    icls = "fa-solid fa-shield-halved text-sm" if compact else "fa-solid fa-shield-halved text-base"
    if url.startswith(("http://", "https://")):
        return f'<img src="{_esc(url)}" alt="" class="{box} object-contain" width="{w}" height="{w}" loading="lazy" />'
    return f'<i class="{icls}" aria-hidden="true"></i>'


def _nav_link_class(href: str, cur: str) -> str:
    base = (
        "box-border flex h-9 min-h-9 min-w-0 shrink-0 items-center justify-start gap-0 rounded-xl px-0 "
        "text-sm font-medium no-underline ring-1 ring-inset ring-transparent transition-colors duration-200 remna-interactive"
    )
    h = href.rstrip("/")
    c = cur.rstrip("/") or "/"
    active = False
    if h == "/admin/dashboard":
        active = c in ("/admin/dashboard", "/admin")
    elif c == h or c.startswith(h + "/"):
        active = True
    if active:
        return f"{base} bg-primary/20 text-primary shadow-sm ring-primary/25"
    return f"{base} text-base-content/75 hover:bg-base-200 hover:text-base-content"


def _sidebar_nav_item(href: str, icon_class: str, label: str, cur: str) -> str:
    cls = _nav_link_class(href, cur)
    return f"""<div class="flex w-full justify-start overflow-hidden">
    <a href="{href}" class="{cls} w-9 max-w-9 min-w-9 group-hover/sidebar:w-full group-hover/sidebar:max-w-none group-hover/sidebar:min-w-0 overflow-hidden">
      <span class="flex h-9 w-9 shrink-0 items-center justify-center"><i class="{icon_class} text-[15px] leading-none opacity-90" aria-hidden="true"></i></span>
      <span class="nav-label pointer-events-none min-w-0 max-w-0 shrink grow-0 basis-0 overflow-hidden whitespace-nowrap opacity-0 group-hover/sidebar:pointer-events-auto group-hover/sidebar:max-w-[14rem] group-hover/sidebar:shrink group-hover/sidebar:basis-auto group-hover/sidebar:opacity-100">{_esc(label)}</span>
    </a></div>"""


def _sidebar_footer_profile(href: str, label: str, avatar_markup: str, cur: str) -> str:
    pcls = _nav_link_class(href, cur)
    return f"""<div class="flex w-full justify-start overflow-hidden">
    <div class="{pcls} relative w-9 max-w-9 min-w-9 group-hover/sidebar:w-full group-hover/sidebar:max-w-none group-hover/sidebar:min-w-0 overflow-hidden">
      <a href="{href}" class="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-full" title="{_esc(label)}">
        {avatar_markup}
      </a>
      <a href="{href}" class="nav-label pointer-events-none absolute left-1/2 top-1/2 min-w-0 max-w-0 -translate-x-1/2 -translate-y-1/2 truncate text-sm font-semibold text-base-content no-underline opacity-0 group-hover/sidebar:pointer-events-auto group-hover/sidebar:max-w-[11rem] group-hover/sidebar:opacity-100 hover:text-primary" title="{_esc(label)}">{_esc(label)}</a>
      <form method="post" action="/admin/logout" class="nav-label pointer-events-none ml-auto flex max-w-0 shrink-0 overflow-hidden opacity-0 group-hover/sidebar:pointer-events-auto group-hover/sidebar:max-w-none group-hover/sidebar:opacity-100">
        <button type="submit" class="btn btn-ghost btn-square btn-sm h-9 w-9 min-h-9 min-w-9 p-0 text-error hover:bg-error/10" title="Выйти" aria-label="Выйти">
          <i class="fa-solid fa-right-from-bracket" aria-hidden="true"></i>
        </button>
      </form>
    </div></div>"""


def _mob_nav_cls(href: str, cur: str) -> str:
    h = href.rstrip("/")
    c = cur.rstrip("/") or "/"
    if h == "/admin/dashboard":
        act = c in ("/admin/dashboard", "/admin")
    else:
        act = c == h or c.startswith(h + "/")
    return "text-primary font-semibold" if act else "text-base-content/55"


def _mob_drawer_link(href: str, icon_class: str, label: str, cur: str) -> str:
    tone = _mob_nav_cls(href, cur)
    return (
        f'<a href="{href}" class="btn btn-ghost btn-sm h-10 min-h-10 w-full justify-start gap-3 border-0 font-medium normal-case {tone}">'
        f'<i class="{icon_class} w-5 shrink-0 text-center text-base" aria-hidden="true"></i>'
        f"<span>{_esc(label)}</span></a>"
    )


def _layout(
    title: str,
    body: str,
    *,
    request: Request | None = None,
    show_nav: bool = True,
    back_href: str | None = None,
    sidebar_avatar_url: str | None = None,
    sidebar_user_label: str | None = None,
) -> HTMLResponse:
    cur = ""
    if request is not None:
        cur = request.url.path.rstrip("/") or "/"

    settings = get_settings()
    brand_title = (settings.admin_panel_title or "Remna").strip() or "Remna"
    fav = (settings.admin_panel_logo_url or "").strip()
    favicon_for_head = fav if fav.startswith(("http://", "https://")) else None

    nav_blocks = ""
    theme_toggle = ""
    remna_chrome = ""
    main_cls = "min-h-screen px-3 py-5 pt-16 pb-24 sm:px-5 md:pt-[4.75rem] md:pb-8 md:pl-[calc(0.5rem+3.75rem+0.75rem)] md:pr-6 lg:pr-8"

    if show_nav and request is not None:
        from shared.services.web_admin_rbac import ADMIN_NAV_ITEMS, has_nav_access

        user_label = (sidebar_user_label or "").strip() or _auth_label(request) or "admin"
        avatar = (sidebar_avatar_url or "").strip() or _auth_avatar(request)
        sidebar_avatar_inner = _simple_avatar_markup(url=avatar, label=user_label, px=36)
        logo_inner = _brand_logo_mark(settings)
        perms_raw = request.session.get("wauth_permissions") or []
        permissions = {str(x) for x in perms_raw}
        is_superadmin = bool(request.session.get("wauth_is_superadmin"))
        nav_desktop = "".join(
            _sidebar_nav_item(href, icon, label, cur)
            for href, icon, label, perm in ADMIN_NAV_ITEMS
            if has_nav_access(perm, permissions=permissions, is_superadmin=is_superadmin)
        )
        nav_mobile = "".join(
            _mob_drawer_link(href, icon, label, cur)
            for href, icon, label, perm in ADMIN_NAV_ITEMS
            if has_nav_access(perm, permissions=permissions, is_superadmin=is_superadmin)
        )
        desktop_sidebar = f"""
    <aside class="group/sidebar fixed left-2 top-3 bottom-3 z-[60] hidden w-[3.75rem] min-w-[3.75rem] max-w-[3.75rem] flex-col overflow-x-hidden rounded-2xl border border-base-content/10 bg-base-300 shadow-xl transition-[width,max-width,min-width] duration-300 ease-out hover:w-64 hover:max-w-none hover:min-w-[16rem] md:flex">
      <div class="flex w-full shrink-0 items-center justify-start gap-2 px-[10px] pt-[10px] pb-[5px]">
        <span class="flex h-10 w-10 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-primary/20 text-primary">
          {logo_inner}
        </span>
        <span class="nav-label pointer-events-none max-h-0 min-w-0 max-w-0 shrink grow-0 basis-0 overflow-hidden whitespace-nowrap text-sm font-bold tracking-tight text-base-content opacity-0 group-hover/sidebar:pointer-events-auto group-hover/sidebar:max-h-6 group-hover/sidebar:max-w-[12rem] group-hover/sidebar:shrink group-hover/sidebar:basis-auto group-hover/sidebar:opacity-100">{_esc(brand_title)}</span>
      </div>
      <nav class="flex min-h-0 flex-1 flex-col items-center gap-[5px] overflow-y-auto overflow-x-hidden px-[10px] pt-[5px] pb-[10px] group-hover/sidebar:items-stretch">
        {nav_desktop}
      </nav>
      <div class="mt-auto flex w-full flex-col items-center pt-2 pb-2 group-hover/sidebar:items-stretch">
        <div class="mx-[3px] mb-2 h-[1px] rounded-full bg-base-content/10"></div>
        <div class="flex w-full flex-col gap-[5px] overflow-hidden px-[10px]">
          {_sidebar_footer_profile("/admin/profile", user_label, sidebar_avatar_inner, cur)}
        </div>
      </div>
    </aside>"""
        mobile_brand_bar = f"""
    <header class="fixed left-0 right-0 top-0 z-40 flex h-12 items-center justify-center gap-2 border-b border-base-content/10 bg-base-300/95 px-12 backdrop-blur-md md:hidden" role="banner" aria-label="Бренд панели">
      <span class="flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-primary/20 text-primary">
        {_brand_logo_mark(settings, compact=True)}
      </span>
      <span class="max-w-[min(14rem,calc(100vw-8.5rem))] truncate text-sm font-bold tracking-tight text-base-content">{_esc(brand_title)}</span>
    </header>"""
        mobile_drawer = f"""
    <button type="button" id="remna-mnav-open" class="btn btn-primary btn-circle fixed bottom-5 left-3 z-50 h-12 w-12 min-h-12 min-w-12 border-0 shadow-xl md:hidden" aria-expanded="false" aria-controls="remna-mnav-drawer" aria-label="Открыть меню">
      <i class="fa-solid fa-bars text-lg" aria-hidden="true"></i>
    </button>
    <div id="remna-mnav-drawer" class="fixed inset-0 z-[75] hidden md:hidden" aria-hidden="true">
      <div class="absolute inset-0 bg-base-content/45 backdrop-blur-sm" data-remna-mnav-close></div>
      <aside class="absolute left-0 top-0 flex h-full w-[min(20rem,90vw)] flex-col gap-1 overflow-y-auto border-r border-base-content/10 bg-base-300 py-14 pl-2 pr-2 shadow-2xl" aria-label="Меню админки">
        <button type="button" class="btn btn-sm btn-circle btn-ghost absolute right-2 top-3 z-10" data-remna-mnav-close aria-label="Закрыть"><i class="fa-solid fa-xmark" aria-hidden="true"></i></button>
        {nav_mobile}
        {_mob_drawer_link("/admin/profile", "fa-solid fa-user", "Мой профиль", cur)}
        <form method="post" action="/admin/logout" class="mt-2 border-t border-base-content/10 pt-2"><button type="submit" class="btn btn-ghost btn-sm h-10 min-h-10 w-full justify-start gap-3 border-0 font-medium normal-case text-error"><i class="fa-solid fa-right-from-bracket w-5 shrink-0 text-center text-base" aria-hidden="true"></i><span>Выйти</span></button></form>
      </aside>
    </div>"""
        theme_toggle = """
    <button type="button" id="remna-theme-toggle" onclick="remnaToggleTheme()" class="btn btn-square fixed right-2 top-2 z-[52] h-8 w-8 min-h-8 min-w-8 shrink-0 border border-base-content/15 bg-base-300/90 p-0 shadow-md backdrop-blur-md md:right-7 md:top-6 md:h-10 md:w-10 md:min-h-10 md:min-w-10 md:shadow-lg" aria-label="Тема"></button>"""
        nav_blocks = desktop_sidebar + mobile_brand_bar + mobile_drawer + theme_toggle
        remna_chrome = """
    <div id="remna-toast-host" aria-live="polite"></div>
    <div id="remna-loading-overlay" class="remna-loading-overlay" aria-hidden="true">
      <div class="remna-loading-box">
        <span class="remna-loading-main">
          <span class="remna-loading-side" aria-hidden="true"><i class="fa-solid fa-hourglass-half remna-hourglass-icon"></i></span>
          <span id="remna-loading-text" class="text-sm font-medium">Загрузка</span>
        </span>
        <span class="remna-spinner" aria-hidden="true"></span>
      </div>
    </div>
    <div id="remna-hwid-overlay" class="fixed inset-0 z-[150] hidden items-center justify-center bg-base-content/45 backdrop-blur-sm p-4" role="dialog" aria-modal="true" aria-labelledby="remna-hwid-title">
      <div class="bg-base-100 border border-base-content/15 rounded-2xl shadow-2xl max-w-md w-full p-6 relative overflow-hidden">
        <button type="button" class="btn btn-sm btn-circle btn-ghost absolute right-2 top-2" data-remna-close="hwid" aria-label="Закрыть">✕</button>
        <h3 id="remna-hwid-title" class="font-bold text-lg mb-2 pr-10">Отвязать устройство</h3>
        <p id="remna-hwid-desc" class="text-sm opacity-80 mb-4"></p>
        <form id="remna-hwid-form" method="post" class="flex flex-col gap-3">
          <input type="hidden" name="hwid" id="remna-hwid-field" value="" />
          <input type="hidden" name="mode" id="remna-hwid-mode" value="keep_slots" />
          <div>
            <button type="submit" class="btn btn-outline btn-primary w-full" data-remna-hwid-mode="keep_slots">Отвязать устройство</button>
            <p class="text-xs opacity-60 mt-1">Снимет HWID с Remnawave; оплаченные слоты в подписке не меняются.</p>
          </div>
          <div id="remna-hwid-decrease-wrap" class="hidden">
            <button type="submit" class="btn btn-error w-full" data-remna-hwid-mode="decrease_slot">Удалить слот</button>
            <p class="text-xs opacity-60 mt-1">Слот полностью снимается с подписки; деньги не возвращаются. Недоступно, если в подписке уже минимум два слота.</p>
          </div>
        </form>
      </div>
    </div>
    <div id="remna-slot-overlay" class="fixed inset-0 z-[150] hidden items-center justify-center bg-base-content/45 backdrop-blur-sm p-4" role="dialog" aria-modal="true" aria-labelledby="remna-slot-title">
      <div class="bg-base-100 border border-base-content/15 rounded-2xl shadow-2xl max-w-md w-full p-6 relative">
        <button type="button" class="btn btn-sm btn-circle btn-ghost absolute right-2 top-2" data-remna-close="slot" aria-label="Закрыть">✕</button>
        <h3 id="remna-slot-title" class="font-bold text-lg mb-2 pr-10">Снять слот</h3>
        <p class="text-sm opacity-80 mb-4">Удалить запись устройства в БД, уменьшить оплаченные слоты и лимит HWID в панели (если применимо).</p>
        <form id="remna-slot-form" method="post" class="flex flex-wrap gap-2 justify-end">
          <input type="hidden" name="device_id" id="remna-slot-device" value="" />
          <button type="button" class="btn btn-ghost" data-remna-close="slot">Отмена</button>
          <button type="submit" class="btn btn-warning">Снять слот</button>
        </form>
      </div>
    </div>
    <div id="remna-subdis-overlay" class="fixed inset-0 z-[150] hidden items-center justify-center bg-base-content/45 backdrop-blur-sm p-4" role="dialog" aria-modal="true" aria-labelledby="remna-subdis-title">
      <div class="bg-base-100 border border-base-content/15 rounded-2xl shadow-2xl max-w-md w-full p-6 relative">
        <button type="button" class="btn btn-sm btn-circle btn-ghost absolute right-2 top-2" data-remna-close="subdis" aria-label="Закрыть">✕</button>
        <h3 id="remna-subdis-title" class="font-bold text-lg mb-2 pr-10">Отключить подписку?</h3>
        <p class="text-sm opacity-80 mb-4">Как в боте: запись подписки станет <code class="text-xs bg-base-300 px-1 rounded">cancelled</code>, учётная запись в панели Remnawave — <code class="text-xs bg-base-300 px-1 rounded">DISABLED</code>.</p>
        <form id="remna-subdis-form" method="post" class="flex flex-wrap gap-2 justify-end">
          <input type="hidden" name="subscription_id" id="remna-subdis-sid" value="" />
          <button type="button" class="btn btn-ghost" data-remna-close="subdis">Отмена</button>
          <button type="submit" class="btn btn-error">Отключить</button>
        </form>
      </div>
    </div>
    <div id="remna-hwid-json-overlay" class="fixed inset-0 z-[150] hidden items-center justify-center bg-base-content/45 backdrop-blur-sm p-4" role="dialog" aria-modal="true" aria-labelledby="remna-hwid-json-title">
      <div class="bg-base-100 border border-base-content/15 rounded-2xl shadow-2xl max-w-2xl w-full max-h-[85vh] flex flex-col p-6 relative">
        <button type="button" class="btn btn-sm btn-circle btn-ghost absolute right-2 top-2 z-10" data-remna-close="hwidjson" aria-label="Закрыть">✕</button>
        <h3 id="remna-hwid-json-title" class="font-bold text-lg mb-3 pr-10">Карточка устройства</h3>
        <div id="remna-hwid-json-card" class="flex-1 overflow-auto rounded-lg border border-base-content/10 bg-base-200 p-4"></div>
      </div>
    </div>
    <div id="remna-confirm-overlay" class="fixed inset-0 z-[160] hidden items-center justify-center bg-base-content/45 backdrop-blur-sm p-4" role="dialog" aria-modal="true" aria-labelledby="remna-confirm-title">
      <div class="bg-base-100 border border-base-content/15 rounded-2xl shadow-2xl max-w-md w-full p-6 relative">
        <h3 id="remna-confirm-title" class="font-bold text-lg mb-2">Подтверждение</h3>
        <p id="remna-confirm-msg" class="text-sm opacity-85 mb-6 whitespace-pre-wrap"></p>
        <div class="flex justify-end gap-2">
          <button type="button" class="btn btn-ghost" id="remna-confirm-cancel">Отмена</button>
          <button type="button" class="btn btn-primary" id="remna-confirm-ok">Да</button>
        </div>
      </div>
    </div>"""
    elif not show_nav:
        main_cls = "min-h-screen flex items-center justify-center p-4 w-full"

    back_fixed = ""
    if back_href and show_nav and request is not None:
        back_fixed = f"""
    <a href="{_esc(back_href)}" class="btn btn-square btn-ghost fixed left-2 top-2 z-40 h-8 w-8 min-h-8 min-w-8 shrink-0 border border-base-content/15 bg-base-300/90 p-0 shadow-md backdrop-blur-md md:left-[calc(0.75rem+4.25rem+0.75rem)] md:top-6 md:h-10 md:w-10 md:min-h-10 md:min-w-10 md:shadow-lg" title="Назад" aria-label="Назад"><i class="fa-solid fa-arrow-left text-sm md:text-base" aria-hidden="true"></i></a>"""
    inner = body

    theme_script = """
  <script>
  (function(){
    var root=document.documentElement;
    function syncIcon(){
      var b=document.getElementById('remna-theme-toggle');
      if(!b)return;
      var night=root.getAttribute('data-theme')==='night';
      b.innerHTML=night?'<i class="fa-solid fa-sun text-sm md:text-base" aria-hidden="true"></i>':'<i class="fa-solid fa-moon text-sm md:text-base" aria-hidden="true"></i>';
      b.setAttribute('aria-label',night?'Светлая тема':'Тёмная тема');
    }
    window.remnaToggleTheme=function(){
      var next=root.getAttribute('data-theme')==='night'?'light':'night';
      root.setAttribute('data-theme',next);
      try{localStorage.setItem('remna-admin-theme',next);}catch(e){}
      syncIcon();
    };
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',syncIcon);else syncIcon();
    var remnaLoadOverlay=document.getElementById('remna-loading-overlay');
    var remnaLoadText=document.getElementById('remna-loading-text');
    var remnaLoadTimer=null;
    var remnaLoadTick=0;
    function remnaLoadSetText(){
      if(!remnaLoadText)return;
      var dots='.'.repeat(remnaLoadTick%4);
      remnaLoadText.textContent='Загрузка'+dots;
      remnaLoadTick++;
    }
    window.remnaShowLoading=function(){
      if(!remnaLoadOverlay)return;
      remnaLoadOverlay.classList.add('is-active');
      remnaLoadOverlay.setAttribute('aria-hidden','false');
      remnaLoadTick=0;
      remnaLoadSetText();
      if(remnaLoadTimer)clearInterval(remnaLoadTimer);
      remnaLoadTimer=setInterval(remnaLoadSetText,350);
    };
    window.remnaHideLoading=function(){
      if(!remnaLoadOverlay)return;
      remnaLoadOverlay.classList.remove('is-active');
      remnaLoadOverlay.setAttribute('aria-hidden','true');
      if(remnaLoadTimer){clearInterval(remnaLoadTimer);remnaLoadTimer=null;}
    };
    window.addEventListener('pageshow',window.remnaHideLoading);
    window.addEventListener('load',window.remnaHideLoading);
  })();
  document.addEventListener('click',function(e){
    var el=e.target&&e.target.closest&&e.target.closest('[data-copy]');
    if(!el)return;
    var t=el.getAttribute('data-copy');
    if(t===null)return;
    e.preventDefault();
    navigator.clipboard.writeText(t).then(function(){
      var ic=el.querySelector('i');if(ic){var c=ic.className;ic.className='fa-solid fa-check text-xs';setTimeout(function(){ic.className=c;},850);}
    });
  });
    document.addEventListener('click',function(e){
    var nav=e.target&&e.target.closest&&e.target.closest('a[href]');
    if(nav){
      var href=(nav.getAttribute('href')||'').trim();
      var htmxNav=nav.hasAttribute('hx-get')||nav.hasAttribute('hx-post')||nav.hasAttribute('hx-put')||nav.hasAttribute('hx-patch')||nav.hasAttribute('hx-delete');
        var isExternalScheme=/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(href) && !/^https?:/i.test(href) && !/^mailto:/i.test(href) && !/^tel:/i.test(href);
      if(href && !href.startsWith('#') && !nav.hasAttribute('data-no-loading')){
        if(!(e.ctrlKey||e.metaKey||e.shiftKey||e.altKey) && nav.getAttribute('target')!=='_blank'){
          if(!htmxNav){window.remnaShowLoading&&window.remnaShowLoading();}
            if(isExternalScheme){
              setTimeout(function(){
                if(document.hidden)return;
                window.remnaHideLoading&&window.remnaHideLoading();
                window.remnaToast&&window.remnaToast('error','Не удалось открыть приложение. Попробуйте еще раз.');
              },5000);
            }
        }
      }
    }
    var tr=e.target&&e.target.closest&&e.target.closest('tr.remna-row-link');
    if(!tr)return;
    if(e.target.closest('a,button,input,textarea,select,label,[data-no-row-nav]'))return;
    var h=tr.getAttribute('data-row-href');
    if(h)window.location.href=h;
  });
  document.addEventListener('keydown',function(e){
    if(e.key!=='Enter')return;
    var tr=e.target&&e.target.closest&&e.target.closest('tr.remna-row-link');
    if(!tr||document.activeElement!==tr)return;
    var h=tr.getAttribute('data-row-href');
    if(h)window.location.href=h;
  });
  (function(){
    var ICONS={success:'fa-circle-check',error:'fa-circle-xmark',warning:'fa-triangle-exclamation',info:'fa-circle-info'};
    var BAR={success:'#22c55e',error:'#ef4444',warning:'#ca8a04',info:'oklch(0.65 0.2 280)'};
    window.remnaToast=function(kind,message,duration){
      kind=kind||'info';
      duration=duration||4800;
      var host=document.getElementById('remna-toast-host');
      if(!host)return;
      var el=document.createElement('div');
      el.className='remna-toast remna-toast--'+kind;
      el.setAttribute('role','status');
      var ic=ICONS[kind]||ICONS.info;
      var bc=BAR[kind]||BAR.info;
      el.innerHTML='<i class="fa-solid '+ic+' remna-toast__icon" aria-hidden="true"></i><div class="remna-toast__text"></div><div class="remna-toast__bar" style="animation-duration:'+duration+'ms;background:'+bc+'"></div>';
      el.querySelector('.remna-toast__text').textContent=message||'';
      host.appendChild(el);
      setTimeout(function(){
        el.classList.add('remna-toast--out');
        setTimeout(function(){try{el.remove();}catch(x){}},320);
      },duration);
    };
    function remnaCloseHwid(){
      var o=document.getElementById('remna-hwid-overlay');
      if(o){o.classList.add('hidden');o.classList.remove('flex');}
      var dec=document.getElementById('remna-hwid-decrease-wrap');
      if(dec)dec.classList.add('hidden');
    }
    function remnaCloseSlot(){
      var o=document.getElementById('remna-slot-overlay');
      if(o){o.classList.add('hidden');o.classList.remove('flex');}
    }
    function remnaCloseSubdis(){
      var o=document.getElementById('remna-subdis-overlay');
      if(o){o.classList.add('hidden');o.classList.remove('flex');}
    }
    function remnaCloseHwidJson(){
      var o=document.getElementById('remna-hwid-json-overlay');
      if(o){o.classList.add('hidden');o.classList.remove('flex');}
      var p=document.getElementById('remna-hwid-json-card');
      if(p)p.innerHTML='';
    }
    function remnaEsc(v){
      return String(v==null?'':v)
        .replace(/&/g,'&amp;')
        .replace(/</g,'&lt;')
        .replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;')
        .replace(/'/g,'&#39;');
    }
    function remnaFmtIso(iso){
      if(!iso)return '—';
      try{
        var dt=new Date(String(iso));
        if(isNaN(dt.getTime()))return remnaEsc(iso);
        return dt.toLocaleString('ru-RU',{
          year:'numeric',month:'2-digit',day:'2-digit',
          hour:'2-digit',minute:'2-digit',second:'2-digit',
          timeZoneName:'short'
        });
      }catch(_e){
        return remnaEsc(iso);
      }
    }
    function remnaDeviceCardFromJson(txt){
      var d=null;
      try{ d=JSON.parse(txt||'{}'); }catch(_e){ d=null; }
      if(!d||typeof d!=='object'){
        return ''
          + '<div class="alert alert-warning mb-3"><span>Не удалось разобрать данные устройства</span></div>'
          + '<pre class="rounded-lg border border-base-content/10 bg-base-300 p-3 text-[11px] leading-relaxed whitespace-pre-wrap font-mono">'
          + remnaEsc(txt||'')
          + '</pre>';
      }
      var title=(d.deviceModel||d.platform||'Устройство');
      var subtitle=((d.platform||'—') + ' · версия ОС: ' + (d.osVersion||'—'));
      var rows=[
        ['HWID', d.hwid || '—'],
        ['UUID пользователя', d.userUuid || '—'],
        ['Платформа', d.platform || '—'],
        ['Версия ОС', d.osVersion || '—'],
        ['Модель', d.deviceModel || '—'],
        ['User-Agent', d.userAgent || '—'],
        ['Создано', remnaFmtIso(d.createdAt)],
        ['Обновлено', remnaFmtIso(d.updatedAt)]
      ];
      var grid=rows.map(function(it){
        return ''
          + '<div class="rounded-lg border border-base-content/10 bg-base-100 p-3">'
          + '<div class="text-xs uppercase tracking-wide opacity-60">'+remnaEsc(it[0])+'</div>'
          + '<div class="mt-1 break-all font-medium">'+remnaEsc(it[1])+'</div>'
          + '</div>';
      }).join('');
      return ''
        + '<div class="flex items-start justify-between gap-3 mb-4">'
        + '<div><div class="text-lg font-semibold">'+remnaEsc(title)+'</div>'
        + '<div class="text-sm opacity-70">'+remnaEsc(subtitle)+'</div></div>'
        + '<span class="badge badge-outline badge-sm">'+remnaEsc(d.platform||'—')+'</span>'
        + '</div>'
        + '<div class="grid gap-3 sm:grid-cols-2">'+grid+'</div>';
    }
    var remnaPendingForm=null;
    var remnaPendingResolve=null;
    function remnaHideConfirmOverlay(){
      var o=document.getElementById('remna-confirm-overlay');
      if(o){o.classList.add('hidden');o.classList.remove('flex');}
    }
    function remnaAbortConfirm(){
      remnaHideConfirmOverlay();
      remnaPendingForm=null;
      if(remnaPendingResolve){var r=remnaPendingResolve;remnaPendingResolve=null;r(false);}
    }
    window.remnaCloseAllModals=function(){remnaCloseHwid();remnaCloseSlot();remnaCloseSubdis();remnaCloseHwidJson();remnaAbortConfirm();};
    document.addEventListener('submit',function(e){
      var f=e.target;
      if(!(f instanceof HTMLFormElement))return;
      var msg=f.getAttribute('data-remna-confirm-msg');
      if(!msg)return;
      if(f.getAttribute('data-remna-confirmed')==='1'){
        f.removeAttribute('data-remna-confirmed');
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      remnaPendingForm=f;
      remnaPendingResolve=null;
      var msgEl=document.getElementById('remna-confirm-msg');
      var titleEl=document.getElementById('remna-confirm-title');
      if(msgEl)msgEl.textContent=msg;
      if(titleEl)titleEl.textContent='Подтверждение';
      var ov=document.getElementById('remna-confirm-overlay');
      if(ov){ov.classList.remove('hidden');ov.classList.add('flex');}
    },true);
    (function(){
      var okBtn=document.getElementById('remna-confirm-ok');
      var cancelBtn=document.getElementById('remna-confirm-cancel');
      if(okBtn)okBtn.addEventListener('click',function(){
        remnaHideConfirmOverlay();
        if(remnaPendingForm){
          var pf=remnaPendingForm;
          remnaPendingForm=null;
          pf.setAttribute('data-remna-confirmed','1');
          pf.requestSubmit();
          return;
        }
        if(remnaPendingResolve){var r=remnaPendingResolve;remnaPendingResolve=null;r(true);}
      });
      if(cancelBtn)cancelBtn.addEventListener('click',remnaAbortConfirm);
    })();
    window.remnaConfirmPromise=function(message,title){
      return new Promise(function(resolve){
        remnaPendingForm=null;
        remnaPendingResolve=resolve;
        var msgEl=document.getElementById('remna-confirm-msg');
        var titleEl=document.getElementById('remna-confirm-title');
        if(msgEl)msgEl.textContent=message||'';
        if(titleEl)titleEl.textContent=title||'Подтверждение';
        var ov=document.getElementById('remna-confirm-overlay');
        if(ov){ov.classList.remove('hidden');ov.classList.add('flex');}
      });
    };
    document.addEventListener('click',function(e){
      var t=e.target;
      if(t&&t.getAttribute&&t.getAttribute('data-remna-close')==='hwid'){e.preventDefault();remnaCloseHwid();}
      if(t&&t.getAttribute&&t.getAttribute('data-remna-close')==='slot'){e.preventDefault();remnaCloseSlot();}
      if(t&&t.getAttribute&&t.getAttribute('data-remna-close')==='subdis'){e.preventDefault();remnaCloseSubdis();}
      if(t&&t.getAttribute&&t.getAttribute('data-remna-close')==='hwidjson'){e.preventDefault();remnaCloseHwidJson();}
      var hw=t&&t.closest&&t.closest('#remna-hwid-overlay');
      if(hw&&t===hw)remnaCloseHwid();
      var sl=t&&t.closest&&t.closest('#remna-slot-overlay');
      if(sl&&t===sl)remnaCloseSlot();
      var sd=t&&t.closest&&t.closest('#remna-subdis-overlay');
      if(sd&&t===sd)remnaCloseSubdis();
      var jn=t&&t.closest&&t.closest('#remna-hwid-json-overlay');
      if(jn&&t===jn)remnaCloseHwidJson();
      var cfm=t&&t.closest&&t.closest('#remna-confirm-overlay');
      if(cfm&&t===cfm)remnaAbortConfirm();
      var openH=t&&t.closest&&t.closest('[data-remna-open-hwid]');
      if(openH){
        e.preventDefault();
        var uid=openH.getAttribute('data-user-id')||'';
        var hwid=openH.getAttribute('data-hwid')||'';
        var title=openH.getAttribute('data-title')||'';
        var form=document.getElementById('remna-hwid-form');
        if(form){form.action='/admin/users/'+uid+'/unlink-hwid';}
        var hf=document.getElementById('remna-hwid-field');
        if(hf)hf.value=hwid;
        var hd=document.getElementById('remna-hwid-desc');
        if(hd)hd.textContent=title;
        var dec=document.getElementById('remna-hwid-decrease-wrap');
        if(dec){
          if((openH.getAttribute('data-slot-removable')||'')==='1')dec.classList.remove('hidden');
          else dec.classList.add('hidden');
        }
        var ov=document.getElementById('remna-hwid-overlay');
        if(ov){ov.classList.remove('hidden');ov.classList.add('flex');}
      }
      var openS=t&&t.closest&&t.closest('[data-remna-open-slot]');
      if(openS){
        e.preventDefault();
        var uid2=openS.getAttribute('data-user-id')||'';
        var did=openS.getAttribute('data-device-id')||'';
        var sf=document.getElementById('remna-slot-form');
        if(sf){sf.action='/admin/users/'+uid2+'/unlink-device';}
        var di=document.getElementById('remna-slot-device');
        if(di)di.value=did;
        var ov2=document.getElementById('remna-slot-overlay');
        if(ov2){ov2.classList.remove('hidden');ov2.classList.add('flex');}
      }
      var openD=t&&t.closest&&t.closest('[data-remna-open-sub-disable]');
      if(openD){
        e.preventDefault();
        var u3=openD.getAttribute('data-user-id')||'';
        var sid=openD.getAttribute('data-sub-id')||'';
        var df=document.getElementById('remna-subdis-form');
        if(df){df.action='/admin/users/'+u3+'/subscription/disable';}
        var si=document.getElementById('remna-subdis-sid');
        if(si)si.value=sid;
        var ov3=document.getElementById('remna-subdis-overlay');
        if(ov3){ov3.classList.remove('hidden');ov3.classList.add('flex');}
      }
      var openJ=t&&t.closest&&t.closest('[data-remna-open-hwid-json]');
      if(openJ){
        e.preventDefault();
        var b64=openJ.getAttribute('data-json-b64')||'';
        var txt='';
        try{
          if(b64){
            var bin=atob(b64);
            var bytes=new Uint8Array(bin.length);
            for(var i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
            txt=new TextDecoder('utf-8').decode(bytes);
          }
        }catch(x){txt='(ошибка декодирования)';}
        var pre=document.getElementById('remna-hwid-json-card');
        if(pre)pre.innerHTML=remnaDeviceCardFromJson(txt);
        var ovj=document.getElementById('remna-hwid-json-overlay');
        if(ovj){ovj.classList.remove('hidden');ovj.classList.add('flex');}
      }
    });
    document.addEventListener('submit',function(e){
      var f=e.target;
      if(!f||f.id!=='remna-hwid-form')return;
      var btn=e.submitter;
      var m=btn&&btn.getAttribute&&btn.getAttribute('data-remna-hwid-mode');
      if(m){
        var im=document.getElementById('remna-hwid-mode');
        if(im)im.value=m;
      }
    },true);
    document.addEventListener('keydown',function(e){
      if(e.key!=='Escape')return;
      var mdr=document.getElementById('remna-mnav-drawer');
      if(mdr&&!mdr.classList.contains('hidden')){
        mdr.classList.add('hidden');
        mdr.setAttribute('aria-hidden','true');
        document.body.style.overflow='';
        var mop=document.getElementById('remna-mnav-open');
        if(mop)mop.setAttribute('aria-expanded','false');
        return;
      }
      window.remnaCloseAllModals();
    });
    document.addEventListener('submit',function(e){
      var f=e.target;
      if(!f || !(f instanceof HTMLFormElement))return;
      if((f.getAttribute('method')||'').toLowerCase()==='dialog')return;
      if(f.hasAttribute('data-no-loading'))return;
      window.remnaShowLoading&&window.remnaShowLoading();
    },true);
    document.addEventListener('load',function(e){
      var img=e.target;
      if(!(img instanceof HTMLImageElement))return;
      if(!img.matches('.remna-avatar-img,[data-remna-avatar]'))return;
      img.classList.add('is-loaded');
    },true);
    function remnaConsumeUrlNotify(){
      try{
        var u=new URL(window.location.href);
        var n=u.searchParams.get('n');
        var err=u.searchParams.get('err');
        var c=u.searchParams.get('c');
        var rw=u.searchParams.get('rw');
        var amt=u.searchParams.get('amt');
        var map={hwid_keep:'Устройство отвязано от панели. Оплаченные слоты не менялись.',hwid_slot:'Устройство отвязано, слот подписки уменьшен.',db_slot:'Слот снят: запись в БД удалена, лимит в панели обновлён.',sub_off:'Подписка отключена (БД и панель).',sub_on:'Подписка снова включена.',ar_on:'Авто-продление включено.',ar_off:'Авто-продление выключено.',months_ok:'Срок подписки продлён.',days_ok:'Срок подписки изменён.',device_slots_ok:'Лимит устройств (слоты) обновлён.',personal_price_ok:'Персональная цена ₽/мес сохранена.',personal_discount_ok:'Персональная скидка сохранена.',bal_ok:'Баланс пополнен.',bal_reset:'Баланс обнулён.',billing_mode_toggled:'Режим биллинга переключён.',user_del:'Пользователь удалён из БД и из панели Remnawave (если был UUID).',risk_reset:'Отметки уведомлений о риске минуса сброшены.',purchase_refund_ok:'Возврат по транзакции выполнен.',tariffs_shop:'Режим продажи тарифов в боте обновлён.',manual_bind_ok:'Подписка вручную привязана к пользователю.',rw_check_ok:'Профиль найден в панели и привязан к пользователю.',saved:'Изменения сохранены.'};
        if(n&&map[n])window.remnaToast('success',map[n]);
        if(n==='mass_payg_done'){
          window.remnaToast('success','Конвертация завершена: пользователей '+(c||'0')+', панель '+(rw||'0')+', начислено '+(amt||'0')+' ₽.');
        }
        if(err)window.remnaToast('error',err);
        if(n||err||c||rw||amt){
          u.searchParams.delete('n');
          u.searchParams.delete('err');
          u.searchParams.delete('c');
          u.searchParams.delete('rw');
          u.searchParams.delete('amt');
          var qs=u.searchParams.toString();
          window.history.replaceState({},'',u.pathname+(qs?'?'+qs:''));
        }
      }catch(x){}
    }
    window.remnaConsumeUrlNotify = remnaConsumeUrlNotify;
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',remnaConsumeUrlNotify);
    else remnaConsumeUrlNotify();
    (function(){
      var dr=document.getElementById('remna-mnav-drawer');
      var op=document.getElementById('remna-mnav-open');
      if(!dr||!op)return;
      function remnaMnavOpen(){
        dr.classList.remove('hidden');
        dr.setAttribute('aria-hidden','false');
        op.setAttribute('aria-expanded','true');
        document.body.style.overflow='hidden';
      }
      function remnaMnavClose(){
        dr.classList.add('hidden');
        dr.setAttribute('aria-hidden','true');
        op.setAttribute('aria-expanded','false');
        document.body.style.overflow='';
      }
      op.addEventListener('click',function(e){
        e.preventDefault();
        if(dr.classList.contains('hidden'))remnaMnavOpen();else remnaMnavClose();
      });
      dr.addEventListener('click',function(e){
        if(e.target&&e.target.closest&&e.target.closest('[data-remna-mnav-close]'))remnaMnavClose();
      });
      dr.querySelectorAll('a[href]').forEach(function(a){
        a.addEventListener('click',function(){remnaMnavClose();});
      });
      dr.querySelectorAll('form[method="post"]').forEach(function(f){
        f.addEventListener('submit',function(){remnaMnavClose();});
      });
    })();
    (function(){
      function extractAndRunScripts(root){
        Array.prototype.slice.call(root.querySelectorAll('script')).forEach(function(s){
          var ns=document.createElement('script');
          if(s.src)ns.src=s.src; else ns.textContent=s.textContent;
          s.parentNode.replaceChild(ns,s);
        });
      }
      async function swapPage(url,init){
        if(window.remnaShowLoading)window.remnaShowLoading();
        try{
          var resp=await fetch(url,Object.assign({credentials:'same-origin'},init||{}));
          var finalUrl=resp.url||url;
          if(finalUrl.indexOf('/admin/login')!==-1){window.location.href=finalUrl;return;}
          var html=await resp.text();
          var doc=new DOMParser().parseFromString(html,'text/html');
          var newPage=doc.querySelector('.remna-page');
          var curPage=document.querySelector('.remna-page');
          if(!newPage||!curPage){window.location.href=finalUrl;return;}
          var saved={};
          curPage.querySelectorAll('textarea[id],input[id]').forEach(function(el){if(el.type!=='file')saved[el.id]=el.value;});
          curPage.innerHTML=newPage.innerHTML;
          curPage.querySelectorAll('textarea[id],input[id]').forEach(function(el){if(el.type!=='file'&&saved[el.id]!==undefined)el.value=saved[el.id];});
          extractAndRunScripts(curPage);
          document.title=doc.title||document.title;
          if(finalUrl!==window.location.href)history.pushState({remnaSwap:1},document.title,finalUrl);
          if(window.remnaConsumeUrlNotify)window.remnaConsumeUrlNotify();
        }catch(x){
          if(window.remnaHideLoading)window.remnaHideLoading();
          window.location.href=url;return;
        }
        if(window.remnaHideLoading)window.remnaHideLoading();
      }
      window.remnaSwapPage=swapPage;
      document.addEventListener('submit',function(e){
        var f=e.target;
        if(!(f instanceof HTMLFormElement))return;
        if((f.getAttribute('method')||'').toLowerCase()==='dialog')return;
        if(f.hasAttribute('data-no-ajax'))return;
        var action=f.action||window.location.href;
        if(/\/(login|logout)(\?|$)/.test(action))return;
        e.preventDefault();
        var method=(f.getAttribute('method')||'GET').toUpperCase();
        var fd=new FormData(f);
        var fetchUrl=action,fi={method:method};
        if(method==='GET'){var qs=new URLSearchParams(fd).toString();fetchUrl=action.split('?')[0]+(qs?'?'+qs:'');}
        else fi.body=fd;
        swapPage(fetchUrl,fi);
      });
      document.addEventListener('click',function(e){
        if(e.defaultPrevented||e.ctrlKey||e.metaKey||e.shiftKey||e.altKey)return;
        var a=e.target&&e.target.closest&&e.target.closest('a[href]');
        if(!a||a.getAttribute('target')==='_blank'||a.hasAttribute('data-no-ajax'))return;
        var href=(a.getAttribute('href')||'').trim();
        if(!href||href[0]==='#'||href.indexOf('javascript:')===0)return;
        try{
          var u=new URL(href,window.location.href);
          if(u.origin!==window.location.origin)return;
          if(/\/(login|logout)(\?|$)/.test(u.pathname))return;
          e.preventDefault();
          var dr=document.getElementById('remna-mnav-drawer');
          if(dr&&!dr.classList.contains('hidden')){dr.classList.add('hidden');dr.setAttribute('aria-hidden','true');document.body.style.overflow='';var op=document.getElementById('remna-mnav-open');if(op)op.setAttribute('aria-expanded','false');}
          swapPage(u.href);
        }catch(x){}
      });
      window.addEventListener('popstate',function(){swapPage(window.location.href);});
    })();
  })();
  </script>"""

    page = f"""<!DOCTYPE html>
<html lang="ru" data-theme="night">
<head>
{_head_common(title, favicon_url=favicon_for_head, background_url=_admin_background_image_url(settings))}
</head>
<body class="text-base-content antialiased">
  {nav_blocks}{back_fixed}{remna_chrome}
  <div class="{main_cls} remna-page w-full min-w-0">
    {inner}
  </div>
{theme_script if show_nav and request is not None else ""}
</body>
</html>"""
    _resp = HTMLResponse(page, headers={"Cache-Control": "private, no-store"})
    _resp.charset = "utf-8"
    _resp.headers["content-type"] = "text/html; charset=utf-8"
    return _resp


def _is_logged(request: Request) -> bool:
    return bool(request.session.get("wauth"))


def _require_login(request: Request) -> RedirectResponse | None:
    if not _is_logged(request):
        return RedirectResponse(_admin_login_url_with_next(request), status_code=303)
    return None


async def _session() -> AsyncSession:
    factory = get_session_factory()
    return factory()


def _clear_web_admin_session(request: Request) -> None:
    request.session.pop("wauth", None)
    request.session.pop("wauth_user_id", None)
    request.session.pop("wauth_session_token", None)
    request.session.pop("wauth_session_exp", None)
    request.session.pop("wauth_login_kind", None)


def _login_method_label(kind: str, *, used_totp: bool) -> str:
    base = "Telegram" if kind == "telegram" else "GitHub" if kind == "github" else "Web"
    return f"{base} + Google Auth" if used_totp else base


def _admin_profile_link_for_notify(settings: Settings, user: User) -> str:
    base = (settings.public_site_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/admin/users/{int(user.id)}"


async def _notify_admin_login(settings: Settings, *, user: User, method_kind: str, used_totp: bool) -> None:
    now_m = time.monotonic()
    uid_i = int(user.id)
    prev = _LOGIN_NOTIFY_LAST.get(uid_i)
    if prev is not None and (now_m - prev) < 55.0:
        return
    _LOGIN_NOTIFY_LAST[uid_i] = now_m
    if len(_LOGIN_NOTIFY_LAST) > 512:
        stale = [k for k, t in _LOGIN_NOTIFY_LAST.items() if (now_m - t) > 600]
        for k in stale:
            _LOGIN_NOTIFY_LAST.pop(k, None)
    chat_id = settings.admin_log_chat_id
    if chat_id is None or (isinstance(chat_id, str) and not chat_id.strip()):
        return
    when = datetime.now(UTC).astimezone(_MSK_TZ).strftime("%H:%M МСК %d.%m.%Y")
    admin_name = html.escape((user.first_name or user.username or f"user#{user.id}").strip())
    admin_tg = int(user.telegram_id or 0)
    admin_link = f'<a href="tg://user?id={admin_tg}">{admin_name}</a>' if admin_tg else admin_name
    profile_link = _admin_profile_link_for_notify(settings, user)
    profile_suffix = f' | <a href="{html.escape(profile_link)}">профиль</a>' if profile_link else ""
    text = (
        "🔐 <b>Вход в web-admin</b>\n"
        f"Администратор: {admin_link}{profile_suffix}\n"
        f"Способ: <b>{html.escape(_login_method_label(method_kind, used_totp=used_totp))}</b>\n"
        f"Время: <b>{html.escape(when)}</b>"
    )
    await send_telegram_message(
        chat_id,
        text,
        parse_mode="HTML",
        message_thread_id=settings.admin_log_thread_for(AdminLogTopic.LOGIN),
        settings=settings,
    )


async def _bind_web_admin_session(
    request: Request,
    *,
    user: User,
    method_kind: str,
    used_totp: bool,
) -> None:
    settings = get_settings()
    wauth = request.session.get("wauth")
    if not isinstance(wauth, dict):
        wauth = {}
    token = token_urlsafe(24)
    async with await _session() as session:
        db_user = await session.get(User, user.id)
        if db_user is None:
            _clear_web_admin_session(request)
            return
        from shared.services.admin_rbac_service import ensure_env_admin_records, sync_session_permissions

        await ensure_env_admin_records(session, settings, db_user)
        await sync_session_permissions(request, session, db_user, settings)
        row = await create_browser_session(
            session,
            user=db_user,
            request=request,
            session_token=token,
            login_kind=method_kind,
            auth_snapshot=auth_snapshot_from_wauth(wauth),
        )
        expires_at = row.expires_at
    request.session["wauth_user_id"] = int(user.id)
    request.session["wauth_session_token"] = token
    request.session["wauth_session_exp"] = int(expires_at.timestamp())
    request.session["wauth_login_kind"] = method_kind
    await _notify_admin_login(settings, user=user, method_kind=method_kind, used_totp=used_totp)


async def _redirect_after_browser_session_restore(
    request: Request,
    *,
    row: WebAdminBrowserSession,
    user: User,
    new_exp: datetime,
) -> RedirectResponse:
    snap = row.auth_snapshot if isinstance(row.auth_snapshot, dict) else {}
    if snap:
        request.session["wauth"] = restore_wauth_from_snapshot(snap)
    request.session["wauth_user_id"] = int(user.id)
    request.session["wauth_session_token"] = str(row.session_token)
    request.session["wauth_session_exp"] = int(new_exp.timestamp())
    request.session["wauth_login_kind"] = str(row.login_kind or request.session.get("wauth_login_kind") or "web")
    settings = get_settings()
    async with await _session() as session:
        from shared.services.admin_rbac_service import sync_session_permissions

        db_user = await session.get(User, user.id)
        if db_user is not None:
            await sync_session_permissions(request, session, db_user, settings)
            await session.commit()
    return RedirectResponse(_login_success_destination(request, explicit=None), status_code=303)


async def _restore_browser_session_row(request: Request, row: WebAdminBrowserSession) -> RedirectResponse | None:
    fp = browser_fingerprint(request)
    if row.fingerprint_hash != fp:
        return None
    async with await _session() as session:
        fresh = await try_get_browser_session(session, token=str(row.session_token), fingerprint_hash=fp)
        if fresh is None:
            return None
        new_exp = await touch_browser_session(session, fresh)
        user = await session.get(User, fresh.user_id)
    if user is None:
        return None
    return await _redirect_after_browser_session_restore(request, row=fresh, user=user, new_exp=new_exp)


async def _maybe_restore_browser_session(request: Request) -> RedirectResponse | None:
    """Продление/восстановление 24-часовой сессии этого браузера без повторного OAuth."""
    token = str(request.session.get("wauth_session_token") or "").strip()
    if not token:
        return None
    fp = browser_fingerprint(request)
    async with await _session() as session:
        row = await try_get_browser_session(session, token=token, fingerprint_hash=fp)
    if row is None:
        return None
    return await _restore_browser_session_row(request, row)


async def _resume_login_from_hint(request: Request, hint: dict) -> RedirectResponse:
    """Вход по карточке «последний аккаунт»: живая браузерная сессия или OAuth того же способа."""
    try:
        user_id = int(hint.get("user_id"))
    except (TypeError, ValueError):
        return RedirectResponse("/admin/login", status_code=303)
    fp = browser_fingerprint(request)
    token = str(request.session.get("wauth_session_token") or "").strip()
    async with await _session() as session:
        row = None
        if token:
            row = await try_get_browser_session(session, token=token, fingerprint_hash=fp)
            if row is not None and int(row.user_id) != user_id:
                row = None
        if row is None:
            row = await find_active_browser_session_for_user_safe(
                session, user_id=user_id, fingerprint_hash=fp
            )
    if row is not None:
        restored = await _restore_browser_session_row(request, row)
        if restored is not None:
            return restored
    kind = str(hint.get("login_kind") or "telegram").strip().lower()
    if kind == "github":
        return RedirectResponse("/admin/login/github/start", status_code=303)
    return RedirectResponse("/admin/login/telegram/start", status_code=303)


async def _profile_browser_sessions_html(request: Request, user_id: int) -> str:
    current_tok = str(request.session.get("wauth_session_token") or "").strip()
    now = datetime.now(UTC)
    async with await _session() as session:
        rows = await list_user_browser_sessions_safe(session, user_id, limit=20)
    if not rows:
        return """
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-2">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-desktop text-primary mr-2" aria-hidden="true"></i>Браузерные сессии (24 ч + история 3 дня)</h3>
        <p class="text-sm opacity-80">Нет записей. Активная сессия живет 24 часа, а неактивные/отозванные устройства хранятся в истории до 3 дней.</p>
      </div>
    </div>"""
    items = []
    for row in rows:
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        active = row.revoked_at is None and exp > now
        is_current = active and row.session_token == current_tok
        status = "текущая" if is_current else ("активна" if active else ("отозвана" if row.revoked_at else "истекла"))
        ua = _esc((row.user_agent or "")[:72])
        ip = _esc(row.ip_address or "—")
        btn = ""
        if active and not is_current:
            btn = (
                f'<form method="post" action="/admin/profile/sessions/{int(row.id)}/revoke" class="inline">'
                f'<button type="submit" class="btn btn-ghost btn-xs text-error">Отозвать</button></form>'
            )
        elif active and is_current:
            btn = '<span class="text-xs opacity-60">это устройство</span>'
        items.append(
            f"<li class='py-2 border-b border-base-content/10 text-sm'>"
            f"<b>{_esc(status)}</b> · {_esc(row.login_kind)} · {ip}<br/>"
            f"<span class='opacity-70'>{ua}</span><br/>"
            f"<span class='opacity-60'>до {_esc(_fmt_dt_msk(exp))}</span> {btn}"
            f"</li>"
        )
    return f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-desktop text-primary mr-2" aria-hidden="true"></i>Браузерные сессии (24 ч + история 3 дня)</h3>
        <p class="text-sm opacity-80">Отзыв сессии отключает вход без повторного Telegram/GitHub и кода 2FA. История неактивных сессий хранится 3 дня.</p>
        <ul class="list-none p-0 m-0">{''.join(items)}</ul>
        <form method="post" action="/admin/profile/sessions/revoke-all" data-remna-confirm-msg="Отозвать все сессии кроме текущей?">
          <button type="submit" class="btn btn-outline btn-error btn-sm h-9 min-h-9">Отозвать все другие</button>
        </form>
      </div>
    </div>"""


def _web_admin_role_title(
    settings: Settings,
    *,
    telegram_id: object = None,
    github_login: str = "",
) -> str:
    try:
        tid = int(telegram_id) if telegram_id is not None else None
    except (TypeError, ValueError):
        tid = None
    if tid is not None and tid in settings.admin_telegram_ids:
        return "Администратор"
    gh = (github_login or "").strip()
    if gh and _admin_allowed_by_gh(gh):
        return "Администратор"
    return "Администратор"


def _login_last_account_html(hint: dict | object | None, *, role_title: str = "") -> str:
    if not isinstance(hint, dict) or not hint.get("label"):
        return ""
    label = _esc(str(hint.get("label") or ""))
    kind = str(hint.get("login_kind") or "telegram")
    avatar = str(hint.get("avatar_url") or "").strip()
    role = _esc((role_title or "Администратор").strip() or "Администратор")
    icon = (
        '<i class="fa-brands fa-telegram text-2xl text-[#229ED9]" aria-hidden="true"></i>'
        if kind == "telegram"
        else '<i class="fa-brands fa-github text-2xl" aria-hidden="true"></i>'
    )
    avatar_html = (
        f'<img src="{_esc_attr(avatar)}" alt="" class="h-12 w-12 rounded-full object-cover ring-2 ring-primary/40" />'
        if avatar
        else f'<span class="flex h-12 w-12 items-center justify-center rounded-full bg-base-200">{icon}</span>'
    )
    return f"""
    <form method="post" action="/admin/login/resume" class="w-full">
      <button type="submit" class="login-last-account-btn w-full rounded-xl border border-primary/25 bg-primary/5 p-4 text-left transition hover:border-primary/45 hover:bg-primary/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/50">
        <span class="flex items-center gap-3">
          {avatar_html}
          <span class="min-w-0 flex-1">
            <span class="block truncate font-semibold">{label}</span>
            <span class="block truncate text-sm opacity-70">{role}</span>
          </span>
          <i class="fa-solid fa-arrow-right-to-bracket shrink-0 text-lg opacity-50" aria-hidden="true"></i>
        </span>
      </button>
    </form>
    """


async def _linked_bot_user_for_admin(request: Request) -> User | None:
    """Пользователь бота по Telegram ID из сессии web-admin (приоритет Telegram)."""
    if not _is_logged(request):
        return None
    auth = _auth_data(request)
    try:
        raw_tid = auth.get("telegram_id")
        if raw_tid is None:
            raw_tid = auth.get("id")
        tid = int(raw_tid)
    except (TypeError, ValueError):
        return None
    async with await _session() as session:
        r = await session.execute(select(User).where(User.telegram_id == tid))
        return r.scalar_one_or_none()


def _set_wauth_telegram(
    request: Request,
    *,
    tid: int,
    label: str,
    avatar_url: str,
    username: str = "",
    github_login: str = "",
    github_avatar_url: str = "",
) -> None:
    request.session["wauth"] = {
        "kind": "telegram",
        "id": tid,
        "telegram_id": tid,
        "label": label,
        "avatar_url": avatar_url,
        "username": username,
        "github_login": github_login,
        "github_avatar_url": github_avatar_url,
    }


def _set_wauth_github(
    request: Request,
    *,
    login: str,
    label: str,
    avatar_url: str,
    telegram_id: int | None = None,
) -> None:
    payload: dict[str, object] = {
        "kind": "github",
        "login": login,
        "label": label,
        "avatar_url": avatar_url,
        "username": login,
    }
    if telegram_id is not None:
        payload["telegram_id"] = telegram_id
    request.session["wauth"] = payload


def _clear_pending_2fa(request: Request) -> None:
    request.session.pop("wauth_pending", None)
    request.session.pop("wauth_pending_user_id", None)
    request.session.pop("wauth_pending_ts", None)


def _totp_normalize_code(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _totp_qr_data_uri(*, secret: str, account_name: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name="Flux Network")
    return segno.make(uri).png_data_uri(scale=5)


async def _resolve_2fa_user_from_auth(session: AsyncSession, auth: dict) -> User | None:
    raw_tid = auth.get("telegram_id")
    if raw_tid is None:
        raw_tid = auth.get("id")
    try:
        tid = int(raw_tid) if raw_tid is not None else 0
    except (TypeError, ValueError):
        tid = 0
    if tid:
        return (
            await session.execute(select(User).where(User.telegram_id == tid).limit(1))
        ).scalar_one_or_none()
    gh_login = str(auth.get("login") or auth.get("username") or "").strip()
    if gh_login:
        return (
            await session.execute(
                select(User).where(func.lower(User.github_username) == gh_login.lower()).limit(1)
            )
        ).scalar_one_or_none()
    return None


async def _finalize_login_with_2fa(
    request: Request,
    *,
    success_redirect: str | None = None,
) -> RedirectResponse:
    auth = _auth_data(request)
    if not auth:
        return RedirectResponse("/admin/login", status_code=303)
    async with await _session() as session:
        user = await _resolve_2fa_user_from_auth(session, auth)
        if (
            user is not None
            and bool(user.web_admin_totp_enabled)
            and bool((user.web_admin_totp_secret or "").strip())
        ):
            request.session["wauth_pending"] = auth
            request.session["wauth_pending_user_id"] = int(user.id)
            request.session["wauth_pending_ts"] = int(time.time())
            request.session.pop("wauth", None)
            return RedirectResponse("/admin/login?totp=1", status_code=303)
        _clear_pending_2fa(request)
        settings = get_settings()
        from shared.services.admin_rbac_service import (
            can_access_web_admin,
            ensure_env_admin_records,
        )
        # Если пользователя нет в БД (суперадмин ещё не запускал бота),
        # для Telegram-суперадмина создаём запись автоматически.
        # Для GitHub и остальных — показываем подсказку.
        if user is None:
            raw_tid = auth.get("telegram_id") or auth.get("id")
            try:
                sa_tid = int(raw_tid) if raw_tid is not None else 0
            except (TypeError, ValueError):
                sa_tid = 0
            if sa_tid and settings.effective_superadmin_telegram_id is not None and sa_tid == int(
                settings.effective_superadmin_telegram_id
            ):
                import uuid as _uuid
                user = User(
                    telegram_id=sa_tid,
                    first_name=str(auth.get("label") or ""),
                    username=str(auth.get("username") or "") or None,
                    referral_code=_uuid.uuid4().hex[:16],
                )
                session.add(user)
                try:
                    await session.flush()
                except Exception:
                    await session.rollback()
                    user = (
                        await session.execute(select(User).where(User.telegram_id == sa_tid).limit(1))
                    ).scalar_one_or_none()
            else:
                request.session.clear()
                gh = str(auth.get("login") or auth.get("username") or "").strip()
                if gh and _admin_allowed_by_gh(gh):
                    msg = "Сначала выполните /start в Telegram-боте и привяжите GitHub в профиле."
                else:
                    msg = "Профиль пользователя не найден. Выполните /start в боте и войдите снова."
                return RedirectResponse("/admin/login?err=" + quote_plus(msg), status_code=303)
        if user is not None:
            await ensure_env_admin_records(session, settings, user)
            if not await can_access_web_admin(session, settings, user=user):
                request.session.clear()
                return RedirectResponse(
                    "/admin/login?err="
                    + quote_plus(
                        "Нет доступа к web-admin. Проверьте SUPERADMIN_TELEGRAM_ID / ADMIN_TELEGRAM_IDS или роль в «Администраторы»."
                    ),
                    status_code=303,
                )
            await session.commit()
            await _bind_web_admin_session(
                request,
                user=user,
                method_kind=str(request.session.get("wauth_login_kind") or auth.get("kind") or "web"),
                used_totp=False,
            )
    return RedirectResponse(_login_success_destination(request, explicit=success_redirect), status_code=303)


def _promo_reward_caption(promo: PromoCode) -> str:
    v = promo.value
    if promo.type in ("balance_rub", "bonus_rub"):
        return f"+{v} ₽"
    if promo.type == "discount_percent":
        return f"-{v}% на покупку"
    if promo.type == "extra_gb":
        return f"+{v} ГБ"
    if promo.type == "extra_devices":
        return f"+{v} устройств"
    if promo.type == "topup_bonus_percent":
        return f"+{v}%"
    if promo.type == "extra_days":
        return f"+{int(v)} дн."
    return f"+{v}"


_PROMO_TYPE_RU: dict[str, str] = {
    "discount_percent": "Скидка на тариф (%)",
    "balance_rub": "Деньги на баланс (₽)",
    "bonus_rub": "Бонус на баланс (₽, устар.)",
    "topup_bonus_percent": "% к первому пополнению",
    "extra_gb": "Гигабайты (устар.)",
    "extra_devices": "Устройства (устар.)",
    "extra_days": "Дни подписки",
}

_PROMO_TYPES_SELECTABLE = frozenset(
    {"discount_percent", "balance_rub", "topup_bonus_percent", "extra_days"}
)


def _promo_type_ru(t: str) -> str:
    """Человеко-читаемое название типа промокода для web-admin."""
    return _PROMO_TYPE_RU.get((t or "").strip(), t or "")


def _parse_promo_eligibility_form(
    require_no_active_subscription: str,
    require_no_paid_subscription_months: str,
) -> tuple[bool, int | None]:
    req_active = (require_no_active_subscription or "").strip() == "1"
    raw = (require_no_paid_subscription_months or "").strip()
    months: int | None = None
    if raw and raw not in ("-", "—"):
        if not raw.isdigit():
            raise ValueError("«Месяцев без покупки подписки» — целое число или пусто")
        months = int(raw)
        if months < 1:
            raise ValueError("«Месяцев без покупки» — минимум 1 или оставьте пусто")
    return req_active, months


def _promo_eligibility_summary(promo: PromoCode) -> str:
    parts: list[str] = []
    if bool(getattr(promo, "require_no_active_subscription", False)):
        parts.append("без активной подписки")
    m = getattr(promo, "require_no_paid_subscription_months", None)
    if m is not None and int(m) > 0:
        parts.append(f"не покупали подписку {int(m)} мес.")
    return ", ".join(parts) if parts else "без доп. условий"


def _web_admin_actor_label(request: Request) -> str:
    auth = request.session.get("wauth") or {}
    kind = str(auth.get("kind") or "").strip()
    if kind == "telegram":
        un = str(auth.get("username") or "").strip()
        if un:
            return "@" + un
        tid = auth.get("id") or auth.get("telegram_id")
        return f"tg:{tid}" if tid else "web-admin"
    if kind == "github":
        login = str(auth.get("login") or auth.get("username") or "").strip()
        return f"github:{login}" if login else "github"
    return "web-admin"


async def _web_admin_actor_user(session: AsyncSession, request: Request) -> User | None:
    auth = request.session.get("wauth") or {}
    raw_tg_id = auth.get("id") or auth.get("telegram_id")
    try:
        tg_id = int(raw_tg_id) if raw_tg_id is not None else 0
    except (TypeError, ValueError):
        tg_id = 0
    if tg_id <= 0:
        return None
    return (await session.execute(select(User).where(User.telegram_id == tg_id).limit(1))).scalar_one_or_none()


def _status_service_card(
    *,
    title: str,
    icon: str,
    ok: bool,
    detail: str,
    latency: str | None = None,
) -> str:
    badge = "badge-success" if ok else "badge-error"
    st = "Онлайн" if ok else "Ошибка"
    lat = f"<p class='text-xs opacity-60 mt-1'>{_esc(latency)}</p>" if latency else ""
    return f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg transition-all duration-200 hover:shadow-xl hover:border-primary/25">
      <div class="card-body gap-2">
        <div class="flex items-start justify-between gap-2">
          <h3 class="card-title text-base"><i class="{icon} text-primary mr-2" aria-hidden="true"></i>{_esc(title)}</h3>
          <span class="badge {badge} badge-sm">{st}</span>
        </div>
        <p class="text-sm opacity-90 break-words">{_esc(detail)}</p>
        {lat}
      </div>
    </div>"""


async def _telegram_bot_getme_status(
    client: httpx.AsyncClient,
    *,
    token: str,
    missing_token_msg: str,
    ok_fallback_msg: str,
) -> tuple[bool, str, str | None]:
    tok = (token or "").strip()
    if not tok:
        return (False, missing_token_msg, None)
    t0 = time.perf_counter()
    try:
        r = await client.get(f"https://api.telegram.org/bot{tok}/getMe")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        latency = f"Задержка: {ms} мс"
        if r.status_code == 200:
            try:
                j = r.json()
            except Exception:
                j = {}
            res = j.get("result") if isinstance(j, dict) else None
            if j.get("ok") and isinstance(res, dict):
                un = str(res.get("username") or "")
                return (True, f"@{un}" if un else ok_fallback_msg, latency)
            return (False, str(j)[:220], latency)
        return (False, f"HTTP {r.status_code}", latency)
    except Exception as e:
        return (False, str(e)[:220], None)


def _read_machine_metrics() -> dict[str, object]:
    total_mb = 0
    avail_mb = 0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            data = f.read()
        m_total = re.search(r"^MemTotal:\s+(\d+)\s+kB$", data, flags=re.MULTILINE)
        m_avail = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", data, flags=re.MULTILINE)
        if m_total:
            total_mb = int(m_total.group(1)) // 1024
        if m_avail:
            avail_mb = int(m_avail.group(1)) // 1024
    except Exception:
        total_mb = 0
        avail_mb = 0
    used_mb = max(total_mb - avail_mb, 0) if total_mb else 0
    ram_part = (
        f"RAM: {used_mb} / {total_mb} MiB"
        if total_mb
        else "RAM: недоступно"
    )
    ram_pct = round((used_mb / total_mb) * 100, 1) if total_mb > 0 else 0.0

    cpu_count = os.cpu_count() or 0
    load_text = ""
    load1 = 0.0
    try:
        l1, l5, l15 = os.getloadavg()
        load1 = float(l1)
        load_text = f" · load: {l1:.2f} / {l5:.2f} / {l15:.2f}"
    except Exception:
        load_text = ""
    cpu_part = f"CPU: логических ядер {cpu_count}" if cpu_count else "CPU: недоступно"
    cpu_load_pct = round(min(max((load1 / cpu_count) * 100, 0.0), 100.0), 1) if cpu_count > 0 else 0.0
    return {
        "ok": True,
        "ram_text": ram_part,
        "cpu_text": cpu_part + load_text,
        "ram_pct": ram_pct,
        "cpu_pct": cpu_load_pct,
    }


async def _detect_server_ips(request: Request) -> dict[str, object]:
    local_ip = "—"
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        pass

    direct_ip = "недоступно"
    direct_err: str | None = None
    try:
        async with httpx.AsyncClient(timeout=4.0, trust_env=False) as c:
            r = await c.get("https://api.ipify.org")
            if r.status_code == 200:
                direct_ip = (r.text or "").strip() or "недоступно"
            else:
                direct_err = f"HTTP {r.status_code}"
    except Exception as e:
        direct_err = str(e)[:120]

    has_proxy = bool(
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("ALL_PROXY")
        or os.getenv("all_proxy")
    )
    proxy_ip = "прокси не задан"
    if has_proxy:
        try:
            async with httpx.AsyncClient(timeout=6.0, trust_env=True) as c:
                r = await c.get("https://api.ipify.org")
                if r.status_code == 200:
                    proxy_ip = (r.text or "").strip() or "недоступно"
                else:
                    proxy_ip = f"ошибка HTTP {r.status_code}"
        except Exception as e:
            proxy_ip = f"ошибка: {str(e)[:120]}"

    hdr_forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    hdr_real = (request.headers.get("x-real-ip") or "").strip()
    client_host = request.client.host if request.client else ""
    via_proxy_hint = ""
    if hdr_forwarded or hdr_real:
        via_proxy_hint = f" · ingress: {hdr_real or hdr_forwarded.split(',')[0].strip()}"
    elif client_host:
        via_proxy_hint = f" · ingress: {client_host}"

    detail = f"Direct: {direct_ip} · Proxy: {proxy_ip} · Local: {local_ip}{via_proxy_hint}"
    ok = direct_ip != "недоступно"
    lat = f"Ошибка direct: {direct_err}" if direct_err else None
    return {
        "ok": ok,
        "detail": detail,
        "latency": lat,
        "direct_ok": direct_ip != "недоступно",
        "proxy_set": has_proxy,
        "proxy_ok": has_proxy and proxy_ip not in ("недоступно", "прокси не задан") and not proxy_ip.startswith("ошибка"),
    }


def _parse_date_any(raw: str) -> datetime | None:
    t = (raw or "").strip()
    if not t or t == "-":
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(t, fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    raise ValueError("Неверный формат даты")


def _fmt_expires(expires_at: datetime | None) -> str:
    if expires_at is None:
        return "∞"
    return expires_at.strftime("%d.%m.%Y")


def _promo_expires_date_input_value(expires_at: datetime | None) -> str:
    """Значение для <input type=\"date\"> (YYYY-MM-DD, UTC-календарный день)."""
    if expires_at is None:
        return ""
    exp = expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp.astimezone(UTC).date().isoformat()


def _promo_expires_from_form(expires_unlimited: str, expires_at_date: str) -> datetime | None:
    """Срок промокода: чекбокс «без срока» или дата из календаря; пустая дата без чекбокса = без срока."""
    ul = (expires_unlimited or "").strip().lower()
    if ul in ("1", "on", "true", "yes"):
        return None
    return _parse_date_any((expires_at_date or "").strip())


def _admin_allowed_by_tg(tg_id: int) -> bool:
    settings = get_settings()
    sid = settings.effective_superadmin_telegram_id
    if sid is not None and int(tg_id) == int(sid):
        return True
    return tg_id in settings.admin_telegram_ids


def _admin_allowed_by_gh(login: str) -> bool:
    allowed = get_settings().web_admin_github_logins
    return login.strip().casefold() in {x.casefold() for x in allowed}


def _user_avatar_photo_src(user: User) -> str:
    """Прокси аватара из Telegram Bot API; при ошибке загрузки <img> показывает инициалы."""
    return f"/admin/users/{user.id}/telegram-photo"


def _user_initial_badge(user: User) -> tuple[str, str]:
    raw = (user.first_name or user.username or str(user.telegram_id) or "?").strip()
    ch = raw[0] if raw else "?"
    if ch.isalpha():
        ch = ch.upper()
    elif not ch.isdigit():
        ch = "?"
    hue = (user.id * 47) % 360
    style = f"background:hsl({hue},42%,34%);color:#f0f2f8"
    return ch, style


def _subscription_list_badge(now: datetime, subs: list[Subscription]) -> tuple[str, str]:
    """Подпись и класс daisyUI badge для колонки «Подписка» в списке пользователей."""
    if not subs:
        return "Нет подписки", "badge-ghost"
    for s in subs:
        if s.status in ("active", "trial") and s.expires_at > now:
            if s.status == "trial":
                return "Триал", "badge-info"
            return "Активна", "badge-success"
    latest = max(subs, key=lambda x: x.expires_at)
    if latest.expires_at <= now or (latest.status or "").lower() == "expired":
        return "Истекла", "badge-error"
    if (latest.status or "").lower() == "cancelled":
        return "Отменена", "badge-warning"
    return "Неактивна", "badge-ghost"


def _active_subscription_devices_slots(now: datetime, subs: list[Subscription]) -> int | None:
    """Число слотов устройств (devices_count) у неистёкшей active/trial подписки — как логика бейджа в списке."""
    if not subs:
        return None
    for s in subs:
        if s.status in ("active", "trial") and s.expires_at > now:
            return int(s.devices_count)
    return None


def _avatar_with_fallback(user: User, *, px: int, ring_tw: str, ring_offset: str = "ring-offset-2") -> str:
    url = _user_avatar_photo_src(user)
    ch, st = _user_initial_badge(user)
    return (
        f"<span class=\"remna-admin-avatar-ring relative inline-flex shrink-0 items-center justify-center rounded-full p-0.5 ring-2 {ring_offset} ring-offset-base-100 {ring_tw}\">"
        f"<span class=\"relative flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-base-300\" "
        f"style=\"width:{px}px;height:{px}px;min-width:{px}px;min-height:{px}px\">"
        f"<img src=\"{_esc(url)}\" alt=\"\" width=\"{px}\" height=\"{px}\" class=\"h-full w-full object-cover remna-avatar-img\" loading=\"lazy\" decoding=\"async\" data-remna-avatar=\"1\" "
        "onerror=\"this.classList.add('hidden');this.nextElementSibling.classList.remove('hidden')\" />"
        f"<span class=\"hidden absolute inset-0 flex items-center justify-center text-sm font-bold leading-none\" "
        f'style="{st}">{_esc(ch)}</span></span></span>'
    )


def _copy_line(*, label: str, value: str, mono: bool = True) -> str:
    mcls = "font-mono text-xs sm:text-sm" if mono else "text-sm"
    dc = html.escape(value, quote=True)
    return (
        f"<div class='flex flex-wrap items-center gap-x-2 gap-y-1 py-0.5'>"
        f"<span class='text-sm opacity-70'>{_esc(label)}</span>"
        f"<span class='inline-flex max-w-full items-center gap-1 rounded-lg bg-base-300 px-2 py-1 {mcls}'>"
        f"<span class='break-all'>{_esc(value)}</span>"
        f"<button type='button' class='btn btn-ghost btn-xs h-7 min-h-7 w-7 min-w-7 shrink-0 p-0' data-copy=\"{dc}\" "
        f"title='Копировать' aria-label='Копировать'><i class='fa-regular fa-copy text-xs'></i></button></span></div>"
    )


def _telegram_profile_actions(user: User) -> str:
    parts: list[str] = []
    un = (user.username or "").strip().lstrip("@")
    if un:
        href = "https://t.me/" + url_quote(un, safe="")
        parts.append(
            f'<a class="btn btn-outline btn-sm h-9 min-h-9 gap-1.5 normal-case" href="{_esc(href)}" target="_blank" rel="noopener noreferrer">'
            '<i class="fa-brands fa-telegram" aria-hidden="true"></i> Профиль t.me</a>'
        )
    parts.append(
        f'<a class="btn btn-outline btn-sm h-9 min-h-9 gap-1.5 normal-case" href="tg://user?id={int(user.telegram_id)}">'
        '<i class="fa-brands fa-telegram" aria-hidden="true"></i> Открыть в приложении</a>'
    )
    return f"<div class=\"flex flex-wrap gap-2\">{''.join(parts)}</div>"


def _as_rw_user_profile(raw: object) -> dict | None:
    """GET users/{{uuid}} в разных версиях панели может вернуть не объект — иначе .get() даёт 500."""
    return raw if isinstance(raw, dict) else None


def _hwid_device_json_block(d: dict) -> str:
    try:
        raw = json.dumps(d, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        raw = str(d)
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return (
        "<button type=\"button\" class=\"btn btn-ghost btn-xs h-8 min-h-8 px-2 font-normal\" "
        "data-remna-open-hwid-json data-no-row-nav "
        f"data-json-b64=\"{_esc_attr(b64)}\">Подробнее</button>"
    )


def _verify_telegram_login(payload: dict[str, str], bot_token: str) -> bool:
    check_hash = payload.get("hash", "")
    if not check_hash:
        return False
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash" and v)
    secret = sha256(bot_token.encode("utf-8")).digest()
    calc_hash = hmac.new(secret, data_check.encode("utf-8"), sha256).hexdigest()
    return hmac.compare_digest(calc_hash, check_hash)


def _jwt_payload_unverified(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    padding = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode((payload_b64 + padding).encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


@router.post("/login/resume")
async def admin_login_resume(request: Request) -> RedirectResponse:
    hint = read_login_hint(request)
    if not isinstance(hint, dict) or not hint.get("label"):
        return RedirectResponse("/admin/login", status_code=303)
    return await _resume_login_from_hint(request, hint)


@router.get("/login")
async def admin_login_page(request: Request, link: str = "") -> HTMLResponse:
    _remember_admin_next_from_query(request)
    pending_2fa = isinstance(request.session.get("wauth_pending"), dict)
    show_2fa = pending_2fa or (request.query_params.get("totp") == "1")
    totp_err = request.query_params.get("err") == "totp"
    link_mode = (link or "").strip().lower() in {"1", "true", "yes", "bind"}
    if not link_mode and not show_2fa:
        restored = await _maybe_restore_browser_session(request)
        if restored is not None:
            return restored
    login_hint = read_login_hint(request) if not link_mode else None
    login_hint_role = ""
    if isinstance(login_hint, dict) and login_hint.get("label"):
        login_hint_role = _web_admin_role_title(
            get_settings(),
            telegram_id=login_hint.get("telegram_id"),
            github_login=str(login_hint.get("github_login") or ""),
        )
    if _is_logged(request) and not link_mode:
        uid_raw = request.session.get("wauth_user_id")
        token = str(request.session.get("wauth_session_token") or "").strip()
        exp_raw = request.session.get("wauth_session_exp")
        valid = False
        try:
            uid = int(uid_raw)
            exp = int(exp_raw)
        except (TypeError, ValueError):
            valid = False
        else:
            now = datetime.now(UTC)
            fp = browser_fingerprint(request)
            if token and exp > int(now.timestamp()):
                async with await _session() as session:
                    row = await try_get_browser_session(session, token=token, fingerprint_hash=fp)
                    if row is not None and int(row.user_id) == uid:
                        db_exp = row.expires_at
                        if db_exp.tzinfo is None:
                            db_exp = db_exp.replace(tzinfo=UTC)
                        valid = db_exp > now
        if not valid:
            _clear_web_admin_session(request)
        else:
            return RedirectResponse(_login_success_destination(request, explicit=None), status_code=303)
    tg_href = "/admin/login/telegram/start"
    if link_mode:
        tg_href += "?link=1"
    telegram_block = (
        f'<a class="btn gap-2 login-auth-btn login-auth-btn-telegram" href="{_esc(tg_href)}">'
        '<i class="fa-brands fa-telegram text-lg" aria-hidden="true"></i>'
        + ("Telegram" if link_mode else "Telegram")
        + "</a>"
    )
    login_notice = ""
    err = (request.query_params.get("err") or "").strip()
    if err == "telegram_login_config":
        login_notice = (
            "<div class='alert alert-warning text-sm'>"
            "<span>Для Telegram OAuth задайте PUBLIC_SITE_URL, WEB_ADMIN_TELEGRAM_CLIENT_ID, WEB_ADMIN_TELEGRAM_CLIENT_SECRET и WEB_ADMIN_TELEGRAM_REDIRECT_URI.</span>"
            "</div>"
        )
    github_href = "/admin/login/github/start"
    login_bg_url = _admin_background_image_url(get_settings())
    login_bg_css = (
        "background-image:\n"
        f"          linear-gradient(140deg, rgba(16,14,36,0.75), rgba(55,25,110,0.7)),\n"
        f"          url('{_esc(login_bg_url)}');"
        if login_bg_url
        else ""
    )
    if link_mode:
        github_href += "?mode=link"
    body = f"""
    <style>
      .remna-login-stage {{
        position: fixed;
        inset: 0;
        z-index: 0;
        overflow: hidden;
      }}
      .remna-login-bg {{
        position: absolute;
        inset: 0;
        background: linear-gradient(140deg, rgba(20,18,42,0.88), rgba(68,34,120,0.8));
      }}
      .remna-login-bg.has-image {{
        {login_bg_css}
        background-size: cover;
        background-position: center;
        background-repeat: no-repeat;
      }}
      .remna-login-particles {{
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        pointer-events: none;
      }}
      .remna-login-card {{
        position: relative;
        z-index: 1;
      }}
      .login-auth-btn {{
        width: 11.75rem;
        min-height: 2.65rem;
        height: 2.65rem;
        padding: 0 .95rem;
        justify-content: center;
        border-width: 0;
        box-shadow: var(--remna-anim-shadow);
        animation: remna-login-float 4.4s ease-in-out infinite;
        font-size: .95rem;
      }}
      .login-auth-btn-telegram {{
        background: #229ED9;
        color: #fff;
      }}
      .login-auth-btn-telegram:hover {{
        background: #1d8fc5;
        color: #fff;
      }}
      .login-auth-btn-github {{
        background: #181717;
        color: #fff;
      }}
      .login-auth-btn-github:hover {{
        background: #24292f;
        color: #fff;
      }}
      .login-auth-btn:nth-of-type(2) {{
        animation-delay: .35s;
      }}
      .login-auth-btn:hover {{
        transform: translateY(-2px) scale(1.01);
        box-shadow:
          0 16px 30px -16px color-mix(in oklab, var(--bc) 45%, transparent),
          0 0 16px color-mix(in oklab, var(--p) 40%, transparent);
        filter: saturate(1.08);
      }}
      .login-auth-btn i {{
        transition: transform .2s ease, filter .2s ease;
      }}
      .login-auth-btn:hover i {{
        transform: translateY(-.5px) scale(1.08);
        filter: drop-shadow(0 0 7px color-mix(in oklab, var(--p) 65%, transparent));
      }}
      @keyframes remna-login-float {{
        0% {{ transform: translateY(0); }}
        50% {{ transform: translateY(-1px); }}
        100% {{ transform: translateY(0); }}
      }}
    </style>
    <div class="remna-login-stage" aria-hidden="true">
      <div id="remna-login-bg" class="remna-login-bg"></div>
      <canvas id="remna-login-particles" class="remna-login-particles"></canvas>
    </div>
    <div class="remna-login-card card bg-base-100 w-full max-w-sm border border-base-content/10 shadow-2xl">
      <div class="card-body items-center gap-6 text-center">
        <h2 class="card-title justify-center text-2xl font-bold">
          <i class="fa-solid fa-right-to-bracket text-primary" aria-hidden="true"></i>
          <span>{'Привязка аккаунта' if link_mode else 'Вход'}</span>
        </h2>
        <div class="flex w-full flex-col items-center gap-4">
          {"<p class='text-sm opacity-70'>Свяжите GitHub и Telegram для единого админ-профиля. Приоритет у Telegram ID.</p>" if link_mode else ""}
          {login_notice}
          {_login_last_account_html(login_hint, role_title=login_hint_role) if not link_mode else ""}
          <div class="flex flex-wrap justify-center">{telegram_block}</div>
          <a class="btn gap-2 login-auth-btn login-auth-btn-github" href="{_esc(github_href)}">
            <i class="fa-brands fa-github text-lg" aria-hidden="true"></i>
            {"GitHub" if link_mode else "GitHub"}
          </a>
        </div>
      </div>
    </div>
    <div id="remna-login-2fa-overlay" class="fixed inset-0 z-[110] {'flex' if show_2fa else 'hidden'} items-center justify-center bg-base-content/45 backdrop-blur-sm p-4" role="dialog" aria-modal="true" aria-labelledby="remna-login-2fa-title">
      <div class="card bg-base-100 w-full max-w-md border border-base-content/10 shadow-2xl">
        <div class="card-body items-center gap-4 text-center">
          <h2 id="remna-login-2fa-title" class="card-title justify-center text-2xl font-bold">
            <i class="fa-solid fa-shield-halved text-primary" aria-hidden="true"></i>
            <span>Подтвердите вход</span>
          </h2>
          <p class="text-sm opacity-70">Введите 6-значный код из Google Authenticator.</p>
          {"<div class='alert alert-error'><span>Неверный код. Попробуйте снова.</span></div>" if totp_err else ""}
          <form method="post" action="/admin/login/2fa" class="flex w-full max-w-xs flex-col gap-3">
            <input
              type="text"
              name="code"
              inputmode="numeric"
              pattern="[0-9 ]{{6,8}}"
              maxlength="8"
              autocomplete="one-time-code"
              required
              class="input input-bordered text-center text-lg tracking-[0.35em]"
              placeholder="123456"
            />
            <button type="submit" class="btn btn-primary gap-2">
              <i class="fa-solid fa-check" aria-hidden="true"></i>Подтвердить
            </button>
          </form>
        </div>
      </div>
    </div>
    <script>
    (function(){{
      var bg=document.getElementById('remna-login-bg');
      if(bg){{
        var src={json.dumps(login_bg_url or "")};
        if(!src)return;
        var probe=new Image();
        probe.onload=function(){{bg.classList.add('has-image');}};
        probe.src=src;
      }}
      var c=document.getElementById('remna-login-particles');
      if(!c||!c.getContext)return;
      var ctx=c.getContext('2d');
      var parts=[];
      var w=0,h=0,last=0;
      function isDark(){{
        var t=(document.documentElement.getAttribute('data-theme')||'').toLowerCase();
        return t==='night'||t==='dark'||t==='black';
      }}
      function resize(){{
        w=window.innerWidth||1;h=window.innerHeight||1;
        c.width=Math.floor(w*window.devicePixelRatio);
        c.height=Math.floor(h*window.devicePixelRatio);
        c.style.width=w+'px';c.style.height=h+'px';
        ctx.setTransform(window.devicePixelRatio,0,0,window.devicePixelRatio,0,0);
      }}
      function spawn(){{
        parts=[];
        var n=Math.max(24,Math.floor((w*h)/42000));
        for(var i=0;i<n;i++){{
          parts.push({{
            x:Math.random()*w,
            y:Math.random()*h,
            r:Math.random()*2.4+0.8,
            vx:(Math.random()-.5)*0.24,
            vy:(Math.random()-.5)*0.24,
            a:Math.random()*0.45+0.12
          }});
        }}
      }}
      function tick(ts){{
        if(!last)last=ts;
        var dt=Math.min(33,ts-last)/16.6;last=ts;
        ctx.clearRect(0,0,w,h);
        var dark=isDark();
        ctx.fillStyle=dark?'rgba(255,255,255,.72)':'rgba(18,18,24,.34)';
        for(var i=0;i<parts.length;i++){{
          var p=parts[i];
          p.x+=p.vx*dt;p.y+=p.vy*dt;
          if(p.x<-6)p.x=w+6;if(p.x>w+6)p.x=-6;
          if(p.y<-6)p.y=h+6;if(p.y>h+6)p.y=-6;
          ctx.globalAlpha=p.a;
          ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,Math.PI*2);ctx.fill();
        }}
        ctx.globalAlpha=1;
        requestAnimationFrame(tick);
      }}
      resize();spawn();requestAnimationFrame(tick);
      window.addEventListener('resize',function(){{resize();spawn();}});
    }})();
    </script>
    """
    return _layout("Вход", body, request=request, show_nav=False)


@router.get("/login/background")
async def admin_login_background() -> Response:
    for p in _LOGIN_BG_CANDIDATES:
        if p.exists() and p.is_file():
            suffix = p.suffix.lower()
            media = "image/png" if suffix == ".png" else "image/jpeg"
            return FileResponse(path=str(p), media_type=media)
    return Response(status_code=404)


@router.get("/login/telegram/start")
async def admin_login_telegram_start(request: Request, link: str = "") -> RedirectResponse:
    settings = get_settings()
    client_id = (settings.web_admin_telegram_client_id or "").strip()
    client_secret = (settings.web_admin_telegram_client_secret or "").strip()
    redirect_uri = (settings.web_admin_telegram_redirect_uri or "").strip()
    if not client_id or not client_secret or not redirect_uri:
        return RedirectResponse("/admin/login?err=telegram_login_config", status_code=303)
    state = urlsafe_b64encode(token_urlsafe(24).encode("utf-8")).decode("ascii")[:40]
    mode = "link" if (link or "").strip().lower() in {"1", "true", "yes", "bind"} else "login"
    request.session["tg_oauth_state"] = state
    request.session["tg_oauth_mode"] = mode
    # New Telegram OIDC endpoint (oauth.tg.dev).
    url = (
        "https://oauth.tg.dev/auth"
        f"?client_id={quote_plus(client_id)}"
        f"&redirect_uri={quote_plus(redirect_uri)}"
        "&response_type=code"
        "&scope=openid%20profile"
        f"&state={quote_plus(state)}"
    )
    return RedirectResponse(url, status_code=303)


@router.get("/login/2fa")
async def admin_login_2fa_page(request: Request, err: str = "") -> HTMLResponse:
    if not isinstance(request.session.get("wauth_pending"), dict):
        return RedirectResponse("/admin/login", status_code=303)
    if (err or "").strip():
        return RedirectResponse("/admin/login?totp=1&err=totp", status_code=303)
    return RedirectResponse("/admin/login?totp=1", status_code=303)


@router.post("/login/2fa")
async def admin_login_2fa_submit(request: Request, code: str = Form("")) -> RedirectResponse:
    pending = request.session.get("wauth_pending")
    pending_uid = request.session.get("wauth_pending_user_id")
    if not isinstance(pending, dict) or pending_uid is None:
        return RedirectResponse("/admin/login", status_code=303)
    try:
        uid = int(pending_uid)
    except (TypeError, ValueError):
        _clear_pending_2fa(request)
        return RedirectResponse("/admin/login", status_code=303)
    otp = _totp_normalize_code(code)
    async with await _session() as session:
        user = await session.get(User, uid)
        secret = (user.web_admin_totp_secret or "").strip() if user is not None else ""
        enabled = bool(user is not None and user.web_admin_totp_enabled)
    if not secret or not enabled:
        _clear_pending_2fa(request)
        return RedirectResponse("/admin/login", status_code=303)
    if not pyotp.TOTP(secret).verify(otp, valid_window=1):
        return RedirectResponse("/admin/login?totp=1&err=totp", status_code=303)
    request.session["wauth"] = pending
    await _bind_web_admin_session(
        request,
        user=user,
        method_kind=str(request.session.get("wauth_login_kind") or pending.get("kind") or "web"),
        used_totp=True,
    )
    _clear_pending_2fa(request)
    return RedirectResponse(_login_success_destination(request, explicit=None), status_code=303)


@router.get("/login/telegram/widget")
async def admin_login_telegram_widget(
    request: Request,
    code: str = "",
    state: str = "",
    id: str = "",
    first_name: str = "",
    last_name: str = "",
    username: str = "",
    photo_url: str = "",
    auth_date: str = "",
    hash: str = "",
):
    settings = get_settings()
    # New Telegram OAuth/OpenID flow (authorization code).
    if (code or "").strip():
        if state != request.session.get("tg_oauth_state"):
            return RedirectResponse("/admin/login", status_code=303)
        client_id = (settings.web_admin_telegram_client_id or "").strip()
        client_secret = (settings.web_admin_telegram_client_secret or "").strip()
        base = (settings.public_site_url or "").strip().rstrip("/")
        redirect_uri = (settings.web_admin_telegram_redirect_uri or "").strip() or f"{base}/admin/login/telegram/widget"
        if not client_id or not client_secret or not redirect_uri:
            return RedirectResponse("/admin/login?err=telegram_login_config", status_code=303)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                t_data: dict | None = None
                last_err: Exception | None = None
                for token_url in ("https://oauth.tg.dev/token", "https://oauth.telegram.org/token"):
                    try:
                        t_resp = await client.post(
                            token_url,
                            headers={"Accept": "application/json"},
                            data={
                                "grant_type": "authorization_code",
                                "code": code.strip(),
                                "client_id": client_id,
                                "client_secret": client_secret,
                                "redirect_uri": redirect_uri,
                            },
                        )
                        t_resp.raise_for_status()
                        payload = t_resp.json()
                        if isinstance(payload, dict):
                            t_data = payload
                            break
                    except Exception as e:
                        last_err = e
                if t_data is None:
                    raise RuntimeError("telegram token exchange failed") from last_err
        except Exception:
            return RedirectResponse("/admin/login", status_code=303)
        tid = 0
        tg_username = ""
        tg_label = ""
        tg_photo = ""
        if isinstance(t_data, dict):
            id_token = str(t_data.get("id_token") or "").strip()
            if id_token:
                claims = _jwt_payload_unverified(id_token)
                try:
                    # Telegram OIDC may expose numeric user id in `id`,
                    # while `sub` can be a non-telegram internal subject string.
                    tid = int(claims.get("id") or claims.get("sub") or 0)
                except (TypeError, ValueError):
                    tid = 0
                tg_username = str(claims.get("preferred_username") or "").strip()
                tg_label = str(claims.get("name") or "").strip()
                tg_photo = str(claims.get("picture") or "").strip()
            if not tid:
                uobj = t_data.get("user")
                if isinstance(uobj, dict):
                    try:
                        tid = int(uobj.get("id") or 0)
                    except (TypeError, ValueError):
                        tid = 0
                    tg_username = tg_username or str(uobj.get("username") or "").strip()
                    tg_label = tg_label or str(uobj.get("first_name") or "").strip()
                    tg_photo = tg_photo or str(uobj.get("photo_url") or "").strip()
                else:
                    try:
                        tid = int(t_data.get("id") or 0)
                    except (TypeError, ValueError):
                        tid = 0
                    tg_username = tg_username or str(t_data.get("username") or "").strip()
                    tg_label = tg_label or str(t_data.get("first_name") or "").strip()
                    tg_photo = tg_photo or str(t_data.get("photo_url") or "").strip()
        request.session.pop("tg_oauth_state", None)
        if not tid:
            return RedirectResponse("/admin/login", status_code=303)
        # Проверяем: env-администратор ИЛИ запись в admin_users (DB-добавленный)
        if not _admin_allowed_by_tg(tid):
            from shared.models.user import User as _User
            from shared.services.admin_rbac_service import get_admin_user_by_user_id as _get_au
            _tg_allowed_via_db = False
            try:
                async with await _session() as _sess:
                    _db_u = (
                        await _sess.execute(
                            select(_User).where(_User.telegram_id == tid).limit(1)
                        )
                    ).scalar_one_or_none()
                    if _db_u is not None:
                        _au = await _get_au(_sess, _db_u.id)
                        _tg_allowed_via_db = _au is not None
            except Exception:
                pass
            if not _tg_allowed_via_db:
                return RedirectResponse("/admin/login", status_code=303)
        current = _auth_data(request)
        if str(current.get("kind") or "") == "github" and str(request.session.get("tg_oauth_mode") or "") == "link":
            request.session.pop("tg_oauth_mode", None)
            # Reuse existing merge/link flow by injecting payload-compatible auth.
            id = str(tid)
            first_name = tg_label
            username = tg_username
            photo_url = tg_photo
        else:
            request.session.pop("tg_oauth_mode", None)
            request.session["wauth_login_kind"] = "telegram"
            _set_wauth_telegram(
                request,
                tid=tid,
                label=tg_label or tg_username or f"tg:{tid}",
                avatar_url=tg_photo,
                username=tg_username,
            )
            return await _finalize_login_with_2fa(request)

    payload = {
        "id": id.strip(),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "username": username.strip(),
        "photo_url": photo_url.strip(),
        "auth_date": auth_date.strip(),
        "hash": hash.strip(),
    }
    if not payload["id"].isdigit():
        return RedirectResponse("/admin/login", status_code=303)
    if not _verify_telegram_login(payload, settings.bot_token):
        return RedirectResponse("/admin/login", status_code=303)
    tid = int(payload["id"])
    if not _admin_allowed_by_tg(tid):
        # Дополнительно проверяем наличие в admin_users (DB-добавленный администратор)
        _widget_allowed_via_db = False
        try:
            from shared.services.admin_rbac_service import get_admin_user_by_user_id as _get_au2
            async with await _session() as _sess2:
                _db_u2 = (
                    await _sess2.execute(select(User).where(User.telegram_id == tid).limit(1))
                ).scalar_one_or_none()
                if _db_u2 is not None:
                    _au2 = await _get_au2(_sess2, _db_u2.id)
                    _widget_allowed_via_db = _au2 is not None
        except Exception:
            pass
        if not _widget_allowed_via_db:
            return RedirectResponse("/admin/login", status_code=303)
    label = payload["first_name"] or payload["username"] or f"tg:{tid}"
    current = _auth_data(request)
    if str(current.get("kind") or "") == "github":
        gh_login = str(current.get("login") or current.get("username") or "").strip()
        gh_avatar = str(current.get("avatar_url") or "").strip()
        gh_id_raw = current.get("github_id")
        try:
            gh_id = int(gh_id_raw) if gh_id_raw is not None else None
        except (TypeError, ValueError):
            gh_id = None
        linked_ok = False
        if gh_login:
            async with await _session() as session:
                tg_user = (
                    await session.execute(select(User).where(User.telegram_id == tid).limit(1))
                ).scalar_one_or_none()
                if tg_user is not None:
                    conflict = (
                        await session.execute(
                            select(User.id)
                            .where(
                                User.id != tg_user.id,
                                func.lower(User.github_username) == gh_login.lower(),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if conflict is not None:
                        return RedirectResponse(
                            "/admin/profile?err="
                            + quote_plus("Этот GitHub уже привязан к другому Telegram-профилю."),
                            status_code=303,
                        )
                    tg_user.github_id = gh_id
                    tg_user.github_username = gh_login
                    tg_user.github_profile_url = f"https://github.com/{gh_login}"
                    await session.commit()
                    linked_ok = True
                else:
                    return RedirectResponse(
                        "/admin/profile?err="
                        + quote_plus("Пользователь бота не найден. Сначала выполните /start в Telegram-боте."),
                        status_code=303,
                    )
        _set_wauth_telegram(
            request,
            tid=tid,
            label=label,
            avatar_url=payload["photo_url"],
            username=payload["username"],
            github_login=gh_login,
            github_avatar_url=gh_avatar,
        )
        return RedirectResponse(
            "/admin/profile?n=linked_tg" if linked_ok else "/admin/profile",
            status_code=303,
        )
    _set_wauth_telegram(
        request,
        tid=tid,
        label=label,
        avatar_url=payload["photo_url"],
        username=payload["username"],
    )
    request.session["wauth_login_kind"] = "telegram"
    return await _finalize_login_with_2fa(request)


@router.get("/login/github/start")
async def admin_login_github_start(request: Request, mode: str = ""):
    settings = get_settings()
    if not settings.web_admin_github_client_id or not settings.web_admin_github_redirect_uri:
        return RedirectResponse("/admin/login", status_code=303)
    state = urlsafe_b64encode(token_urlsafe(24).encode("utf-8")).decode("ascii")[:40]
    request.session["gh_oauth_state"] = state
    request.session["gh_oauth_mode"] = "link" if (mode or "").strip().lower() == "link" else "login"
    url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={quote_plus(settings.web_admin_github_client_id)}"
        f"&redirect_uri={quote_plus(settings.web_admin_github_redirect_uri)}"
        f"&scope=read:user&state={quote_plus(state)}"
    )
    return RedirectResponse(url, status_code=303)


@router.get("/login/github/callback")
async def admin_login_github_callback(request: Request, code: str = "", state: str = ""):
    settings = get_settings()
    if state != request.session.get("gh_oauth_state"):
        return RedirectResponse("/admin/login", status_code=303)
    if not code or not settings.web_admin_github_client_id or not settings.web_admin_github_client_secret:
        return RedirectResponse("/admin/login", status_code=303)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.web_admin_github_client_id,
                    "client_secret": settings.web_admin_github_client_secret,
                    "code": code,
                    "redirect_uri": settings.web_admin_github_redirect_uri,
                },
            )
            token_resp.raise_for_status()
            token = token_resp.json().get("access_token")
            if not token:
                return RedirectResponse("/admin/login", status_code=303)
            me_resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            me_resp.raise_for_status()
            me = me_resp.json()
    except httpx.HTTPError:
        return RedirectResponse("/admin/login", status_code=303)
    login = str(me.get("login") or "").strip()
    if not login:
        return RedirectResponse("/admin/login", status_code=303)
    gh_avatar = str(me.get("avatar_url") or f"https://github.com/{login}.png")
    gh_label = str(me.get("name") or login)
    gh_id_raw = me.get("id")
    try:
        gh_id = int(gh_id_raw) if gh_id_raw is not None else None
    except (TypeError, ValueError):
        gh_id = None
    oauth_mode = str(request.session.get("gh_oauth_mode") or "login")
    request.session.pop("gh_oauth_mode", None)
    request.session.pop("gh_oauth_state", None)

    async with await _session() as session:
        linked_user = (
            await session.execute(
                select(User)
                .where(func.lower(User.github_username) == login.lower())
                .limit(1)
            )
        ).scalar_one_or_none()
        linked_tg_allowed = bool(linked_user is not None and _admin_allowed_by_tg(int(linked_user.telegram_id)))

        if oauth_mode == "link":
            current = _auth_data(request)
            try:
                tg_id = int(current.get("telegram_id") or current.get("id"))
            except (TypeError, ValueError):
                tg_id = 0
            if not tg_id or str(current.get("kind") or "") != "telegram":
                return RedirectResponse(
                    "/admin/profile?err="
                    + quote_plus("Для привязки GitHub сначала войдите через Telegram."),
                    status_code=303,
                )
            tg_user = (
                await session.execute(select(User).where(User.telegram_id == tg_id).limit(1))
            ).scalar_one_or_none()
            if tg_user is None:
                return RedirectResponse(
                    "/admin/profile?err="
                    + quote_plus("Пользователь бота не найден. Сначала выполните /start в Telegram-боте."),
                    status_code=303,
                )
            conflict = (
                await session.execute(
                    select(User.id)
                    .where(User.id != tg_user.id, func.lower(User.github_username) == login.lower())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if conflict is not None:
                return RedirectResponse(
                    "/admin/profile?err="
                    + quote_plus("Этот GitHub уже привязан к другому Telegram-профилю."),
                    status_code=303,
                )
            tg_user.github_id = gh_id
            tg_user.github_username = login
            tg_user.github_profile_url = f"https://github.com/{login}"
            await session.commit()
            _set_wauth_telegram(
                request,
                tid=tg_id,
                label=str(current.get("label") or f"tg:{tg_id}"),
                avatar_url=str(current.get("avatar_url") or ""),
                username=str(current.get("username") or ""),
                github_login=login,
                github_avatar_url=gh_avatar,
            )
            return RedirectResponse("/admin/profile?n=linked_gh", status_code=303)

    if not _admin_allowed_by_gh(login) and not linked_tg_allowed:
        return RedirectResponse("/admin/login", status_code=303)
    if linked_user is not None and _admin_allowed_by_tg(int(linked_user.telegram_id)):
        request.session["wauth_login_kind"] = "github"
        tg_label = str(linked_user.first_name or linked_user.username or f"tg:{linked_user.telegram_id}")
        _set_wauth_telegram(
            request,
            tid=int(linked_user.telegram_id),
            label=tg_label,
            avatar_url=gh_avatar,
            username=str(linked_user.username or ""),
            github_login=login,
            github_avatar_url=gh_avatar,
        )
        return await _finalize_login_with_2fa(request)
    _set_wauth_github(
        request,
        login=login,
        label=gh_label,
        avatar_url=gh_avatar,
        telegram_id=int(linked_user.telegram_id) if linked_user is not None else None,
    )
    request.session["wauth_login_kind"] = "github"
    request.session["wauth"]["github_id"] = gh_id
    return await _finalize_login_with_2fa(request)


@router.post("/logout")
async def admin_logout(request: Request):
    uid_raw = request.session.get("wauth_user_id")
    token = str(request.session.get("wauth_session_token") or "").strip()
    wauth = request.session.get("wauth")
    try:
        uid = int(uid_raw)
    except (TypeError, ValueError):
        uid = 0
    hint_payload: dict | None = None
    if uid and isinstance(wauth, dict):
        async with await _session() as session:
            user = await session.get(User, uid)
            if user is not None:
                if token:
                    await revoke_browser_session(session, token=token)
                user.web_admin_session_token = None
                user.web_admin_session_expires_at = None
                await session.commit()
                kind = str(wauth.get("kind") or request.session.get("wauth_login_kind") or "telegram")
                label = str(wauth.get("label") or user.username or user.first_name or str(user.telegram_id))
                tid = wauth.get("telegram_id") or wauth.get("id") or user.telegram_id
                try:
                    tid_i = int(tid)
                except (TypeError, ValueError):
                    tid_i = int(user.telegram_id)
                hint_payload = login_hint_payload(
                    user_id=user.id,
                    login_kind=kind,
                    label=label,
                    avatar_url=str(wauth.get("avatar_url") or ""),
                    telegram_id=tid_i,
                    github_login=str(wauth.get("github_login") or wauth.get("login") or user.github_username or ""),
                )
    _clear_web_admin_session(request)
    resp = RedirectResponse("/admin/login", status_code=303)
    if hint_payload:
        set_login_hint_cookie(resp, hint_payload)
    else:
        clear_login_hint_cookie(resp)
    return resp


async def _admin_broadcast_job(
    text: str,
    *,
    send_users: bool = True,
    send_channel: bool = False,
    channel_id: int | None = None,
    media_type: str | None = None,
    media_file_id: str | None = None,
) -> None:
    import logging

    from aiogram import Bot

    from shared.services.broadcast_service import broadcast_to_users, send_broadcast_to_channel

    log = logging.getLogger("api.broadcast")
    settings = get_settings()
    tok = (settings.bot_token or "").strip()
    if not tok:
        log.error("фоновая рассылка: BOT_TOKEN пуст — пропуск")
        return
    draft = (text or "").strip()
    try:
        log.info(
            "фоновая рассылка из web-admin: длина текста=%s симв., users=%s channel=%s media=%s",
            len(draft),
            send_users,
            send_channel,
            media_type,
        )
        ok = failed = 0
        ch_ok = False
        async with Bot(token=tok) as bot:
            if send_users:
                ok, failed = await broadcast_to_users(
                    bot, draft, media_type=media_type, media_file_id=media_file_id
                )
            if send_channel and channel_id:
                ch_ok = await send_broadcast_to_channel(
                    bot, draft, chat_id=int(channel_id),
                    media_type=media_type, media_file_id=media_file_id,
                )
        if send_users:
            await save_broadcast_history(
                body_draft=draft,
                recipients_ok=ok,
                recipients_failed=failed,
                source="mass",
                media_type=media_type,
                media_file_id=media_file_id,
            )
        if send_channel:
            await save_broadcast_history(
                body_draft=draft,
                recipients_ok=1 if ch_ok else 0,
                recipients_failed=0 if ch_ok else 1,
                source="channel",
                media_type=media_type,
                media_file_id=media_file_id,
            )
        # Удаляем локальный файл после отправки
        if media_file_id:
            from shared.services.broadcast_service import delete_broadcast_media_file
            delete_broadcast_media_file(media_file_id)
        log.info(
            "фоновая рассылка завершена: users ok=%s fail=%s channel_ok=%s",
            ok,
            failed,
            ch_ok,
        )
    except Exception:
        log.exception("фоновая рассылка: необработанная ошибка")


def _broadcast_tpl_meta_b64(*, tpl_id: int, title: str, body: str) -> str:
    payload = json.dumps({"id": tpl_id, "title": title, "body": body}, ensure_ascii=False)
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _sched_meta_b64(row: ScheduledBroadcast) -> str:
    when = row.scheduled_at
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    iso = when.isoformat() if when else ""
    payload = json.dumps(
        {
            "id": int(row.id),
            "body": row.body_text or "",
            "scheduled_at_utc": iso,
            "send_to_users": bool(getattr(row, "send_to_users", True)),
            "send_to_channel": bool(getattr(row, "send_to_channel", False)),
            "media_type": row.media_type or "",
            "media_file_id": row.media_file_id or "",
        },
        ensure_ascii=False,
    )
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _history_meta_b64(row: BroadcastHistory) -> str:
    payload = json.dumps(
        {
            "id": int(row.id),
            "body": row.body_text or "",
            "ok": int(row.recipients_ok),
            "fail": int(row.recipients_failed),
            "media_type": row.media_type or "",
            "media_file_id": row.media_file_id or "",
        },
        ensure_ascii=False,
    )
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


@router.get("/broadcast")
async def admin_broadcast_page(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    bc_settings = get_settings()
    bc_ch_id = getattr(bc_settings, "broadcast_main_channel_id", None)
    bc_ch_hint = (
        f"<p class='text-xs opacity-60'>Канал по умолчанию: <code class='bg-base-300 px-1 rounded'>{bc_ch_id}</code> "
        "(переменная <code class='bg-base-300 px-1 rounded'>BROADCAST_MAIN_CHANNEL_ID</code>).</p>"
        if bc_ch_id
        else "<p class='text-xs text-warning'>Канал не задан в конфиге — отметка «В канал» не сработает, пока не задан BROADCAST_MAIN_CHANNEL_ID.</p>"
    )
    sp = request.query_params
    alert = ""
    if sp.get("started") == "1":
        alert = (
            "<div class='alert alert-success mb-4'><span>Рассылка поставлена в очередь на фоновую отправку. "
            "Результат смотрите в логах API.</span></div>"
        )
    err = (sp.get("err") or "").strip()
    if err == "empty":
        alert = "<div class='alert alert-warning mb-4'><span>Введите текст сообщения.</span></div>"
    elif err == "no_bot_token":
        alert = "<div class='alert alert-error mb-4'><span>BOT_TOKEN не задан — рассылка невозможна.</span></div>"
    elif err == "test_no_tg":
        alert = "<div class='alert alert-warning mb-4'><span>Нет Telegram ID в сессии: войдите через Telegram или провьте профиль.</span></div>"
    elif err == "schedule_bad_time":
        alert = "<div class='alert alert-error mb-4'><span>Неверная дата или время отложенной отправки.</span></div>"
    elif err == "no_targets":
        alert = "<div class='alert alert-warning mb-4'><span>Выберите хотя бы одну цель: пользователям из БД или канал.</span></div>"
    elif err == "no_channel":
        alert = "<div class='alert alert-error mb-4'><span>В настройках не задан ID канала для рассылки (BROADCAST_MAIN_CHANNEL_ID).</span></div>"
    elif err == "template_bad":
        alert = ""
    elif err == "test_fail":
        alert = ""

    tpl_cards: list[str] = []
    pending_cards: list[str] = []
    hist_cards: list[str] = []
    async with await _session() as session:
        tpl_rows = (
            await session.execute(
                select(BroadcastTemplate).order_by(BroadcastTemplate.sort_order.asc(), BroadcastTemplate.id.asc())
            )
        ).scalars().all()
        pend_rows = (
            await session.execute(
                select(ScheduledBroadcast)
                .where(ScheduledBroadcast.status == "pending")
                .order_by(ScheduledBroadcast.scheduled_at.asc())
                .limit(25)
            )
        ).scalars().all()
        hist_rows = (
            await session.execute(select(BroadcastHistory).order_by(desc(BroadcastHistory.sent_at)).limit(40))
        ).scalars().all()

    for t in tpl_rows:
        meta_b64 = _broadcast_tpl_meta_b64(tpl_id=int(t.id), title=t.title, body=t.body)
        prev_html = broadcast_html_preview_fragment(t.body)
        tpl_cards.append(
            f"""
      <div class="card bg-base-200/80 border border-base-content/10 rounded-2xl p-3 flex flex-col gap-2">
        <div class="flex items-start justify-between gap-2">
          <span class="font-semibold text-sm">{_esc(t.title)}</span>
          <span class="text-[10px] opacity-50">#{int(t.id)}</span>
        </div>
        <div class="rounded-2xl border border-white/10 bg-[#2b5278] px-3 py-2 text-sm text-white shadow max-w-[min(100%,280px)] break-words">{prev_html}</div>
        <div class="flex flex-wrap gap-1 pt-1">
          <button type="button" class="btn btn-primary btn-xs bc-tpl-use" data-b64tpl="{_esc(meta_b64)}">В поле ввода</button>
          <button type="button" class="btn btn-ghost btn-xs bc-tpl-edit" data-b64tpl="{_esc(meta_b64)}">Правка</button>
          <form method="post" action="/admin/broadcast/template/{int(t.id)}/delete" class="inline" data-remna-confirm-msg="Удалить шаблон?">
            <button type="submit" class="btn btn-ghost btn-xs text-error">Удалить</button>
          </form>
        </div>
      </div>"""
        )
    for j in pend_rows:
        when = j.scheduled_at
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when_s = _fmt_dt_msk(when)
        else:
            when_s = "—"
        smeta = _sched_meta_b64(j)
        prev_p = broadcast_html_preview_fragment(j.body_text or "")
        parts_tgt: list[str] = []
        if getattr(j, "send_to_users", True):
            parts_tgt.append("БД")
        if getattr(j, "send_to_channel", False):
            parts_tgt.append("канал")
        tgt_s = " + ".join(parts_tgt) if parts_tgt else "—"
        j_media_badge = (
            f'<span class="badge badge-sm badge-outline opacity-70">📎 {_esc(j.media_type or "")}</span>'
            if j.media_type else ""
        )
        pending_cards.append(
            f"""
      <div class="card bg-base-200/80 border border-base-content/10 rounded-2xl p-3 flex flex-col gap-2">
        <div class="flex items-start justify-between gap-2">
          <span class="text-xs opacity-80">{_esc(when_s)}</span>
          <span class="text-[10px] opacity-50">#{int(j.id)}</span>
        </div>
        <div class="text-[10px] opacity-70 flex flex-wrap gap-2 items-center">Куда: {_esc(tgt_s)}{' ' + j_media_badge if j_media_badge else ''}</div>
        <div class="rounded-2xl border border-white/10 bg-[#2b5278] px-3 py-2 text-sm text-white shadow max-w-[min(100%,280px)] break-words">{prev_p}</div>
        <div class="flex flex-wrap gap-1 pt-1">
          <button type="button" class="btn btn-primary btn-xs bc-pend-use" data-b64sched="{_esc(smeta)}">В поле ввода</button>
          <button type="button" class="btn btn-ghost btn-xs bc-pend-edit" data-b64sched="{_esc(smeta)}">Правка</button>
          <form method="post" action="/admin/broadcast/schedule/{int(j.id)}/delete" class="inline" data-remna-confirm-msg="Удалить из очереди?">
            <button type="submit" class="btn btn-ghost btn-xs text-error">Удалить</button>
          </form>
        </div>
      </div>"""
        )
    for h in hist_rows:
        hm = _history_meta_b64(h)
        prev_h = broadcast_html_preview_fragment(h.body_text or "")
        when_h = _fmt_dt_msk(h.sent_at)
        src_l = _esc((h.source or "mass")[:16])
        h_media_badge = (
            f'<span class="badge badge-sm badge-outline opacity-70">📎 {_esc(h.media_type or "")}</span>'
            if h.media_type else ""
        )
        hist_cards.append(
            f"""
      <div class="card bg-base-200/60 border border-base-content/10 rounded-2xl p-3 flex flex-col gap-2">
        <div class="flex items-start justify-between gap-2 text-xs opacity-80">
          <span>{_esc(when_h)} · {src_l}{' ' + h_media_badge if h_media_badge else ''}</span>
          <span class="opacity-70">✓{int(h.recipients_ok)} / ✗{int(h.recipients_failed)}</span>
        </div>
        <div class="rounded-2xl border border-white/10 bg-[#2b5278] px-3 py-2 text-sm text-white shadow max-w-[min(100%,280px)] break-words">{prev_h}</div>
        <div class="flex flex-wrap gap-1 pt-1">
          <button type="button" class="btn btn-primary btn-xs bc-hist-use" data-b64hist="{_esc(hm)}">В поле ввода</button>
        </div>
      </div>"""
        )

    tpl_block = "".join(tpl_cards) or "<p class='text-sm opacity-50'>Шаблонов пока нет — создайте первый ниже.</p>"
    pend_block = (
        "".join(pending_cards)
        if pending_cards
        else "<p class='text-sm opacity-50'>Нет запланированных отправок.</p>"
    )
    hist_block = (
        "".join(hist_cards)
        if hist_cards
        else "<p class='text-sm opacity-50'>История появится после первой отправки всем или по расписанию.</p>"
    )

    body = f"""
    <div class="mx-auto w-full max-w-6xl px-2">
      <div class="grid gap-6 lg:grid-cols-2 items-start">
        <div class="card bg-base-100 border border-base-content/10 shadow-lg">
          <div class="card-body gap-4">
            <h2 class="card-title text-2xl"><i class="fa-solid fa-bullhorn text-primary mr-2" aria-hidden="true"></i>Рассылка в Telegram</h2>
            <p class="text-sm opacity-80 leading-relaxed">Отправка всем из БД. Формат: <strong>MarkdownV2</strong> (как в Telegram Bot API): жирный <code class="bg-base-300 px-1 rounded text-xs">**</code> или <code class="bg-base-300 px-1 rounded text-xs">*текст*</code>, курсив <code class="bg-base-300 px-1 rounded text-xs">_курсив_</code>, подчёркнутый <code class="bg-base-300 px-1 rounded text-xs">__текст__</code>, зачёркнутый <code class="bg-base-300 px-1 rounded text-xs">~~</code>/<code class="bg-base-300 px-1 rounded text-xs">~</code>, моно <code class="bg-base-300 px-1 rounded text-xs">`код`</code>, блок <code class="bg-base-300 px-1 rounded text-xs">```</code>, ссылка <code class="bg-base-300 px-1 rounded text-xs">[текст](url)</code>, спойлер <code class="bg-base-300 px-1 rounded text-xs">||текст||</code>, цитата строкой с <code class="bg-base-300 px-1 rounded text-xs">&gt;</code>.</p>
            <p class="text-xs opacity-70">Предпросмотр ниже повторяет переносы строк и разметку; в Telegram уйдёт сконвертированный MarkdownV2.</p>
            {bc_ch_hint}
            <textarea name="text" id="bc-text" form="bc-send" class="textarea textarea-bordered min-h-[220px] w-full font-mono text-sm" placeholder="Текст рассылки..." required></textarea>
            <div class="flex flex-wrap gap-1 items-center">
              <span class="text-xs opacity-60 w-full">Вставки:</span>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="**текст**">**жирный**</button>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="*текст*">*жирный*</button>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="_курсив_">_курсив_</button>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="__подчёрк__">__подчёрк__</button>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="~~зачёрк~~">~~зачёрк~~</button>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="~зачёрк~">~зачёрк~</button>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="||спойлер||">||спойлер||</button>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="`код`">`код`</button>
              <button type="button" class="btn btn-ghost btn-xs" id="bc-ins-pre" title="Блок кода">```блок```</button>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="[подпись](https://example.com)">ссылка</button>
              <button type="button" class="btn btn-ghost btn-xs" data-bc-ins="&#10;&gt; цитата">цитата</button>
              <button type="button" class="btn btn-ghost btn-xs" id="bc-ins-date">дата/время</button>
            </div>
            <div id="bc-live-prev" class="rounded-xl border border-base-content/10 bg-base-200/50 p-3 text-sm">
              <div class="text-xs opacity-60 mb-2">Предпросмотр (переносы строк как при отправке)</div>
              <div class="rounded-2xl border border-base-content/20 block w-full max-w-full bg-[#2b5278] text-white shadow text-left overflow-hidden">
                <div id="bc-live-media-row" class="hidden">
                  <img id="bc-live-media-img" src="" alt="" class="hidden w-full max-h-48 object-cover" />
                  <div id="bc-live-media-doc" class="hidden flex items-center gap-2 bg-white/10 px-3 py-2 text-xs"><i class="fa-solid fa-file-lines" aria-hidden="true"></i><span id="bc-live-media-doc-name" class="truncate"></span></div>
                </div>
                <div class="px-3 py-2" id="bc-live-prev-inner"><span class="opacity-70">Начните ввод…</span></div>
              </div>
            </div>
            <div class="flex flex-col gap-2 rounded-xl border border-base-content/10 bg-base-200/30 p-3">
              <span class="text-xs font-medium opacity-80">Куда отправить</span>
              <label class="label cursor-pointer justify-start gap-3 py-1">
                <input type="checkbox" name="send_users" id="bc-cb-users" form="bc-send" value="1" class="checkbox checkbox-sm" checked />
                <span class="label-text text-sm">Всем пользователям из базы (не заблокированным)</span>
              </label>
              <label class="label cursor-pointer justify-start gap-3 py-1">
                <input type="checkbox" name="send_channel" id="bc-cb-channel" form="bc-send" value="1" class="checkbox checkbox-sm" />
                <span class="label-text text-sm">В главный канал (ID из конфига)</span>
              </label>
            </div>
            <div class="flex flex-col gap-2 rounded-xl border border-base-content/10 bg-base-200/30 p-3">
              <span class="text-xs font-medium opacity-80">Вложение (фото или документ)</span>
              <div class="flex flex-wrap gap-2 items-center">
                <label class="btn btn-ghost btn-sm gap-1.5 cursor-pointer">
                  <i class="fa-solid fa-paperclip" aria-hidden="true"></i>Прикрепить файл
                  <input type="file" id="bc-media-file" class="hidden" accept="image/*,.pdf,.txt,.doc,.docx,.xls,.xlsx,.csv,.ppt,.pptx,.json,.xml,.zip,.rar,.tar,.gz,.tgz,.bz2,.xz,.7z,.zst,.tar.gz,.tar.bz2,.tar.xz" />
                </label>
                <button type="button" id="bc-media-clear" class="btn btn-ghost btn-sm text-error hidden">✕ Убрать</button>
              </div>
              <div id="bc-media-preview" class="hidden flex items-center gap-2 text-sm">
                <img id="bc-media-img-prev" src="" alt="" class="hidden max-h-16 rounded-lg border border-base-content/20" />
                <span id="bc-media-name" class="opacity-80 text-xs truncate max-w-xs"></span>
                <span id="bc-media-type-label" class="badge badge-sm badge-outline opacity-60"></span>
              </div>
              <div id="bc-media-uploading" class="hidden text-xs opacity-60">Загрузка файла…</div>
              <div id="bc-media-err" class="hidden text-xs text-error"></div>
            </div>
            <div class="flex flex-wrap gap-2">
              <form id="bc-send" method="post" action="/admin/broadcast" class="inline flex flex-wrap items-center gap-2">
                <input type="hidden" name="media_type" id="bc-media-type-h" value="" />
                <input type="hidden" name="media_file_id" id="bc-media-fid-h" value="" />
                <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5"><i class="fa-solid fa-paper-plane" aria-hidden="true"></i>Отправить (в фоне)</button>
              </form>
              <form method="post" action="/admin/broadcast/test" id="bc-test-f" class="inline">
                <input type="hidden" name="text" id="bc-test-hidden" value="" />
                <input type="hidden" name="media_type" id="bc-test-mt-h" value="" />
                <input type="hidden" name="media_file_id" id="bc-test-fid-h" value="" />
                <button type="submit" class="btn btn-secondary btn-sm h-9 min-h-9 gap-1.5"><i class="fa-solid fa-vial" aria-hidden="true"></i>Тест себе</button>
              </form>
            </div>
            <form method="post" action="/admin/broadcast/schedule" id="bc-sched-f" class="flex flex-col gap-2 rounded-xl border border-base-content/10 bg-base-200/30 p-3">
              <span class="text-sm font-medium">Отправка по времени</span>
              <input type="hidden" name="text" id="bc-sched-body" value="" />
              <input type="hidden" name="send_users" id="bc-sched-h-su" value="1" />
              <input type="hidden" name="send_channel" id="bc-sched-h-sc" value="" />
              <input type="hidden" name="media_type" id="bc-sched-h-mt" value="" />
              <input type="hidden" name="media_file_id" id="bc-sched-h-fid" value="" />
              <input type="hidden" name="scheduled_at_utc" id="bc-sched-utc" value="" />
              <input type="datetime-local" name="scheduled_at_local" id="bc-sched-local" class="input input-bordered input-sm w-full max-w-xs" required />
              <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9 w-fit gap-1.5"><i class="fa-solid fa-clock" aria-hidden="true"></i>Запланировать</button>
              <span class="text-xs opacity-60">Время берётся из календаря браузера и переводится в UTC автоматически.</span>
            </form>
          </div>
        </div>
        <div class="flex flex-col gap-4">
          <div class="card bg-base-100 border border-base-content/10 shadow-lg">
            <div class="card-body gap-3">
              <h3 class="font-semibold text-lg">Шаблоны на сервере</h3>
              <p class="text-xs opacity-70">Единые для всех браузеров и устройств. «В поле ввода» подставляет текст слева.</p>
              <div class="grid gap-3 max-h-[480px] overflow-y-auto pr-1">{tpl_block}</div>
              <form method="post" action="/admin/broadcast/template" class="flex flex-col gap-2 border-t border-base-content/10 pt-3">
                <span class="text-sm font-medium">Новый шаблон</span>
                <input name="title" class="input input-bordered input-sm" placeholder="Название" maxlength="160" />
                <textarea name="tpl_body" class="textarea textarea-bordered textarea-sm min-h-[90px]" placeholder="Текст шаблона"></textarea>
                <button type="submit" class="btn btn-primary btn-sm w-fit gap-1.5"><i class="fa-solid fa-plus" aria-hidden="true"></i>Сохранить шаблон</button>
              </form>
            </div>
          </div>
          <div class="card bg-base-100 border border-base-content/10 shadow-lg">
            <div class="card-body gap-3">
              <h3 class="font-semibold">Очередь отложенных</h3>
              <p class="text-xs opacity-70">Редактирование и удаление только для статуса «ожидает».</p>
              <div class="grid gap-3 max-h-[320px] overflow-y-auto pr-1">{pend_block}</div>
            </div>
          </div>
          <div class="card bg-base-100 border border-base-content/10 shadow-lg">
            <div class="card-body gap-3">
              <h3 class="font-semibold">История отправлений</h3>
              <p class="text-xs opacity-70">Последние рассылки; «В поле ввода» — изменить текст и отправить снова.</p>
              <div class="grid gap-3 max-h-[360px] overflow-y-auto pr-1">{hist_block}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <dialog id="bc-tpl-modal" class="modal">
      <div class="modal-box w-[min(96vw,1100px)] max-w-[1100px] max-h-[86vh] overflow-y-auto">
        <form method="dialog"><button class="btn btn-sm btn-circle btn-ghost absolute right-2 top-2">✕</button></form>
        <h3 class="font-bold text-lg mb-2">Редактирование шаблона</h3>
        <form method="post" id="bc-tpl-edit-form" action="/admin/broadcast/template/0/edit" class="grid gap-4 lg:grid-cols-[1.1fr_0.9fr]" data-no-loading>
          <input type="hidden" name="tpl_id" id="bc-tpl-edit-id" value="0" />
          <div class="flex min-h-[520px] flex-col gap-2">
            <label class="form-control"><span class="label-text text-xs">Название</span>
              <input name="title" id="bc-tpl-edit-title" class="input input-bordered input-sm" maxlength="160" /></label>
            <label class="form-control flex-1"><span class="label-text text-xs">Текст</span>
              <textarea name="tpl_body" id="bc-tpl-edit-body" class="textarea textarea-bordered min-h-[420px] flex-1 font-mono text-sm"></textarea></label>
            <button type="submit" class="btn btn-primary btn-sm w-fit">Сохранить</button>
          </div>
          <div class="flex min-h-[520px] flex-col gap-2">
            <span class="label-text text-xs opacity-80">Превью</span>
            <div class="flex-1 rounded-xl border border-base-content/10 bg-base-200/40 p-3">
              <div class="rounded-2xl border border-white/10 bg-[#2b5278] px-3 py-2 text-sm text-white shadow">
                <div id="bc-tpl-edit-preview" class="whitespace-pre-wrap break-words text-left"><span class="opacity-70">Начните ввод…</span></div>
              </div>
            </div>
          </div>
        </form>
      </div>
      <form method="dialog" class="modal-backdrop"><button>close</button></form>
    </dialog>
    <dialog id="bc-sched-modal" class="modal">
      <div class="modal-box max-w-lg">
        <form method="dialog"><button class="btn btn-sm btn-circle btn-ghost absolute right-2 top-2">✕</button></form>
        <h3 class="font-bold text-lg mb-2">Отложенная отправка</h3>
        <form method="post" id="bc-sched-edit-form" action="/admin/broadcast/schedule/0/edit" class="flex flex-col gap-2">
          <input type="hidden" name="scheduled_at_utc" id="bc-sched-edit-utc" value="" />
          <input type="hidden" name="media_type" id="bc-sched-edit-mt" value="" />
          <input type="hidden" name="media_file_id" id="bc-sched-edit-fid" value="" />
          <label class="form-control"><span class="label-text text-xs">Текст сообщения</span>
            <textarea name="tpl_body" id="bc-sched-edit-body" class="textarea textarea-bordered min-h-[160px] font-mono text-sm"></textarea></label>
          <label class="form-control"><span class="label-text text-xs">Когда отправить</span>
            <input type="datetime-local" id="bc-sched-edit-local" class="input input-bordered input-sm" required /></label>
          <label class="label cursor-pointer justify-start gap-2">
            <input type="checkbox" name="send_users" id="bc-sched-edit-su" value="1" class="checkbox checkbox-sm" checked />
            <span class="label-text text-xs">Пользователям из БД</span>
          </label>
          <label class="label cursor-pointer justify-start gap-2">
            <input type="checkbox" name="send_channel" id="bc-sched-edit-sc" value="1" class="checkbox checkbox-sm" />
            <span class="label-text text-xs">В канал</span>
          </label>
          <div id="bc-sched-edit-media-info" class="hidden text-xs opacity-70"></div>
          <button type="submit" class="btn btn-primary btn-sm">Сохранить</button>
        </form>
      </div>
      <form method="dialog" class="modal-backdrop"><button>close</button></form>
    </dialog>
    <script>
    (function(){{
      var ta=document.getElementById('bc-text');
      var live=document.getElementById('bc-live-prev-inner');
      var tplModal=document.getElementById('bc-tpl-modal');
      var tplEditTitle=document.getElementById('bc-tpl-edit-title');
      var tplEditBody=document.getElementById('bc-tpl-edit-body');
      var tplEditForm=document.getElementById('bc-tpl-edit-form');
      var tplEditPreview=document.getElementById('bc-tpl-edit-preview');
      var previewEndpoint='/admin/broadcast/preview-html';
      var deb=null;
      var tplDeb=null;
      var tplInitTitle='';
      var tplInitBody='';
      function esc(s){{return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
      function jsonFromUtf8B64(b64){{
        try{{
          var bin=atob(b64||'');
          var bytes=new Uint8Array(bin.length);
          for(var i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i)&255;
          var txt=new TextDecoder('utf-8').decode(bytes);
          return JSON.parse(txt);
        }}catch(_e){{ return null; }}
      }}
      function updateLiveMedia(type,filename,objUrl){{
        var row=document.getElementById('bc-live-media-row');
        var img=document.getElementById('bc-live-media-img');
        var doc=document.getElementById('bc-live-media-doc');
        var docName=document.getElementById('bc-live-media-doc-name');
        if(!row)return;
        if(!type){{row.classList.add('hidden');if(img){{img.src='';img.classList.add('hidden');}}if(doc)doc.classList.add('hidden');return;}}
        row.classList.remove('hidden');
        if(type==='photo'&&objUrl){{if(img){{img.src=objUrl;img.classList.remove('hidden');}}if(doc)doc.classList.add('hidden');}}
        else{{if(img){{img.src='';img.classList.add('hidden');}}if(doc)doc.classList.remove('hidden');if(docName)docName.textContent=filename||'вложение';}}
      }}
      async function renderLive(){{
        var v=(ta&&ta.value)||'';
        if(!live)return;
        if(!v.trim()){{ live.innerHTML='<span class="opacity-70">Начните ввод…</span>'; return; }}
        try{{
          var fd=new FormData(); fd.append('text', v);
          var r=await fetch(previewEndpoint,{{method:'POST', body:fd, credentials:'same-origin'}});
          var j=await r.json();
          if(j&&j.html) live.innerHTML=j.html; else live.innerHTML='<span class="whitespace-pre-wrap">'+esc(v)+'</span>';
        }}catch(e){{
          live.innerHTML='<span class="whitespace-pre-wrap">'+esc(v)+'</span>';
        }}
      }}
      function queueLive(){{
        if(deb)clearTimeout(deb);
        deb=setTimeout(renderLive, 120);
      }}
      if(ta){{ ta.addEventListener('input', queueLive); queueLive(); }}
      (function consumeBroadcastNotify(){{
        try{{
          var u=new URL(window.location.href);
          var n=u.searchParams.get('n');
          var err=u.searchParams.get('err');
          var started=u.searchParams.get('started');
          var mapN={{
            test_sent:'Тестовое сообщение отправлено вам в Telegram.',
            tpl_ok:'Шаблон сохранён.',
            tpl_del:'Шаблон удалён.',
            scheduled:'Отложенная рассылка добавлена в очередь.',
            sched_ok:'Отложенная отправка обновлена.',
            sched_del:'Отложенная отправка удалена.'
          }};
          var mapErr={{
            empty:'Введите текст сообщения.',
            no_bot_token:'BOT_TOKEN не задан — рассылка невозможна.',
            test_no_tg:'Нет Telegram ID в сессии: войдите через Telegram или откройте профиль.',
            schedule_bad_time:'Неверная дата или время отложенной отправки.',
            no_targets:'Выберите хотя бы одну цель: пользователям из БД или канал.',
            no_channel:'В настройках не задан ID канала для рассылки (BROADCAST_MAIN_CHANNEL_ID).',
            template_bad:'Заполните название и текст шаблона.',
            test_fail:'Не удалось отправить тест: проверьте разметку MarkdownV2 и доступ к Telegram.'
          }};
          if(started==='1'&&window.remnaToast)window.remnaToast('success','Рассылка поставлена в очередь на фоновую отправку.');
          if(n&&mapN[n]&&window.remnaToast)window.remnaToast('success',mapN[n]);
          if(err&&mapErr[err]&&window.remnaToast)window.remnaToast('error',mapErr[err]);
          if(n||err||started){{
            u.searchParams.delete('n');
            u.searchParams.delete('err');
            u.searchParams.delete('started');
            history.replaceState({{}},'',u.toString());
          }}
        }}catch(_e){{}}
      }})();
      async function renderTplPreview(){{
        var v=(tplEditBody&&tplEditBody.value)||'';
        if(!tplEditPreview)return;
        if(!v.trim()){{ tplEditPreview.innerHTML='<span class="opacity-70">Начните ввод…</span>'; return; }}
        try{{
          var fd=new FormData(); fd.append('text', v);
          var r=await fetch(previewEndpoint,{{method:'POST', body:fd, credentials:'same-origin'}});
          var j=await r.json();
          if(j&&j.html) tplEditPreview.innerHTML=j.html; else tplEditPreview.innerHTML='<span class="whitespace-pre-wrap">'+esc(v)+'</span>';
        }}catch(_e){{
          tplEditPreview.innerHTML='<span class="whitespace-pre-wrap">'+esc(v)+'</span>';
        }}
      }}
      function queueTplPreview(){{
        if(tplDeb)clearTimeout(tplDeb);
        tplDeb=setTimeout(renderTplPreview, 120);
      }}
      if(tplEditBody) tplEditBody.addEventListener('input', queueTplPreview);
      if(tplEditForm) tplEditForm.addEventListener('submit', function(e){{
        var curT=(tplEditTitle&&tplEditTitle.value||'').trim();
        var curB=(tplEditBody&&tplEditBody.value||'').trim();
        if(curT===tplInitTitle && curB===tplInitBody){{
          e.preventDefault();
          if(tplModal) tplModal.close();
          if(window.remnaToast)window.remnaToast('info','Изменений нет');
        }}
      }});
      function ins(w){{
        if(!ta)return;
        var s=ta.selectionStart||0,e=ta.selectionEnd||0,x=ta.value||'';
        ta.value=x.slice(0,s)+w+x.slice(e);
        ta.focus(); ta.selectionStart=ta.selectionEnd=s+w.length; queueLive();
      }}
      function isoUtcToDatetimeLocal(iso){{
        if(!iso)return '';
        var d=new Date(iso);
        if(isNaN(d.getTime()))return '';
        var pad=function(n){{ return (n<10?'0':'')+n; }};
        return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes());
      }}
      document.querySelectorAll('[data-bc-ins]').forEach(function(btn){{
        btn.addEventListener('click',function(){{
          ins(btn.getAttribute('data-bc-ins')||'');
        }});
      }});
      var preBtn=document.getElementById('bc-ins-pre');
      if(preBtn) preBtn.addEventListener('click',function(){{ ins('```\\n\\n```'); }});
      var dtBtn=document.getElementById('bc-ins-date');
      if(dtBtn) dtBtn.addEventListener('click',function(){{
        ins(new Date().toLocaleString('ru-RU',{{hour12:false}}));
      }});
      function applyMediaFromObj(o){{
        var mtH=document.getElementById('bc-media-type-h');
        var fidH=document.getElementById('bc-media-fid-h');
        var previewBox=document.getElementById('bc-media-preview');
        var clearBtn=document.getElementById('bc-media-clear');
        var imgPrev=document.getElementById('bc-media-img-prev');
        var nameSpan=document.getElementById('bc-media-name');
        var typeLabel=document.getElementById('bc-media-type-label');
        if(o&&o.media_type&&o.media_file_id){{
          if(mtH) mtH.value=o.media_type;
          if(fidH) fidH.value=o.media_file_id;
          if(nameSpan) nameSpan.textContent='file_id: '+o.media_file_id.slice(0,20)+'…';
          if(typeLabel) typeLabel.textContent=o.media_type==='photo'?'фото':'документ';
          if(imgPrev){{ imgPrev.src=''; imgPrev.classList.add('hidden'); }}
          if(previewBox) previewBox.classList.remove('hidden');
          if(clearBtn) clearBtn.classList.remove('hidden');
          updateLiveMedia(o.media_type, o.media_file_id, null);
        }} else {{
          if(mtH) mtH.value='';
          if(fidH) fidH.value='';
          if(previewBox) previewBox.classList.add('hidden');
          if(clearBtn) clearBtn.classList.add('hidden');
          updateLiveMedia(null);
        }}
      }}
      document.querySelectorAll('.bc-pend-use').forEach(function(btn){{
        btn.addEventListener('click',function(){{
          var m=btn.getAttribute('data-b64sched'); if(!m||!ta)return;
          var o=jsonFromUtf8B64(m); if(!o)return;
          ta.value=o.body||''; ta.focus(); queueLive();
          applyMediaFromObj(o);
          if(window.remnaToast)window.remnaToast('success','Текст из очереди подставлен');
        }});
      }});
      document.querySelectorAll('.bc-pend-edit').forEach(function(btn){{
        btn.addEventListener('click',function(){{
          var m=btn.getAttribute('data-b64sched'); if(!m)return;
          var o=jsonFromUtf8B64(m); if(!o)return;
          var bodyEl=document.getElementById('bc-sched-edit-body');
          var locEl=document.getElementById('bc-sched-edit-local');
          var utcEl=document.getElementById('bc-sched-edit-utc');
          var form=document.getElementById('bc-sched-edit-form');
          if(bodyEl) bodyEl.value=o.body||'';
          if(locEl) locEl.value=isoUtcToDatetimeLocal(o.scheduled_at_utc||'');
          if(utcEl) utcEl.value='';
          var esu=document.getElementById('bc-sched-edit-su');
          var esc=document.getElementById('bc-sched-edit-sc');
          if(esu) esu.checked = (o.send_to_users !== false && o.send_to_users !== undefined) ? !!o.send_to_users : true;
          if(esc) esc.checked = !!o.send_to_channel;
          if(form) form.action='/admin/broadcast/schedule/'+encodeURIComponent(o.id)+'/edit';
          var emt=document.getElementById('bc-sched-edit-mt');
          var efid=document.getElementById('bc-sched-edit-fid');
          var eminfo=document.getElementById('bc-sched-edit-media-info');
          if(emt) emt.value=o.media_type||'';
          if(efid) efid.value=o.media_file_id||'';
          if(eminfo){{
            if(o.media_type&&o.media_file_id){{eminfo.textContent='📎 Прикреплено: '+o.media_type; eminfo.classList.remove('hidden');}}
            else {{eminfo.textContent=''; eminfo.classList.add('hidden');}}
          }}
          document.getElementById('bc-sched-modal').showModal();
        }});
      }});
      var sef=document.getElementById('bc-sched-edit-form');
      if(sef) sef.addEventListener('submit',function(){{
        var loc=document.getElementById('bc-sched-edit-local');
        var utc=document.getElementById('bc-sched-edit-utc');
        if(loc&&utc&&loc.value){{
          var d=new Date(loc.value);
          if(!isNaN(d.getTime())) utc.value=d.toISOString();
        }}
      }});
      document.querySelectorAll('.bc-hist-use').forEach(function(btn){{
        btn.addEventListener('click',function(){{
          var m=btn.getAttribute('data-b64hist'); if(!m||!ta)return;
          var o=jsonFromUtf8B64(m); if(!o)return;
          ta.value=o.body||''; ta.focus(); queueLive();
          applyMediaFromObj(o);
          if(window.remnaToast)window.remnaToast('success','Текст из истории подставлен — измените и отправьте');
        }});
      }});
      document.querySelectorAll('.bc-tpl-use').forEach(function(btn){{
        btn.addEventListener('click',function(){{
          var m=btn.getAttribute('data-b64tpl'); if(!m||!ta)return;
          try{{ var o=jsonFromUtf8B64(m); if(o){{ ta.value=o.body||''; ta.focus(); queueLive(); if(window.remnaToast)window.remnaToast('success','Шаблон подставлен'); }} }}catch(e){{}}
        }});
      }});
      document.querySelectorAll('.bc-tpl-edit').forEach(function(btn){{
        btn.addEventListener('click',function(){{
          var m=btn.getAttribute('data-b64tpl'); if(!m)return;
          try{{
            var o=jsonFromUtf8B64(m); if(!o)return;
            document.getElementById('bc-tpl-edit-id').value=o.id||'0';
            document.getElementById('bc-tpl-edit-title').value=o.title||'';
            document.getElementById('bc-tpl-edit-body').value=o.body||'';
            tplInitTitle=(o.title||'').trim();
            tplInitBody=(o.body||'').trim();
            var f=document.getElementById('bc-tpl-edit-form');
            if(f) f.action='/admin/broadcast/template/'+encodeURIComponent(o.id)+'/edit';
            queueTplPreview();
            document.getElementById('bc-tpl-modal').showModal();
          }}catch(e){{}}
        }});
      }});
      var tf=document.getElementById('bc-test-f');
      if(tf) tf.addEventListener('submit',function(){{
        var h=document.getElementById('bc-test-hidden');
        if(h&&ta) h.value=ta.value||'';
        var tmt=document.getElementById('bc-test-mt-h');
        var tfid=document.getElementById('bc-test-fid-h');
        if(tmt) tmt.value=document.getElementById('bc-media-type-h').value||'';
        if(tfid) tfid.value=document.getElementById('bc-media-fid-h').value||'';
      }});
      var sf=document.getElementById('bc-sched-f');
      if(sf) sf.addEventListener('submit',function(){{
        var h=document.getElementById('bc-sched-body');
        if(h&&ta) h.value=ta.value||'';
        var loc=document.getElementById('bc-sched-local');
        var utc=document.getElementById('bc-sched-utc');
        if(loc&&utc&&loc.value){{
          var d=new Date(loc.value);
          if(!isNaN(d.getTime())) utc.value=d.toISOString();
        }}
        var su=document.getElementById('bc-cb-users');
        var sc=document.getElementById('bc-cb-channel');
        var hsu=document.getElementById('bc-sched-h-su');
        var hsc=document.getElementById('bc-sched-h-sc');
        if(hsu) hsu.value = su&&su.checked ? '1' : '';
        if(hsc) hsc.value = sc&&sc.checked ? '1' : '';
        var hmt=document.getElementById('bc-sched-h-mt');
        var hfid=document.getElementById('bc-sched-h-fid');
        if(hmt) hmt.value=document.getElementById('bc-media-type-h').value||'';
        if(hfid) hfid.value=document.getElementById('bc-media-fid-h').value||'';
      }});
      (function setupMediaUpload(){{
        var fileInput=document.getElementById('bc-media-file');
        var clearBtn=document.getElementById('bc-media-clear');
        var previewBox=document.getElementById('bc-media-preview');
        var imgPrev=document.getElementById('bc-media-img-prev');
        var nameSpan=document.getElementById('bc-media-name');
        var typeLabel=document.getElementById('bc-media-type-label');
        var uploadingDiv=document.getElementById('bc-media-uploading');
        var errDiv=document.getElementById('bc-media-err');
        var mtH=document.getElementById('bc-media-type-h');
        var fidH=document.getElementById('bc-media-fid-h');
        function clearMedia(){{
          if(mtH) mtH.value='';
          if(fidH) fidH.value='';
          if(previewBox) previewBox.classList.add('hidden');
          if(clearBtn) clearBtn.classList.add('hidden');
          if(imgPrev){{ imgPrev.src=''; imgPrev.classList.add('hidden'); }}
          if(nameSpan) nameSpan.textContent='';
          if(typeLabel) typeLabel.textContent='';
          if(errDiv){{ errDiv.textContent=''; errDiv.classList.add('hidden'); }}
          if(fileInput) fileInput.value='';
          updateLiveMedia(null);
        }}
        // Restore visual state after AJAX swap (hidden inputs already have values)
        (function(){{
          var mt=mtH&&mtH.value;
          var fid=fidH&&fidH.value;
          if(!mt||!fid)return;
          if(nameSpan) nameSpan.textContent='файл прикреплён';
          if(typeLabel) typeLabel.textContent=mt==='photo'?'фото':'документ';
          if(previewBox) previewBox.classList.remove('hidden');
          if(clearBtn) clearBtn.classList.remove('hidden');
          updateLiveMedia(mt, 'вложение', null);
        }})();
        if(clearBtn) clearBtn.addEventListener('click', clearMedia);
        if(fileInput) fileInput.addEventListener('change', async function(){{
          var f=fileInput.files&&fileInput.files[0];
          if(!f) return;
          clearMedia();
          if(uploadingDiv) uploadingDiv.classList.remove('hidden');
          var fd=new FormData(); fd.append('file', f);
          try{{
            var r=await fetch('/admin/broadcast/upload-media',{{method:'POST',body:fd,credentials:'same-origin'}});
            var j=await r.json();
            if(uploadingDiv) uploadingDiv.classList.add('hidden');
            if(!j||!j.ok){{
              if(errDiv){{ errDiv.textContent='Ошибка загрузки: '+(j&&j.error||'неизвестно'); errDiv.classList.remove('hidden'); }}
              return;
            }}
            if(mtH) mtH.value=j.media_type||'';
            if(fidH) fidH.value=j.file_id||'';
            if(nameSpan) nameSpan.textContent=j.filename||f.name||'';
            if(typeLabel) typeLabel.textContent=j.media_type==='photo'?'фото':'документ';
            if(previewBox) previewBox.classList.remove('hidden');
            if(clearBtn) clearBtn.classList.remove('hidden');
            var objUrl=null;
            if(j.media_type==='photo'&&imgPrev){{
              objUrl=URL.createObjectURL(f);
              imgPrev.src=objUrl;
              imgPrev.classList.remove('hidden');
            }}
            updateLiveMedia(j.media_type, j.filename, objUrl);
            if(window.remnaToast) window.remnaToast('success','Файл прикреплён: '+j.filename);
          }}catch(e){{
            if(uploadingDiv) uploadingDiv.classList.add('hidden');
            if(errDiv){{ errDiv.textContent='Ошибка: '+String(e); errDiv.classList.remove('hidden'); }}
          }}
        }});
      }})();
    }})();
    </script>
    """
    return _layout("Рассылка", body, request=request)


_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}
_MAX_BROADCAST_MEDIA_BYTES = 50 * 1024 * 1024  # 50 МБ


@router.post("/broadcast/upload-media")
async def admin_broadcast_upload_media(
    request: Request,
    file: UploadFile = File(...),
) -> JSONResponse:
    """Загрузить файл рассылки на сервер (без отправки в Telegram). file_id = local-ссылка."""
    denied = _require_login(request)
    if denied is not None:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)

    content_type = (file.content_type or "").lower()
    is_photo = content_type in _PHOTO_CONTENT_TYPES
    media_kind = "photo" if is_photo else "document"
    filename = (file.filename or "file").strip() or "file"

    data = await file.read()
    if len(data) == 0:
        return JSONResponse({"error": "empty_file"}, status_code=400)
    if len(data) > _MAX_BROADCAST_MEDIA_BYTES:
        return JSONResponse({"error": "file_too_large"}, status_code=400)

    from shared.services.broadcast_service import save_broadcast_media_file

    try:
        local_ref = save_broadcast_media_file(data, filename)
    except Exception as e:
        logging.getLogger("api.broadcast").warning("upload-media save failed: %s", e)
        return JSONResponse({"error": str(e)[:200]}, status_code=500)

    return JSONResponse({"ok": True, "media_type": media_kind, "file_id": local_ref, "filename": filename})


@router.post("/broadcast/preview-html")
async def admin_broadcast_preview_html(request: Request, text: str = Form("")) -> JSONResponse:
    denied = _require_login(request)
    if denied is not None:
        return JSONResponse({"html": "", "denied": True}, status_code=401)
    frag = broadcast_html_preview_fragment(text)
    return JSONResponse({"html": f'<div class="bc-prev-wrap">{frag}</div>'})


@router.post("/broadcast/test")
async def admin_broadcast_test(
    request: Request,
    text: str = Form(""),
    media_type: str = Form(""),
    media_file_id: str = Form(""),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    body = (text or "").strip()
    mt = (media_type or "").strip() or None
    mfid = (media_file_id or "").strip() or None
    if not body and not (mt and mfid):
        return RedirectResponse("/admin/broadcast?err=empty", status_code=303)
    settings = get_settings()
    if not (settings.bot_token or "").strip():
        return RedirectResponse("/admin/broadcast?err=no_bot_token", status_code=303)
    auth = _auth_data(request)
    raw_tid = auth.get("telegram_id") or auth.get("id")
    try:
        tid = int(raw_tid) if raw_tid is not None else 0
    except (TypeError, ValueError):
        tid = 0
    if tid <= 0:
        return RedirectResponse("/admin/broadcast?err=test_no_tg", status_code=303)
    from aiogram import Bot
    from shared.services.broadcast_service import _send_one_message
    tok = (settings.bot_token or "").strip()
    md_body = draft_to_markdown_v2(body) if body else ""
    try:
        async with Bot(token=tok) as bot:
            await _send_one_message(
                bot, tid, md_body, body,
                parse_mode="MarkdownV2",
                media_type=mt,
                media_file_id=mfid,
            )
    except Exception:
        return RedirectResponse("/admin/broadcast?err=test_fail", status_code=303)
    return RedirectResponse("/admin/broadcast?n=test_sent", status_code=303)


@router.post("/broadcast/schedule")
async def admin_broadcast_schedule(
    request: Request,
    text: str = Form(""),
    scheduled_at_utc: str = Form(""),
    send_users: str | None = Form(None),
    send_channel: str | None = Form(None),
    media_type: str = Form(""),
    media_file_id: str = Form(""),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    body = (text or "").strip()
    mt = (media_type or "").strip() or None
    mfid = (media_file_id or "").strip() or None
    if not body and not (mt and mfid):
        return RedirectResponse("/admin/broadcast?err=empty", status_code=303)
    su = send_users == "1"
    sc = send_channel == "1"
    if not su and not sc:
        return RedirectResponse("/admin/broadcast?err=no_targets", status_code=303)
    if sc:
        sch = get_settings()
        cid0 = getattr(sch, "broadcast_main_channel_id", None)
        if cid0 is None or int(cid0) == 0:
            return RedirectResponse("/admin/broadcast?err=no_channel", status_code=303)
    raw_iso = (scheduled_at_utc or "").strip().replace("Z", "+00:00")
    if not raw_iso:
        return RedirectResponse("/admin/broadcast?err=schedule_bad_time", status_code=303)
    try:
        scheduled_utc = datetime.fromisoformat(raw_iso)
    except ValueError:
        return RedirectResponse("/admin/broadcast?err=schedule_bad_time", status_code=303)
    if scheduled_utc.tzinfo is None:
        scheduled_utc = scheduled_utc.replace(tzinfo=timezone.utc)
    else:
        scheduled_utc = scheduled_utc.astimezone(timezone.utc)
    if scheduled_utc <= datetime.now(timezone.utc):
        return RedirectResponse("/admin/broadcast?err=schedule_bad_time", status_code=303)
    async with await _session() as session:
        session.add(
            ScheduledBroadcast(
                body_text=body,
                scheduled_at=scheduled_utc,
                status="pending",
                send_to_users=su,
                send_to_channel=sc,
                media_type=mt,
                media_file_id=mfid,
            )
        )
        await session.commit()
    return RedirectResponse("/admin/broadcast?n=scheduled", status_code=303)


@router.post("/broadcast/schedule/{sid}/delete")
async def admin_broadcast_schedule_delete(request: Request, sid: int) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        row = await session.get(ScheduledBroadcast, sid)
        if row is not None and row.status == "pending":
            await session.delete(row)
            await session.commit()
    return RedirectResponse("/admin/broadcast?n=sched_del", status_code=303)


@router.post("/broadcast/schedule/{sid}/edit")
async def admin_broadcast_schedule_edit(
    request: Request,
    sid: int,
    tpl_body: str = Form(""),
    scheduled_at_utc: str = Form(""),
    send_users: str | None = Form(None),
    send_channel: str | None = Form(None),
    media_type: str = Form(""),
    media_file_id: str = Form(""),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    body = (tpl_body or "").strip()
    mt = (media_type or "").strip() or None
    mfid = (media_file_id or "").strip() or None
    if not body and not (mt and mfid):
        return RedirectResponse("/admin/broadcast?err=empty", status_code=303)
    raw_iso = (scheduled_at_utc or "").strip().replace("Z", "+00:00")
    if not raw_iso:
        return RedirectResponse("/admin/broadcast?err=schedule_bad_time", status_code=303)
    try:
        scheduled_utc = datetime.fromisoformat(raw_iso)
    except ValueError:
        return RedirectResponse("/admin/broadcast?err=schedule_bad_time", status_code=303)
    if scheduled_utc.tzinfo is None:
        scheduled_utc = scheduled_utc.replace(tzinfo=timezone.utc)
    else:
        scheduled_utc = scheduled_utc.astimezone(timezone.utc)
    if scheduled_utc <= datetime.now(timezone.utc):
        return RedirectResponse("/admin/broadcast?err=schedule_bad_time", status_code=303)
    su = send_users == "1"
    sc = send_channel == "1"
    if not su and not sc:
        return RedirectResponse("/admin/broadcast?err=no_targets", status_code=303)
    if sc:
        sch = get_settings()
        cid0 = getattr(sch, "broadcast_main_channel_id", None)
        if cid0 is None or int(cid0) == 0:
            return RedirectResponse("/admin/broadcast?err=no_channel", status_code=303)
    async with await _session() as session:
        row = await session.get(ScheduledBroadcast, sid)
        if row is None or row.status != "pending":
            return RedirectResponse("/admin/broadcast", status_code=303)
        row.body_text = body
        row.scheduled_at = scheduled_utc
        row.send_to_users = su
        row.send_to_channel = sc
        row.media_type = mt
        row.media_file_id = mfid
        await session.commit()
    return RedirectResponse("/admin/broadcast?n=sched_ok", status_code=303)


@router.post("/broadcast/template")
async def admin_broadcast_template_create(
    request: Request, title: str = Form(""), tpl_body: str = Form("")
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    title = (title or "").strip()
    tpl_body = (tpl_body or "").strip()
    if not title or not tpl_body:
        return RedirectResponse("/admin/broadcast?err=template_bad", status_code=303)
    async with await _session() as session:
        session.add(BroadcastTemplate(title=title[:160], body=tpl_body, sort_order=0))
        await session.commit()
    return RedirectResponse("/admin/broadcast?n=tpl_ok", status_code=303)


@router.post("/broadcast/template/{tpl_id}/edit")
async def admin_broadcast_template_edit(
    request: Request, tpl_id: int, title: str = Form(""), tpl_body: str = Form("")
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    title = (title or "").strip()
    tpl_body = (tpl_body or "").strip()
    if not title or not tpl_body:
        return RedirectResponse("/admin/broadcast?err=template_bad", status_code=303)
    async with await _session() as session:
        row = await session.get(BroadcastTemplate, tpl_id)
        if row is None:
            return RedirectResponse("/admin/broadcast", status_code=303)
        row.title = title[:160]
        row.body = tpl_body
        await session.commit()
    return RedirectResponse("/admin/broadcast?n=tpl_ok", status_code=303)


@router.post("/broadcast/template/{tpl_id}/delete")
async def admin_broadcast_template_delete(request: Request, tpl_id: int) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        row = await session.get(BroadcastTemplate, tpl_id)
        if row is not None:
            await session.delete(row)
            await session.commit()
    return RedirectResponse("/admin/broadcast?n=tpl_del", status_code=303)


@router.post("/broadcast")
async def admin_broadcast_post(
    request: Request,
    background: BackgroundTasks,
    text: str = Form(""),
    send_users: str | None = Form(None),
    send_channel: str | None = Form(None),
    media_type: str = Form(""),
    media_file_id: str = Form(""),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    body = (text or "").strip()
    mt = (media_type or "").strip() or None
    mfid = (media_file_id or "").strip() or None
    if not body and not (mt and mfid):
        return RedirectResponse("/admin/broadcast?err=empty", status_code=303)
    settings = get_settings()
    if not (settings.bot_token or "").strip():
        return RedirectResponse("/admin/broadcast?err=no_bot_token", status_code=303)
    su = send_users == "1"
    sc = send_channel == "1"
    if not su and not sc:
        return RedirectResponse("/admin/broadcast?err=no_targets", status_code=303)
    ch_id: int | None = None
    if sc:
        cid = getattr(settings, "broadcast_main_channel_id", None)
        if cid is None or int(cid) == 0:
            return RedirectResponse("/admin/broadcast?err=no_channel", status_code=303)
        ch_id = int(cid)
    background.add_task(
        _admin_broadcast_job, body,
        send_users=su, send_channel=sc, channel_id=ch_id,
        media_type=mt, media_file_id=mfid,
    )
    return RedirectResponse("/admin/broadcast?started=1", status_code=303)


async def _ticket_owner_user_id(session: AsyncSession, ticket_id: int) -> int | None:
    r = (
        await session.execute(text("SELECT user_id FROM tickets WHERE id = :tid"), {"tid": ticket_id})
    ).mappings().first()
    return int(r["user_id"]) if r else None


@router.post("/tickets/{ticket_id}/user/add-balance")
async def admin_ticket_user_add_balance(
    request: Request, ticket_id: int, amount: str = Form(...)
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    raw = (amount or "").strip().replace(",", ".")
    try:
        amt = Decimal(raw)
    except (InvalidOperation, ValueError):
        return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus('Неверная сумма')}", status_code=303)
    if amt <= 0:
        return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus('Сумма должна быть > 0')}", status_code=303)
    wauth = request.session.get("wauth") or {}
    admin_tg = int(wauth.get("telegram_id") or 0)
    async with await _session() as session:
        uid = await _ticket_owner_user_id(session, ticket_id)
        if uid is None:
            return RedirectResponse("/admin/tickets", status_code=303)
        u = await session.get(User, uid)
        if u is None:
            return RedirectResponse("/admin/tickets", status_code=303)
        admin_db_id = None
        if admin_tg:
            au = (await session.execute(select(User).where(User.telegram_id == admin_tg))).scalar_one_or_none()
            if au is not None:
                admin_db_id = au.id
        u.balance += amt
        txn_bal = Transaction(
            user_id=u.id,
            type="admin_balance_add",
            amount=amt,
            currency="RUB",
            payment_provider="admin",
            payment_id=None,
            status="completed",
            description=f"Админ (web) добавил баланс: +{amt} ₽",
            meta={"admin_id": admin_db_id, "source": "web_tickets"},
        )
        session.add(txn_bal)
        await session.flush()
        settings = get_settings()
        await apply_balance_credit_followups(
            session,
            user=u,
            credited=amt,
            settings=settings,
            triggering_txn=txn_bal,
            grant_referrer_reward=True,
            try_smart_cart=True,
        )
        await session.commit()
    return RedirectResponse(f"/admin/tickets/{ticket_id}?n=bal_ok", status_code=303)


@router.post("/tickets/{ticket_id}/user/add-months")
async def admin_ticket_user_add_months(
    request: Request, ticket_id: int, subscription_id: int = Form(...), months: int = Form(...)
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    if months < 1 or months > 120:
        return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus('Месяцев: от 1 до 120')}", status_code=303)
    settings = get_settings()
    async with await _session() as session:
        uid = await _ticket_owner_user_id(session, ticket_id)
        if uid is None:
            return RedirectResponse("/admin/tickets", status_code=303)
        sub = (
            await session.execute(
                select(Subscription)
                .options(selectinload(Subscription.plan))
                .where(Subscription.id == subscription_id, Subscription.user_id == uid)
            )
        ).scalar_one_or_none()
        if sub is None:
            return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus('Подписка не найдена')}", status_code=303)
        sub.expires_at = _add_calendar_months(sub.expires_at, months)
        pl = sub.plan
        if not (sub.status == "trial" and pl is not None and pl.name == "Триал"):
            bp = await get_base_subscription_plan(session)
            if bp is not None:
                sub.plan_id = bp.id
        u = await session.get(User, uid)
        if u is not None and u.remnawave_uuid is not None and not settings.remnawave_stub:
            rw = RemnaWaveClient(settings)
            try:
                await update_rw_user_respecting_hwid_limit(
                    rw,
                    str(u.remnawave_uuid),
                    devices_limit_for_panel=sub.devices_count,
                    expire_at=sub.expires_at,
                    status="ACTIVE",
                )
            except RemnaWaveError:
                pass
        await session.commit()
    return RedirectResponse(f"/admin/tickets/{ticket_id}?n=months_ok", status_code=303)


@router.post("/tickets/{ticket_id}/user/subscription/auto-renew")
async def admin_ticket_user_sub_auto_renew(
    request: Request, ticket_id: int, enabled: str = Form("0")
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    want_on = (enabled or "").strip() == "1"
    async with await _session() as session:
        uid = await _ticket_owner_user_id(session, ticket_id)
        if uid is None:
            return RedirectResponse("/admin/tickets", status_code=303)
        ok, msg = await set_subscription_auto_renew(session, uid, want_on)
        if ok:
            await session.commit()
            n = "ar_on" if want_on else "ar_off"
            return RedirectResponse(f"/admin/tickets/{ticket_id}?n={n}", status_code=303)
        await session.rollback()
    err = str(msg).replace("\n", " ")[:400]
    return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus(err)}", status_code=303)


@router.post("/tickets/{ticket_id}/user/subscription/disable")
async def admin_ticket_user_sub_disable(
    request: Request, ticket_id: int, subscription_id: int = Form(...)
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        uid = await _ticket_owner_user_id(session, ticket_id)
        if uid is None:
            return RedirectResponse("/admin/tickets", status_code=303)
        sub = await session.get(Subscription, subscription_id)
        if sub is None or sub.user_id != uid:
            return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus('Подписка не найдена')}", status_code=303)
        ok, msg = await admin_disable_subscription_record(
            session,
            user_id=uid,
            subscription_id=subscription_id,
            settings=settings,
        )
        if ok:
            await session.commit()
            return RedirectResponse(f"/admin/tickets/{ticket_id}?n=sub_off", status_code=303)
        await session.rollback()
    err = str(msg).replace("\n", " ")[:400]
    return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus(err)}", status_code=303)


@router.post("/tickets/{ticket_id}/user/subscription/enable")
async def admin_ticket_user_sub_enable(
    request: Request, ticket_id: int, subscription_id: int = Form(...)
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        uid = await _ticket_owner_user_id(session, ticket_id)
        if uid is None:
            return RedirectResponse("/admin/tickets", status_code=303)
        sub = await session.get(Subscription, subscription_id)
        if sub is None or sub.user_id != uid:
            return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus('Подписка не найдена')}", status_code=303)
        ok, msg = await admin_enable_subscription_record(
            session,
            user_id=uid,
            subscription_id=subscription_id,
            settings=settings,
        )
        if ok:
            await session.commit()
            return RedirectResponse(f"/admin/tickets/{ticket_id}?n=sub_on", status_code=303)
        await session.rollback()
    err = str(msg).replace("\n", " ")[:400]
    return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus(err)}", status_code=303)


@router.get("")
async def admin_root():
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.get("/status")
async def admin_status(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    placeholder = """
    <div class="card bg-base-100 border border-base-content/10 shadow-lg mb-4">
      <div class="card-body gap-2">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-heart-pulse text-primary mr-2" aria-hidden="true"></i>Состояние сервисов</h2>
        <p class="text-sm opacity-70">Страница открывается сразу, проверки статусов выполняются в фоне.</p>
      </div>
    </div>
    <style>
      .status-skel{position:relative;overflow:hidden;background:color-mix(in oklab, var(--fallback-b2, #e5e7eb) 78%, #fff);}
      .status-skel::after{
        content:"";
        position:absolute;
        inset:0;
        transform:translateX(-100%);
        background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);
        animation:statusShimmer 1.25s ease-in-out infinite;
      }
      @keyframes statusShimmer{100%{transform:translateX(100%);}}
    </style>
    <div id="status-machine" class="mb-4">
      <div class="card bg-base-100 border border-base-content/10 shadow-lg">
        <div class="card-body gap-3">
          <h3 class="card-title text-lg"><i class="fa-solid fa-microchip text-primary mr-2" aria-hidden="true"></i>Отчет о машине</h3>
          <div class="grid gap-2 sm:grid-cols-3">
            <div class="rounded-lg border border-base-content/10 p-3"><span class="status-skel inline-block h-4 w-28 rounded"></span><div class="mt-2 status-skel h-5 w-40 rounded"></div></div>
            <div class="rounded-lg border border-base-content/10 p-3"><span class="status-skel inline-block h-4 w-24 rounded"></span><div class="mt-2 status-skel h-5 w-44 rounded"></div></div>
            <div class="rounded-lg border border-base-content/10 p-3"><span class="status-skel inline-block h-4 w-16 rounded"></span><div class="mt-2 status-skel h-5 w-full rounded"></div></div>
          </div>
        </div>
      </div>
    </div>
    <div id="status-grid" class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      <div id="status-card-panel" class="card bg-base-100 border border-base-content/10 shadow-lg transition-all duration-200 hover:shadow-xl hover:border-primary/25"><div class="card-body gap-2"><div class="flex items-start justify-between gap-2"><h3 class="card-title text-base"><i class="fa-solid fa-server text-primary mr-2" aria-hidden="true"></i>Панель Remnawave (API)</h3><span class="badge badge-sm"><span class="status-skel inline-block h-3 w-16 rounded-full"></span></span></div><p class="text-sm opacity-90"><span class="status-skel inline-block h-4 w-56 rounded"></span></p><p class="text-xs opacity-60 mt-1"><span class="status-skel inline-block h-3 w-40 rounded"></span></p></div></div>
      <div id="status-card-bot" class="card bg-base-100 border border-base-content/10 shadow-lg transition-all duration-200 hover:shadow-xl hover:border-primary/25"><div class="card-body gap-2"><div class="flex items-start justify-between gap-2"><h3 class="card-title text-base"><i class="fa-brands fa-telegram text-primary mr-2" aria-hidden="true"></i>Telegram-бот</h3><span class="badge badge-sm"><span class="status-skel inline-block h-3 w-16 rounded-full"></span></span></div><p class="text-sm opacity-90"><span class="status-skel inline-block h-4 w-56 rounded"></span></p><p class="text-xs opacity-60 mt-1"><span class="status-skel inline-block h-3 w-40 rounded"></span></p></div></div>
      <div id="status-card-tickets_bot" class="card bg-base-100 border border-base-content/10 shadow-lg transition-all duration-200 hover:shadow-xl hover:border-primary/25"><div class="card-body gap-2"><div class="flex items-start justify-between gap-2"><h3 class="card-title text-base"><i class="fa-solid fa-headset text-primary mr-2" aria-hidden="true"></i>Бот тикетов</h3><span class="badge badge-sm"><span class="status-skel inline-block h-3 w-16 rounded-full"></span></span></div><p class="text-sm opacity-90"><span class="status-skel inline-block h-4 w-56 rounded"></span></p><p class="text-xs opacity-60 mt-1"><span class="status-skel inline-block h-3 w-40 rounded"></span></p></div></div>
      <div id="status-card-db" class="card bg-base-100 border border-base-content/10 shadow-lg transition-all duration-200 hover:shadow-xl hover:border-primary/25"><div class="card-body gap-2"><div class="flex items-start justify-between gap-2"><h3 class="card-title text-base"><i class="fa-solid fa-database text-primary mr-2" aria-hidden="true"></i>База данных</h3><span class="badge badge-sm"><span class="status-skel inline-block h-3 w-16 rounded-full"></span></span></div><p class="text-sm opacity-90"><span class="status-skel inline-block h-4 w-56 rounded"></span></p><p class="text-xs opacity-60 mt-1"><span class="status-skel inline-block h-3 w-40 rounded"></span></p></div></div>
      <div id="status-card-redis" class="card bg-base-100 border border-base-content/10 shadow-lg transition-all duration-200 hover:shadow-xl hover:border-primary/25"><div class="card-body gap-2"><div class="flex items-start justify-between gap-2"><h3 class="card-title text-base"><i class="fa-solid fa-bolt text-primary mr-2" aria-hidden="true"></i>Redis</h3><span class="badge badge-sm"><span class="status-skel inline-block h-3 w-16 rounded-full"></span></span></div><p class="text-sm opacity-90"><span class="status-skel inline-block h-4 w-56 rounded"></span></p><p class="text-xs opacity-60 mt-1"><span class="status-skel inline-block h-3 w-40 rounded"></span></p></div></div>
    </div>
    <script>
    (function(){
      function esc(s){return String(s||'').replace(/[&<>\"']/g,function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch]||ch;});}
      function card(it){
        var ok=!!it.ok;
        var badge=ok?'badge-success':'badge-error';
        var st=ok?'Онлайн':'Ошибка';
        var lat=it.latency?('<p class="text-xs opacity-60 mt-1">'+esc(it.latency)+'</p>'):'';
        return '<div class="card bg-base-100 border border-base-content/10 shadow-lg transition-all duration-200 hover:shadow-xl hover:border-primary/25"><div class="card-body gap-2"><div class="flex items-start justify-between gap-2"><h3 class="card-title text-base"><i class="'+esc(it.icon)+' text-primary mr-2" aria-hidden="true"></i>'+esc(it.title)+'</h3><span class="badge '+badge+' badge-sm">'+st+'</span></div><p class="text-sm opacity-90 break-words">'+esc(it.detail)+'</p>'+lat+'</div></div>';
      }
      function fetchTimeout(url, ms){
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort('timeout'), ms);
        return fetch(url,{credentials:'same-origin', signal: ctrl.signal})
          .finally(()=>clearTimeout(t));
      }
      async function loadOneService(key){
        const el = document.getElementById('status-card-'+key);
        if(!el) return;
        try{
          const r = await fetchTimeout('/admin/status/service/'+key, 15000);
          const j = await r.json();
          if(!r.ok || !j || !j.service){ throw new Error((j&&j.error)||('HTTP '+r.status)); }
          el.outerHTML = card(j.service);
        }catch(e){
          el.outerHTML = card({title:key, icon:'fa-solid fa-triangle-exclamation', ok:false, detail:'timeout', latency:null});
        }
      }
      async function loadMachine(){
        const machine=document.getElementById('status-machine');
        if(!machine) return;
        try{
          const r = await fetchTimeout('/admin/status/machine', 15000);
          const j = await r.json();
          if(!r.ok || !j){ throw new Error((j&&j.error)||('HTTP '+r.status)); }
          machine.innerHTML = j.machine_html || '';
        }catch(_e){
          machine.innerHTML = '<div class="alert alert-error"><span>Отчет о машине: timeout</span></div>';
        }
      }
      function load(){
        loadMachine();
        ['panel','bot','tickets_bot','db','redis'].forEach(loadOneService);
      }
      load();
      setInterval(load, 10000);
    })();
    </script>
    """
    return _layout("Статус сервисов", placeholder, request=request)


@router.get("/status/service/{service_key}")
async def admin_status_service(request: Request, service_key: str) -> JSONResponse:
    denied = _require_login(request)
    if denied is not None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    settings = get_settings()
    key = (service_key or "").strip().lower()
    if key == "panel":
        rw = RemnaWaveClient(settings)
        try:
            ok, msg, ms = await rw.ping_api()
            lat = f"Задержка API: {ms} мс" if ms is not None else None
            return JSONResponse({"service": {"title": "Панель Remnawave (API)", "icon": "fa-solid fa-server", "ok": ok, "detail": msg, "latency": lat}})
        except Exception:
            return JSONResponse({"service": {"title": "Панель Remnawave (API)", "icon": "fa-solid fa-server", "ok": False, "detail": "timeout", "latency": None}})
    if key in ("bot", "tickets_bot"):
        token = settings.bot_token if key == "bot" else tickets_config.bot_token
        title = "Telegram-бот" if key == "bot" else "Бот тикетов"
        icon = "fa-brands fa-telegram" if key == "bot" else "fa-solid fa-headset"
        missing = "BOT_TOKEN не задан в окружении" if key == "bot" else "TICKETS_BOT_TOKEN не задан в окружении"
        fallback = "бот отвечает (getMe OK)" if key == "bot" else "бот тикетов отвечает (getMe OK)"
        try:
            async with httpx.AsyncClient(timeout=12.0) as tg_client:
                ok, msg, lat = await _telegram_bot_getme_status(
                    tg_client,
                    token=token,
                    missing_token_msg=missing,
                    ok_fallback_msg=fallback,
                )
            return JSONResponse({"service": {"title": title, "icon": icon, "ok": ok, "detail": msg, "latency": lat}})
        except Exception:
            return JSONResponse({"service": {"title": title, "icon": icon, "ok": False, "detail": "timeout", "latency": None}})
    if key == "db":
        t0 = time.perf_counter()
        try:
            async with await _session() as session:
                await session.execute(text("SELECT 1"))
            ms = round((time.perf_counter() - t0) * 1000, 1)
            return JSONResponse({"service": {"title": "База данных", "icon": "fa-solid fa-database", "ok": True, "detail": "PostgreSQL отвечает", "latency": f"Задержка: {ms} мс"}})
        except Exception as e:
            return JSONResponse({"service": {"title": "База данных", "icon": "fa-solid fa-database", "ok": False, "detail": str(e)[:240], "latency": None}})
    if key == "redis":
        try:
            rcli = redis_async.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
            try:
                t0 = time.perf_counter()
                await rcli.ping()
                ms = round((time.perf_counter() - t0) * 1000, 1)
                return JSONResponse({"service": {"title": "Redis", "icon": "fa-solid fa-bolt", "ok": True, "detail": "PONG", "latency": f"Задержка: {ms} мс"}})
            finally:
                await rcli.aclose()
        except Exception as e:
            return JSONResponse({"service": {"title": "Redis", "icon": "fa-solid fa-bolt", "ok": False, "detail": str(e)[:240], "latency": None}})
    return JSONResponse({"error": "unknown service"}, status_code=404)


@router.get("/status/machine")
async def admin_status_machine(request: Request) -> JSONResponse:
    denied = _require_login(request)
    if denied is not None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    machine = _read_machine_metrics()
    ip_diag = await _detect_server_ips(request)
    host_ok = bool(machine.get("ok"))
    ip_ok = bool(ip_diag.get("ok"))
    host_ram = str(machine.get("ram_text") or "RAM: недоступно")
    host_cpu = str(machine.get("cpu_text") or "CPU: недоступно")
    ram_pct = float(machine.get("ram_pct") or 0.0)
    cpu_pct = float(machine.get("cpu_pct") or 0.0)
    ip_detail = str(ip_diag.get("detail") or "IP: недоступно")
    ip_lat = ip_diag.get("latency")
    machine_ok = host_ok and ip_ok
    machine_badge = "badge-success" if machine_ok else "badge-warning"
    machine_state = "Норма" if machine_ok else "Частично"
    machine_html = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-3">
        <div class="flex items-start justify-between gap-2">
          <h3 class="card-title text-lg"><i class="fa-solid fa-microchip text-primary mr-2" aria-hidden="true"></i>Отчет о машине</h3>
          <span class="badge {machine_badge} badge-sm">{machine_state}</span>
        </div>
        <div class="grid gap-2 sm:grid-cols-3">
          <div class="rounded-lg border border-base-content/10 bg-base-200/25 p-3">
            <div class="mx-auto mb-2 flex h-14 w-14 items-center justify-center rounded-full border border-base-content/10"
                 style="background: conic-gradient(color-mix(in oklab, var(--p) 78%, transparent) 0 {ram_pct}%, color-mix(in oklab, var(--bc) 10%, transparent) {ram_pct}% 100%);">
              <span class="text-[11px] font-semibold">{ram_pct:.0f}%</span>
            </div>
            <p class="text-xs opacity-60 uppercase tracking-wide">RAM</p>
            <p class="text-sm font-medium break-words">{_esc(host_ram)}</p>
          </div>
          <div class="rounded-lg border border-base-content/10 bg-base-200/25 p-3">
            <div class="mx-auto mb-2 flex h-14 w-14 items-center justify-center rounded-full border border-base-content/10"
                 style="background: conic-gradient(color-mix(in oklab, var(--info) 74%, transparent) 0 {cpu_pct}%, color-mix(in oklab, var(--bc) 10%, transparent) {cpu_pct}% 100%);">
              <span class="text-[11px] font-semibold">{cpu_pct:.0f}%</span>
            </div>
            <p class="text-xs opacity-60 uppercase tracking-wide">CPU</p>
            <p class="text-sm font-medium break-words">{_esc(host_cpu)}</p>
          </div>
          <div class="rounded-lg border border-base-content/10 bg-base-200/25 p-3">
            <p class="text-xs opacity-60 uppercase tracking-wide">IP</p>
            <p class="text-sm font-medium break-words">{_esc(ip_detail)}</p>
            {"<p class='text-xs opacity-60 mt-1'>" + _esc(ip_lat) + "</p>" if ip_lat else ""}
          </div>
        </div>
      </div>
    </div>
    """
    return JSONResponse({"machine_html": machine_html})


@router.get("/status/data")
async def admin_status_data(request: Request) -> JSONResponse:
    denied = _require_login(request)
    if denied is not None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    settings = get_settings()
    rw = RemnaWaveClient(settings)
    panel_ok, panel_msg, panel_ms = await rw.ping_api()
    panel_lat = f"Задержка API: {panel_ms} мс" if panel_ms is not None else None

    async with httpx.AsyncClient(timeout=12.0) as tg_client:
        bot_ok, bot_msg, bot_lat = await _telegram_bot_getme_status(
            tg_client,
            token=settings.bot_token,
            missing_token_msg="BOT_TOKEN не задан в окружении",
            ok_fallback_msg="бот отвечает (getMe OK)",
        )
        tickets_bot_ok, tickets_bot_msg, tickets_bot_lat = await _telegram_bot_getme_status(
            tg_client,
            token=tickets_config.bot_token,
            missing_token_msg="TICKETS_BOT_TOKEN не задан в окружении",
            ok_fallback_msg="бот тикетов отвечает (getMe OK)",
        )

    db_ok = False
    db_msg = "—"
    db_lat: str | None = None
    t0 = time.perf_counter()
    try:
        async with await _session() as session:
            await session.execute(text("SELECT 1"))
        ms = round((time.perf_counter() - t0) * 1000, 1)
        db_ok = True
        db_msg = "PostgreSQL отвечает"
        db_lat = f"Задержка: {ms} мс"
    except Exception as e:
        db_msg = str(e)[:240]

    redis_ok = False
    redis_msg = "—"
    redis_lat: str | None = None
    try:
        rcli = redis_async.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
        try:
            t0 = time.perf_counter()
            await rcli.ping()
            ms = round((time.perf_counter() - t0) * 1000, 1)
            redis_ok = True
            redis_msg = "PONG"
            redis_lat = f"Задержка: {ms} мс"
        finally:
            await rcli.aclose()
    except Exception as e:
        redis_msg = str(e)[:240]

    machine = _read_machine_metrics()
    ip_diag = await _detect_server_ips(request)
    host_ok = bool(machine.get("ok"))
    ip_ok = bool(ip_diag.get("ok"))
    host_ram = str(machine.get("ram_text") or "RAM: недоступно")
    host_cpu = str(machine.get("cpu_text") or "CPU: недоступно")
    ram_pct = float(machine.get("ram_pct") or 0.0)
    cpu_pct = float(machine.get("cpu_pct") or 0.0)
    ip_detail = str(ip_diag.get("detail") or "IP: недоступно")
    ip_lat = ip_diag.get("latency")
    ip_pct = 100.0 if bool(ip_diag.get("direct_ok")) else 0.0
    if bool(ip_diag.get("proxy_set")) and bool(ip_diag.get("proxy_ok")):
        ip_pct = 100.0
    elif bool(ip_diag.get("proxy_set")) and not bool(ip_diag.get("proxy_ok")):
        ip_pct = 50.0 if bool(ip_diag.get("direct_ok")) else 0.0
    machine_ok = host_ok and ip_ok
    machine_badge = "badge-success" if machine_ok else "badge-warning"
    machine_state = "Норма" if machine_ok else "Частично"
    machine_html = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-3">
        <div class="flex items-start justify-between gap-2">
          <h3 class="card-title text-lg"><i class="fa-solid fa-microchip text-primary mr-2" aria-hidden="true"></i>Отчет о машине</h3>
          <span class="badge {machine_badge} badge-sm">{machine_state}</span>
        </div>
        <div class="grid gap-2 sm:grid-cols-3">
          <div class="rounded-lg border border-base-content/10 bg-base-200/25 p-3">
            <div class="mx-auto mb-2 flex h-14 w-14 items-center justify-center rounded-full border border-base-content/10"
                 style="background: conic-gradient(color-mix(in oklab, var(--p) 78%, transparent) 0 {ram_pct}%, color-mix(in oklab, var(--bc) 10%, transparent) {ram_pct}% 100%);">
              <span class="text-[11px] font-semibold">{ram_pct:.0f}%</span>
            </div>
            <p class="text-xs opacity-60 uppercase tracking-wide">RAM</p>
            <p class="text-sm font-medium break-words">{_esc(host_ram)}</p>
          </div>
          <div class="rounded-lg border border-base-content/10 bg-base-200/25 p-3">
            <div class="mx-auto mb-2 flex h-14 w-14 items-center justify-center rounded-full border border-base-content/10"
                 style="background: conic-gradient(color-mix(in oklab, var(--info) 74%, transparent) 0 {cpu_pct}%, color-mix(in oklab, var(--bc) 10%, transparent) {cpu_pct}% 100%);">
              <span class="text-[11px] font-semibold">{cpu_pct:.0f}%</span>
            </div>
            <p class="text-xs opacity-60 uppercase tracking-wide">CPU</p>
            <p class="text-sm font-medium break-words">{_esc(host_cpu)}</p>
          </div>
          <div class="rounded-lg border border-base-content/10 bg-base-200/25 p-3">
            <p class="text-xs opacity-60 uppercase tracking-wide">IP</p>
            <p class="text-sm font-medium break-words">{_esc(ip_detail)}</p>
            {"<p class='text-xs opacity-60 mt-1'>" + _esc(ip_lat) + "</p>" if ip_lat else ""}
          </div>
        </div>
      </div>
    </div>
    """
    nodes_table_html = ""

    services = [
        {
            "title": "Панель Remnawave (API)",
            "icon": "fa-solid fa-server",
            "ok": panel_ok,
            "detail": panel_msg,
            "latency": panel_lat,
        },
        {
            "title": "Telegram-бот",
            "icon": "fa-brands fa-telegram",
            "ok": bot_ok,
            "detail": bot_msg,
            "latency": bot_lat,
        },
        {
            "title": "Бот тикетов",
            "icon": "fa-solid fa-headset",
            "ok": tickets_bot_ok,
            "detail": tickets_bot_msg,
            "latency": tickets_bot_lat,
        },
        {
            "title": "База данных",
            "icon": "fa-solid fa-database",
            "ok": db_ok,
            "detail": db_msg,
            "latency": db_lat,
        },
        {
            "title": "Redis",
            "icon": "fa-solid fa-bolt",
            "ok": redis_ok,
            "detail": redis_msg,
            "latency": redis_lat,
        },
    ]
    return JSONResponse({"services": services, "nodes_html": nodes_table_html, "machine_html": machine_html})


@router.get("/dashboard")
async def admin_dashboard(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    global _DASHBOARD_HTML_CACHE
    now_m = time.monotonic()
    if _DASHBOARD_HTML_CACHE is not None and now_m - _DASHBOARD_HTML_CACHE[0] < _DASHBOARD_HTML_TTL_SEC:
        return _layout("Web-admin Dashboard", _DASHBOARD_HTML_CACHE[1], request=request)
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_ago = now - timedelta(days=1)
    async with await _session() as session:
        metrics_row = (
            await session.execute(
                select(
                    select(func.coalesce(func.sum(Transaction.amount), 0))
                    .where(
                        Transaction.type == "topup",
                        Transaction.status == "completed",
                    )
                    .scalar_subquery()
                    .label("total_income"),
                    select(func.coalesce(func.sum(Transaction.amount), 0))
                    .where(
                        Transaction.type == "topup",
                        Transaction.status == "completed",
                        Transaction.created_at >= month_start,
                    )
                    .scalar_subquery()
                    .label("month_income"),
                    select(func.coalesce(func.sum(Transaction.amount), 0))
                    .where(
                        Transaction.type == "topup",
                        Transaction.status == "completed",
                        Transaction.created_at >= day_start,
                    )
                    .scalar_subquery()
                    .label("day_income"),
                    select(func.count()).select_from(User).scalar_subquery().label("users_count"),
                    select(func.count()).select_from(PromoCode).scalar_subquery().label("promos_count"),
                    select(func.count())
                    .select_from(User)
                    .where(User.risk_notified_1h_at.is_not(None))
                    .scalar_subquery()
                    .label("risk_1h_users"),
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.risk_notified_24h_at.is_not(None),
                        User.risk_notified_1h_at.is_(None),
                    )
                    .scalar_subquery()
                    .label("risk_24h_users"),
                    select(func.count())
                    .select_from(RemnawaveWebhookEvent)
                    .where(
                        RemnawaveWebhookEvent.received_at >= day_ago,
                        RemnawaveWebhookEvent.signature_valid.is_(True),
                    )
                    .scalar_subquery()
                    .label("webhook_ok_24h"),
                    select(func.count())
                    .select_from(RemnawaveWebhookEvent)
                    .where(
                        RemnawaveWebhookEvent.received_at >= day_ago,
                        RemnawaveWebhookEvent.status == "duplicate",
                    )
                    .scalar_subquery()
                    .label("webhook_dup_24h"),
                    select(func.count())
                    .select_from(RemnawaveWebhookEvent)
                    .where(
                        RemnawaveWebhookEvent.received_at >= day_ago,
                        RemnawaveWebhookEvent.signature_valid.is_(False),
                    )
                    .scalar_subquery()
                    .label("webhook_invalid_24h"),
                    select(func.count())
                    .select_from(BillingUsageEvent)
                    .where(BillingUsageEvent.created_at >= day_ago)
                    .scalar_subquery()
                    .label("rating_events_24h"),
                    select(func.count())
                    .select_from(BillingLedgerEntry)
                    .where(
                        BillingLedgerEntry.created_at >= day_ago,
                        BillingLedgerEntry.entry_type == "reject",
                    )
                    .scalar_subquery()
                    .label("ledger_rejects_24h"),
                    select(func.count())
                    .select_from(Transaction)
                    .where(
                        Transaction.created_at >= day_ago,
                        Transaction.type == "billing_transition",
                    )
                    .scalar_subquery()
                    .label("transitions_24h"),
                    select(func.count(distinct(Subscription.user_id)))
                    .where(
                        Subscription.status.in_(("active", "trial")),
                        Subscription.expires_at > now,
                    )
                    .scalar_subquery()
                    .label("active_sub_users"),
                    select(func.count()).select_from(Subscription).scalar_subquery().label("subs_rows_total"),
                    select(func.count())
                    .select_from(User)
                    .where(User.is_blocked.is_(True))
                    .scalar_subquery()
                    .label("users_blocked"),
                    select(func.count())
                    .select_from(Transaction)
                    .where(
                        Transaction.type == "topup",
                        Transaction.status == "completed",
                        Transaction.created_at >= day_ago,
                    )
                    .scalar_subquery()
                    .label("topups_24h"),
                )
            )
        ).one()
        total_income = metrics_row.total_income
        month_income = metrics_row.month_income
        day_income = metrics_row.day_income
        users_count = int(metrics_row.users_count or 0)
        promos_count = int(metrics_row.promos_count or 0)
        risk_1h_users = int(metrics_row.risk_1h_users or 0)
        risk_24h_users = int(metrics_row.risk_24h_users or 0)
        webhook_ok_24h = int(metrics_row.webhook_ok_24h or 0)
        webhook_dup_24h = int(metrics_row.webhook_dup_24h or 0)
        webhook_invalid_24h = int(metrics_row.webhook_invalid_24h or 0)
        rating_events_24h = int(metrics_row.rating_events_24h or 0)
        ledger_rejects_24h = int(metrics_row.ledger_rejects_24h or 0)
        transitions_24h = int(metrics_row.transitions_24h or 0)
        active_sub_users = int(metrics_row.active_sub_users or 0)
        subs_rows_total = int(metrics_row.subs_rows_total or 0)
        users_blocked = int(metrics_row.users_blocked or 0)
        topups_24h = int(metrics_row.topups_24h or 0)
    safe_total = float(total_income or 0)
    safe_month = float(month_income or 0)
    safe_day = float(day_income or 0)
    month_pct = int(min(100, round((safe_month / safe_total) * 100))) if safe_total > 0 else 0
    day_pct = int(min(100, round((safe_day / safe_month) * 100))) if safe_month > 0 else 0
    sub_pct = int(min(100, round((active_sub_users / users_count) * 100))) if users_count > 0 else 0
    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-6">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-sack-dollar text-primary mr-2" aria-hidden="true"></i>Доход</h2>
        <p class="text-sm opacity-70">Суммы в шапке — по UTC-дню и месяцу сервера; дневная таблица ниже — <b>календарные сутки по МСК</b>.</p>
        <p class="text-base-content/80">За все время: <span class="font-bold text-primary">{_esc(total_income)} ₽</span>
        · За месяц: <span class="font-bold">{_esc(month_income)} ₽</span>
        · За сутки: <span class="font-bold">{_esc(day_income)} ₽</span></p>
        <p class="text-sm opacity-75">Записей подписок в БД: <b>{subs_rows_total}</b>
        · Заблокированных пользователей: <b>{users_blocked}</b>
        · Успешных пополнений за 24 ч: <b>{topups_24h}</b></p>
        <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <div class="card bg-base-200/50 border border-base-content/5 shadow-md">
            <div class="card-body items-center text-center gap-2">
              <p class="text-sm opacity-60">Месяц от всего оборота</p>
              <div class="radial-progress text-primary" style="--value:{month_pct}; --size:7.5rem; --thickness: 10px;" role="progressbar" aria-valuenow="{month_pct}">{month_pct}%</div>
            </div>
          </div>
          <div class="card bg-base-200/50 border border-base-content/5 shadow-md">
            <div class="card-body items-center text-center gap-2">
              <p class="text-sm opacity-60">День от месяца</p>
              <div class="radial-progress text-secondary" style="--value:{day_pct}; --size:7.5rem; --thickness: 10px;" role="progressbar" aria-valuenow="{day_pct}">{day_pct}%</div>
            </div>
          </div>
          <div class="card bg-base-200/50 border border-base-content/5 shadow-md">
            <div class="card-body items-center text-center gap-2">
              <p class="text-sm opacity-60">С подпиской / всего пользователей</p>
              <p class="text-2xl font-bold"><span class="text-success">{active_sub_users}</span> <span class="opacity-40">/</span> <span>{users_count}</span></p>
              <p class="text-xs opacity-50">Активная или триал, срок не истёк</p>
              <div class="radial-progress text-success" style="--value:{sub_pct}; --size:7.5rem; --thickness: 10px;" role="progressbar" aria-valuenow="{sub_pct}">{sub_pct}%</div>
            </div>
          </div>
          <div class="card bg-base-200/50 border border-base-content/5 shadow-md">
            <div class="card-body justify-center text-center">
              <p class="text-sm opacity-60 mb-2">Промокодов в базе</p>
              <p class="text-2xl font-bold text-accent">{promos_count}</p>
            </div>
          </div>
          <div class="card bg-base-200/50 border border-base-content/5 shadow-md">
            <div class="card-body justify-center text-center gap-2">
              <p class="text-sm opacity-60">Риск минуса</p>
              <p class="text-base"><span class="badge badge-error badge-sm mr-2">1ч: {risk_1h_users}</span><span class="badge badge-warning badge-sm">24ч: {risk_24h_users}</span></p>
              <a class="link link-primary text-xs" href="/admin/users?risk=1h">Открыть критичных</a>
            </div>
          </div>
          <div class="card bg-base-200/50 border border-base-content/5 shadow-md">
            <div class="card-body justify-center text-center gap-2">
              <p class="text-sm opacity-60">Observability (24ч)</p>
              <p class="text-xs">
                <span class="badge badge-success badge-xs mr-1">webhook ok: {webhook_ok_24h}</span>
                <span class="badge badge-warning badge-xs mr-1">dup: {webhook_dup_24h}</span>
                <span class="badge badge-error badge-xs">invalid: {webhook_invalid_24h}</span>
              </p>
              <p class="text-xs">
                rating: <b>{rating_events_24h}</b> · rejects: <b>{ledger_rejects_24h}</b> · transitions: <b>{transitions_24h}</b>
              </p>
            </div>
          </div>
        </div>
        <p class="text-sm opacity-60">Учитываются только платежи (<code class="bg-base-300 px-1.5 py-0.5 rounded text-xs">type=topup,status=completed</code>).</p>
      </div>
    </div>
    """
    _DASHBOARD_HTML_CACHE = (time.monotonic(), body)
    return _layout("Web-admin Dashboard", body, request=request)


@router.get("/topups")
async def admin_topups_history(request: Request, limit: int = 200) -> HTMLResponse:
    """Лента пополнений баланса (topup + ручные начисления админом)."""
    denied = _require_login(request)
    if denied is not None:
        return denied
    try:
        lim = max(10, min(500, int(limit)))
    except (TypeError, ValueError):
        lim = 200
    now = datetime.now(UTC)
    async with await _session() as session:
        rows = (
            await session.execute(
                select(Transaction, User)
                .join(User, User.id == Transaction.user_id)
                .where(
                    Transaction.status == "completed",
                    Transaction.type.in_(("topup", "admin_balance_add")),
                    Transaction.amount > 0,
                )
                .order_by(desc(Transaction.created_at))
                .limit(lim)
            )
        ).all()

    def _type_label(t: str) -> str:
        if t == "topup":
            return "Пополнение"
        if t == "admin_balance_add":
            return "Начисление админом"
        return t

    body_rows: list[str] = []
    for txn, u in rows:
        rel = _fmt_relative_ru(txn.created_at, now=now)
        amt = txn.amount
        try:
            amt_s = str(int(amt)) if amt == amt.to_integral_value() else str(amt)
        except Exception:
            amt_s = str(amt)
        un = (u.username or "").strip()
        name_bits = " ".join(x for x in ((u.first_name or "").strip(), (u.last_name or "").strip()) if x)
        who = f"@{un}" if un else (name_bits or f"#{u.id}")
        prov = (txn.payment_provider or "").strip() or "—"
        body_rows.append(
            f"<tr class='border-b border-base-content/10 hover:bg-base-200/40'>"
            f"<td class='whitespace-nowrap text-xs opacity-80'>{_esc(rel)}</td>"
            f"<td class='text-sm'><a class='link link-primary font-medium' href='/admin/users/{u.id}'>{_esc(who)}</a>"
            f"<span class='text-xs opacity-60 ml-1'>#{u.id} · tg:{u.telegram_id}</span></td>"
            f"<td class='font-mono font-semibold text-success'>+{_esc(amt_s)} ₽</td>"
            f"<td class='text-xs'><span class='badge badge-ghost badge-sm'>{_esc(_type_label(txn.type))}</span></td>"
            f"<td class='text-xs opacity-70'>{_esc(prov)}</td>"
            f"<td class='text-xs whitespace-nowrap opacity-60'>{_esc(_fmt_dt_msk(txn.created_at))}</td>"
            f"</tr>"
        )

    table = (
        "<div class='card bg-base-100 border border-base-content/10 shadow-lg'>"
        "<div class='card-body gap-4'>"
        "<div class='flex flex-wrap items-center justify-between gap-2'>"
        "<h2 class='card-title text-2xl mb-0'><i class='fa-solid fa-money-bill-transfer text-primary mr-2' aria-hidden='true'></i>"
        "История пополнений</h2>"
        "<span class='text-xs opacity-60'>Последние записи: пополнения через платежи и ручные начисления из админки</span>"
        "</div>"
        "<div class='overflow-x-auto rounded-xl border border-base-content/10'>"
        "<table class='table table-zebra table-sm'>"
        "<thead><tr>"
        "<th>Когда</th><th>Пользователь</th><th>Сумма</th><th>Тип</th><th>Провайдер / источник</th><th>Дата МСК</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body_rows) or '<tr><td colspan=\"6\" class=\"opacity-50\">Записей пока нет</td></tr>'}</tbody>"
        "</table></div></div></div>"
    )
    return _layout("История пополнений", table, request=request)


@router.get("/tickets")
async def admin_tickets(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    ops_block = ""
    async with await _session() as session:
        from shared.services.ticket_operator_stats_service import list_operator_stats

        stats = await list_operator_stats(session)
        if stats:
            rows = []
            for s in stats:
                score = int(s.get("score") or 0)
                score_cls = "text-success" if score > 0 else ("text-error" if score < 0 else "")
                label = (s.get("first_name") or s.get("username") or f"#{s.get('user_id')}").strip()
                un = f" @{s['username']}" if s.get("username") else ""
                rows.append(
                    f"<tr><td>{_esc(label)}{_esc(un)}</td>"
                    f"<td class='{_esc(score_cls)} font-semibold'>{score:+d}</td>"
                    f"<td><span class='text-success'>{int(s.get('likes') or 0)}</span> / "
                    f"<span class='text-error'>{int(s.get('dislikes') or 0)}</span></td>"
                    f"<td>{int(s.get('closed_tickets') or 0)}</td></tr>"
                )
            ops_block = (
                "<div class='card bg-base-100 border border-base-content/10 shadow-lg mb-4'>"
                "<div class='card-body gap-3'>"
                "<h3 class='card-title text-lg'><i class='fa-solid fa-user-check text-primary mr-2'></i>Операторы</h3>"
                "<div class='overflow-x-auto'><table class='table table-sm'>"
                "<thead><tr><th>Оператор</th><th>Рейтинг</th><th>👍 / 👎</th><th>Закрыто</th></tr></thead>"
                "<tbody>" + "".join(rows) + "</tbody></table></div></div></div>"
            )
    body = ops_block + """
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="card-title text-2xl"><i class="fa-solid fa-headset text-primary mr-2" aria-hidden="true"></i>Тикеты</h2>
          <a class="btn btn-outline btn-sm h-9 min-h-9 gap-1.5" href="/admin/tickets" title="Сбросить фильтры"><i class="fa-solid fa-rotate" aria-hidden="true"></i>Сброс</a>
        </div>
        <div class="grid gap-2 md:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-6">
          <label class="form-control"><span class="label-text text-xs opacity-70">Статус</span>
            <select id="tk-status" class="select select-bordered select-sm h-9 min-h-9 text-sm">
              <option value="">Все</option>
              <option value="open">Открыт</option>
              <option value="in_progress">В работе</option>
              <option value="closed">Закрыт</option>
            </select>
          </label>
          <label class="form-control"><span class="label-text text-xs opacity-70">Тема (topic_id)</span>
            <input id="tk-topic" type="number" min="0" step="1" class="input input-bordered input-sm h-9 min-h-9 text-sm" placeholder="пусто — все" />
          </label>
          <label class="form-control"><span class="label-text text-xs opacity-70">Назначение</span>
            <select id="tk-assigned" class="select select-bordered select-sm h-9 min-h-9 text-sm">
              <option value="">Все</option>
              <option value="none">Без ответственного</option>
              <option value="me">На мне</option>
            </select>
          </label>
          <label class="form-control"><span class="label-text text-xs opacity-70">Биллинг юзера</span>
            <select id="tk-billing" class="select select-bordered select-sm h-9 min-h-9 text-sm">
              <option value="">Все</option>
              <option value="hybrid">Hybrid</option>
              <option value="legacy">Legacy</option>
            </select>
          </label>
          <label class="form-control"><span class="label-text text-xs opacity-70">Дата с</span>
            <input id="tk-from" type="date" class="input input-bordered input-sm h-9 min-h-9 text-sm" />
          </label>
          <label class="form-control"><span class="label-text text-xs opacity-70">Дата по</span>
            <input id="tk-to" type="date" class="input input-bordered input-sm h-9 min-h-9 text-sm" />
          </label>
          <label class="form-control md:col-span-2 xl:col-span-2"><span class="label-text text-xs opacity-70">Поиск</span>
            <input id="tk-q" type="text" class="input input-bordered input-sm h-9 min-h-9 text-sm" placeholder="ID, текст, имя, username" />
          </label>
        </div>
        <div class="flex items-center gap-2">
          <button id="tk-apply" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5"><i class="fa-solid fa-filter" aria-hidden="true"></i>Применить</button>
          <select id="tk-sort" class="select select-bordered select-sm h-9 min-h-9 text-sm w-[220px]">
            <option value="desc">Новые по активности</option>
            <option value="asc">Старые по активности</option>
          </select>
        </div>
      </div>
    </div>
    <div id="tk-grid" class="grid gap-4 md:grid-cols-2 mt-4"></div>
    <div id="tk-empty" class="hidden alert mt-4"><span>Нет тикетов по текущим фильтрам.</span></div>
    <script>
    (function(){
      var grid=document.getElementById('tk-grid');
      var empty=document.getElementById('tk-empty');
      var st=document.getElementById('tk-status');
      var topic=document.getElementById('tk-topic');
      var assigned=document.getElementById('tk-assigned');
      var billing=document.getElementById('tk-billing');
      var df=document.getElementById('tk-from');
      var dt=document.getElementById('tk-to');
      var q=document.getElementById('tk-q');
      var sort=document.getElementById('tk-sort');
      var apply=document.getElementById('tk-apply');
      var debounceTimer=null;
      var inFlight=null;
      function esc(s){return String(s||'').replace(/[&<>\"']/g,function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch]||ch;});}
      function statusBadge(s){
        if(s==='open')return '<span class=\"badge badge-info badge-sm\">Открыт</span>';
        if(s==='in_progress')return '<span class=\"badge badge-warning badge-sm\">В работе</span>';
        return '<span class=\"badge badge-ghost badge-sm\">Закрыт</span>';
      }
      function avatarFor(u){
        var uid=u&&u.id?u.id:'0';
        var nm=((u&&u.first_name)||'user');
        var initials=(nm[0]||'?').toUpperCase();
        return ''
          +'<div class=\"avatar placeholder\">'
          +'<div class=\"bg-base-300 text-base-content rounded-full w-10 h-10 overflow-hidden relative\">'
          +'<img src=\"/admin/users/'+uid+'/telegram-photo\" alt=\"\" loading=\"lazy\" decoding=\"async\" class=\"w-10 h-10 object-cover remna-avatar-img\" '
          +'onerror=\"this.remove();this.nextElementSibling.classList.remove(\\'hidden\\')\" />'
          +'<span class=\"hidden absolute inset-0 flex items-center justify-center\">'+esc(initials)+'</span>'
          +'</div></div>';
      }
      function card(t){
        var u=t.user||{};
        var uname=u.username?('@'+u.username):'—';
        var nm=(u.first_name||u.username||('user#'+u.id||'—'));
        var prev=esc((t.preview||'').slice(0,180));
        var ass=t.operator_id?('#'+t.operator_id):'—';
        var top=(t.topic_id!==undefined&&t.topic_id!==null)?('<span class=\"badge badge-ghost badge-xs\">topic '+esc(String(t.topic_id))+'</span>'):'';
        return ''
          +'<div class=\"card bg-base-100 border border-base-content/10 shadow-md hover:shadow-lg transition-shadow\">'
          +'<div class=\"card-body gap-3\">'
          +'<div class=\"flex items-start justify-between gap-2 flex-wrap\">'
          +'<div class=\"flex items-center gap-2 flex-wrap\"><h3 class=\"card-title text-lg\">Тикет #'+t.id+'</h3>'+top+'</div>'
          +statusBadge(t.status)+'</div>'
          +'<div class=\"flex items-center gap-3\">'+avatarFor(u)
          +'<div class=\"min-w-0\"><a class=\"link link-primary font-medium truncate block\" href=\"/admin/users/'+u.id+'\">'+esc(nm)+'</a>'
          +'<p class=\"text-xs opacity-70 truncate\">'+esc(uname)+'</p></div></div>'
          +'<p class=\"text-sm opacity-80 line-clamp-3\">'+prev+'</p>'
          +'<div class=\"text-xs opacity-70\">Создан: '+esc(t.created_at||'—')+'</div>'
          +'<div class=\"text-xs opacity-70\">Последняя активность: '+esc(t.last_activity||'—')+'</div>'
          +'<div class=\"text-xs opacity-70\">Назначен: '+esc(ass)+'</div>'
          +'<div class=\"card-actions justify-end\"><a class=\"btn btn-ghost btn-sm\" href=\"/admin/tickets/'+t.id+'\">Открыть</a></div>'
          +'</div></div>';
      }
      async function loadTickets(){
        if(inFlight){ try{ inFlight.abort(); }catch(e){} }
        inFlight=new AbortController();
        var p=new URLSearchParams();
        if(st.value)p.set('status',st.value);
        if(topic&&String(topic.value||'').trim()!=='') p.set('topic_id', String(parseInt(topic.value,10)||0));
        if(assigned&&assigned.value)p.set('assigned',assigned.value);
        if(billing&&billing.value)p.set('user_billing',billing.value);
        if(df.value)p.set('date_from',df.value);
        if(dt.value)p.set('date_to',dt.value);
        if((q.value||'').trim())p.set('q',q.value.trim());
        p.set('sort',sort.value||'desc');
        p.set('limit','200');
        try{
          var res=await fetch('/api/tickets?'+p.toString(),{credentials:'include',signal:inFlight.signal});
          if(!res.ok){grid.innerHTML='<div class=\"alert alert-error\"><span>Ошибка загрузки: '+res.status+'</span></div>';empty.classList.add('hidden');return;}
          var data=await res.json();
          var items=(data&&data.items)||[];
          if(!items.length){grid.innerHTML='';empty.classList.remove('hidden');return;}
          empty.classList.add('hidden');
          grid.innerHTML=items.map(card).join('');
        }catch(e){
          if(e && e.name==='AbortError')return;
          grid.innerHTML='<div class=\"alert alert-error\"><span>Ошибка загрузки списка тикетов.</span></div>';
          empty.classList.add('hidden');
        }
      }
      apply.addEventListener('click',function(){loadTickets();});
      q.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();loadTickets();}});
      q.addEventListener('input',function(){
        if(debounceTimer)clearTimeout(debounceTimer);
        debounceTimer=setTimeout(function(){loadTickets();},320);
      });
      st.addEventListener('change',loadTickets);
      if(topic) topic.addEventListener('change',loadTickets);
      if(assigned) assigned.addEventListener('change',loadTickets);
      if(billing) billing.addEventListener('change',loadTickets);
      df.addEventListener('change',loadTickets);
      dt.addEventListener('change',loadTickets);
      sort.addEventListener('change',loadTickets);
      loadTickets();
    })();
    </script>
    """
    return _layout("Web-admin Tickets", body, request=request)


@router.post("/payg/mass-convert")
async def admin_payg_mass_convert(
    request: Request,
    confirm_word: str = Form(""),
) -> RedirectResponse:
    global _DASHBOARD_HTML_CACHE
    denied = _require_login(request)
    if denied is not None:
        return denied
    if (confirm_word or "").strip().upper() != "PAYG":
        return RedirectResponse(
            "/admin?err=" + quote_plus("Подтверждение не прошло: введите PAYG."),
            status_code=303,
        )
    async with await _session() as session:
        changed, rw_changed, total_credit = await admin_convert_monthly_subscriptions_to_payg_balance(
            session,
            settings=get_settings(),
        )
        await session.commit()
    _USERS_HTML_CACHE.clear()
    _DASHBOARD_HTML_CACHE = None
    return RedirectResponse(
        "/admin?"
        + "&".join(
            [
                "n=mass_payg_done",
                f"c={quote_plus(str(changed))}",
                f"rw={quote_plus(str(rw_changed))}",
                f"amt={quote_plus(str(total_credit))}",
            ]
        ),
        status_code=303,
    )


@router.get("/tickets/{ticket_id}")
async def admin_ticket_detail_stub(request: Request, ticket_id: int) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    admin_opts: list[dict[str, object]] = []
    # Загружаем операторов из таблицы admin_users (RBAC) — db_id = admin_users.id
    async with await _session() as session:
        from shared.models.admin_user import AdminUser
        au_rows = (
            await session.execute(
                select(AdminUser, User)
                .join(User, User.id == AdminUser.user_id)
                .order_by(AdminUser.id.asc())
            )
        ).all()
        for au, u in au_rows:
            name = ((u.first_name or u.username or "").strip() or f"admin#{au.id}")
            label = f"#{u.id} {name}"
            admin_opts.append({
                "db_id": int(au.id),          # admin_users.id — FK в tickets.operator_id
                "tg_id": int(u.telegram_id),
                "label": label,
            })
    admins_json = json.dumps(admin_opts, ensure_ascii=False)
    body = f"""
    <div class="grid gap-4 lg:grid-cols-3 items-start">
      <div class="card bg-base-100 border border-base-content/10 shadow-lg lg:col-span-1 self-start">
        <div class="card-body gap-3">
          <h2 class="card-title text-2xl"><i class="fa-solid fa-ticket text-primary mr-2" aria-hidden="true"></i>Тикет #{ticket_id}</h2>
          <div id="tk-meta" class="text-sm opacity-80">Загрузка...</div>
          <div id="tk-user" class="text-sm border border-base-content/10 rounded-lg p-2 bg-base-200/30 mt-2 hidden"></div>
          <div id="tk-mgmt" class="text-sm border border-warning/25 rounded-lg p-2 bg-base-200/40 mt-2 hidden"></div>
          <div class="grid gap-2">
            <label class="form-control">
              <span class="label-text text-xs opacity-70">Назначенный админ</span>
              <select id="tk-assign" class="select select-bordered select-sm h-9 min-h-9 text-sm"></select>
            </label>
            <button id="tk-assign-save" class="btn btn-outline btn-sm h-9 min-h-9">Сохранить назначение</button>
          </div>
          <div class="flex flex-wrap gap-2 pt-1">
            <button id="tk-set-open" class="btn btn-ghost btn-sm h-9 min-h-9">Открыт</button>
            <button id="tk-set-progress" class="btn btn-warning btn-sm h-9 min-h-9">В работе</button>
            <button id="tk-set-closed" class="btn btn-error btn-sm h-9 min-h-9">Закрыть</button>
          </div>
        </div>
      </div>
      <div class="card bg-base-100 border border-base-content/10 shadow-lg lg:col-span-2 self-start">
        <div class="card-body gap-3">
          <h3 class="card-title text-xl"><i class="fa-solid fa-comments text-primary mr-2" aria-hidden="true"></i>Диалог</h3>
          <div id="tk-chat" class="max-h-[62vh] overflow-y-auto rounded-xl border border-base-content/10 bg-base-200/40 p-3 space-y-2"></div>
          <div id="tk-compose" class="grid gap-2">
            <div class="flex items-start gap-2">
              <input id="tk-file-input" type="file" accept="image/*,video/*" class="hidden"/>
              <button id="tk-attach" class="btn btn-ghost btn-sm btn-square h-10 min-h-10" title="Прикрепить фото/видео"><i class="fa-solid fa-paperclip" aria-hidden="true"></i></button>
              <textarea id="tk-text" class="textarea textarea-bordered min-h-[44px] h-[44px] max-h-56 resize-none flex-1" placeholder="Введите ответ пользователю или заметку (Enter = отправить, Shift+Enter = новая строка)"></textarea>
              <button id="tk-send-reply" class="btn btn-primary btn-sm btn-square h-10 min-h-10" title="Отправить ответ"><i class="fa-solid fa-paper-plane" aria-hidden="true"></i></button>
            </div>
            <button id="tk-send-note" class="btn btn-outline btn-sm w-full h-10 min-h-10" title="Добавить внутреннюю заметку"><i class="fa-solid fa-note-sticky mr-2" aria-hidden="true"></i>Добавить заметку</button>
            <button id="tk-sound-toggle" class="btn btn-ghost btn-xs self-end" type="button" title="Вкл/выкл звук уведомлений">Звук: вкл</button>
          </div>
        </div>
      </div>
    </div>
    <div id="tk-photo-lb" class="hidden fixed inset-0 z-[60] flex items-center justify-center bg-black/80 p-6 cursor-zoom-out" role="dialog" aria-modal="true" aria-label="Просмотр вложения">
      <a id="tk-media-download" href="#" download class="absolute top-5 right-5 btn btn-circle btn-sm btn-ghost text-white/90 hover:text-white" title="Скачать"><i class="fa-solid fa-download"></i></a>
      <img id="tk-photo-lb-img" src="" alt="" class="hidden max-h-[90vh] max-w-[min(95vw,1280px)] w-auto h-auto object-contain rounded-lg shadow-2xl ring-2 ring-white/10 cursor-default pointer-events-auto" decoding="async"/>
      <video id="tk-photo-lb-video" class="hidden max-h-[90vh] max-w-[min(95vw,1280px)] rounded-lg shadow-2xl ring-2 ring-white/10 cursor-default pointer-events-auto" controls playsinline></video>
    </div>
    <style>
    .tk-msg-enter {{
      animation: tkMsgIn .25s ease-out both;
    }}
    @keyframes tkMsgIn {{
      from {{ opacity: .0; transform: translateY(6px) scale(.99); }}
      to {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}
    </style>
    <script>
    (function(){{
      var ticketId={ticket_id};
      var admins={admins_json};
      var stMap={{"open":["badge-info","Открыт"],"in_progress":["badge-warning","В работе"],"closed":["badge-ghost","Закрыт"]}};
      var meta=document.getElementById('tk-meta');
      var user=document.getElementById('tk-user');
      var chat=document.getElementById('tk-chat');
      var lb=document.getElementById('tk-photo-lb');
      var lbImg=document.getElementById('tk-photo-lb-img');
      var lbVideo=document.getElementById('tk-photo-lb-video');
      var mediaDownload=document.getElementById('tk-media-download');
      var txt=document.getElementById('tk-text');
      var fileInput=document.getElementById('tk-file-input');
      var assign=document.getElementById('tk-assign');
      var compose=document.getElementById('tk-compose');
      var model=null;
      var lastSig='';
      var notifyInit=false;
      var lastMsgCount=0;
      var soundEnabled=true;
      var loadInFlight=false;
      function esc(s){{return String(s||'').replace(/[&<>\"']/g,function(ch){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[ch]||ch;}});}}
      function modelSig(m){{
        var t=(m&&m.ticket)||{{}};
        var msgs=(m&&m.messages)||[];
        var last=msgs.length?msgs[msgs.length-1]:null;
        var r=m&&m.rating;
        return [
          String(t.status||''),
          String(t.last_activity||''),
          String(msgs.length),
          String(last&&last.id||''),
          String(last&&last.created_at||''),
          String(last&&last.text||''),
          String(last&&last.photo_file_id||''),
          String(last&&last.video_file_id||''),
          String(r&&r.value!==undefined?r.value:'')
        ].join('|');
      }}
      function loadSoundPref() {{
        try {{
          var raw=localStorage.getItem('remna_ticket_sound_enabled');
          soundEnabled = raw===null ? true : raw==='1';
        }} catch(_e) {{
          soundEnabled = true;
        }}
      }}
      function saveSoundPref() {{
        try {{ localStorage.setItem('remna_ticket_sound_enabled', soundEnabled?'1':'0'); }} catch(_e) {{}}
      }}
      function updateSoundBtn() {{
        var b=document.getElementById('tk-sound-toggle');
        if(!b) return;
        b.textContent='Звук: '+(soundEnabled?'вкл':'выкл');
      }}
      function playNotifyTone() {{
        if(!soundEnabled || document.hidden) return;
        try {{
          var Ctx=window.AudioContext||window.webkitAudioContext;
          if(!Ctx) return;
          var ctx=new Ctx();
          var osc=ctx.createOscillator();
          var gain=ctx.createGain();
          osc.type='sine';
          osc.frequency.value=930;
          gain.gain.value=0.0001;
          osc.connect(gain); gain.connect(ctx.destination);
          var now=ctx.currentTime;
          gain.gain.exponentialRampToValueAtTime(0.07, now+0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, now+0.20);
          osc.start(now);
          osc.stop(now+0.22);
        }} catch(_e) {{}}
      }}
      function initAssign() {{
        assign.innerHTML='';
        var opt=document.createElement('option');opt.value='';opt.textContent='— не назначен —';assign.appendChild(opt);
        admins.forEach(function(a){{
          var o=document.createElement('option');
          o.value=String(a.tg_id||'');
          o.textContent=String(a.label||('admin:'+a.tg_id));
          o.dataset.dbId=(a.db_id===null||a.db_id===undefined)?'':String(a.db_id);
          assign.appendChild(o);
        }});
      }}
      function renderMeta() {{
        if(!model) return;
        var t=model.ticket||{{}};
        var u=t.user_id||'—';
        var tg=t.telegram_user_id||0;
        var st=t.status||'open';
        var b=stMap[st]||['badge-ghost',st];
        meta.innerHTML=''
          +'Статус: <span class="badge '+b[0]+' badge-sm">'+esc(b[1])+'</span><br>'
          +'Пользователь: <a class="link link-primary" href="/admin/users/'+u+'">#'+u+'</a>'+(tg?(' · <a class="link" href="tg://user?id='+tg+'">tg://user?id='+tg+'</a>'):'')+'<br>'
          +'Создан: '+esc(t.created_at||'—')+'<br>'
          +'Последняя активность: '+esc(t.last_activity||'—')+'<br>'
          +'Закрыт: '+esc(t.closed_at||'—');
        compose.classList.toggle('hidden', st==='closed');
      }}
      function renderUserPanel() {{
        if(!user) return;
        var u=model&&model.user;
        var s=model&&model.user_subscription;
        if(!u){{ user.innerHTML=''; user.classList.add('hidden'); return; }}
        user.classList.remove('hidden');
        var name=((u.first_name||'')+' '+(u.last_name||'')).trim()||'—';
        var un=u.username?('@'+u.username):'—';
        var sub=s
          ? '<p class="text-xs mt-1">Подписка: <span class="badge badge-success badge-sm">'+esc(s.status)+'</span> '+esc(s.plan_name||'')+' · до '+esc(s.expires_at||'')+'</p>'
          : '<p class="text-xs opacity-60 mt-1">Активной подписки в боте нет</p>';
        user.innerHTML='<div class="font-semibold">'+esc(name)+'</div>'
          +'<p class="text-xs opacity-70">'+esc(un)+' · tg id '+esc(String(u.telegram_id))+'</p>'
          +'<p class="text-xs">Баланс: <b>'+esc(String(u.balance))+' ₽</b>'
          +(u.is_blocked?' · <span class="badge badge-error badge-sm">заблокирован</span>':'')
          +' · <a class="link link-primary" href="/admin/users/'+u.id+'">Профиль</a></p>'
          +sub;
      }}
      function renderMgmt() {{
        var m=document.getElementById('tk-mgmt');
        if(!m) return;
        var u=model&&model.user;
        var s=model&&model.user_subscription;
        var lc=model&&model.last_cancelled_subscription_id;
        if(!u||u.is_blocked){{ m.classList.add('hidden'); m.innerHTML=''; return; }}
        m.classList.remove('hidden');
        var base='/admin/tickets/'+ticketId+'/user';
        var html='<div class="font-semibold text-warning">Управление пользователем</div>';
        html+='<form method="post" action="'+base+'/add-balance" class="flex flex-wrap gap-2 items-end mt-2">'
          +'<label class="form-control"><span class="label-text text-xs">Баланс +₽</span>'
          +'<input type="text" name="amount" class="input input-bordered input-sm w-28" placeholder="0" required/></label>'
          +'<button type="submit" class="btn btn-primary btn-sm">Пополнить</button>'
          +'</form>';
        if(s&&s.id){{
          html+='<form method="post" action="'+base+'/add-months" class="flex flex-wrap gap-2 items-end mt-2">'
            +'<input type="hidden" name="subscription_id" value="'+s.id+'"/>'
            +'<label class="form-control"><span class="label-text text-xs">Продление (мес.)</span>'
            +'<input type="number" name="months" min="1" max="120" class="input input-bordered input-sm w-24" value="1" required/></label>'
            +'<button type="submit" class="btn btn-outline btn-sm">Продлить</button>'
            +'<div class="w-full flex items-center gap-1 text-xs opacity-80">'
            +'<span>Быстро:</span>'
            +'<button type="button" class="btn btn-ghost btn-xs" onclick="this.closest(&quot;form&quot;).querySelector(&quot;input[name=months]&quot;).value=1">1</button>'
            +'<button type="button" class="btn btn-ghost btn-xs" onclick="this.closest(&quot;form&quot;).querySelector(&quot;input[name=months]&quot;).value=3">3</button>'
            +'<button type="button" class="btn btn-ghost btn-xs" onclick="this.closest(&quot;form&quot;).querySelector(&quot;input[name=months]&quot;).value=6">6</button>'
            +'<button type="button" class="btn btn-ghost btn-xs" onclick="this.closest(&quot;form&quot;).querySelector(&quot;input[name=months]&quot;).value=12">12</button>'
            +'</div>'
            +'</form>';
          var ar=!!s.auto_renew;
          var nxt=ar?'0':'1';
          var lbl=ar?'Выключить авто-продление':'Включить авто-продление';
          html+='<form method="post" action="'+base+'/subscription/auto-renew" class="mt-2">'
            +'<input type="hidden" name="enabled" value="'+nxt+'"/>'
            +'<button type="submit" class="btn btn-ghost btn-xs">'+esc(lbl)+'</button>'
            +'</form>';
          html+='<form method="post" action="'+base+'/subscription/disable" class="mt-2" data-remna-confirm-msg="Отключить подписку пользователя?">'
            +'<input type="hidden" name="subscription_id" value="'+s.id+'"/>'
            +'<button type="submit" class="btn btn-error btn-outline btn-sm">Отключить подписку</button>'
            +'</form>';
        }}
        if(!s&&lc){{
          html+='<form method="post" action="'+base+'/subscription/enable" class="mt-2">'
            +'<input type="hidden" name="subscription_id" value="'+lc+'"/>'
            +'<button type="submit" class="btn btn-success btn-sm">Включить отключённую подписку</button>'
            +'</form>';
        }}
        m.innerHTML=html;
      }}
      function renderRating() {{
        var r=model&&model.rating;
        var existing=document.getElementById('tk-rating-block');
        if(r&&r.value!==undefined) {{
          var isPos=r.is_positive;
          var badge=isPos?'badge-success':'badge-error';
          var icon=isPos?'fa-thumbs-up':'fa-thumbs-down';
          var label=isPos?'Положительная':'Отрицательная';
          var html='<div id="tk-rating-block" class="flex items-center gap-2 px-3 py-2 rounded-xl border '+
            (isPos?'border-success/30 bg-success/10':'border-error/30 bg-error/10')+'">'
            +'<i class="fa-solid '+icon+' '+(isPos?'text-success':'text-error')+'"></i>'
            +'<span class="text-sm font-medium">Оценка поддержки:</span>'
            +'<span class="badge '+badge+' badge-sm">'+esc(label)+'</span>'
            +'<span class="text-xs opacity-60">'+esc(r.created_at||'')+'</span>'
            +'</div>';
          if(existing) {{ existing.outerHTML=html; }}
          else {{
            var chatParent=chat&&chat.parentNode;
            if(chatParent) {{
              var ratingEl=document.createElement('div');
              ratingEl.innerHTML=html;
              chatParent.insertBefore(ratingEl.firstChild, chat.nextSibling);
            }}
          }}
        }} else if(existing) {{
          existing.remove();
        }}
      }}
      function renderChat(shouldStickBottom) {{
        var msgs=(model&&model.messages)||[];
        if(!msgs.length) {{
          chat.innerHTML='<div class="opacity-60 text-sm">Сообщений пока нет.</div>'; return;
        }}
        chat.innerHTML=msgs.map(function(m){{
          var left=m.sender_role==='user';
          var note=!!m.is_internal;
          var cls=note?'bg-warning/15 border-warning/35':(left?'bg-base-100 border-base-content/15':'bg-primary/10 border-primary/30');
          var row=left?'justify-start':'justify-end';
          var who=note?'Заметка':(left?'Пользователь':'Администратор');
          if(!left && m.sender_label){{ who='Администратор '+String(m.sender_label); }}
          var mediaHtml='';
          if(m.photo_file_id){{
            var psrc='/api/tickets/'+ticketId+'/messages/'+m.id+'/photo';
            mediaHtml+='<div class="mt-2 relative"><img src="'+psrc+'" alt="" title="Нажмите, чтобы открыть крупно" data-kind="image" class="tk-ticket-thumb max-h-64 max-w-full rounded-lg border border-base-content/10 object-contain bg-base-300/30 cursor-pointer hover:opacity-90 transition-opacity" loading="lazy" decoding="async"/><a href="'+psrc+'" download class="btn btn-xs btn-circle absolute top-2 right-2" title="Скачать"><i class="fa-solid fa-download"></i></a></div>';
          }}
          if(m.video_file_id){{
            var vsrc='/api/tickets/'+ticketId+'/messages/'+m.id+'/video';
            mediaHtml+='<div class="tk-vid-wrap mt-2 relative rounded-xl overflow-hidden bg-black cursor-pointer" style="max-width:300px"><video class="tk-vid-video block w-full" src="'+vsrc+'" preload="metadata" playsinline style="max-height:220px;object-fit:contain"></video><button type="button" class="tk-vid-playbtn absolute inset-0 flex items-center justify-center transition-opacity" style="background:rgba(0,0,0,.18)"><div class="w-11 h-11 rounded-full flex items-center justify-center" style="background:rgba(0,0,0,.6)"><i class="fa-solid fa-play text-white tk-vid-icon" style="font-size:16px;margin-left:2px"></i></div></button><div class="tk-vid-controls absolute bottom-0 left-0 right-0 flex items-center gap-2 px-3 py-2" style="background:linear-gradient(transparent,rgba(0,0,0,.75))"><span class="tk-vid-time text-white text-xs" style="min-width:38px;font-variant-numeric:tabular-nums">0:00</span><div class="tk-vid-track flex-1 relative rounded-full cursor-pointer" style="height:3px;background:rgba(255,255,255,.3)"><div class="tk-vid-fill h-full rounded-full" style="width:0%;background:rgba(255,255,255,.9)"></div></div><a href="'+vsrc+'" download class="text-white/70 hover:text-white ml-1" title="Скачать" onclick="event.stopPropagation()"><i class="fa-solid fa-download" style="font-size:11px"></i></a></div></div>';
          }}
          if(m.document_file_id){{
            var dsrc='/api/tickets/'+ticketId+'/messages/'+m.id+'/document';
            var dname=esc(m.document_file_name||'document');
            mediaHtml+='<div class="mt-2"><a href="'+dsrc+'" class="btn btn-sm btn-ghost border border-base-content/15" download><i class="fa-solid fa-paperclip mr-2"></i>'+dname+'</a></div>';
          }}
          if(m.voice_file_id){{
            var vsrc2='/api/tickets/'+ticketId+'/messages/'+m.id+'/voice';
            mediaHtml+='<div class="mt-2 flex items-center gap-2"><i class="fa-solid fa-microphone text-primary opacity-70 text-sm"></i><audio controls preload="metadata" class="max-w-full h-8" style="min-width:180px"><source src="'+vsrc2+'" type="audio/ogg"></audio><a href="'+vsrc2+'" download class="btn btn-xs btn-circle btn-ghost" title="Скачать"><i class="fa-solid fa-download text-xs"></i></a></div>';
          }}
          if(m.video_note_file_id){{
            var vnsrc='/api/tickets/'+ticketId+'/messages/'+m.id+'/video-note';
            mediaHtml+='<div class="tk-vn-wrap mt-2 flex flex-col items-start" style="gap:4px"><div class="relative" style="width:112px;height:112px"><video class="tk-vn-video" src="'+vnsrc+'" preload="metadata" playsinline style="width:112px;height:112px;border-radius:50%;object-fit:cover;background:#000;display:block"></video><button type="button" class="tk-vn-playbtn" style="position:absolute;inset:0;border-radius:50%;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.4);border:none;cursor:pointer;transition:background .15s"><i class="fa-solid fa-play text-white tk-vn-icon" style="font-size:22px;margin-left:3px"></i></button><a href="'+vnsrc+'" download style="position:absolute;bottom:2px;right:2px" class="btn btn-xs btn-circle bg-base-300/90 border border-base-content/20 shadow" title="Скачать" onclick="event.stopPropagation()"><i class="fa-solid fa-download" style="font-size:10px"></i></a></div><span class="text-xs opacity-60">Видеосообщение</span></div>';
          }}
          if(m.audio_file_id){{
            var asrc='/api/tickets/'+ticketId+'/messages/'+m.id+'/audio';
            var aname=esc(m.audio_file_name||'Аудио');
            mediaHtml+='<div class="mt-2 flex flex-col gap-1"><div class="flex items-center gap-2"><i class="fa-solid fa-music text-primary opacity-70 text-sm"></i><audio controls preload="metadata" class="max-w-full h-8" style="min-width:200px"><source src="'+asrc+'" type="audio/mpeg"></audio><a href="'+asrc+'" download class="btn btn-xs btn-circle btn-ghost" title="Скачать"><i class="fa-solid fa-download text-xs"></i></a></div><span class="text-xs opacity-50">'+aname+'</span></div>';
          }}
          var textHtml=(m.text&&String(m.text).trim())?('<div class="whitespace-pre-wrap break-words text-sm">'+esc(m.text||'')+'</div>'):'';
          return ''
            +'<div class="flex w-full '+row+'">'
            +'<div class="max-w-[88%] rounded-xl border px-3 py-2 '+cls+' tk-msg-enter">'
            +'<div class="text-xs opacity-70 mb-1">'+esc(who)+' · '+esc(m.created_at||'')+'</div>'
            +textHtml
            +mediaHtml
            +'</div></div>';
        }}).join('');
        if(shouldStickBottom) chat.scrollTop=chat.scrollHeight;
        setupCustomPlayers();
      }}
      function fmtVidTime(s){{
        s=Math.floor(s||0);
        return Math.floor(s/60)+':'+(s<0?'00':('0'+(s%60)).slice(-2));
      }}
      function setupCustomPlayers(){{
        if(!chat)return;
        // Video notes (circles)
        chat.querySelectorAll('.tk-vn-wrap').forEach(function(wrap){{
          if(wrap._tkReady)return; wrap._tkReady=true;
          var video=wrap.querySelector('.tk-vn-video');
          var btn=wrap.querySelector('.tk-vn-playbtn');
          var icon=btn&&btn.querySelector('.tk-vn-icon');
          if(!video)return;
          btn&&btn.addEventListener('click',function(e){{
            e.stopPropagation();
            if(video.paused){{video.play();}}else{{video.pause();}}
          }});
          video.addEventListener('play',function(){{
            if(icon)icon.className='fa-solid fa-pause text-white tk-vn-icon';
            if(icon)icon.style.marginLeft='0';
            if(btn)btn.style.background='rgba(0,0,0,.15)';
          }});
          video.addEventListener('pause',function(){{
            if(icon)icon.className='fa-solid fa-play text-white tk-vn-icon';
            if(icon)icon.style.marginLeft='3px';
            if(btn)btn.style.background='rgba(0,0,0,.4)';
          }});
          video.addEventListener('ended',function(){{
            video.currentTime=0;
            if(icon)icon.className='fa-solid fa-play text-white tk-vn-icon';
            if(icon)icon.style.marginLeft='3px';
            if(btn)btn.style.background='rgba(0,0,0,.4)';
          }});
        }});
        // Regular videos
        chat.querySelectorAll('.tk-vid-wrap').forEach(function(wrap){{
          if(wrap._tkReady)return; wrap._tkReady=true;
          var video=wrap.querySelector('.tk-vid-video');
          var btn=wrap.querySelector('.tk-vid-playbtn');
          var icon=btn&&btn.querySelector('.tk-vid-icon');
          var timeEl=wrap.querySelector('.tk-vid-time');
          var fill=wrap.querySelector('.tk-vid-fill');
          var track=wrap.querySelector('.tk-vid-track');
          if(!video)return;
          btn&&btn.addEventListener('click',function(e){{
            e.stopPropagation();
            if(video.paused){{video.play();}}else{{video.pause();}}
          }});
          video.addEventListener('play',function(){{
            if(icon)icon.className='fa-solid fa-pause text-white tk-vid-icon';
            if(icon)icon.style.marginLeft='0';
            if(btn)btn.style.opacity='0';
          }});
          video.addEventListener('pause',function(){{
            if(icon)icon.className='fa-solid fa-play text-white tk-vid-icon';
            if(icon)icon.style.marginLeft='2px';
            if(btn)btn.style.opacity='1';
          }});
          video.addEventListener('ended',function(){{
            video.currentTime=0;
            if(icon)icon.className='fa-solid fa-play text-white tk-vid-icon';
            if(icon)icon.style.marginLeft='2px';
            if(btn)btn.style.opacity='1';
          }});
          video.addEventListener('loadedmetadata',function(){{
            if(timeEl&&video.duration)timeEl.textContent='0:00 / '+fmtVidTime(video.duration);
          }});
          video.addEventListener('timeupdate',function(){{
            if(!video.duration)return;
            var p=(video.currentTime/video.duration)*100;
            if(fill)fill.style.width=p+'%';
            if(timeEl)timeEl.textContent=fmtVidTime(video.currentTime)+' / '+fmtVidTime(video.duration);
          }});
          track&&track.addEventListener('click',function(e){{
            e.stopPropagation();
            var rect=track.getBoundingClientRect();
            video.currentTime=((e.clientX-rect.left)/rect.width)*(video.duration||0);
          }});
        }});
      }}
      function closePhotoLb() {{
        if(!lb) return;
        lb.classList.add('hidden');
        if(lbImg) {{ lbImg.removeAttribute('src'); lbImg.classList.add('hidden'); }}
        if(lbVideo) {{ lbVideo.pause(); lbVideo.removeAttribute('src'); lbVideo.classList.add('hidden'); }}
        if(mediaDownload) mediaDownload.setAttribute('href', '#');
        document.body.classList.remove('overflow-hidden');
      }}
      function openPhotoLb(src, kind) {{
        if(!lb||!src) return;
        if(lbImg) lbImg.classList.add('hidden');
        if(lbVideo) lbVideo.classList.add('hidden');
        if(kind==='video' && lbVideo){{
          lbVideo.src=src;
          lbVideo.classList.remove('hidden');
        }} else if(lbImg) {{
          lbImg.src=src;
          lbImg.classList.remove('hidden');
        }}
        if(mediaDownload) mediaDownload.setAttribute('href', src);
        lb.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
      }}
      if(lb){{
        lb.addEventListener('click', function(e) {{
          if(e.target===lbImg || e.target===lbVideo || e.target===mediaDownload) return;
          closePhotoLb();
        }});
        if(lbImg) lbImg.addEventListener('click', function(e) {{ e.stopPropagation(); }});
        if(lbVideo) lbVideo.addEventListener('click', function(e) {{ e.stopPropagation(); }});
      }}
      document.addEventListener('keydown', function(e) {{
        if(e.key!=='Escape') return;
        if(lb&&!lb.classList.contains('hidden')) closePhotoLb();
      }});
      if(chat){{
        chat.addEventListener('click', function(e) {{
          var t=e.target;
          if(!t||!t.closest) return;
          var img=t.closest('img.tk-ticket-thumb');
          if(!img||!chat.contains(img)) return;
          e.preventDefault();
          openPhotoLb(img.getAttribute('src')||'', img.getAttribute('data-kind')==='video'?'video':'image');
        }});
      }}
      async function load() {{
        if(loadInFlight) return;
        loadInFlight=true;
        try {{
          var prevTop=chat?chat.scrollTop:0;
          var prevHeight=chat?chat.scrollHeight:0;
          var wasNearBottom=chat?(prevTop+chat.clientHeight>=prevHeight-24):true;
          var r=await fetch('/api/tickets/'+ticketId,{{credentials:'include'}});
          if(!r.ok){{meta.textContent='Ошибка загрузки: HTTP '+r.status;chat.innerHTML='<div class="text-error text-sm">HTTP '+r.status+'</div>';return;}}
          var ct=(r.headers.get('content-type')||'');
          if(ct.indexOf('application/json')===-1){{meta.textContent='Ответ не JSON (проверьте, что /api открыт на этом же домене)';chat.innerHTML='';return;}}
          var nextModel=await r.json();
          var nextMsgs=(nextModel&&nextModel.messages)||[];
          if(notifyInit && nextMsgs.length>lastMsgCount){{
            var lastIncoming=nextMsgs[nextMsgs.length-1];
            if(lastIncoming && lastIncoming.sender_role==='user') {{
              playNotifyTone();
            }}
          }}
          lastMsgCount=nextMsgs.length;
          notifyInit=true;
          var sig=modelSig(nextModel);
          if(sig===lastSig) return;
          model=nextModel;
          renderMeta(); renderUserPanel(); renderMgmt(); renderChat(wasNearBottom); renderRating(); setupCustomPlayers();
          if(chat&&!wasNearBottom){{
            var newHeight=chat.scrollHeight;
            chat.scrollTop=Math.max(0, prevTop + (newHeight - prevHeight));
          }}
          lastSig=sig;
          var atg=(model.ticket&&model.ticket.telegram_assigned_admin_id)||'';
          assign.value=atg?String(atg):'';
        }} catch(e) {{
          meta.textContent='Ошибка разбора ответа: '+(e&&e.message?e.message:String(e));
          chat.innerHTML='<div class="text-error text-sm opacity-90">Не удалось отобразить тикет. Откройте консоль браузера (F12) и вкладку Network для /api/tickets/'+ticketId+'.</div>';
        }} finally {{
          loadInFlight=false;
        }}
      }}
      async function sendJson(url, method, data) {{
        var r=await fetch(url,{{method:method,credentials:'include',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(data||{{}})}});
        if(!r.ok) throw new Error('HTTP '+r.status);
        return await r.json();
      }}
      function failToast(message){{
        if(window.remnaToast) window.remnaToast('error', message||'Ошибка');
      }}
      async function sendMedia(isInternal) {{
        var f=fileInput&&fileInput.files&&fileInput.files[0];
        if(!f) return;
        var fd=new FormData();
        fd.append('file', f);
        fd.append('text_value', (txt.value||'').trim());
        fd.append('is_internal', isInternal ? 'true' : 'false');
        var r=await fetch('/api/tickets/'+ticketId+'/reply-media',{{method:'POST',credentials:'include',body:fd}});
        if(!r.ok) throw new Error('HTTP '+r.status);
        fileInput.value='';
      }}
      function autosizeText() {{
        if(!txt) return;
        txt.style.height='44px';
        txt.style.height=Math.min(txt.scrollHeight, 224)+'px';
      }}
      document.getElementById('tk-send-reply').addEventListener('click', async function(){{
        var v=(txt.value||'').trim(); if(!v) return;
        try{{await sendJson('/api/tickets/'+ticketId+'/reply','POST',{{text:v}}); txt.value=''; await load();}}catch(e){{failToast('Ошибка отправки ответа');}}
      }});
      document.getElementById('tk-send-note').addEventListener('click', async function(){{
        var v=(txt.value||'').trim(); if(!v) return;
        try{{await sendJson('/api/tickets/'+ticketId+'/note','POST',{{text:v}}); txt.value=''; await load();}}catch(e){{failToast('Ошибка добавления заметки');}}
      }});
      document.getElementById('tk-set-open').addEventListener('click', async function(){{try{{await sendJson('/api/tickets/'+ticketId+'/status','PATCH',{{status:'open'}});await load();}}catch(e){{failToast('Не удалось сменить статус');}}}});
      document.getElementById('tk-set-progress').addEventListener('click', async function(){{try{{await sendJson('/api/tickets/'+ticketId+'/status','PATCH',{{status:'in_progress'}});await load();}}catch(e){{failToast('Не удалось сменить статус');}}}});
      document.getElementById('tk-set-closed').addEventListener('click', async function(){{
        var ok=false;
        try{{ ok = await window.remnaConfirmPromise('Закрыть тикет?','Подтверждение'); }}catch(_e){{ return; }}
        if(!ok)return;
        try{{await sendJson('/api/tickets/'+ticketId+'/status','PATCH',{{status:'closed'}});await load();}}catch(e){{failToast('Не удалось закрыть тикет');}}
      }});
      document.getElementById('tk-assign-save').addEventListener('click', async function(){{
        var tg=assign.value||'';
        var db=assign.options[assign.selectedIndex] ? (assign.options[assign.selectedIndex].dataset.dbId||'') : '';
        try{{await sendJson('/api/tickets/'+ticketId+'/assign','PATCH',{{operator_id:db?parseInt(db,10):null,telegram_assigned_admin_id:tg?parseInt(tg,10):null}});await load();}}catch(e){{failToast('Не удалось сохранить назначение');}}
      }});
      if(txt){{
        txt.addEventListener('input', autosizeText);
        txt.addEventListener('keydown', async function(e){{
          if(e.key!=='Enter' || e.shiftKey) return;
          e.preventDefault();
          var v=(txt.value||'').trim();
          if(!v) return;
          try{{await sendJson('/api/tickets/'+ticketId+'/reply','POST',{{text:v}}); txt.value=''; autosizeText(); await load();}}catch(_e){{}}
        }});
        autosizeText();
      }}
      var soundBtn=document.getElementById('tk-sound-toggle');
      if(soundBtn){{
        soundBtn.addEventListener('click', function(){{
          soundEnabled=!soundEnabled;
          saveSoundPref();
          updateSoundBtn();
        }});
      }}
      var attachBtn=document.getElementById('tk-attach');
      if(attachBtn&&fileInput){{
        attachBtn.addEventListener('click', function(){{ fileInput.click(); }});
        fileInput.addEventListener('change', async function(){{
          if(!fileInput.files||!fileInput.files.length) return;
          try{{await sendMedia(false); await load();}}catch(e){{failToast('Ошибка отправки файла');}}
        }});
      }}
      var liveTimer=null;
      var notifyInited=false;
      var lastTicketCount=0;
      async function loadTicketCount(){{
        try{{
          var r=await fetch('/api/tickets?status=open&limit=1',{{credentials:'include'}});
          if(!r.ok) return;
          var d=await r.json();
          var c=Number(d&&d.count||0);
          if(notifyInited && c>lastTicketCount && document.hidden && 'Notification' in window && Notification.permission==='granted'){{
            new Notification('Новый тикет',{{body:'Поступил новый тикет в поддержку'}});
          }}
          lastTicketCount=c;
          notifyInited=true;
        }} catch(_e) {{}}
      }}
      function startLive(){{
        if(liveTimer)clearInterval(liveTimer);
        liveTimer=setInterval(function(){{
          if(document.hidden)return;
          load();
          loadTicketCount();
        }},2500);
      }}
      document.addEventListener('visibilitychange', function(){{
        if(document.hidden)return;
        load();
      }});
      window.addEventListener('beforeunload', function(){{
        if(liveTimer)clearInterval(liveTimer);
      }});
      if('Notification' in window && Notification.permission==='default'){{ Notification.requestPermission(); }}
      loadSoundPref();
      updateSoundBtn();
      initAssign(); load(); loadTicketCount(); startLive();
    }})();
    </script>
    """
    return _layout(f"Ticket {ticket_id}", body, request=request, back_href="/admin/tickets")


async def _web_lookup_remnawave_by_tg_or_username(
    rw: RemnaWaveClient,
    query: str,
) -> tuple[dict | None, str]:
    typed = (query or "").strip()
    if not typed:
        return None, "empty"
    if typed.isdigit():
        return await rw.find_user_by_telegram_id(int(typed)), "telegram_id"
    return await rw.find_user_by_username(typed), "username"


@router.get("/users")
async def admin_users(
    request: Request,
    q: str = "",
    page: int = 1,
    sub: str = "",
    blocked: str = "",
    risk: str = "",
    bill: str = "",
    gh: str = "",
    usage: str = "",
    sort: str = "",
    dev_slots: str = "",
) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    want_partial = (request.headers.get("hx-request") or "").strip().lower() == "true"
    needle = q.strip()
    sub_f = (sub or "").strip().lower()
    blk_f = (blocked or "").strip()
    risk_f = (risk or "").strip().lower()
    bill_f = (bill or "").strip().lower()
    gh_f = (gh or "").strip().lower()
    usage_f = (usage or "").strip().lower()
    sort_f = (sort or "").strip().lower()
    dev_slots_f = (dev_slots or "").strip()
    page = max(1, page)
    cache_key = (needle.casefold(), page, sub_f, blk_f, risk_f, bill_f, gh_f, usage_f, sort_f, dev_slots_f)
    now_m = time.monotonic()
    if not want_partial:
        cached_users = _USERS_HTML_CACHE.get(cache_key)
        if cached_users is not None and now_m - cached_users[0] < _USERS_HTML_TTL_SEC:
            return _layout("Web-admin Users", cached_users[1], request=request)
    if len(_USERS_HTML_CACHE) > 128:
        stale_keys = [k for k, v in _USERS_HTML_CACHE.items() if now_m - v[0] >= _USERS_HTML_TTL_SEC]
        for k in stale_keys:
            _USERS_HTML_CACHE.pop(k, None)
    per_page = 15
    async with await _session() as session:
        now_for_filter = datetime.now(timezone.utc)
        active_sub_exists = exists().where(
            and_(
                Subscription.user_id == User.id,
                Subscription.status.in_(("active", "trial")),
                Subscription.expires_at > now_for_filter,
            )
        )
        risk_priority = case(
            (User.risk_notified_1h_at.is_not(None), 2),
            (User.risk_notified_24h_at.is_not(None), 1),
            else_=0,
        )
        query = select(User)
        count_query = select(func.count()).select_from(User)
        if needle:
            if needle.isdigit():
                # Защита от переполнения bigint в БД: слишком длинный numeric-запрос
                # не должен падать 500, а просто давать пустую выборку.
                if len(needle) > 19:
                    query = query.where(text("1=0"))
                    count_query = count_query.where(text("1=0"))
                else:
                    tid = int(needle)
                    if tid > _INT64_MAX:
                        query = query.where(text("1=0"))
                        count_query = count_query.where(text("1=0"))
                    else:
                        conds = [User.telegram_id == tid]
                        if tid <= _INT32_MAX:
                            conds.append(User.id == tid)
                        query = query.where(or_(*conds))
                        count_query = count_query.where(or_(*conds))
            else:
                search_filter = or_(
                    User.username.ilike(f"%{needle}%"),
                    User.first_name.ilike(f"%{needle}%"),
                    User.last_name.ilike(f"%{needle}%"),
                    User.github_username.ilike(f"%{needle}%"),
                )
                query = query.where(search_filter)
                count_query = count_query.where(search_filter)
        if sub_f == "active":
            query = query.where(active_sub_exists)
            count_query = count_query.where(active_sub_exists)
        elif sub_f == "none":
            query = query.where(~active_sub_exists)
            count_query = count_query.where(~active_sub_exists)
        if blk_f == "1":
            query = query.where(User.is_blocked.is_(True))
            count_query = count_query.where(User.is_blocked.is_(True))
        elif blk_f == "0":
            query = query.where(User.is_blocked.is_(False))
            count_query = count_query.where(User.is_blocked.is_(False))
        if risk_f == "24h":
            query = query.where(User.risk_notified_24h_at.is_not(None))
            count_query = count_query.where(User.risk_notified_24h_at.is_not(None))
        elif risk_f == "1h":
            query = query.where(User.risk_notified_1h_at.is_not(None))
            count_query = count_query.where(User.risk_notified_1h_at.is_not(None))
        if bill_f in {"legacy", "hybrid"}:
            query = query.where(User.billing_mode == bill_f)
            count_query = count_query.where(User.billing_mode == bill_f)
        if gh_f == "1":
            query = query.where(User.github_username.is_not(None))
            count_query = count_query.where(User.github_username.is_not(None))
        elif gh_f == "0":
            query = query.where(User.github_username.is_(None))
            count_query = count_query.where(User.github_username.is_(None))
        if usage_f == "payg_active":
            since_u = now_for_filter - timedelta(days=14)
            payg_exists = exists().where(
                BillingUsageEvent.user_id == User.id,
                BillingUsageEvent.created_at >= since_u,
                BillingUsageEvent.event_type.in_(("traffic_gb_step", "device_daily")),
            )
            query = query.where(payg_exists)
            count_query = count_query.where(payg_exists)
        elif usage_f == "heavy":
            since_h = now_for_filter - timedelta(days=30)
            heavy_sq = (
                select(BillingUsageEvent.user_id.label("uid"))
                .where(
                    BillingUsageEvent.created_at >= since_h,
                    BillingUsageEvent.event_type.in_(("traffic_gb_step", "device_daily")),
                )
                .group_by(BillingUsageEvent.user_id)
                .having(func.count(BillingUsageEvent.id) >= 45)
            ).subquery()
            query = query.where(User.id.in_(select(heavy_sq.c.uid)))
            count_query = count_query.where(User.id.in_(select(heavy_sq.c.uid)))
        if dev_slots_f:
            try:
                dev_n = int(dev_slots_f)
            except ValueError:
                dev_n = -1
            if 1 <= dev_n <= 64:
                dev_slots_match = exists().where(
                    and_(
                        Subscription.user_id == User.id,
                        Subscription.status.in_(("active", "trial")),
                        Subscription.expires_at > now_for_filter,
                        Subscription.devices_count == dev_n,
                    )
                )
                query = query.where(dev_slots_match)
                count_query = count_query.where(dev_slots_match)
        if sort_f == "bal_desc":
            query = query.order_by(desc(User.balance), desc(User.id))
        elif sort_f == "bal_asc":
            query = query.order_by(User.balance.asc(), User.id.asc())
        elif sort_f == "id_asc":
            query = query.order_by(User.id.asc())
        elif sort_f == "id_desc":
            query = query.order_by(desc(User.id))
        else:
            query = query.order_by(desc(risk_priority), desc(User.id))
        total_users = int((await session.execute(count_query)).scalar_one() or 0)
        total_pages = max(1, (total_users + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        users = list(
            (
                await session.execute(
                    query.offset((page - 1) * per_page).limit(per_page)
                )
            ).scalars().all()
        )
        user_ids = [u.id for u in users]
        subs_by_user: dict[int, list[Subscription]] = defaultdict(list)
        if user_ids:
            sr = await session.execute(select(Subscription).where(Subscription.user_id.in_(user_ids)))
            for sub in sr.scalars().all():
                subs_by_user[sub.user_id].append(sub)
    now_utc = datetime.now(timezone.utc)
    rows = []
    for u in users:
        ring_tw = "ring-red-500" if u.is_blocked else "ring-emerald-500"
        sub_lbl, sub_badge = _subscription_list_badge(now_utc, subs_by_user.get(u.id, []))
        display = u.first_name or u.username or "-"
        username = f"@{u.username}" if u.username else "-"
        av = _avatar_with_fallback(u, px=36, ring_tw=ring_tw)
        risk_badge = "<span class='badge badge-ghost badge-xs'>—</span>"
        if u.risk_notified_1h_at is not None:
            risk_badge = "<span class='badge badge-error badge-xs'>1ч</span>"
        elif u.risk_notified_24h_at is not None:
            risk_badge = "<span class='badge badge-warning badge-xs'>24ч</span>"
        dev_slot = _active_subscription_devices_slots(now_utc, subs_by_user.get(u.id, []))
        dev_cell = str(dev_slot) if dev_slot is not None else "—"
        rows.append(
            f"<tr class='remna-row-link cursor-pointer' data-row-href='/admin/users/{u.id}' tabindex='0' role='link' aria-label='Открыть пользователя'>"
            f"<td><div class='flex items-center gap-3'>{av}"
            f"<span class='link link-primary font-medium'>{_esc(display)}</span></div></td>"
            f"<td>{_esc(username)}</td><td><code class='bg-base-300 px-1.5 py-0.5 rounded text-xs'>{u.telegram_id}</code></td><td>{u.id}</td><td class='font-medium'>{_esc(u.balance)}</td>"
            f"<td><span class='badge {sub_badge} badge-sm'>{_esc(sub_lbl)}</span></td>"
            f"<td class='text-center font-mono text-sm'>{_esc(dev_cell)}</td>"
            f"<td>{risk_badge}</td></tr>"
        )
    pager = _pagination_bar(
        page=page,
        total_pages=total_pages,
        base_path="/admin/users",
        query_extra={
            "q": needle,
            "sub": sub_f,
            "blocked": blk_f,
            "risk": risk_f,
            "bill": bill_f,
            "gh": gh_f,
            "usage": usage_f,
            "sort": sort_f,
            "dev_slots": dev_slots_f,
        },
        htmx_target="#remna-users-results",
    )
    sub_opts = (
        '<option value=""'
        + (" selected" if not sub_f else "")
        + '>Все</option>'
        + '<option value="active"'
        + (" selected" if sub_f == "active" else "")
        + '>С активной подпиской</option>'
        + '<option value="none"'
        + (" selected" if sub_f == "none" else "")
        + '>Без активной</option>'
    )
    blk_opts = (
        '<option value=""'
        + (" selected" if not blk_f else "")
        + '>Все</option>'
        + '<option value="1"'
        + (" selected" if blk_f == "1" else "")
        + '>Заблокированные</option>'
        + '<option value="0"'
        + (" selected" if blk_f == "0" else "")
        + '>Не заблокированные</option>'
    )
    risk_opts = (
        '<option value=""'
        + (" selected" if not risk_f else "")
        + '>Все</option>'
        + '<option value="24h"'
        + (" selected" if risk_f == "24h" else "")
        + '>Риск 24ч</option>'
        + '<option value="1h"'
        + (" selected" if risk_f == "1h" else "")
        + '>Риск 1ч</option>'
    )
    bill_opts = (
        '<option value=""'
        + (" selected" if not bill_f else "")
        + '>Все</option>'
        + '<option value="legacy"'
        + (" selected" if bill_f == "legacy" else "")
        + '>Legacy</option>'
        + '<option value="hybrid"'
        + (" selected" if bill_f == "hybrid" else "")
        + '>Hybrid</option>'
    )
    gh_opts = (
        '<option value=""'
        + (" selected" if not gh_f else "")
        + '>Все</option>'
        + '<option value="1"'
        + (" selected" if gh_f == "1" else "")
        + '>GitHub есть</option>'
        + '<option value="0"'
        + (" selected" if gh_f == "0" else "")
        + '>Без GitHub</option>'
    )
    usage_opts = (
        '<option value=""'
        + (" selected" if not usage_f else "")
        + '>Все</option>'
        + '<option value="payg_active"'
        + (" selected" if usage_f == "payg_active" else "")
        + '>PAYG за 14 дн.</option>'
        + '<option value="heavy"'
        + (" selected" if usage_f == "heavy" else "")
        + '>Массовое потребление (≥45 событий / 30 дн.)</option>'
    )
    sort_opts = (
        '<option value=""'
        + (" selected" if not sort_f else "")
        + '>Риск → ID</option>'
        + '<option value="id_desc"'
        + (" selected" if sort_f == "id_desc" else "")
        + '>ID убыв.</option>'
        + '<option value="id_asc"'
        + (" selected" if sort_f == "id_asc" else "")
        + '>ID возр.</option>'
        + '<option value="bal_desc"'
        + (" selected" if sort_f == "bal_desc" else "")
        + '>Баланс ↓</option>'
        + '<option value="bal_asc"'
        + (" selected" if sort_f == "bal_asc" else "")
        + '>Баланс ↑</option>'
    )
    dev_opts_parts: list[str] = [
        '<option value=""' + (" selected" if not dev_slots_f else "") + ">Все</option>"
    ]
    for dv in range(1, 33):
        sel = " selected" if dev_slots_f == str(dv) else ""
        dev_opts_parts.append(f"<option value='{dv}'{sel}>{dv} слот.</option>")
    dev_opts = "".join(dev_opts_parts)
    users_results_inner = (
        "<div class='overflow-x-auto rounded-xl border border-base-content/10'>"
        "<table class='table table-zebra table-sm'><thead><tr><th>Пользователь</th><th>Telegram</th><th>Telegram ID</th><th>ID в боте</th><th>Баланс</th><th>Подписка</th><th class='text-center'>Слоты</th><th>Риск</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"8\" class=\"opacity-50\">Нет данных</td></tr>'}</tbody></table></div>"
        f"{pager}"
    )
    body = (
        "<div class='card bg-base-100 border border-base-content/10 shadow-lg'><div class='card-body gap-4'>"
        "<h2 class='card-title text-2xl'><i class='fa-solid fa-users text-primary mr-2' aria-hidden='true'></i>Пользователи</h2>"
        "<form id='us-form' method='get' class='flex flex-wrap items-end gap-2' "
        "hx-get='/admin/users' hx-target='#remna-users-results' hx-swap='innerHTML' hx-push-url='true' "
        "hx-trigger='submit, change from:select, keyup changed delay:320ms from:#us-q'>"
        f"<input id='us-q' class='input input-bordered input-sm h-9 min-h-9 w-full max-w-md text-sm' name='q' value='{_esc(needle)}' placeholder='ID, Telegram username, имя'/>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Подписка</span>"
        f"<select id='us-sub' name='sub' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{sub_opts}</select></label>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Аккаунт</span>"
        f"<select id='us-blocked' name='blocked' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{blk_opts}</select></label>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Риск минуса</span>"
        f"<select id='us-risk' name='risk' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{risk_opts}</select></label>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Биллинг</span>"
        f"<select id='us-bill' name='bill' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{bill_opts}</select></label>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>GitHub</span>"
        f"<select id='us-gh' name='gh' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{gh_opts}</select></label>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Потребление</span>"
        f"<select id='us-usage' name='usage' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{usage_opts}</select></label>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Слоты (active/trial)</span>"
        f"<select id='us-dev' name='dev_slots' class='select select-bordered select-sm h-9 min-h-9 text-sm' title='По числу devices_count у неистёкшей подписки'>{dev_opts}</select></label>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Сортировка</span>"
        f"<select id='us-sort' name='sort' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{sort_opts}</select></label>"
        "<a class='btn btn-outline btn-sm h-9 min-h-9 gap-1.5' href='/admin/users' title='Сбросить все фильтры'><i class='fa-solid fa-rotate-left' aria-hidden='true'></i>Сбросить</a>"
        "<button id='us-apply' class='btn btn-primary btn-sm h-9 min-h-9 gap-1.5' type='submit'><i class='fa-solid fa-magnifying-glass' aria-hidden='true'></i>Применить</button></form>"
        "<div id='remna-users-results'>"
        f"{users_results_inner}"
        "</div>"
        "<script src='https://unpkg.com/htmx.org@1.9.12' crossorigin='anonymous'></script>"
        "</div></div>"
    )
    if want_partial:
        return HTMLResponse(
            users_results_inner,
            headers={"Cache-Control": "private, no-store"},
        )
    _USERS_HTML_CACHE[cache_key] = (time.monotonic(), body)
    return _layout("Web-admin Users", body, request=request)


@router.get("/subscriptions")
async def admin_subscription_history(request: Request, page: int = 1) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    page = max(1, page)
    per_page = 15
    async with await _session() as session:
        total_subs = int(
            (await session.execute(select(func.count()).select_from(Subscription))).scalar_one() or 0
        )
        total_pages = max(1, (total_subs + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page
        r = await session.execute(
            select(Subscription, User, Plan)
            .join(User, User.id == Subscription.user_id)
            .join(Plan, Plan.id == Subscription.plan_id)
            .order_by(desc(Subscription.created_at))
            .offset(offset)
            .limit(per_page)
        )
        rows = r.all()
    tr: list[str] = []
    for sub, u, pl in rows:
        disp = u.first_name or u.username or f"#{u.id}"
        tr.append(
            f"<tr class='remna-row-link cursor-pointer' data-row-href='/admin/users/{u.id}' tabindex='0' role='link' aria-label='Карточка пользователя'>"
            f"<td class='whitespace-nowrap text-xs opacity-80'>{_fmt_dt_msk(sub.created_at)}</td>"
            f"<td><span class='link link-primary font-medium'>{_esc(disp)}</span></td>"
            f"<td class='font-mono text-xs'>{u.id}</td>"
            f"<td>{_esc(pl.name)}</td>"
            f"<td><span class='badge badge-ghost badge-sm'>{_esc(sub.status)}</span></td>"
            f"<td class='text-xs whitespace-nowrap'>{_fmt_dt_msk(sub.started_at)}</td>"
            f"<td class='text-xs whitespace-nowrap'>{_fmt_dt_msk(sub.expires_at)}</td>"
            f"<td>{sub.devices_count}</td></tr>"
        )
    pager = _pagination_bar(page=page, total_pages=total_pages, base_path="/admin/subscriptions", query_extra={})
    body = (
        "<div class='card bg-base-100 border border-base-content/10 shadow-lg'><div class='card-body gap-4'>"
        "<h2 class='card-title text-2xl'><i class='fa-solid fa-clock-rotate-left text-primary mr-2' aria-hidden='true'></i>История подписок</h2>"
        "<p class='text-sm opacity-60'>Все записи подписок из базы, от новых к старым (по дате создания записи). Одному пользователю соответствуют несколько строк при продлениях и сменах тарифа.</p>"
        "<div class='overflow-x-auto rounded-xl border border-base-content/10'>"
        "<table class='table table-zebra table-sm'><thead><tr>"
        "<th>Создана</th><th>Пользователь</th><th>ID</th><th>Тариф</th><th>Статус</th><th>Старт</th><th>Истекает</th><th>Слотов</th></tr></thead>"
        f"<tbody>{''.join(tr) or '<tr><td colspan=\"8\" class=\"opacity-50\">Нет записей</td></tr>'}</tbody></table></div>"
        f"{pager}</div></div>"
    )
    return _layout("История подписок", body, request=request)


@router.get("/users/{user_id}/telegram-photo")
async def admin_user_telegram_photo(request: Request, user_id: int) -> Response:
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        user = await session.get(User, user_id)
    if user is None:
        return Response(status_code=404)
    now_m = time.monotonic()
    hit = _AVATAR_CACHE.get(user_id)
    if hit is not None and now_m - hit[0] < _AVATAR_TTL_SEC:
        return Response(
            content=hit[1],
            media_type=hit[2],
            headers={"Cache-Control": "private, max-age=300, stale-while-revalidate=120"},
        )
    async with _avatar_fetch_lock(user_id):
        now_m = time.monotonic()
        hit = _AVATAR_CACHE.get(user_id)
        if hit is not None and now_m - hit[0] < _AVATAR_TTL_SEC:
            return Response(
                content=hit[1],
                media_type=hit[2],
                headers={"Cache-Control": "private, max-age=300, stale-while-revalidate=120"},
            )
        loaded = await _load_telegram_profile_photo(user)
        if loaded is None and user.username:
            loaded = await _fetch_telegram_public_userpic(user.username)
        if loaded is None:
            return Response(status_code=404)
        body_b, mime = loaded
        _AVATAR_CACHE[user_id] = (time.monotonic(), body_b, mime)
        return Response(
            content=body_b,
            media_type=mime,
            headers={"Cache-Control": "private, max-age=300, stale-while-revalidate=120"},
        )


@router.get("/users/{user_id}")
async def admin_user_detail(request: Request, user_id: int) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    billing_detail_block = ""
    hwid_devices: list[dict] = []
    hwid_err: str | None = None
    tx_rows_pref = ""
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return _layout(
                "User not found",
                "<div class='alert alert-warning shadow-lg'><i class='fa-solid fa-user-slash mr-2' aria-hidden='true'></i><span>Пользователь не найден</span></div>",
                request=request,
                back_href="/admin/users",
            )
        referrer = await session.get(User, user.referred_by) if user.referred_by else None
        invited_count = await count_invited_users(session, user.id)
        invited_list = await list_invited_users(session, user.id, limit=50)
        subs = list(
            (
                await session.execute(
                    select(Subscription).where(Subscription.user_id == user_id).order_by(desc(Subscription.id))
                )
            ).scalars()
        )
        txs = list(
            (
                await session.execute(
                    select(Transaction).where(Transaction.user_id == user_id).order_by(desc(Transaction.id)).limit(100)
                )
            ).scalars()
        )
        tix_rows = (
            await session.execute(
                text(
                    """
                    SELECT t.id, t.status, t.created_at, t.closed_at,
                           (SELECT tr.value FROM ticket_ratings tr WHERE tr.ticket_id = t.id LIMIT 1) AS rating
                    FROM tickets t
                    WHERE t.user_id = :uid
                    ORDER BY t.id DESC
                    LIMIT 200
                    """
                ),
                {"uid": user_id},
            )
        ).all()
        payments_total = (
            await session.execute(
                select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.user_id == user_id,
                    Transaction.type == "topup",
                    Transaction.status == "completed",
                )
            )
        ).scalar_one()
        active_sub = await get_admin_manageable_subscription(session, user.id)
        n_db_dev_active = await count_devices(session, active_sub.id) if active_sub else 0
        bill_anchor = billing_today(settings)
        spend_from = bill_anchor - timedelta(days=2)
        spend_rows = list(
            (
                await session.execute(
                    select(BillingDailySummary).where(
                        BillingDailySummary.user_id == user.id,
                        BillingDailySummary.day >= spend_from,
                        BillingDailySummary.day <= bill_anchor,
                    )
                )
            ).scalars()
        )
        avg_daily_spend = Decimal("0")
        if spend_rows:
            total_spend = Decimal("0")
            uniq_days: set[date] = set()
            for row in spend_rows:
                total_spend += row.total_amount_rub
                uniq_days.add(row.day)
            if uniq_days:
                avg_daily_spend = (total_spend / Decimal(len(uniq_days))).quantize(Decimal("0.01"))
        remain_to_floor = (user.balance - settings.billing_balance_floor_rub).quantize(Decimal("0.01"))
        eta_to_floor_hours: float | None = None
        if avg_daily_spend > 0 and remain_to_floor > 0:
            hourly = avg_daily_spend / Decimal("24")
            if hourly > 0:
                eta_to_floor_hours = float(remain_to_floor / hourly)
        last_cancelled_sub = (
            await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id, Subscription.status == "cancelled")
                .order_by(Subscription.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if settings.billing_v2_enabled and user.billing_mode == "hybrid":
            bt = bill_anchor
            row_td = await get_today_summary(session, user_id=user.id, today=bt)
            rows_m = await get_month_summaries(session, user_id=user.id, anchor_day=bt)
            month_start, next_month = month_bounds(bt)
            month_from = billing_local_day_start_utc(settings, month_start)
            month_to = billing_local_day_start_utc(settings, next_month)
            pack_td = await usage_package_breakdown(
                session,
                user_id=user.id,
                from_dt=billing_local_day_start_utc(settings, bt),
                to_dt=billing_local_day_end_utc_exclusive(settings, bt),
            )
            pack_m = await usage_package_breakdown(session, user_id=user.id, from_dt=month_from, to_dt=month_to)
            month_total = summarize_month_total(rows_m)
            if row_td is None:
                today_html = '<p class="text-sm opacity-80">За сегодня списаний нет.</p>'
            else:
                today_html = (
                    "<ul class=\"list-disc list-inside text-sm\">"
                    f"<li>ГБ к оплате: {_esc(row_td.gb_amount_rub)} ₽ ({_esc(row_td.gb_units)} шт.)</li>"
                    f"<li>Устройства: {_esc(row_td.device_amount_rub)} ₽ ({_esc(row_td.device_units)} шт.)</li>"
                    f"<li>Моб. интернет: {_esc(row_td.mobile_amount_rub)} ₽ ({_esc(row_td.mobile_gb_units)} шт.)</li>"
                    f"<li><b>Итого за день: {_esc(row_td.total_amount_rub)} ₽</b></li>"
                    "</ul>"
                    f"<p class=\"text-xs opacity-70\">Пакет: ГБ покрыто {_esc(pack_td['gb_covered'])}, устройства "
                    f"{_esc(pack_td['device_covered'])}; сверх пакета ГБ {_esc(pack_td['gb_charged'])}, устройства "
                    f"{_esc(pack_td['device_charged'])}.</p>"
                )
            days_rows = "".join(
                f"<tr><td>{_esc(r.day.strftime('%d.%m.%Y'))}</td>"
                f"<td class=\"text-right font-mono\">{_esc(r.total_amount_rub)} ₽</td></tr>"
                for r in rows_m[:31]
            )
            billing_detail_block = f"""
    <div class="card bg-base-100 border border-info/25 shadow-lg mt-4">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-chart-column text-info mr-2" aria-hidden="true"></i>Детализация списаний (гибрид v2)</h3>
        <p class="text-xs opacity-70 mb-2">Сутки и месяц — по <code class="text-xs bg-base-300 px-1 rounded">{_esc(settings.billing_calendar_timezone)}</code>.</p>
        <h4 class="text-sm font-semibold">Сегодня ({_esc(bt.strftime('%d.%m.%Y'))})</h4>
        {today_html}
        <div class="divider my-1"></div>
        <h4 class="text-sm font-semibold">Текущий календарный месяц</h4>
        <p class="text-sm"><b>Итого:</b> {_esc(month_total)} ₽</p>
        <p class="text-xs opacity-70 mb-2">Пакет за месяц: ГБ покрыто {_esc(pack_m['gb_covered'])}, устройства {_esc(pack_m['device_covered'])}; сверх пакета ГБ {_esc(pack_m['gb_charged'])}, устройства {_esc(pack_m['device_charged'])}.</p>
        <div class="overflow-x-auto rounded-lg border border-base-content/10 max-h-60 overflow-y-auto"><table class="table table-zebra table-sm"><thead><tr><th>День</th><th class="text-right">Сумма</th></tr></thead><tbody>{days_rows or '<tr><td colspan="2" class="opacity-50">Нет строк</td></tr>'}</tbody></table></div>
      </div>
    </div>
    """

        ud = SimpleNamespace(
            id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
            github_username=user.github_username,
            github_profile_url=user.github_profile_url,
            telegram_id=user.telegram_id,
            balance=user.balance,
            referral_code=user.referral_code,
            remnawave_uuid=user.remnawave_uuid,
            is_blocked=user.is_blocked,
            created_at=user.created_at,
            billing_mode=user.billing_mode,
            risk_notified_24h_at=user.risk_notified_24h_at,
            risk_notified_1h_at=user.risk_notified_1h_at,
            avg_daily_spend=avg_daily_spend,
            eta_to_floor_hours=eta_to_floor_hours,
            personal_tariff_discount_percent=user.personal_tariff_discount_percent,
            custom_subscription_month_price_rub=user.custom_subscription_month_price_rub,
        )
        referrer_sn = (
            SimpleNamespace(id=referrer.id, first_name=referrer.first_name, username=referrer.username)
            if referrer
            else None
        )
        subs_tuples = [(s.id, s.status, s.started_at, s.expires_at, s.devices_count) for s in subs]
        txs_tuples = [
            (t.id, t.type, t.amount, t.status, t.payment_provider, t.created_at) for t in txs
        ]
        tx_rows_pref = "".join(
            f"<tr><td>{int(t.id)}</td><td>{_txn_type_hint_html(t.type)}</td><td>{_esc(t.amount)}</td>"
            f"<td>{_esc(t.status)}</td><td>{_esc(t.payment_provider or '-')}</td>"
            f"<td>{_fmt_dt_msk(t.created_at)}</td><td>{_txn_history_action_cell(user_id, t)}</td></tr>"
            for t in txs
        )
        tix_tuples = [
            (int(r[0]), str(r[1]), r[2], r[3], r[4]) for r in tix_rows
        ]
        invited_tuples = [
            (u.id, u.first_name, u.username, u.telegram_id, u.created_at) for u in invited_list
        ]
        active_snap: dict | None = None
        if active_sub:
            pl = active_sub.plan
            active_snap = {
                "id": active_sub.id,
                "status": active_sub.status,
                "expires_at": active_sub.expires_at,
                "devices_count": active_sub.devices_count,
                "auto_renew": active_sub.auto_renew,
                "plan_name": pl.name if pl else None,
                "plan_traffic_limit_gb": pl.traffic_limit_gb if pl else None,
            }
        last_cancelled_id = last_cancelled_sub.id if last_cancelled_sub else None

    dev_bill_preview_rub = ""
    if active_snap:
        dev_bill_preview_rub = str(
            monthly_extra_devices_rub_preview(settings, int(active_snap["devices_count"]))
        )

    uinf: dict | None = None
    hwid_list_ok = False
    if ud.remnawave_uuid:
        try:
            rw = RemnaWaveClient(settings)
            uinf = _as_rw_user_profile(await rw.get_user(str(ud.remnawave_uuid)))
            raw = await rw.get_user_hwid_devices(str(ud.remnawave_uuid))
            hwid_devices = normalize_hwid_devices_list(raw if isinstance(raw, list) else [])
            hwid_list_ok = True
        except RemnaWaveError as e:
            hwid_err = str(e)

    n_occ = 0
    if active_snap:
        if uinf:
            if hwid_list_ok:
                n_occ = len(hwid_devices)
            else:
                ext = extract_connected_devices_from_rw_user(uinf)
                n_occ = ext if ext is not None else n_db_dev_active
        else:
            n_occ = n_db_dev_active

    now_utc = datetime.now(UTC)
    sub_summary_html = ""
    if active_snap:
        plan_name = active_snap["plan_name"]
        plan_traffic = active_snap["plan_traffic_limit_gb"]
        exp = active_snap["expires_at"]
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        left_phr = _humanize_left_ru(exp, now_utc) if exp else "—"
        exp_msk = _fmt_dt_msk(exp)
        if uinf:
            used_gb, _lim_u = extract_traffic_gb_from_rw_user(uinf)
            used_s = f"{used_gb:.2f}" if used_gb is not None else "—"
            if is_rw_traffic_unlimited(uinf):
                lim_s = "∞"
            else:
                lg = traffic_limit_gb_for_display(uinf)
                lim_s = f"{lg:.1f}" if lg is not None else "—"
            traffic_line = (
                f"<span class='font-mono'><b>{used_s}</b> / <b>{lim_s}</b> ГБ</span>"
                "<span class='text-xs opacity-60'> (панель Remnawave)</span>"
            )
        else:
            plg = ""
            try:
                pt_ok = plan_traffic is not None and int(plan_traffic) > 0
            except (TypeError, ValueError):
                pt_ok = False
            if pt_ok:
                plg = f" · лимит по тарифу в боте: ~{plan_traffic} ГБ"
            traffic_line = f"<span class='opacity-70'>данные панели недоступны</span>{_esc(plg)}"
        slots_line = (
            f"<span class='font-mono'><b>{n_occ}</b> / <b>{active_snap['devices_count']}</b></span>"
            "<span class='text-xs opacity-60'> (занято / слотов в боте)</span>"
        )
        st_badge = "success" if active_snap["status"] in ("active", "trial") else "warning"
        conn_extra = ""
        if uinf:
            oa = rw_user_online_at(uinf)
            fa = rw_user_first_connected_at(uinf)
            if oa is not None:
                conn_extra += f"<p class='sm:col-span-2 text-xs text-base-content/80'>Последняя активность в панели: <b>{_fmt_dt_msk(oa)}</b></p>"
            if fa is not None:
                conn_extra += f"<p class='sm:col-span-2 text-xs text-base-content/80'>Первое подключение: <b>{_fmt_dt_msk(fa)}</b></p>"
        snap_title = "Активная подписка"
        if exp is not None and exp <= now_utc:
            snap_title = "Подписка (срок истёк)"
        sub_summary_html = f"""
    <div class="rounded-2xl border border-primary/30 bg-gradient-to-br from-primary/15 via-base-200/90 to-base-100 p-4 shadow-md backdrop-blur-sm">
      <h3 class="mb-3 text-xs font-bold uppercase tracking-wider text-primary">{_esc(snap_title)}</h3>
      <div class="grid gap-3 text-sm sm:grid-cols-2">
        <p>Тариф: <b>{_esc(plan_name or '—')}</b> · <span class="badge badge-{st_badge} badge-sm">{_esc(active_snap['status'])}</span></p>
        <p>Трафик: {traffic_line}</p>
        <p>Устройства: {slots_line}</p>
        <p class="sm:col-span-2">Окончание: <b>{_esc(exp_msk)}</b> <span class="opacity-70">(осталось: {_esc(left_phr)})</span></p>
        {conn_extra}
      </div>
    </div>"""
    else:
        conn_only = ""
        if uinf:
            oa = rw_user_online_at(uinf)
            fa = rw_user_first_connected_at(uinf)
            if oa is not None or fa is not None:
                bits = []
                if oa is not None:
                    bits.append(f"Последняя активность в панели: <b>{_fmt_dt_msk(oa)}</b>")
                if fa is not None:
                    bits.append(f"Первое подключение: <b>{_fmt_dt_msk(fa)}</b>")
                conn_only = (
                    "<div class='rounded-xl border border-base-content/15 bg-base-200/40 p-3 text-sm shadow-sm'>"
                    f"<p class='text-xs font-semibold uppercase tracking-wide text-base-content/60 mb-2'>Панель Remnawave</p>"
                    f"{'<br/>'.join(bits)}</div>"
                )
        sub_summary_html = (
            "<div class='alert alert-info text-sm shadow-sm'>Нет активной подписки (статусы active/trial с неистёкшим сроком).</div>"
            + conn_only
        )

    subs_rows = "".join(
        f"<tr><td>{sid}</td><td>{_esc(st)}</td><td>{_fmt_dt_msk(sa)}</td>"
        f"<td>{_fmt_dt_msk(se)}</td><td>{dc}</td></tr>"
        for sid, st, sa, se, dc in subs_tuples
    )
    tx_rows = tx_rows_pref
    tix_total = len(tix_tuples)
    tix_open = sum(1 for _tid, st, _ca, _cl, _rt in tix_tuples if st in ("open", "in_progress"))
    tix_closed = sum(1 for _tid, st, _ca, _cl, _rt in tix_tuples if st == "closed")
    tix_rates = [1 if rt is True else 0 for _tid, _st, _ca, _cl, rt in tix_tuples if rt is not None]
    tix_rate_pct = f"{(sum(tix_rates) / len(tix_rates) * 100):.0f}%" if tix_rates else "—"
    tix_status_label = {"open": "Открыт", "in_progress": "В работе", "closed": "Закрыт"}
    tix_rows_html = "".join(
        f"<tr>"
        f"<td><a class='link link-primary' href='/admin/tickets/{tid}'>#{tid}</a></td>"
        f"<td><span class='badge badge-sm {'badge-warning' if st == 'in_progress' else ('badge-info' if st == 'open' else 'badge-ghost')}'>{_esc(tix_status_label.get(st, st))}</span></td>"
        f"<td>{_fmt_dt_msk(ca)}</td>"
        f"<td>{_fmt_dt_msk(cl) if cl else '—'}</td>"
        f"<td>{'👍' if rt is True else ('👎' if rt is False else '—')}</td>"
        f"</tr>"
        for tid, st, ca, cl, rt in tix_tuples
    )
    tickets_block = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-headset text-primary mr-2" aria-hidden="true"></i>Тикеты ({tix_total})</h3>
        <div class="grid gap-2 sm:grid-cols-4 text-sm">
          <div class="rounded-lg border border-base-content/10 p-2.5"><div class="opacity-60 text-xs">Всего</div><div class="text-lg font-semibold">{tix_total}</div></div>
          <div class="rounded-lg border border-base-content/10 p-2.5"><div class="opacity-60 text-xs">Активные</div><div class="text-lg font-semibold">{tix_open}</div></div>
          <div class="rounded-lg border border-base-content/10 p-2.5"><div class="opacity-60 text-xs">Закрытые</div><div class="text-lg font-semibold">{tix_closed}</div></div>
          <div class="rounded-lg border border-base-content/10 p-2.5"><div class="opacity-60 text-xs">Позитивные оценки</div><div class="text-lg font-semibold">{_esc(tix_rate_pct)}</div></div>
        </div>
        <div class="overflow-x-auto rounded-lg border border-base-content/10"><table class="table table-zebra table-sm"><thead><tr><th>ID</th><th>Статус</th><th>Создан</th><th>Закрыт</th><th>Оценка</th></tr></thead>
        <tbody>{tix_rows_html or '<tr><td colspan="5" class="opacity-50">Тикетов пока нет</td></tr>'}</tbody></table></div>
      </div>
    </div>
    """
    ring = "bad" if ud.is_blocked else "ok"
    ring_tw = "ring-emerald-500" if ring == "ok" else "ring-red-500"

    sub_url_rw: str | None = None
    if uinf:
        sub_url_rw = subscription_url_for_telegram(uinf.get("subscriptionUrl"), settings)

    risk_badge = "badge-ghost"
    risk_text = "Недостаточно данных"
    eta_txt = "—"
    if isinstance(ud.eta_to_floor_hours, float):
        if ud.eta_to_floor_hours <= 0:
            risk_badge = "badge-error"
            risk_text = "Порог уже достигнут"
            eta_txt = "0 ч"
        else:
            eta_delta = timedelta(hours=ud.eta_to_floor_hours)
            hh = int(eta_delta.total_seconds() // 3600)
            mm = int((eta_delta.total_seconds() % 3600) // 60)
            eta_txt = f"{hh} ч {mm} мин"
            if ud.eta_to_floor_hours <= 1.5:
                risk_badge = "badge-error"
                risk_text = "Высокий риск (< 1.5ч)"
            elif ud.eta_to_floor_hours <= 26:
                risk_badge = "badge-warning"
                risk_text = "Риск в горизонте суток"
            else:
                risk_badge = "badge-success"
                risk_text = "Риск вне ближайших суток"
    risk_24 = _fmt_dt_msk(ud.risk_notified_24h_at) if ud.risk_notified_24h_at else "—"
    risk_1 = _fmt_dt_msk(ud.risk_notified_1h_at) if ud.risk_notified_1h_at else "—"
    admin_ids_ui = frozenset(int(x) for x in (settings.admin_telegram_ids or []))
    show_github_in_profile = int(ud.telegram_id) in admin_ids_ui
    github_lines_html = ""
    if show_github_in_profile:
        github_lines_html = (
            f"<p>GitHub: <b>{_esc(ud.github_username or '-')}</b></p>"
            + _copy_line(label="GitHub URL", value=str(ud.github_profile_url) if ud.github_profile_url else "—")
        )
    else:
        github_lines_html = (
            "<p class=\"text-xs opacity-70\">GitHub скрыт: ссылка и профиль показываются только для Telegram ID из "
            "<code class=\"bg-base-300 px-1 rounded text-[11px]\">ADMIN_TELEGRAM_IDS</code>.</p>"
        )

    custom_price_cur = (
        str(ud.custom_subscription_month_price_rub)
        if ud.custom_subscription_month_price_rub is not None
        else ""
    )
    personal_disc_cur = (
        str(ud.personal_tariff_discount_percent)
        if ud.personal_tariff_discount_percent is not None
        else ""
    )
    personal_pricing_block = f"""
    <div class="rounded-2xl border border-secondary/30 bg-base-200/30 p-4 mt-3">
      <h3 class="text-xs font-bold uppercase tracking-wide text-base-content/60 mb-2">Персональные тарифы</h3>
      <p class="text-xs opacity-70 mb-3">Действуют только для этого пользователя. Своя цена и скидка % суммируются (скидка применяется к персональной цене).</p>
      <div class="flex flex-wrap items-end gap-3">
        <form method="post" action="/admin/users/{user_id}/personal-pricing/custom-month-price" class="flex flex-wrap items-end gap-2">
          <label class="form-control">
            <span class="label-text text-xs opacity-70">Своя цена ₽/мес</span>
            <input type="text" name="price_rub" inputmode="decimal" placeholder="150" value="{_esc(custom_price_cur)}" class="input input-bordered input-sm h-9 min-h-9 w-28" />
          </label>
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9">Сохранить</button>
        </form>
        <form method="post" action="/admin/users/{user_id}/personal-pricing/custom-month-price" class="inline">
          <input type="hidden" name="price_rub" value="" />
          <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9">Сбросить цену</button>
        </form>
      </div>
      <div class="flex flex-wrap items-end gap-3 mt-3">
        <form method="post" action="/admin/users/{user_id}/personal-pricing/discount" class="flex flex-wrap items-end gap-2">
          <label class="form-control">
            <span class="label-text text-xs opacity-70">Скидка на тарифы %</span>
            <input type="text" name="discount_percent" inputmode="decimal" placeholder="10" value="{_esc(personal_disc_cur)}" class="input input-bordered input-sm h-9 min-h-9 w-24" />
          </label>
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9">Сохранить</button>
        </form>
        <form method="post" action="/admin/users/{user_id}/personal-pricing/discount" class="inline">
          <input type="hidden" name="discount_percent" value="" />
          <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9">Сбросить скидку</button>
        </form>
      </div>
    </div>
    """

    negative_risk_block = f"""
    <div class="rounded-2xl border border-warning/30 bg-base-200/30 p-4">
      <h3 class="text-xs font-bold uppercase tracking-wide text-base-content/60 mb-2">Риск ухода в минус</h3>
      <div class="grid gap-2 text-sm sm:grid-cols-2">
        <p>Режим биллинга: <b>{_esc(ud.billing_mode)}</b></p>
        <p>Статус: <span class="badge {risk_badge} badge-sm">{_esc(risk_text)}</span></p>
        <p>Средний расход/день (3д): <b>{_esc(ud.avg_daily_spend)} ₽</b></p>
        <p>ETA до {_esc(str(settings.billing_balance_floor_rub))} ₽: <b>{_esc(eta_txt)}</b></p>
        <p>Последнее уведомление 24ч: <b>{_esc(risk_24)}</b></p>
        <p>Последнее уведомление 1ч: <b>{_esc(risk_1)}</b></p>
      </div>
      <form method="post" action="/admin/users/{user_id}/risk-notify/reset" class="inline mt-3"
        data-remna-confirm-msg="{_esc_attr('Сбросить отметки уведомлений о риске минуса (уведомления 24 ч и 1 ч)?')}">
        <button type="submit" class="btn btn-outline btn-warning btn-sm h-9 min-h-9 gap-1.5">
          <i class="fa-solid fa-bell-slash" aria-hidden="true"></i>Сбросить статистику риска
        </button>
      </form>
    </div>
    """

    vpn_link_card = ""
    if sub_url_rw:
        vpn_link_card = f"""
    <div class="card bg-base-100 border border-accent/25 shadow-lg bg-gradient-to-br from-accent/8 via-base-100 to-base-100">
      <div class="card-body gap-4">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-qrcode text-accent mr-2" aria-hidden="true"></i>Подключение VPN</h3>
        {_copy_line(label="Ссылка подписки", value=sub_url_rw)}
        <div class="flex flex-col items-center gap-2 rounded-xl border border-base-content/10 bg-base-200/40 p-4">
          <p class="text-xs opacity-60">QR для импорта в клиент</p>
          <img src="/admin/users/{user_id}/subscription-qr.png" alt="QR" class="max-w-[240px] rounded-lg border border-base-content/15 bg-base-100 p-2 shadow-inner" width="240" height="240" loading="lazy" />
        </div>
      </div>
    </div>"""

    now_check = datetime.now(UTC)
    mgmt_html = ""
    if active_snap and active_snap["status"] in ("active", "trial"):
        exp_chk = active_snap["expires_at"]
        if exp_chk is not None and exp_chk.tzinfo is None:
            exp_chk = exp_chk.replace(tzinfo=UTC)
        expired_notice = ""
        if exp_chk is None:
            expired_notice = (
                '<div class="alert alert-warning text-sm mb-3"><span>У записи нет даты окончания — '
                "добавление дней может быть недоступно до появления срока.</span></div>"
            )
        elif exp_chk <= now_check:
            expired_notice = (
                '<div class="alert alert-warning text-sm mb-3"><span><b>Срок подписки истёк.</b> '
                "Ниже можно добавить дни (отсчёт от сегодня) и изменить лимит слотов устройств.</span></div>"
            )
        ar_on = active_snap["auto_renew"]
        nxt = "0" if ar_on else "1"
        lbl = "Выключить авто-продление" if ar_on else "Включить авто-продление"
        tip = "После срока списание не произойдёт." if ar_on else "За ~1 ч до конца — попытка продлить с баланса."
        mgmt_html = f"""
    <div class="card bg-base-100 border border-warning/35 shadow-lg">
      <div class="card-body gap-4">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-sliders text-warning mr-2" aria-hidden="true"></i>Управление подпиской</h3>
        {expired_notice}
        <form method="post" action="/admin/users/{user_id}/subscription/auto-renew" class="flex flex-wrap items-center gap-3">
          <input type="hidden" name="enabled" value="{nxt}"/>
          <button type="submit" class="btn btn-outline btn-warning btn-sm h-9 min-h-9">{_esc(lbl)}</button>
          <span class="text-xs opacity-60 max-w-xs">{_esc(tip)}</span>
        </form>
        <div class="divider my-0"></div>
        <p class="text-sm font-medium">Изменить срок подписки</p>
        <p class="text-xs opacity-70 mb-2">Сдвигает дату окончания на указанное число календарных дней (положительное — продлить, отрицательное — сократить; если подписка истекла — отсчёт от сегодня).</p>
        <form method="post" action="/admin/users/{user_id}/subscription/add-days" class="flex flex-wrap items-end gap-2">
          <input type="hidden" name="subscription_id" value="{int(active_snap['id'])}"/>
          <label class="form-control w-32">
            <span class="label-text text-xs">Дней (+/−)</span>
            <input type="number" name="days" min="-3650" max="3650" value="30" class="input input-bordered input-sm h-9 min-h-9 w-full" required />
          </label>
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9">Применить</button>
        </form>
        <div class="flex flex-wrap gap-2 mt-2">
          <form method="post" action="/admin/users/{user_id}/subscription/add-days" class="inline">
            <input type="hidden" name="subscription_id" value="{int(active_snap['id'])}"/>
            <input type="hidden" name="days" value="-7"/>
            <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9">−7</button>
          </form>
          <form method="post" action="/admin/users/{user_id}/subscription/add-days" class="inline">
            <input type="hidden" name="subscription_id" value="{int(active_snap['id'])}"/>
            <input type="hidden" name="days" value="-30"/>
            <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9">−30</button>
          </form>
          <form method="post" action="/admin/users/{user_id}/subscription/add-days" class="inline">
            <input type="hidden" name="subscription_id" value="{int(active_snap['id'])}"/>
            <input type="hidden" name="days" value="7"/>
            <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9">+7</button>
          </form>
          <form method="post" action="/admin/users/{user_id}/subscription/add-days" class="inline">
            <input type="hidden" name="subscription_id" value="{int(active_snap['id'])}"/>
            <input type="hidden" name="days" value="30"/>
            <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9">+30</button>
          </form>
          <form method="post" action="/admin/users/{user_id}/subscription/add-days" class="inline">
            <input type="hidden" name="subscription_id" value="{int(active_snap['id'])}"/>
            <input type="hidden" name="days" value="90"/>
            <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9">+90</button>
          </form>
        </div>
        <div class="divider my-0"></div>
        <p class="text-sm font-medium">Лимит устройств (слоты)</p>
        <p class="text-xs opacity-70 mb-2">Меняет <code class="text-xs bg-base-300 px-1 rounded">devices_count</code> в записи подписки и лимит HWID в Remnawave (если в панели не отключён лимит).</p>
        <p class="text-xs opacity-60 mb-2">Плановый биллинг (не в боте): до {_esc(str(settings.subscription_included_device_slots))} устр. без отдельной доплаты; сверх — {_esc(str(settings.extra_device_monthly_rub))} ₽/мес за слот. При текущем лимите предпросмотр: <b>{_esc(dev_bill_preview_rub)}</b> ₽/мес.</p>
        <p class="text-xs opacity-60 mb-2">Минимум {MIN_DEVICES} слота (базовая подписка), максимум {MAX_DEVICES}. Без списания с баланса.</p>
        <form method="post" action="/admin/users/{user_id}/subscription/set-device-slots" class="flex flex-wrap items-end gap-2">
          <input type="hidden" name="subscription_id" value="{int(active_snap['id'])}"/>
          <label class="form-control w-32">
            <span class="label-text text-xs">Слотов</span>
            <input type="number" name="devices_count" min="{MIN_DEVICES}" max="{MAX_DEVICES}" value="{int(active_snap['devices_count'])}" class="input input-bordered input-sm h-9 min-h-9 w-full" required />
          </label>
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9">Сохранить</button>
        </form>
        <div class="flex flex-wrap gap-2 mt-2">
          <form method="post" action="/admin/users/{user_id}/subscription/adjust-device-slots" class="inline">
            <input type="hidden" name="subscription_id" value="{int(active_snap['id'])}" />
            <input type="hidden" name="delta" value="1" />
            <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9" {"disabled" if int(active_snap['devices_count']) >= MAX_DEVICES else ""}>+1 слот</button>
          </form>
          <form method="post" action="/admin/users/{user_id}/subscription/adjust-device-slots" class="inline">
            <input type="hidden" name="subscription_id" value="{int(active_snap['id'])}" />
            <input type="hidden" name="delta" value="-1" />
            <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9" {"disabled" if int(active_snap['devices_count']) <= MIN_DEVICES else ""}>−1 слот</button>
          </form>
        </div>
        <div class="divider my-0"></div>
        <p class="text-sm opacity-80">Полное отключение (как в Telegram-админке): <code class="text-xs bg-base-300 px-1 rounded">cancelled</code> в БД и <code class="text-xs bg-base-300 px-1 rounded">DISABLED</code> в панели.</p>
        <button type="button" class="btn btn-error btn-outline btn-sm h-9 min-h-9 w-fit" data-remna-open-sub-disable data-no-row-nav data-user-id="{user_id}" data-sub-id="{active_snap['id']}">Отключить подписку</button>
      </div>
    </div>"""
    elif last_cancelled_id is not None:
        mgmt_html = f"""
    <div class="card bg-base-100 border border-success/35 shadow-lg">
      <div class="card-body gap-4">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-plug-circle-check text-success mr-2" aria-hidden="true"></i>Включить подписку</h3>
        <p class="text-sm opacity-80">Последняя отключённая запись: <b>#{last_cancelled_id}</b>.</p>
        <form method="post" action="/admin/users/{user_id}/subscription/enable" class="flex flex-wrap gap-2">
          <input type="hidden" name="subscription_id" value="{last_cancelled_id}"/>
          <button type="submit" class="btn btn-success btn-sm h-9 min-h-9">Включить снова</button>
        </form>
      </div>
    </div>"""

    ref_by_block = ""
    if referrer_sn is not None:
        r_disp = referrer_sn.first_name or referrer_sn.username or f"#{referrer_sn.id}"
        ref_by_block = f"<p>Пригласил: <a class='link link-primary font-medium' href='/admin/users/{referrer_sn.id}'>{_esc(r_disp)}</a> <span class='opacity-60'>(id {referrer_sn.id})</span></p>"
    else:
        ref_by_block = "<p class='opacity-60'>Пригласитель: не указан (прямая регистрация).</p>"

    invited_rows = "".join(
        f"<tr><td>{iid}</td><td><a class='link link-primary font-medium' href='/admin/users/{iid}'>{_esc(str(ifn or '').strip() or (str(iun).strip() if iun is not None else '') or '-')}</a></td>"
        f"<td>{_esc('@' + str(iun).strip().lstrip('@')) if iun is not None and str(iun).strip() else '-'}</td>"
        f"<td><code class='bg-base-300 px-1 rounded text-xs'>{itg}</code></td>"
        f"<td>{_fmt_dt_msk(ica)}</td></tr>"
        for iid, ifn, iun, itg, ica in invited_tuples
    )
    ref_block = f"""
    <div class="divider my-0"></div>
    <h3 class="text-lg font-semibold"><i class="fa-solid fa-user-group text-primary mr-2" aria-hidden="true"></i>Рефералы</h3>
    {ref_by_block}
    <p>Привели по реф-ссылке: <b class="text-primary">{invited_count}</b></p>
    <div class="overflow-x-auto rounded-lg border border-base-content/10"><table class="table table-zebra table-sm"><thead><tr><th>ID</th><th>Имя</th><th>Username</th><th>Telegram</th><th>Регистрация</th></tr></thead>
    <tbody>{invited_rows or '<tr><td colspan="5" class="opacity-50">Пока никого</td></tr>'}</tbody></table></div>
    """

    hwid_rows = []
    slot_rm_attr = (
        "1"
        if (active_snap is not None and int(active_snap.get("devices_count") or 0) > MIN_DEVICES)
        else "0"
    )
    for i, d in enumerate(hwid_devices):
        hwid = str(d.get("hwid") or "")
        title = hwid_device_title(d, i + 1)
        dt = format_rw_device_datetime_local(str(d.get("createdAt") or ""))
        plat = _esc(str(d.get("platform") or "—"))
        detail = _hwid_device_json_block(d)
        hwid_rows.append(
            "<tr>"
            f"<td class='font-medium'>{_esc(title)}</td><td>{plat}</td><td class='text-sm opacity-80'>{_esc(dt)}</td>"
            f"<td class='align-top'>{detail}</td>"
            "<td class='text-right align-top'>"
            f"<button type='button' class='btn btn-error btn-outline btn-sm h-9 min-h-9' data-remna-open-hwid data-no-row-nav "
            f'data-user-id="{user_id}" data-hwid="{_esc_attr(hwid)}" data-title="{_esc_attr(title)}" data-slot-removable="{slot_rm_attr}">Отвязать</button></td></tr>'
        )
    hwid_alert = ""
    if hwid_err:
        hwid_alert = f"<div class='alert alert-warning text-sm'>{_esc(hwid_err)}</div>"
    elif not ud.remnawave_uuid:
        hwid_alert = "<p class='text-sm opacity-60'>Нет RemnaWave UUID — список HWID с панели недоступен.</p>"

    devices_block = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-mobile-screen-button text-accent mr-2" aria-hidden="true"></i>Устройства панели (HWID)</h3>
        {hwid_alert}
        <div class="overflow-x-auto rounded-lg border border-base-content/10"><table class="table table-zebra table-sm"><thead><tr><th>Устройство</th><th>Платформа</th><th>Создано</th><th>Данные</th><th></th></tr></thead>
        <tbody>{''.join(hwid_rows) or '<tr><td colspan="5" class="opacity-50">Нет привязанных устройств</td></tr>'}</tbody></table></div>
        <p class="text-xs opacity-60">«Отвязать»: в модальном окне — «Отвязать устройство» (только панель) или «Удалить слот» с подписки (если слотов больше двух).</p>
      </div>
    </div>
    """

    body = f"""
    <div class="grid gap-4 xl:grid-cols-3">
      <div class="card bg-base-100 border border-base-content/10 shadow-lg xl:col-span-2">
        <div class="card-body gap-4">
          <div class="flex flex-wrap items-start gap-4">
            {_avatar_with_fallback(ud, px=64, ring_tw=ring_tw, ring_offset="ring-offset-4")}
            <div class="min-w-0 flex-1">
              <h2 class="text-2xl font-bold">Пользователь #{ud.id}</h2>
              <p class="text-sm opacity-60">{_esc(ud.first_name or ud.username or '-')}</p>
              {_telegram_profile_actions(ud)}
            </div>
          </div>
          <div class="divider my-0"></div>
          {sub_summary_html}
          <div class="divider my-0"></div>
          <h3 class="text-sm font-bold uppercase tracking-wide text-base-content/50">Данные аккаунта</h3>
          <div class="grid gap-2 text-sm sm:grid-cols-2">
            <p>Имя: <b>{_esc((ud.first_name or '') + ' ' + (ud.last_name or ''))}</b></p>
            <p>Username: <b>{_esc(ud.username or '-')}</b></p>
            {github_lines_html}
          </div>
          {_copy_line(label="ID в боте", value=str(ud.id))}
          {_copy_line(label="Telegram ID", value=str(ud.telegram_id))}
          {_copy_line(label="UUID в панели Remnawave", value=str(ud.remnawave_uuid) if ud.remnawave_uuid else "—")}
          {_copy_line(label="Реф. код", value=str(ud.referral_code))}
          <div class="rounded-xl border border-base-content/10 bg-base-200/30 p-3">
            <h4 class="text-xs font-bold uppercase tracking-wide text-base-content/60 mb-2">Remnawave: проверка и ручная привязка</h4>
            <div class="flex flex-wrap items-end gap-3">
              <form method="post" action="/admin/users/{user_id}/remnawave/check" class="flex items-end gap-2">
                <button type="submit" class="btn btn-outline btn-sm h-10 min-h-10 gap-1.5">
                  <i class="fa-solid fa-magnifying-glass" aria-hidden="true"></i>Проверить в панели
                </button>
              </form>
              <form method="post" action="/admin/users/{user_id}/subscription/manual-bind" class="flex items-end gap-2"
                data-remna-confirm-msg="{_esc_attr(f'Привязать существующую подписку к пользователю #{user_id}?')}">
                <label class="form-control">
                  <span class="label-text text-xs opacity-70">Ручная привязка: ID подписки (локальный) или ID пользователя Remnawave</span>
                  <input
                    type="text"
                    name="subscription_id"
                    required
                    inputmode="numeric"
                    placeholder="105"
                    class="input input-bordered input-sm h-10 min-h-10 w-32"
                  />
                </label>
                <button type="submit" class="btn btn-secondary btn-sm h-10 min-h-10 gap-1.5">
                  <i class="fa-solid fa-link" aria-hidden="true"></i>Привязать
                </button>
              </form>
            </div>
          </div>
          <div class="flex flex-wrap items-end gap-2">
            <p class="m-0">Баланс: <b class="text-primary">{_esc(ud.balance)} ₽</b></p>
            <p class="m-0">Billing mode: <b>{_esc(ud.billing_mode)}</b></p>
            <form method="post" action="/admin/users/{user_id}/billing-mode/toggle"
              data-remna-confirm-msg="{_esc_attr(f'Переключить billing mode пользователя #{user_id}?')}">
              <button type="submit" class="btn btn-outline btn-sm h-9 min-h-9 gap-1.5">
                <i class="fa-solid fa-right-left" aria-hidden="true"></i>Переключить billing mode
              </button>
            </form>
            <form method="post" action="/admin/users/{user_id}/add-balance" class="flex flex-wrap items-end gap-2">
              <label class="form-control">
                <span class="label-text text-xs opacity-70">Баланс +₽</span>
                <input
                  type="text"
                  name="amount"
                  required
                  inputmode="decimal"
                  placeholder="0"
                  class="input input-bordered input-sm h-9 min-h-9 w-28"
                />
              </label>
              <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5">
                <i class="fa-solid fa-plus" aria-hidden="true"></i>Выдать
              </button>
            </form>
            <form method="post" action="/admin/users/{user_id}/reset-balance"
              data-remna-confirm-msg="{_esc_attr(f'Обнулить баланс пользователя #{user_id}?')}">
              <button type="submit" class="btn btn-warning btn-sm h-9 min-h-9 gap-1.5">
                <i class="fa-solid fa-eraser" aria-hidden="true"></i>Обнулить
              </button>
            </form>
          </div>
          {personal_pricing_block}
          {negative_risk_block}
          {billing_detail_block}
          <p>Регистрация: <b>{_fmt_dt_msk(ud.created_at)}</b></p>
          <p>Всего оплатил (без админ-бонусов): <b>{_esc(payments_total)} ₽</b></p>
          {ref_block}
          <div class="divider my-0"></div>
          <div class="rounded-xl border border-error/40 bg-error/5 p-4">
            <h3 class="text-sm font-bold uppercase tracking-wide text-error">Опасная зона</h3>
            <p class="text-xs opacity-80 mt-2">Полное удаление из PostgreSQL (подписки, транзакции и связанные данные по CASCADE) и удаление учётной записи в Remnawave, если задан UUID и не включён REMNAWAVE_STUB.</p>
            <form method="post" action="/admin/users/{user_id}/delete" class="mt-3 flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end"
              data-remna-confirm-msg="{_esc_attr('Удалить пользователя навсегда? Это необратимо.')}">
              <label class="form-control w-full max-w-xs">
                <span class="label-text text-xs font-medium">Подтверждение: введите Telegram ID</span>
                <input type="text" name="confirm_telegram_id" required inputmode="numeric" autocomplete="off" placeholder="{_esc(str(ud.telegram_id))}" class="input input-bordered input-sm h-9 min-h-9 font-mono" />
              </label>
              <button type="submit" class="btn btn-error btn-sm h-9 min-h-9 gap-1.5">
                <i class="fa-solid fa-user-slash" aria-hidden="true"></i>Удалить из БД и панели
              </button>
            </form>
          </div>
        </div>
      </div>
      <div class="flex flex-col gap-4">
        {vpn_link_card}
        {mgmt_html}
      </div>
    </div>
    {devices_block}
    <div class="grid gap-4 mt-4 xl:grid-cols-2">
      <div class="card bg-base-100 border border-base-content/10 shadow-lg">
        <div class="card-body gap-3">
          <h3 class="text-lg font-semibold"><i class="fa-solid fa-clock-rotate-left text-secondary mr-2" aria-hidden="true"></i>История подписок ({len(subs_tuples)})</h3>
          <div class="overflow-x-auto rounded-lg border border-base-content/10"><table class="table table-zebra table-sm"><thead><tr><th>ID</th><th>Статус</th><th>Старт</th><th>До</th><th>Устройства</th></tr></thead>
          <tbody>{subs_rows or '<tr><td colspan="5" class="opacity-50">Нет подписок</td></tr>'}</tbody></table></div>
        </div>
      </div>
      <div class="card bg-base-100 border border-base-content/10 shadow-lg">
        <div class="card-body gap-3">
          <h3 class="text-lg font-semibold"><i class="fa-solid fa-receipt text-accent mr-2" aria-hidden="true"></i>История транзакций ({len(txs_tuples)})</h3>
          <div class="overflow-x-auto rounded-lg border border-base-content/10"><table class="table table-zebra table-sm"><thead><tr><th>ID</th><th>Тип</th><th>Сумма</th><th>Статус</th><th>Провайдер</th><th>Дата</th><th class="whitespace-nowrap">Действие</th></tr></thead>
          <tbody>{tx_rows or '<tr><td colspan="7" class="opacity-50">Нет транзакций</td></tr>'}</tbody></table></div>
          <p class="text-xs opacity-70">Возврат доступен только для покупки тарифа или слота устройства с баланса (не для PAYG и пополнений).</p>
        </div>
      </div>
    </div>
    {tickets_block}
    """
    return _layout(f"User {user_id}", body, request=request, back_href="/admin/users")


@router.post("/users/{user_id}/transactions/{txn_id}/refund")
async def admin_user_transaction_refund(request: Request, user_id: int, txn_id: int) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        ok, msg = await admin_refund_purchase_transaction(
            session,
            user_id=user_id,
            txn_id=txn_id,
            settings=settings,
        )
        if ok:
            await session.commit()
            _USERS_HTML_CACHE.clear()
            return RedirectResponse(f"/admin/users/{user_id}?n=purchase_refund_ok", status_code=303)
        await session.rollback()
        return RedirectResponse(f"/admin/users/{user_id}?err={quote_plus(str(msg)[:800])}", status_code=303)


@router.post("/tariffs/toggle-shop")
async def admin_tariffs_toggle_shop_post(request: Request, enabled: str = Form("")) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    on = enabled.strip().lower() in ("1", "true", "yes", "on")
    await set_tariff_purchases_enabled(settings, on)
    return RedirectResponse("/admin/tariffs?n=tariffs_shop", status_code=303)


@router.get("/users/{user_id}/subscription-qr.png")
async def admin_user_subscription_qr(request: Request, user_id: int) -> Response:
    if not _is_logged(request):
        return Response(status_code=401)
    settings = get_settings()
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return Response(status_code=404)
        rw_uuid = user.remnawave_uuid
    if rw_uuid is None:
        return Response(status_code=404)
    try:
        rw = RemnaWaveClient(settings)
        uinf = _as_rw_user_profile(await rw.get_user(str(rw_uuid)))
        if not uinf:
            return Response(status_code=502)
        url = subscription_url_for_telegram(uinf.get("subscriptionUrl"), settings)
        if not url:
            return Response(status_code=404)
        png = subscription_url_qr_png(url)
    except (RemnaWaveError, ValueError, OSError):
        return Response(status_code=502)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/users/{user_id}/subscription/auto-renew")
async def admin_user_subscription_auto_renew(
    request: Request, user_id: int, enabled: str = Form("0")
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    want_on = (enabled or "").strip() == "1"
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        ok, msg = await set_subscription_auto_renew(session, user_id, want_on)
        if ok:
            await session.commit()
            n = "ar_on" if want_on else "ar_off"
            return RedirectResponse(f"/admin/users/{user_id}?n={n}", status_code=303)
        await session.rollback()
    err = str(msg).replace("\n", " ")[:400]
    return RedirectResponse(f"/admin/users/{user_id}?err={quote_plus(err)}", status_code=303)


@router.post("/users/{user_id}/add-balance")
async def admin_user_add_balance(
    request: Request, user_id: int, amount: str = Form(...)
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    raw = (amount or "").strip().replace(",", ".")
    try:
        amt = Decimal(raw)
    except (InvalidOperation, ValueError):
        return RedirectResponse(f"/admin/users/{user_id}?err={quote_plus('Неверная сумма')}", status_code=303)
    if amt <= 0:
        return RedirectResponse(f"/admin/users/{user_id}?err={quote_plus('Сумма должна быть > 0')}", status_code=303)
    wauth = request.session.get("wauth") or {}
    admin_tg = int(wauth.get("telegram_id") or 0)
    async with await _session() as session:
        u = await session.get(User, user_id)
        if u is None:
            return RedirectResponse("/admin/users", status_code=303)
        admin_db_id = None
        if admin_tg:
            au = (await session.execute(select(User).where(User.telegram_id == admin_tg))).scalar_one_or_none()
            if au is not None:
                admin_db_id = au.id
        u.balance += amt
        txn_bal = Transaction(
            user_id=u.id,
            type="admin_balance_add",
            amount=amt,
            currency="RUB",
            payment_provider="admin",
            payment_id=None,
            status="completed",
            description=f"Админ (web user) добавил баланс: +{amt} ₽",
            meta={"admin_id": admin_db_id, "source": "web_user"},
        )
        session.add(txn_bal)
        await session.flush()
        settings = get_settings()
        await apply_balance_credit_followups(
            session,
            user=u,
            credited=amt,
            settings=settings,
            triggering_txn=txn_bal,
            grant_referrer_reward=True,
            try_smart_cart=True,
        )
        await session.commit()
        await notify_admin(
            settings,
            title="💰 " + bold("Баланс пополнен (web-admin)"),
            lines=[
                web_admin_target_user_line(settings, u),
                plain("Сумма: ") + bold(f"+{amt} ₽"),
                web_admin_actor_notify_line(),
            ],
            event_type="admin_balance_add_web",
            topic=AdminLogTopic.BONUSES,
            subject_user=u,
            session=session,
        )
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=bal_ok", status_code=303)


@router.post("/users/{user_id}/reset-balance")
async def admin_user_reset_balance(request: Request, user_id: int) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    wauth = request.session.get("wauth") or {}
    admin_tg = int(wauth.get("telegram_id") or 0)
    async with await _session() as session:
        u = await session.get(User, user_id)
        if u is None:
            return RedirectResponse("/admin/users", status_code=303)
        admin_db_id = None
        if admin_tg:
            au = (await session.execute(select(User).where(User.telegram_id == admin_tg))).scalar_one_or_none()
            if au is not None:
                admin_db_id = au.id
        before = u.balance
        u.balance = Decimal("0")
        session.add(
            Transaction(
                user_id=u.id,
                type="admin_balance_reset",
                amount=Decimal("0"),
                currency="RUB",
                payment_provider="admin",
                payment_id=None,
                status="completed",
                description=f"Админ (web user) обнулил баланс: {before} ₽ -> 0 ₽",
                meta={
                    "admin_id": admin_db_id,
                    "source": "web_user",
                    "balance_before": str(before),
                    "balance_after": "0",
                },
            )
        )
        await session.flush()
        if settings.billing_v2_enabled and u.billing_mode == "hybrid":
            try:
                await baseline_meter_at_hybrid_transition(session, user=u, settings=settings)
            except Exception:
                logger.exception("baseline_meter after admin balance reset failed user_id=%s", u.id)
        await session.commit()
        await notify_admin(
            settings,
            title="💰 " + bold("Баланс обнулён (web-admin)"),
            lines=[
                web_admin_target_user_line(settings, u),
                plain("Было: ") + bold(f"{before} ₽") + plain(" → ") + bold("0 ₽"),
                web_admin_actor_notify_line(),
            ],
            event_type="admin_balance_reset_web",
            topic=AdminLogTopic.BONUSES,
            subject_user=u,
            session=session,
        )
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=bal_reset", status_code=303)


@router.post("/users/{user_id}/delete")
async def admin_user_delete_post(
    request: Request,
    user_id: int,
    confirm_telegram_id: str = Form(""),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    ct = (confirm_telegram_id or "").strip()
    linked = await _linked_bot_user_for_admin(request)
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        if ct != str(user.telegram_id):
            return RedirectResponse(
                f"/admin/users/{user_id}?err={quote_plus('Введите в поле точный Telegram ID этого пользователя (как подтверждение).')}",
                status_code=303,
            )
        if linked is not None and linked.id == user.id:
            return RedirectResponse(
                f"/admin/users/{user_id}?err={quote_plus('Нельзя удалить свою собственную учётную запись.')}",
                status_code=303,
            )
        target_line = web_admin_target_user_line(settings, user)
        ok, msg = await delete_user_from_app(session, user_id=user_id, settings=settings)
        if not ok:
            await session.rollback()
            err = str(msg).replace("\n", " ")[:800]
            return RedirectResponse(f"/admin/users/{user_id}?err={quote_plus(err)}", status_code=303)
        await session.commit()
        await notify_admin(
            settings,
            title="🗑 " + bold("Пользователь удалён (web-admin)"),
            lines=[
                target_line,
                plain("Удалён из БД и Remnawave (если был UUID)."),
                web_admin_actor_notify_line(),
            ],
            event_type="user_delete_web",
            topic=AdminLogTopic.USERS,
            session=session,
        )
    _USERS_HTML_CACHE.clear()
    return RedirectResponse("/admin/users?n=user_del", status_code=303)


@router.post("/users/{user_id}/billing-mode/toggle")
async def admin_user_toggle_billing_mode(request: Request, user_id: int) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        user.billing_mode = "hybrid" if user.billing_mode == "legacy" else "legacy"
        await session.flush()
        if user.billing_mode == "hybrid":
            try:
                await baseline_meter_at_hybrid_transition(session, user=user, settings=settings)
            except Exception:
                logger.exception("baseline_meter_at_hybrid_transition failed user_id=%s", user.id)
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=billing_mode_toggled", status_code=303)


@router.post("/users/{user_id}/remnawave/check")
async def admin_user_remnawave_check(
    request: Request,
    user_id: int,
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    if settings.remnawave_stub:
        return RedirectResponse(
            f"/admin/users/{user_id}?err={quote_plus('REMNAWAVE_STUB включен: проверка недоступна')}",
            status_code=303,
        )
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        rw = RemnaWaveClient(settings)
        hit: dict | None = None
        try:
            hit, _ = await _web_lookup_remnawave_by_tg_or_username(rw, str(user.telegram_id))
            if hit is None and (user.username or "").strip():
                hit, _ = await _web_lookup_remnawave_by_tg_or_username(rw, str(user.username))
        except RemnaWaveError as e:
            return RedirectResponse(
                f"/admin/users/{user_id}?err={quote_plus('Ошибка Remnawave: ' + str(e)[:220])}",
                status_code=303,
            )
        if hit is None:
            return RedirectResponse(
                f"/admin/users/{user_id}?err={quote_plus('В панели Remnawave запись не найдена по Telegram ID/username открытого пользователя')}",
                status_code=303,
            )
        rw_uuid_raw = str(hit.get("uuid") or "").strip()
        if not rw_uuid_raw:
            return RedirectResponse(
                f"/admin/users/{user_id}?err={quote_plus('Панель вернула запись без UUID')}",
                status_code=303,
            )
        try:
            rw_uuid = UUID(rw_uuid_raw)
        except ValueError:
            return RedirectResponse(
                f"/admin/users/{user_id}?err={quote_plus('UUID из панели имеет неверный формат')}",
                status_code=303,
            )
        user.remnawave_uuid = rw_uuid
        local_sub = (
            await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if local_sub is not None:
            local_sub.remnawave_sub_uuid = rw_uuid
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=rw_check_ok", status_code=303)


@router.post("/users/{user_id}/subscription/manual-bind")
async def admin_user_manual_bind_subscription(
    request: Request,
    user_id: int,
    subscription_id: str = Form(""),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    sid_raw = (subscription_id or "").strip()
    if not sid_raw.isdigit():
        return RedirectResponse(
            f"/admin/users/{user_id}?err={quote_plus('Неверный ID подписки')}",
            status_code=303,
        )
    sid = int(sid_raw)
    if sid <= 0:
        return RedirectResponse(
            f"/admin/users/{user_id}?err={quote_plus('Неверный ID подписки')}",
            status_code=303,
        )
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        sub = await session.get(Subscription, sid)
        panel_id_used = False
        if sub is None:
            settings = get_settings()
            if settings.remnawave_stub:
                return RedirectResponse(
                    f"/admin/users/{user_id}?err={quote_plus('Подписка не найдена локально: #' + str(sid))}",
                    status_code=303,
                )
            rw = RemnaWaveClient(settings)
            try:
                rw_user = await rw.find_user_by_panel_id(sid)
            except RemnaWaveError as e:
                return RedirectResponse(
                    f"/admin/users/{user_id}?err={quote_plus('Ошибка Remnawave: ' + str(e)[:220])}",
                    status_code=303,
                )
            if rw_user is None:
                return RedirectResponse(
                    f"/admin/users/{user_id}?err={quote_plus('Не найдено: ни локальная подписка #' + str(sid) + ', ни пользователь Remnawave с id=' + str(sid))}",
                    status_code=303,
                )
            rw_uuid_raw = str(rw_user.get("uuid") or "").strip()
            if not rw_uuid_raw:
                return RedirectResponse(
                    f"/admin/users/{user_id}?err={quote_plus('У пользователя Remnawave нет UUID')}",
                    status_code=303,
                )
            try:
                rw_uuid = UUID(rw_uuid_raw)
            except ValueError:
                return RedirectResponse(
                    f"/admin/users/{user_id}?err={quote_plus('UUID из Remnawave имеет неверный формат')}",
                    status_code=303,
                )
            sub = (
                await session.execute(
                    select(Subscription)
                    .where(Subscription.remnawave_sub_uuid == rw_uuid)
                    .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if sub is None:
                return RedirectResponse(
                    f"/admin/users/{user_id}?err={quote_plus('В панели найден пользователь id=' + str(sid) + ', но локальная подписка с его UUID не найдена')}",
                    status_code=303,
                )
            panel_id_used = True
        sub.user_id = user.id
        if sub.remnawave_sub_uuid is not None:
            user.remnawave_uuid = sub.remnawave_sub_uuid
        dev_rows = await session.execute(select(Device).where(Device.subscription_id == sub.id))
        for d in dev_rows.scalars().all():
            d.user_id = user.id
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=manual_bind_ok", status_code=303)


@router.post("/users/{user_id}/risk-notify/reset")
async def admin_user_risk_notify_reset(request: Request, user_id: int) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        user.risk_notified_24h_at = None
        user.risk_notified_1h_at = None
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=risk_reset", status_code=303)


@router.post("/users/{user_id}/subscription/disable")
async def admin_user_subscription_disable(
    request: Request, user_id: int, subscription_id: int = Form(...)
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        ok, msg = await admin_disable_subscription_record(
            session,
            user_id=user_id,
            subscription_id=subscription_id,
            settings=settings,
        )
        if ok:
            await session.commit()
            return RedirectResponse(f"/admin/users/{user_id}?n=sub_off", status_code=303)
        await session.rollback()
    err = str(msg).replace("\n", " ")[:400]
    return RedirectResponse(f"/admin/users/{user_id}?err={quote_plus(err)}", status_code=303)


@router.post("/users/{user_id}/subscription/enable")
async def admin_user_subscription_enable(
    request: Request, user_id: int, subscription_id: int = Form(...)
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        ok, msg = await admin_enable_subscription_record(
            session,
            user_id=user_id,
            subscription_id=subscription_id,
            settings=settings,
        )
        if ok:
            await session.commit()
            return RedirectResponse(f"/admin/users/{user_id}?n=sub_on", status_code=303)
        await session.rollback()
    err = str(msg).replace("\n", " ")[:400]
    return RedirectResponse(f"/admin/users/{user_id}?err={quote_plus(err)}", status_code=303)


@router.post("/users/{user_id}/subscription/add-days")
async def admin_user_subscription_add_days(
    request: Request,
    user_id: int,
    subscription_id: int = Form(...),
    days: int = Form(...),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        ok, err = await admin_adjust_subscription_days(
            session,
            user_id=user_id,
            sub_id=subscription_id,
            days_delta=int(days),
            settings=settings,
        )
        if not ok:
            return RedirectResponse(
                f"/admin/users/{user_id}?err={quote_plus(err)}",
                status_code=303,
            )
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=days_ok", status_code=303)


@router.post("/users/{user_id}/subscription/set-device-slots")
async def admin_user_subscription_set_device_slots(
    request: Request,
    user_id: int,
    subscription_id: int = Form(...),
    devices_count: int = Form(...),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    dc = int(devices_count)
    if dc < MIN_DEVICES or dc > MAX_DEVICES:
        return RedirectResponse(
            f"/admin/users/{user_id}?err={quote_plus(f'Слотов: целое число от {MIN_DEVICES} до {MAX_DEVICES}')}",
            status_code=303,
        )
    settings = get_settings()
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        sub = (
            await session.execute(
                select(Subscription).where(
                    Subscription.id == subscription_id,
                    Subscription.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if sub is None:
            return RedirectResponse(
                f"/admin/users/{user_id}?err={quote_plus('Подписка не найдена')}",
                status_code=303,
            )
        sub.devices_count = dc
        if user.remnawave_uuid is not None and not settings.remnawave_stub:
            rw = RemnaWaveClient(settings)
            try:
                await update_rw_user_respecting_hwid_limit(
                    rw,
                    str(user.remnawave_uuid),
                    devices_limit_for_panel=sub.devices_count,
                )
            except RemnaWaveError:
                pass
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=device_slots_ok", status_code=303)


@router.post("/users/{user_id}/subscription/adjust-device-slots")
async def admin_user_subscription_adjust_device_slots(
    request: Request,
    user_id: int,
    subscription_id: int = Form(...),
    delta: int = Form(...),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        ok, msg = await admin_adjust_subscription_device_slots(
            session,
            user_id=user_id,
            sub_id=int(subscription_id),
            delta=int(delta),
            settings=settings,
        )
        if not ok:
            return RedirectResponse(
                f"/admin/users/{user_id}?err={quote_plus(str(msg))}",
                status_code=303,
            )
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=device_slots_ok", status_code=303)


@router.post("/users/{user_id}/personal-pricing/custom-month-price")
async def admin_user_set_custom_month_price(
    request: Request,
    user_id: int,
    price_rub: str = Form(""),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    raw = (price_rub or "").strip().replace(",", ".")
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        if not raw:
            user.custom_subscription_month_price_rub = None
        else:
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                return RedirectResponse(
                    f"/admin/users/{user_id}?err={quote_plus('Некорректная цена')}",
                    status_code=303,
                )
            if amount <= 0:
                user.custom_subscription_month_price_rub = None
            else:
                user.custom_subscription_month_price_rub = amount.quantize(Decimal("0.01"))
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=personal_price_ok", status_code=303)


@router.post("/users/{user_id}/personal-pricing/discount")
async def admin_user_set_personal_discount(
    request: Request,
    user_id: int,
    discount_percent: str = Form(""),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    raw = (discount_percent or "").strip().replace(",", ".")
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        if not raw:
            user.personal_tariff_discount_percent = None
        else:
            try:
                pct = Decimal(raw)
            except InvalidOperation:
                return RedirectResponse(
                    f"/admin/users/{user_id}?err={quote_plus('Некорректная скидка')}",
                    status_code=303,
                )
            if pct <= 0:
                user.personal_tariff_discount_percent = None
            elif pct > 100:
                return RedirectResponse(
                    f"/admin/users/{user_id}?err={quote_plus('Скидка не может быть больше 100%')}",
                    status_code=303,
                )
            else:
                user.personal_tariff_discount_percent = pct.quantize(Decimal("0.01"))
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=personal_discount_ok", status_code=303)


@router.post("/users/{user_id}/unlink-hwid")
async def admin_user_unlink_hwid(
    request: Request,
    user_id: int,
    hwid: str = Form(""),
    mode: str = Form("decrease_slot"),
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    mode_n = (mode or "decrease_slot").strip()
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        if mode_n == "keep_slots":
            ok, msg = await unlink_hwid_device_keep_slots(
                session, user=user, hwid=hwid, settings=settings, initiator="web_admin"
            )
            ncode = "hwid_keep"
        else:
            ok, msg = await remove_hwid_device_from_panel(
                session, user=user, hwid=hwid, settings=settings, initiator="web_admin"
            )
            ncode = "hwid_slot"
        if ok:
            await session.commit()
            return RedirectResponse(f"/admin/users/{user_id}?n={ncode}", status_code=303)
        await session.rollback()
    err = str(msg).replace("\n", " ")[:400]
    return RedirectResponse(f"/admin/users/{user_id}?err={quote_plus(err)}", status_code=303)


@router.post("/users/{user_id}/unlink-device")
async def admin_user_unlink_device(request: Request, user_id: int, device_id: int = Form(...)) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        ok, msg = await remove_device_slot(
            session, user=user, device_id=device_id, settings=settings, initiator="web_admin"
        )
        if ok:
            await session.commit()
            return RedirectResponse(f"/admin/users/{user_id}?n=db_slot", status_code=303)
        await session.rollback()
    err = str(msg).replace("\n", " ")[:400]
    return RedirectResponse(f"/admin/users/{user_id}?err={quote_plus(err)}", status_code=303)


@router.get("/profile/vpn-qr.png")
async def admin_profile_vpn_qr(request: Request) -> Response:
    if not _is_logged(request):
        return Response(status_code=401)
    settings = get_settings()
    linked = await _linked_bot_user_for_admin(request)
    if linked is None or linked.remnawave_uuid is None:
        return Response(status_code=404)
    try:
        rw = RemnaWaveClient(settings)
        uinf = await rw.get_user(str(linked.remnawave_uuid))
        url = subscription_url_for_telegram(uinf.get("subscriptionUrl"), settings)
        if not url:
            return Response(status_code=404)
        png = subscription_url_qr_png(url)
    except (RemnaWaveError, ValueError, OSError):
        return Response(status_code=502)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/profile")
async def admin_profile(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    auth = _auth_data(request)
    linked = await _linked_bot_user_for_admin(request)
    display = (settings.web_admin_profile_display_name or "").strip() or str(
        (linked.first_name if linked is not None and linked.first_name else "")
        or auth.get("label")
        or "Администратор"
    )
    kind = str(auth.get("kind") or "")
    avatar = _esc(_user_avatar_photo_src(linked) if linked is not None else _auth_avatar(request))
    admin_ids = set(settings.admin_telegram_ids)
    parts: list[str] = []
    if kind == "telegram":
        tid = auth.get("id")
        try:
            tid_int = int(tid) if tid is not None else None
        except (TypeError, ValueError):
            tid_int = None
        un = str(auth.get("username") or "").strip()
        parts.append("<p class='text-base'>Вход через <b>Telegram</b>.</p>")
        if tid_int is not None:
            parts.append(f"<p>Telegram ID: <code class='bg-base-300 px-1.5 py-0.5 rounded text-xs'>{tid_int}</code></p>")
        if un:
            tg_url = f"https://t.me/{url_quote(un)}"
            parts.append(
                f"<p>Профиль: <a class='link link-primary font-medium' href=\"{_esc(tg_url)}\" target=\"_blank\" rel=\"noopener\">@{_esc(un)}</a></p>"
            )
    elif kind == "github":
        login = str(auth.get("login") or auth.get("username") or "").strip()
        parts.append("<p class='text-base'>Вход через <b>GitHub</b>.</p>")
        tg_from_session = auth.get("telegram_id")
        if tg_from_session is not None:
            parts.append(
                "<p>Связанный Telegram ID: <code class='bg-base-300 px-1.5 py-0.5 rounded text-xs'>"
                + _esc(str(tg_from_session))
                + "</code> (приоритетный аккаунт)</p>"
            )
        if login:
            parts.append(
                f"<p>Аккаунт: <a class='link link-primary font-medium' href=\"https://github.com/{_esc(login)}\" target=\"_blank\" rel=\"noopener\">{_esc(login)}</a></p>"
            )
    else:
        parts.append("<p class='opacity-70'>Способ входа не определён.</p>")

    panel_raw = (settings.remnawave_public_url or settings.remnawave_api_url or "").strip().rstrip("/")
    if panel_raw:
        parts.append(
            f"<p><a class='btn btn-outline btn-sm h-9 min-h-9 gap-1.5 normal-case' href=\"{_esc(panel_raw)}\" target=\"_blank\" rel=\"noopener\">"
            "<i class='fa-solid fa-arrow-up-right-from-square' aria-hidden='true'></i>Панель Remnawave</a></p>"
        )
    ties = "\n".join(parts)
    bot_profile_href = f"/admin/users/{int(linked.id)}" if linked is not None else "/admin/profile"
    account_link_button = ""
    if linked is not None and (linked.github_username or "").strip():
        account_link_button = """
          <form method="post" action="/admin/profile/github/unlink" data-remna-confirm-msg="Отменить связку Telegram и GitHub?">
            <button type="submit" class="btn btn-outline btn-error btn-sm h-9 min-h-9 gap-1.5">
              <i class="fa-solid fa-link-slash" aria-hidden="true"></i>Отменить связку аккаунтов
            </button>
          </form>
        """
    else:
        account_link_button = (
            '<a class="btn btn-sm h-9 min-h-9 gap-1.5" href="/admin/login?link=1">'
            '<i class="fa-solid fa-link" aria-hidden="true"></i>Связать Telegram и GitHub</a>'
        )
    profile_notice = ""
    ncode = (request.query_params.get("n") or "").strip()
    err = (request.query_params.get("err") or "").strip()
    if ncode == "bal_ok":
        profile_notice = "<div class='alert alert-success shadow-sm'><span>Баланс успешно пополнен.</span></div>"
    elif ncode == "linked_gh":
        profile_notice = "<div class='alert alert-success shadow-sm'><span>GitHub успешно привязан к вашему Telegram-профилю.</span></div>"
    elif ncode == "linked_tg":
        profile_notice = "<div class='alert alert-success shadow-sm'><span>Telegram успешно привязан. Теперь профиль работает с приоритетом Telegram ID.</span></div>"
    elif ncode == "gh_unlinked":
        profile_notice = "<div class='alert alert-success shadow-sm'><span>GitHub отвязан от профиля.</span></div>"
    elif ncode == "2fa_setup":
        profile_notice = "<div class='alert alert-success shadow-sm'><span>Секрет 2FA создан. Отсканируйте QR и подтвердите код.</span></div>"
    elif ncode == "2fa_on":
        profile_notice = "<div class='alert alert-success shadow-sm'><span>2FA включена для входа в web-admin.</span></div>"
    elif ncode == "2fa_off":
        profile_notice = "<div class='alert alert-success shadow-sm'><span>2FA отключена.</span></div>"
    elif ncode == "session_revoked" or ncode.startswith("sessions_revoked_"):
        profile_notice = "<div class='alert alert-success shadow-sm'><span>Сессия отозвана. Повторный вход потребует OAuth и 2FA.</span></div>"
    elif err:
        profile_notice = (
            "<div class='alert alert-error shadow-sm'><span>"
            + _esc(err)
            + "</span></div>"
        )
    uinf_p: dict | None = None
    sub_url_p: str | None = None
    if linked is not None and linked.remnawave_uuid is not None:
        try:
            rw = RemnaWaveClient(settings)
            uinf_p = await rw.get_user(str(linked.remnawave_uuid))
            sub_url_p = subscription_url_for_telegram(uinf_p.get("subscriptionUrl"), settings)
        except RemnaWaveError:
            pass

    vpn_block = ""
    profile_balance_block = ""
    profile_2fa_block = ""
    profile_sessions_block = ""
    setup_secret = str(request.session.get("admin_2fa_setup_secret") or "").strip()
    setup_uid_raw = request.session.get("admin_2fa_setup_user_id")
    try:
        setup_uid = int(setup_uid_raw) if setup_uid_raw is not None else 0
    except (TypeError, ValueError):
        setup_uid = 0
    if linked is not None:
        profile_sessions_block = await _profile_browser_sessions_html(request, linked.id)
        link_badges = ""
        if linked.github_username:
            gh = _esc(linked.github_username)
            link_badges = (
                "<div class='flex flex-wrap items-center gap-2 text-xs'>"
                "<span class='badge badge-success badge-sm'>Telegram связан</span>"
                f"<span class='badge badge-outline badge-sm'>GitHub: @{gh}</span>"
                "</div>"
            )
        profile_balance_block = f"""
    <div class="card bg-base-100 border border-primary/25 shadow-lg">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-wallet text-primary mr-2" aria-hidden="true"></i>Баланс в боте</h3>
        {link_badges}
        <p class="text-sm opacity-75">Привязанный профиль: <b>#{linked.id}</b> · Telegram ID: <code class="bg-base-300 px-1 rounded text-xs">{linked.telegram_id}</code></p>
        <p class="text-base">Текущий баланс: <b class="text-primary text-xl">{_esc(linked.balance)} ₽</b></p>
        <form method="post" action="/admin/profile/add-balance" class="flex flex-wrap items-end gap-2">
          <label class="form-control">
            <span class="label-text text-xs opacity-70">Сумма пополнения, ₽</span>
            <input type="text" name="amount" required class="input input-bordered input-sm h-9 min-h-9 w-40" placeholder="100" />
          </label>
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-plus" aria-hidden="true"></i>Пополнить
          </button>
        </form>
      </div>
    </div>"""
    elif kind == "telegram":
        profile_balance_block = """
    <div class="card bg-base-100 border border-warning/30 shadow-lg">
      <div class="card-body gap-2">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-wallet text-warning mr-2" aria-hidden="true"></i>Баланс в боте</h3>
        <p class="text-sm opacity-80">Не найден связанный пользователь бота. Нажмите <b>/start</b> в боте и обновите страницу.</p>
      </div>
    </div>"""
    if linked is not None:
        enabled = bool(linked.web_admin_totp_enabled and (linked.web_admin_totp_secret or "").strip())
        if setup_secret and setup_uid == linked.id and not enabled:
            account = str(linked.username or linked.first_name or f"tg{linked.telegram_id}")
            qr_data = _totp_qr_data_uri(secret=setup_secret, account_name=account)
            profile_2fa_block = f"""
    <div class="card bg-base-100 border border-info/30 shadow-lg">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-shield-halved text-info mr-2" aria-hidden="true"></i>Двухэтапная авторизация (2FA)</h3>
        <p class="text-sm opacity-80">1) Отсканируйте QR в Google Authenticator.<br/>2) Введите 6-значный код для подтверждения.</p>
        <div class="flex flex-col items-center gap-2 rounded-xl border border-base-content/10 bg-base-200/50 p-4">
          <img src="{_esc(qr_data)}" alt="QR 2FA" class="max-w-[260px] rounded-xl border border-base-content/15 bg-base-100 p-2 shadow-md" width="260" height="260" loading="lazy" />
          <code class="text-xs opacity-70">{_esc(setup_secret)}</code>
        </div>
        <form method="post" action="/admin/profile/2fa/enable" class="flex flex-wrap items-end gap-2">
          <label class="form-control">
            <span class="label-text text-xs opacity-70">Код подтверждения</span>
            <input type="text" name="code" required inputmode="numeric" pattern="[0-9 ]{{6,8}}" maxlength="8" class="input input-bordered input-sm h-9 min-h-9 w-36 tracking-[0.2em]" placeholder="123456" />
          </label>
          <button type="submit" class="btn btn-info btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-check" aria-hidden="true"></i>Включить 2FA
          </button>
        </form>
      </div>
    </div>"""
        elif enabled:
            profile_2fa_block = """
    <div class="card bg-base-100 border border-success/30 shadow-lg">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-shield-halved text-success mr-2" aria-hidden="true"></i>Двухэтапная авторизация (2FA)</h3>
        <p class="text-sm opacity-80">2FA включена. При входе в web-admin нужно подтверждение кодом из Google Authenticator.</p>
        <form method="post" action="/admin/profile/2fa/disable" class="flex flex-wrap items-end gap-2">
          <label class="form-control">
            <span class="label-text text-xs opacity-70">Код для отключения</span>
            <input type="text" name="code" required inputmode="numeric" pattern="[0-9 ]{6,8}" maxlength="8" class="input input-bordered input-sm h-9 min-h-9 w-36 tracking-[0.2em]" placeholder="123456" />
          </label>
          <button type="submit" class="btn btn-error btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-power-off" aria-hidden="true"></i>Отключить 2FA
          </button>
        </form>
      </div>
    </div>"""
        else:
            profile_2fa_block = """
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-shield-halved text-primary mr-2" aria-hidden="true"></i>Двухэтапная авторизация (2FA)</h3>
        <p class="text-sm opacity-80">Добавьте второй фактор через Google Authenticator для защиты входа в web-admin.</p>
        <form method="post" action="/admin/profile/2fa/setup">
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-qrcode" aria-hidden="true"></i>Настроить 2FA
          </button>
        </form>
      </div>
    </div>"""
    if sub_url_p:
        conn_lines = ""
        if uinf_p is not None:
            oa = rw_user_online_at(uinf_p)
            fa = rw_user_first_connected_at(uinf_p)
            if oa is not None:
                conn_lines += f"<p class='text-sm text-base-content/75'>Последняя активность в панели: <b>{_fmt_dt_msk(oa)}</b></p>"
            if fa is not None:
                conn_lines += f"<p class='text-sm text-base-content/75'>Первое подключение: <b>{_fmt_dt_msk(fa)}</b></p>"
        vpn_block = f"""
    <div class="card bg-base-100 border border-success/30 shadow-lg overflow-hidden">
      <div class="h-1.5 w-full bg-gradient-to-r from-success/70 via-primary/60 to-accent/60"></div>
      <div class="card-body gap-4">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-link text-success mr-2" aria-hidden="true"></i>Моя VPN-подписка</h3>
        <p class="text-xs opacity-70">Данные вашего аккаунта в боте (совпадающий Telegram ID).</p>
        {_copy_line(label="Ссылка подписки", value=sub_url_p)}
        {conn_lines}
        <div class="flex flex-col items-center gap-2 rounded-xl border border-base-content/10 bg-base-200/50 p-4">
          <span class="text-xs font-medium uppercase tracking-wide text-base-content/50">QR-код</span>
          <img src="/admin/profile/vpn-qr.png" alt="QR подписки" class="max-w-[260px] rounded-xl border border-base-content/15 bg-base-100 p-2 shadow-md" width="260" height="260" loading="lazy" />
        </div>
      </div>
    </div>"""
    elif kind == "github":
        vpn_block = """
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-2">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-circle-info text-info mr-2" aria-hidden="true"></i>VPN-подписка</h3>
        <p class="text-sm opacity-80">Ссылку подписки и QR можно посмотреть, войдя в админку через <b>Telegram</b> тем же аккаунтом, что в боте.</p>
      </div>
    </div>"""
    elif kind == "telegram" and linked is None:
        vpn_block = """
    <div class="card bg-base-100 border border-warning/30 shadow-lg">
      <div class="card-body gap-2">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-triangle-exclamation text-warning mr-2" aria-hidden="true"></i>VPN-подписка</h3>
        <p class="text-sm opacity-80">В базе бота нет пользователя с вашим Telegram ID. Нажмите /start в боте, затем обновите эту страницу.</p>
      </div>
    </div>"""
    elif kind == "telegram" and linked is not None and linked.remnawave_uuid is None:
        vpn_block = """
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-2">
        <h3 class="text-lg font-semibold">VPN-подписка</h3>
        <p class="text-sm opacity-80">У записи в боте ещё нет UUID панели Remnawave — активируйте триал или купите подписку в боте.</p>
      </div>
    </div>"""

    body = f"""
    <div class="mx-auto flex max-w-3xl flex-col gap-6">
    {profile_notice}
    <div class="relative overflow-hidden rounded-2xl border border-base-content/10 bg-base-100 shadow-xl">
      <div class="pointer-events-none absolute -right-4 -top-8 h-40 w-60 rotate-12 rounded-3xl bg-gradient-to-br from-secondary/50 via-primary/45 to-accent/35 blur-sm" aria-hidden="true"></div>
      <div class="absolute right-4 top-4 z-10">
        <span class="badge badge-secondary badge-lg font-semibold shadow-md">Админ</span>
      </div>
      <div class="card-body relative z-[1] gap-4 pt-8">
        <div class="flex flex-col items-center gap-3">
          <img src="{avatar}" alt="" class="h-24 w-24 rounded-full border-4 border-primary/35 object-cover shadow-lg ring-4 ring-base-200" width="96" height="96" />
          <h2 class="text-center text-2xl font-bold tracking-tight">{_esc(display)}</h2>
        </div>
      </div>
    </div>
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold border-b border-base-content/10 pb-2"><i class="fa-solid fa-key text-primary mr-2" aria-hidden="true"></i>Сессия и доступ</h3>
        <div class="space-y-2 text-sm">{ties}</div>
        <div class="flex flex-wrap gap-2 pt-1">
          <a class="btn btn-outline btn-sm h-9 min-h-9 gap-1.5" href="{_esc(bot_profile_href)}">
            <i class="fa-solid fa-user" aria-hidden="true"></i>Мой профиль в боте
          </a>
          {account_link_button}
        </div>
      </div>
    </div>
    {profile_2fa_block}
    {profile_sessions_block}
    {profile_balance_block}
    {vpn_block}
    </div>
    """
    return _layout(
        "Мой профиль",
        body,
        request=request,
        back_href="/admin/dashboard",
        sidebar_avatar_url=_user_avatar_photo_src(linked) if linked is not None else None,
        sidebar_user_label=linked.first_name if linked is not None and linked.first_name else None,
    )


@router.post("/profile/sessions/{session_id}/revoke")
async def admin_profile_revoke_session(request: Request, session_id: int) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    linked = await _linked_bot_user_for_admin(request)
    if linked is None:
        return RedirectResponse("/admin/profile?err=" + quote_plus("Нет профиля бота"), status_code=303)
    async with await _session() as session:
        ok = await revoke_browser_session_by_id(
            session, user_id=linked.id, session_row_id=session_id
        )
    if not ok:
        return RedirectResponse("/admin/profile?err=" + quote_plus("Сессия не найдена"), status_code=303)
    return RedirectResponse("/admin/profile?n=session_revoked", status_code=303)


@router.post("/profile/sessions/revoke-all")
async def admin_profile_revoke_all_sessions(request: Request) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    linked = await _linked_bot_user_for_admin(request)
    if linked is None:
        return RedirectResponse("/admin/profile?err=" + quote_plus("Нет профиля бота"), status_code=303)
    current = str(request.session.get("wauth_session_token") or "").strip()
    async with await _session() as session:
        n = await revoke_all_user_browser_sessions(
            session, linked.id, except_token=current or None
        )
    return RedirectResponse(f"/admin/profile?n=sessions_revoked_{n}", status_code=303)


@router.post("/profile/add-balance")
async def admin_profile_add_balance(request: Request, amount: str = Form(...)) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    linked = await _linked_bot_user_for_admin(request)
    if linked is None:
        return RedirectResponse(
            f"/admin/profile?err={quote_plus('Связанный пользователь бота не найден')}",
            status_code=303,
        )
    raw = (amount or "").strip().replace(",", ".")
    try:
        amt = Decimal(raw)
    except (InvalidOperation, ValueError):
        return RedirectResponse(f"/admin/profile?err={quote_plus('Неверная сумма')}", status_code=303)
    if amt <= 0:
        return RedirectResponse(f"/admin/profile?err={quote_plus('Сумма должна быть > 0')}", status_code=303)
    wauth = request.session.get("wauth") or {}
    admin_tg = int(wauth.get("telegram_id") or 0)
    async with await _session() as session:
        u = await session.get(User, linked.id)
        if u is None:
            return RedirectResponse(f"/admin/profile?err={quote_plus('Пользователь не найден')}", status_code=303)
        admin_db_id = None
        if admin_tg:
            au = (await session.execute(select(User).where(User.telegram_id == admin_tg))).scalar_one_or_none()
            if au is not None:
                admin_db_id = au.id
        u.balance += amt
        txn_bal = Transaction(
            user_id=u.id,
            type="admin_balance_add",
            amount=amt,
            currency="RUB",
            payment_provider="admin",
            payment_id=None,
            status="completed",
            description=f"Админ (web profile) добавил баланс: +{amt} ₽",
            meta={"admin_id": admin_db_id, "source": "web_profile"},
        )
        session.add(txn_bal)
        await session.flush()
        settings = get_settings()
        await apply_balance_credit_followups(
            session,
            user=u,
            credited=amt,
            settings=settings,
            triggering_txn=txn_bal,
            grant_referrer_reward=True,
            try_smart_cart=True,
        )
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse("/admin/profile?n=bal_ok", status_code=303)


@router.post("/profile/2fa/setup")
async def admin_profile_2fa_setup(request: Request) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    linked = await _linked_bot_user_for_admin(request)
    if linked is None:
        return RedirectResponse(
            "/admin/profile?err=" + quote_plus("Сначала нужен связанный Telegram-профиль."),
            status_code=303,
        )
    request.session["admin_2fa_setup_secret"] = pyotp.random_base32()
    request.session["admin_2fa_setup_user_id"] = int(linked.id)
    return RedirectResponse("/admin/profile?n=2fa_setup", status_code=303)


@router.post("/profile/2fa/enable")
async def admin_profile_2fa_enable(request: Request, code: str = Form("")) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    linked = await _linked_bot_user_for_admin(request)
    if linked is None:
        return RedirectResponse(
            "/admin/profile?err=" + quote_plus("Сначала нужен связанный Telegram-профиль."),
            status_code=303,
        )
    secret = str(request.session.get("admin_2fa_setup_secret") or "").strip()
    setup_uid = request.session.get("admin_2fa_setup_user_id")
    try:
        setup_uid_i = int(setup_uid) if setup_uid is not None else 0
    except (TypeError, ValueError):
        setup_uid_i = 0
    if not secret or setup_uid_i != linked.id:
        return RedirectResponse(
            "/admin/profile?err=" + quote_plus("Сначала создайте QR для настройки 2FA."),
            status_code=303,
        )
    otp = _totp_normalize_code(code)
    if not pyotp.TOTP(secret).verify(otp, valid_window=1):
        return RedirectResponse(
            "/admin/profile?err=" + quote_plus("Неверный код подтверждения 2FA."),
            status_code=303,
        )
    async with await _session() as session:
        user = await session.get(User, linked.id)
        if user is None:
            return RedirectResponse(
                "/admin/profile?err=" + quote_plus("Пользователь не найден."),
                status_code=303,
            )
        user.web_admin_totp_secret = secret
        user.web_admin_totp_enabled = True
        await session.commit()
    request.session.pop("admin_2fa_setup_secret", None)
    request.session.pop("admin_2fa_setup_user_id", None)
    return RedirectResponse("/admin/profile?n=2fa_on", status_code=303)


@router.post("/profile/2fa/disable")
async def admin_profile_2fa_disable(request: Request, code: str = Form("")) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    linked = await _linked_bot_user_for_admin(request)
    if linked is None:
        return RedirectResponse(
            "/admin/profile?err=" + quote_plus("Сначала нужен связанный Telegram-профиль."),
            status_code=303,
        )
    secret = (linked.web_admin_totp_secret or "").strip()
    if not linked.web_admin_totp_enabled or not secret:
        return RedirectResponse("/admin/profile?n=2fa_off", status_code=303)
    otp = _totp_normalize_code(code)
    if not pyotp.TOTP(secret).verify(otp, valid_window=1):
        return RedirectResponse(
            "/admin/profile?err=" + quote_plus("Неверный код для отключения 2FA."),
            status_code=303,
        )
    async with await _session() as session:
        user = await session.get(User, linked.id)
        if user is None:
            return RedirectResponse(
                "/admin/profile?err=" + quote_plus("Пользователь не найден."),
                status_code=303,
            )
        user.web_admin_totp_enabled = False
        user.web_admin_totp_secret = None
        await session.commit()
    request.session.pop("admin_2fa_setup_secret", None)
    request.session.pop("admin_2fa_setup_user_id", None)
    return RedirectResponse("/admin/profile?n=2fa_off", status_code=303)


@router.post("/profile/github/unlink")
async def admin_profile_github_unlink(request: Request) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    linked = await _linked_bot_user_for_admin(request)
    if linked is None:
        return RedirectResponse(
            "/admin/profile?err=" + quote_plus("Сначала нужен связанный Telegram-профиль."),
            status_code=303,
        )
    async with await _session() as session:
        user = await session.get(User, linked.id)
        if user is None:
            return RedirectResponse(
                "/admin/profile?err=" + quote_plus("Пользователь не найден."),
                status_code=303,
            )
        user.github_id = None
        user.github_username = None
        user.github_profile_url = None
        await session.commit()
    wauth = _auth_data(request)
    if isinstance(wauth, dict):
        wauth.pop("github_login", None)
        wauth.pop("github_avatar_url", None)
        request.session["wauth"] = wauth
    return RedirectResponse("/admin/profile?n=gh_unlinked", status_code=303)


@router.get("/settings")
async def admin_settings(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    vals = read_whitelist_values()
    bg_assets = _list_admin_image_assets()
    bg_source = vals.get("ADMIN_BACKGROUND_SOURCE", "default") or "default"
    bg_url = vals.get("ADMIN_BACKGROUND_URL", "") or ""
    bg_asset = vals.get("ADMIN_BACKGROUND_ASSET", "") or ""
    bg_asset_cards = "".join(
        (
            f"<label class='cursor-pointer rounded-xl border border-base-content/10 bg-base-200/40 p-2 hover:border-primary/35'>"
            f"<input type='radio' class='radio radio-sm mr-2' name='ADMIN_BACKGROUND_ASSET_PICK' value='{_esc(name)}' {'checked' if name == bg_asset else ''} />"
            f"<span class='text-xs font-medium'>{_esc(name)}</span>"
            f"<img src='/assets/{url_quote(name)}' alt='' class='mt-2 h-20 w-full rounded-lg object-cover border border-base-content/10' loading='lazy' />"
            f"</label>"
        )
        for name in bg_assets
    ) or "<p class='text-sm opacity-60'>В папке /assets пока нет подходящих изображений.</p>"
    tab_buttons: list[str] = []
    tab_panels: list[str] = []
    for idx, (sec_id, sec_title, fields) in enumerate(WEB_ADMIN_ENV_SECTIONS):
        active = "btn-outline btn-primary" if idx == 0 else "btn-ghost border border-primary/40 hover:border-primary/70"
        tab_buttons.append(
            f"<button type=\"button\" data-env-tab=\"{_esc(sec_id)}\" class=\"btn btn-sm h-9 min-h-9 shrink-0 gap-1.5 {active}\">"
            f"{_esc(sec_title)}</button>"
        )
        hidden = "" if idx == 0 else " hidden"
        flds: list[str] = []
        for key, label, _getter, help_text in fields:
            v = vals.get(key, "")
            if key in _ENV_BOOL_KEYS:
                bool_state = _env_bool_state(v)
                bool_state = bool_state if bool_state is not None else False
                ctrl = (
                    f"<label class='remna-env-toggle'>"
                    f"<input type='hidden' name='{_esc(key)}' value='{'true' if bool_state else 'false'}' data-env-bool-hidden />"
                    f"<input type='checkbox' {'checked' if bool_state else ''} data-env-bool-toggle />"
                    f"<span class='remna-env-toggle-track'></span>"
                    f"</label>"
                )
            elif key in _ENV_MULTI_CHOICE_OPTIONS:
                chosen = {part.strip() for part in v.split(",") if part.strip()}
                buttons = "".join(
                    f"<button type='button' class='btn btn-sm h-9 min-h-9 {'btn-primary' if opt_value in chosen else 'btn-ghost'}' "
                    f"data-env-multi-option='{_esc(opt_value)}'>{_esc(opt_label)}</button>"
                    for opt_value, opt_label in _ENV_MULTI_CHOICE_OPTIONS[key]
                )
                ctrl = (
                    f"<div class='flex flex-col gap-2'>"
                    f"<input type='hidden' name='{_esc(key)}' value='{_esc(v)}' data-env-multi-hidden />"
                    f"<div class='flex flex-wrap gap-2' data-env-multi-group>{buttons}</div>"
                    f"</div>"
                )
            elif key in _ENV_CHOICE_OPTIONS:
                buttons = "".join(
                    f"<button type='button' class='btn btn-sm h-9 min-h-9 {'btn-primary' if opt_value == v else 'btn-ghost'}' "
                    f"data-env-choice-value='{_esc(opt_value)}'>{_esc(opt_label)}</button>"
                    for opt_value, opt_label in _ENV_CHOICE_OPTIONS[key]
                )
                ctrl = (
                    f"<div class='flex flex-col gap-2'>"
                    f"<input type='hidden' name='{_esc(key)}' value='{_esc(v)}' data-env-choice-hidden />"
                    f"<div class='flex flex-wrap gap-2' data-env-choice-group>{buttons}</div>"
                    f"<input class='input input-bordered input-sm h-9 min-h-9 w-full font-mono text-xs' value='{_esc(v)}' "
                    f"data-env-choice-custom autocomplete='off' />"
                    f"</div>"
                )
            else:
                ctrl = (
                    f"<input class='input input-bordered input-sm h-9 min-h-9 w-full font-mono text-xs' name='{_esc(key)}' "
                    f'value="{_esc(v)}" autocomplete="off" />'
                )
            flds.append(
                f"<label class=\"form-control w-full border-b border-base-content/5 pb-4 last:border-0 last:pb-0\">"
                f"<div class=\"label\"><span class=\"label-text font-medium\">{_esc(label)}</span>"
                f"<code class=\"label-text-alt text-[10px] opacity-50\">{_esc(key)}</code></div>"
                f"<p class=\"text-xs leading-snug text-base-content/70 mb-2 max-w-3xl\">{_esc(help_text)}</p>"
                f"{ctrl}</label>"
            )
        tab_panels.append(
            f"<div data-env-panel=\"{_esc(sec_id)}\" class=\"env-tab-panel flex flex-col gap-4{hidden}\">{''.join(flds)}</div>"
        )
    saved_note = ""
    if request.query_params.get("env_saved") == "1":
        saved_note = (
            "<div class='alert alert-success shadow-sm'><span>Значения записаны в файл <code class=\"bg-base-300 px-1 rounded\">.env</code>. "
            "Часть параметров подхватится без перезапуска; для секретов и подключений перезапустите контейнеры API и бота.</span></div>"
        )
    backup_note = ""
    if request.query_params.get("backup_run") == "1":
        backup_note = "<div class='alert alert-success shadow-sm'><span>Пробный бэкап запущен. Результат отправлен в тему BACKUPS.</span></div>"
    backup_err = (request.query_params.get("backup_err") or "").strip()
    if backup_err:
        backup_note = (
            "<div class='alert alert-error shadow-sm'><span>"
            + _esc(backup_err)
            + "</span></div>"
        )
    env_tabs_script = """
    <script>
    (function(){
      function show(id){
        document.querySelectorAll('[data-env-panel]').forEach(function(p){
          p.classList.toggle('hidden', p.getAttribute('data-env-panel')!==id);
        });
        var bgBlock=document.getElementById('env-admin-bg-block');
        if(bgBlock){
          bgBlock.classList.toggle('hidden', id!=='panel');
        }
        document.querySelectorAll('[data-env-tab]').forEach(function(b){
          var on=b.getAttribute('data-env-tab')===id;
          b.classList.toggle('btn-outline',on);
          b.classList.toggle('btn-primary',on);
          b.classList.toggle('btn-ghost',!on);
          b.classList.toggle('border',!on);
          b.classList.toggle('border-primary/40',!on);
          b.classList.toggle('hover:border-primary/70',!on);
        });
      }
      document.querySelectorAll('[data-env-tab]').forEach(function(b){
        b.addEventListener('click',function(){show(b.getAttribute('data-env-tab'));});
      });
      document.querySelectorAll('[data-env-bool-toggle]').forEach(function(toggle){
        toggle.addEventListener('change', function(){
          var hidden=toggle.parentElement&&toggle.parentElement.querySelector('[data-env-bool-hidden]');
          if(hidden) hidden.value=toggle.checked ? 'true' : 'false';
        });
      });
      document.querySelectorAll('[data-env-choice-group]').forEach(function(group){
        var wrap=group.parentElement;
        var hidden=wrap&&wrap.querySelector('[data-env-choice-hidden]');
        var custom=wrap&&wrap.querySelector('[data-env-choice-custom]');
        function syncButtons(val){
          group.querySelectorAll('[data-env-choice-value]').forEach(function(btn){
            var on=btn.getAttribute('data-env-choice-value')===val;
            btn.classList.toggle('btn-primary', on);
            btn.classList.toggle('btn-ghost', !on);
          });
        }
        group.querySelectorAll('[data-env-choice-value]').forEach(function(btn){
          btn.addEventListener('click', function(){
            var val=btn.getAttribute('data-env-choice-value')||'';
            if(hidden) hidden.value=val;
            if(custom) custom.value=val;
            syncButtons(val);
          });
        });
        if(custom){
          custom.addEventListener('input', function(){
            if(hidden) hidden.value=custom.value;
            syncButtons(custom.value);
          });
        }
      });
      document.querySelectorAll('[data-env-multi-group]').forEach(function(group){
        var wrap=group.parentElement;
        var hidden=wrap&&wrap.querySelector('[data-env-multi-hidden]');
        function syncHidden(){
          if(!hidden) return;
          var vals=[];
          group.querySelectorAll('[data-env-multi-option]').forEach(function(btn){
            if(btn.classList.contains('btn-primary')) vals.push(btn.getAttribute('data-env-multi-option')||'');
          });
          hidden.value=vals.filter(Boolean).join(',');
        }
        group.querySelectorAll('[data-env-multi-option]').forEach(function(btn){
          btn.addEventListener('click', function(){
            var on=btn.classList.contains('btn-primary');
            btn.classList.toggle('btn-primary', !on);
            btn.classList.toggle('btn-ghost', on);
            syncHidden();
          });
        });
      });
      var src=document.getElementById('bg-source');
      var url=document.getElementById('bg-url');
      var pick=document.getElementById('bg-asset-picker');
      var hidden=document.getElementById('bg-asset-hidden');
      var img=document.getElementById('bg-preview-img');
      var empty=document.getElementById('bg-preview-empty');
      var btn=document.getElementById('bg-preview-btn');
      function showPreview(v){
        v=(v||'').trim();
        if(v && img && empty){
          img.src=v; img.classList.remove('hidden'); empty.classList.add('hidden');
        }else if(img && empty){
          img.classList.add('hidden'); empty.classList.remove('hidden'); empty.textContent='Сейчас используется фиолетовый фон по умолчанию.';
        }
      }
      function syncMode(){
        var m=(src&&src.value)||'default';
        if(pick)pick.classList.toggle('hidden', m!=='asset');
        if(url && url.closest('label')) url.closest('label').classList.toggle('opacity-60', m!=='url');
      }
      if(btn)btn.addEventListener('click', function(){ if(src&&src.value==='url')showPreview(url&&url.value||''); });
      document.querySelectorAll('input[name="ADMIN_BACKGROUND_ASSET_PICK"]').forEach(function(r){
        r.addEventListener('change', function(){
          if(hidden)hidden.value=r.value||'';
          if(src)src.value='asset';
          syncMode();
          showPreview('/assets/'+encodeURIComponent(r.value||''));
        });
      });
      if(src)src.addEventListener('change', function(){
        syncMode();
        if(src.value==='default')showPreview('');
        if(src.value==='asset' && hidden && hidden.value)showPreview('/assets/'+encodeURIComponent(hidden.value));
      });
      syncMode();
      var first=document.querySelector('[data-env-tab].btn-primary')||document.querySelector('[data-env-tab]');
      if(first){
        show(first.getAttribute('data-env-tab'));
      }
    })();
    </script>"""
    body = f"""
    <div class="tabs-env card bg-base-100 border-0 shadow-lg">
      <div class="card-body gap-4">
        <style>
          .tabs-env {{
            border-color: color-mix(in oklab, var(--bc) 10%, transparent);
            box-shadow: 0 18px 42px -26px rgba(15, 23, 42, 0.55);
          }}
          .tabs-env .card-body {{
            gap: 1rem;
          }}
          .tabs-env [role="tablist"] {{
            border-color: color-mix(in oklab, var(--bc) 8%, transparent);
          }}
          .tabs-env [data-env-tab].btn-ghost {{
            color: color-mix(in oklab, var(--bc) 72%, transparent);
            background: color-mix(in oklab, var(--b2) 65%, transparent);
            border: 1px solid color-mix(in oklab, var(--bc) 9%, transparent);
          }}
          .tabs-env [data-env-tab].btn-primary {{
            background: color-mix(in oklab, var(--p) 78%, var(--b1) 22%);
            border-color: color-mix(in oklab, var(--p) 46%, transparent);
            box-shadow: 0 8px 20px -18px color-mix(in oklab, var(--p) 40%, transparent);
          }}
          .tabs-env .env-soft-card {{
            border-color: color-mix(in oklab, var(--bc) 8%, transparent);
            background-color: hsl(var(--b2) / 1);
            background: color-mix(in oklab, var(--b2) 88%, transparent);
          }}
          .remna-env-toggle {{
            position: relative;
            display: inline-flex;
            align-items: center;
            width: 68px;
            height: 36px;
            cursor: pointer;
          }}
          .remna-env-toggle input[type="checkbox"] {{
            position: absolute;
            inset: 0;
            opacity: 0;
            cursor: pointer;
            z-index: 2;
          }}
          .remna-env-toggle-track {{
            position: relative;
            display: block;
            width: 68px;
            height: 24px;
            border-radius: 999px;
            background: rgba(220, 38, 38, .82);
            transition: background-color .22s ease;
            box-shadow: inset 0 0 0 1px rgba(255,255,255,.08);
          }}
          .remna-env-toggle-track::after {{
            content: "";
            position: absolute;
            top: 50%;
            left: -2px;
            width: 28px;
            height: 28px;
            border-radius: 999px;
            background: #fff;
            transform: translate(0, -50%);
            transition: transform .22s ease;
            box-shadow: 0 4px 14px rgba(0,0,0,.25);
          }}
          .remna-env-toggle input[type="checkbox"]:checked + .remna-env-toggle-track {{
            background: rgba(22, 163, 74, .88);
          }}
          .remna-env-toggle input[type="checkbox"]:checked + .remna-env-toggle-track::after {{
            transform: translate(42px, -50%);
          }}
        </style>
        <h2 class="card-title text-2xl"><i class="fa-solid fa-sliders text-primary mr-2" aria-hidden="true"></i>Настройки .env</h2>
        {saved_note}
        {backup_note}
        <div role="tablist" class="flex flex-wrap gap-2 border-b border-base-content/10 pb-3">
          {''.join(tab_buttons)}
        </div>
        <form method="post" action="/admin/settings/env" class="flex flex-col gap-4">
          <div id="env-admin-bg-block" class="env-soft-card rounded-2xl border p-4">
            <div class="flex flex-col gap-4">
              <div class="flex items-center gap-2">
                <i class="fa-solid fa-image text-primary" aria-hidden="true"></i>
                <h3 class="text-lg font-semibold">Фон админки</h3>
              </div>
              <div class="grid gap-3 md:grid-cols-3">
                <label class="form-control">
                  <span class="label-text text-xs opacity-70">Режим</span>
                  <select id="bg-source" class="select select-bordered select-sm h-9 min-h-9" name="ADMIN_BACKGROUND_SOURCE">
                    <option value="default" {'selected' if bg_source == 'default' else ''}>Фиолетовый по умолчанию</option>
                    <option value="url" {'selected' if bg_source == 'url' else ''}>Картинка по ссылке</option>
                    <option value="asset" {'selected' if bg_source == 'asset' else ''}>Файл из /assets</option>
                  </select>
                </label>
                <label class="form-control md:col-span-2">
                  <span class="label-text text-xs opacity-70">Ссылка на изображение</span>
                  <div class="flex gap-2">
                    <input id="bg-url" class="input input-bordered input-sm h-9 min-h-9 w-full font-mono text-xs" name="ADMIN_BACKGROUND_URL" value="{_esc(bg_url)}" autocomplete="off" placeholder="https://..." />
                    <button id="bg-preview-btn" type="button" class="btn btn-ghost btn-sm h-9 min-h-9">Показать</button>
                  </div>
                </label>
              </div>
              <input type="hidden" id="bg-asset-hidden" name="ADMIN_BACKGROUND_ASSET" value="{_esc(bg_asset)}" />
              <div id="bg-asset-picker" class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{bg_asset_cards}</div>
              <div id="bg-preview-wrap" class="rounded-xl border border-base-content/10 bg-base-100/50 p-3">
                <div class="text-xs opacity-70 mb-2">Предпросмотр</div>
                <img id="bg-preview-img" src="{_esc(_admin_background_image_url(get_settings()) or '')}" alt="" class="{'h-40 w-full rounded-lg object-cover border border-base-content/10' if _admin_background_image_url(get_settings()) else 'hidden'}" />
                <div id="bg-preview-empty" class="{'hidden' if _admin_background_image_url(get_settings()) else 'text-sm opacity-60'}">Сейчас используется фиолетовый фон по умолчанию.</div>
              </div>
            </div>
          </div>
          {''.join(tab_panels)}
          <button class="btn btn-primary btn-sm h-9 min-h-9 w-fit gap-1.5" type="submit"><i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Сохранить в .env</button>
        </form>
      </div>
    </div>
    <details role="tabpanel" class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <summary class="card-body cursor-pointer select-none">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-rotate text-warning mr-2" aria-hidden="true"></i>Конвертация legacy в PAYG</h2>
        <p class="text-sm opacity-70">Секция скрыта. Нажмите, чтобы раскрыть.</p>
      </summary>
      <div class="card-body gap-4 pt-0">
        <p class="text-sm opacity-70 max-w-4xl">Массово переводит пользователей со старыми подписками на PAYG: начисляет кредит в баланс по калькулятору перехода и обновляет срок/ограничения подписки под текущую PAYG-модель.</p>
        <div class="alert alert-warning shadow-sm">
          <i class="fa-solid fa-triangle-exclamation mr-2" aria-hidden="true"></i>
          <span>Операция массовая. Перед запуском проверьте настройки BILLING_TRANSITION_* и BILLING_PAYG_SUBSCRIPTION_DAYS.</span>
        </div>
        <form method="post" action="/admin/payg/mass-convert" data-remna-confirm-msg="Запустить массовую конвертацию legacy подписок в PAYG?" class="flex flex-wrap items-end gap-2">
          <label class="form-control">
            <span class="label-text text-xs opacity-70">Подтверждение: введите PAYG</span>
            <input
              type="text"
              name="confirm_word"
              required
              autocomplete="off"
              class="input input-bordered input-sm h-9 min-h-9 w-44 font-mono uppercase"
              placeholder="PAYG"
            />
          </label>
          <button type="submit" class="btn btn-warning btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-rotate" aria-hidden="true"></i>Запустить конвертацию
          </button>
        </form>
      </div>
    </details>
    <details role="tabpanel" class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <summary class="card-body cursor-pointer select-none">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-database text-secondary mr-2" aria-hidden="true"></i>Бэкап</h2>
        <p class="text-sm opacity-70">Секция скрыта. Нажмите, чтобы раскрыть.</p>
      </summary>
      <div class="card-body gap-4 pt-0">
        <form method="post" action="/admin/settings/backup/run" data-remna-confirm-msg="Запустить пробный бэкап PostgreSQL сейчас?" class="flex flex-wrap items-end gap-2">
          <button class="btn btn-secondary btn-sm h-9 min-h-9 gap-1.5" type="submit">
            <i class="fa-solid fa-database" aria-hidden="true"></i>Пробный бэкап сейчас
          </button>
        </form>
      </div>
    </details>
    <details role="tabpanel" class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <summary class="card-body cursor-pointer select-none">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-triangle-exclamation text-error mr-2" aria-hidden="true"></i>Опасная зона</h2>
        <p class="text-sm opacity-70">Секция скрыта. Нажмите, чтобы раскрыть.</p>
      </summary>
      <div class="card-body gap-4 pt-0">
        <div class="alert alert-warning shadow-sm"><i class="fa-solid fa-triangle-exclamation mr-2" aria-hidden="true"></i><span>Полный сброс удалит пользователей, подписки, транзакции, промокоды и прочие данные.</span></div>
        <form method="post" action="/admin/settings/factory-reset" class="flex flex-wrap items-end gap-2">
          <input class="input input-bordered input-sm h-9 min-h-9 w-full max-w-md" name="confirm_text" placeholder="Введите WIPE ALL" autocomplete="off" />
          <button class="btn btn-error btn-sm h-9 min-h-9 gap-1.5" type="submit"><i class="fa-solid fa-bomb" aria-hidden="true"></i>Сделать factory reset</button>
        </form>
        <p class="text-sm opacity-60">То же, что сброс из Telegram-админки, с подтверждением в браузере.</p>
      </div>
    </details>
    {env_tabs_script}
    """
    return _layout("Web-admin Settings", body, request=request)


@router.post("/settings/env", response_model=None)
async def admin_settings_env_post(request: Request):
    denied = _require_login(request)
    if denied is not None:
        return denied
    form = await request.form()
    allowed = {entry[0] for entry in WEB_ADMIN_ENV_WHITELIST}
    updates = {k: str(form.get(k) or "") for k in allowed if k in form}
    try:
        patch_dotenv(updates)
    except OSError as e:
        return _layout(
            "Ошибка .env",
            f"<div class='alert alert-error'>Не удалось записать .env: {_esc(e)}</div>",
            request=request,
        )
    return RedirectResponse("/admin/settings?env_saved=1", status_code=303)


@router.post("/settings/backup/run")
async def admin_settings_backup_run(request: Request) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    ok, msg = await run_backup_once(settings, notify=True)
    if ok:
        return RedirectResponse("/admin/settings?backup_run=1", status_code=303)
    return RedirectResponse("/admin/settings?backup_err=" + quote_plus(msg[:400]), status_code=303)


@router.post("/settings/factory-reset")
async def admin_factory_reset(request: Request, confirm_text: str = Form("")) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    if confirm_text.strip() != "WIPE ALL":
        return _layout(
            "Reset rejected",
            "<div class='alert alert-info shadow-lg'><h2 class='font-bold'>Сброс отменен</h2><p>Неверная фраза подтверждения.</p></div>",
            request=request,
        )
    async with await _session() as session:
        await wipe_all_application_data(session)
        await session.commit()
    return _layout(
        "Reset done",
        "<div class='alert alert-success shadow-lg'><h2 class='font-bold'>База очищена</h2><p>Factory reset выполнен успешно.</p></div>",
        request=request,
    )


@router.get("/api/users-search")
async def admin_api_users_search(request: Request, q: str = "", limit: int = 20) -> JSONResponse:
    """Поиск пользователей для виджета chips: по id, telegram_id, @username, имени.

    Возвращает: {"users": [{"id", "telegram_id", "username", "label", "label_html"}, ...]}.
    """
    denied = _require_login(request)
    if denied is not None:
        return JSONResponse({"users": [], "denied": True}, status_code=401)
    try:
        lim = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        lim = 20
    needle = (q or "").strip()
    async with await _session() as session:
        if not needle:
            stmt = select(User).order_by(desc(User.id)).limit(lim)
        else:
            cleaned = needle.lstrip("@").lstrip("#").strip()
            digits_only = cleaned.isdigit()
            ilike = f"%{cleaned}%"
            conds = [
                User.username.ilike(ilike),
                User.first_name.ilike(ilike),
                User.last_name.ilike(ilike),
            ]
            if digits_only:
                try:
                    n = int(cleaned)
                    conds.extend([User.id == n, User.telegram_id == n])
                except ValueError:
                    pass
            from sqlalchemy import or_ as _or  # локальный импорт чтобы не править шапку

            stmt = (
                select(User)
                .where(_or(*conds))
                .order_by(desc(User.id))
                .limit(lim)
            )
        rows = list((await session.execute(stmt)).scalars().all())
    out: list[dict] = []
    for u in rows:
        uid = int(u.id)
        tg = int(u.telegram_id or 0)
        un = (u.username or "").strip()
        fn = (u.first_name or "").strip()
        ln = (u.last_name or "").strip()
        name = (fn + (" " + ln if ln else "")).strip()
        label_main = f"@{un}" if un else (name or f"tg:{tg}")
        label = f"{label_main} · #{uid}"
        # HTML-версия — для подсветки иконки и второстепенной строки
        sub_parts = []
        if name and un:
            sub_parts.append(_esc(name))
        sub_parts.append(f"tg:{tg}")
        sub_parts.append(f"#{uid}")
        label_html = (
            "<div class='flex flex-col'>"
            f"<span class='font-medium'>{_esc(label_main)}</span>"
            f"<span class='text-[11px] opacity-60'>{' · '.join(sub_parts)}</span>"
            "</div>"
        )
        out.append(
            {
                "id": uid,
                "telegram_id": tg,
                "username": un or None,
                "first_name": fn or None,
                "last_name": ln or None,
                "label": label,
                "label_html": label_html,
            }
        )
    return JSONResponse({"users": out})


@router.get("/promos")
async def admin_promos(request: Request, q: str = "") -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    now = datetime.now(UTC)
    needle = q.strip().upper()
    async with await _session() as session:
        stmt = select(PromoCode).order_by(desc(PromoCode.id)).limit(300)
        if needle:
            stmt = stmt.where(PromoCode.code.ilike(f"%{needle}%"))
        promos = list((await session.execute(stmt)).scalars().all())
    rows = []
    for p in promos:
        is_expired = p.expires_at is not None and p.expires_at < now
        status = "истек" if is_expired else ("активен" if p.is_active else "неактивен")
        tw = "text-error font-medium" if is_expired else ("text-success font-medium" if p.is_active else "text-warning font-medium")
        rows.append(
            f"<tr class='remna-row-link cursor-pointer' data-row-href='/admin/promos/{p.id}' tabindex='0' role='link' aria-label='Открыть промокод'>"
            f"<td><span class='link link-primary font-mono font-semibold'>{_esc(p.code)}</span></td>"
            f"<td><span class='badge badge-ghost badge-sm'>{_esc(_promo_type_ru(p.type))}</span></td><td>{_esc(_promo_reward_caption(p))}</td>"
            f"<td>{p.used_count}/{_esc(p.max_uses if p.max_uses is not None else '∞')}</td>"
            f"<td>{_esc(_fmt_expires(p.expires_at))}</td><td class='{tw}'>{status}</td></tr>"
        )
    create_modal = _modal_shell(
        modal_id="promo-create-modal",
        title="Создание промокода",
        inner=_promo_form(action="/admin/promos/new"),
    )
    body = (
        "<div class='card bg-base-100 border border-base-content/10 shadow-lg'><div class='card-body gap-4'>"
        "<div class='flex flex-wrap items-center justify-between gap-2'><h2 class='card-title text-2xl mb-0'><i class='fa-solid fa-ticket text-primary mr-2' aria-hidden='true'></i>Промокоды</h2>"
        "<button type='button' class='btn btn-primary btn-sm h-9 min-h-9 gap-1.5' data-remna-modal-open='promo-create-modal'><i class='fa-solid fa-plus' aria-hidden='true'></i>Создать промокод</button></div>"
        "<form method='get' class='flex flex-wrap items-end gap-2'>"
        f"<input class='input input-bordered input-sm h-9 min-h-9 w-full max-w-md font-mono text-sm uppercase' name='q' value='{_esc(needle)}' placeholder='Поиск по коду'/>"
        "<button class='btn btn-primary btn-sm h-9 min-h-9 gap-1.5' type='submit'><i class='fa-solid fa-magnifying-glass' aria-hidden='true'></i>Искать</button></form>"
        "<div class='overflow-x-auto rounded-xl border border-base-content/10'><table class='table table-zebra table-sm'><thead><tr><th>Код</th><th>Тип</th><th>Награда</th><th>Активации</th><th>Срок</th><th>Статус</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"6\" class=\"opacity-50\">Нет промокодов</td></tr>'}</tbody></table></div></div></div>"
        f"{create_modal}"
        "<script>(function(){document.querySelectorAll('[data-remna-modal-open]').forEach(function(b){b.addEventListener('click',function(){var id=b.getAttribute('data-remna-modal-open');var m=document.getElementById(id);if(m)m.classList.remove('hidden');if(m)m.classList.add('flex');});});document.querySelectorAll('[data-remna-modal-close]').forEach(function(b){b.addEventListener('click',function(){var id=b.getAttribute('data-remna-modal-close');var m=document.getElementById(id);if(m)m.classList.add('hidden');if(m)m.classList.remove('flex');});});document.querySelectorAll('[role=\"dialog\"]').forEach(function(m){m.addEventListener('click',function(e){if(e.target===m){m.classList.add('hidden');m.classList.remove('flex');}});});})();</script>"
    )
    return _layout("Web-admin Promos", body, request=request)


def _user_chip_label(user_id: int, telegram_id: int, username: str | None, first_name: str | None, last_name: str | None) -> str:
    """Короткая подпись для чипа выбранного пользователя."""
    name = (first_name or "").strip() or (last_name or "").strip() or ""
    if username:
        head = f"@{username}"
    elif name:
        head = name
    else:
        head = f"tg:{telegram_id}"
    return f"{head} · #{user_id}"


def _promo_form(
    *,
    action: str,
    promo: PromoCode | None = None,
    error: str | None = None,
    selected_users: list[dict] | None = None,
) -> str:
    p = promo
    e = f"<div class='alert alert-error text-sm'>{_esc(error)}</div>" if error else ""
    ro = "readonly" if p else ""

    selected_users = selected_users or []
    chips_html_parts: list[str] = []
    selected_ids: list[str] = []
    for u in selected_users:
        uid = int(u.get("id") or 0)
        if uid <= 0:
            continue
        selected_ids.append(str(uid))
        label = _user_chip_label(
            user_id=uid,
            telegram_id=int(u.get("telegram_id") or 0),
            username=u.get("username"),
            first_name=u.get("first_name"),
            last_name=u.get("last_name"),
        )
        chips_html_parts.append(
            f"<span class='badge badge-primary gap-1 py-3 pl-3 pr-1' data-allowed-user-chip data-user-id='{uid}'>"
            f"<span class='text-xs'>{_esc(label)}</span>"
            "<button type='button' class='btn btn-ghost btn-xs btn-circle' data-allowed-user-remove aria-label='Убрать'>"
            "<i class='fa-solid fa-xmark text-[10px]' aria-hidden='true'></i></button>"
            "</span>"
        )
    chips_initial_html = "".join(chips_html_parts)
    selected_ids_csv = ",".join(selected_ids)

    type_options = "".join(
        f"<option value='{_esc(k)}' {'selected' if p and p.type == k else ''}>{_esc(v)}</option>"
        for k, v in _PROMO_TYPE_RU.items()
        if k in _PROMO_TYPES_SELECTABLE
    )
    # Устаревшие типы — только чтобы select остался валидным при редактировании
    if p is not None and p.type not in _PROMO_TYPES_SELECTABLE:
        legacy_label = _promo_type_ru(p.type)
        type_options = (
            f"<option value='{_esc(p.type)}' selected>{_esc(legacy_label)}</option>"
            + type_options
        )

    req_no_active = bool(getattr(p, "require_no_active_subscription", False)) if p else False
    req_months = getattr(p, "require_no_paid_subscription_months", None) if p else None
    req_months_val = str(int(req_months)) if req_months is not None and int(req_months) > 0 else ""

    return f"""
    <div class="flex w-full flex-col items-center justify-center py-6 min-h-[min(70vh,calc(100vh-10rem))]">
    <div class="card bg-base-100 border border-base-content/10 shadow-lg w-full max-w-2xl">
      <div class="card-body gap-4">
        <h2 class="card-title text-xl"><i class="fa-solid fa-pen-to-square text-primary mr-2" aria-hidden="true"></i>{'Редактирование промокода' if p else 'Создание промокода'}</h2>
        {e}
        <form method="post" action="{_esc(action)}" class="flex flex-col gap-4">
          <label class="form-control w-full"><span class="label-text font-medium">Код</span>
            <div class="join w-full">
              <input id="promo-code-input" class="input input-bordered input-sm h-9 min-h-9 font-mono text-sm uppercase join-item w-full" name="code" value="{_esc(p.code if p else '')}" {ro} />
              <button type="button" id="promo-code-dice" class="btn btn-primary btn-sm h-9 min-h-9 join-item gap-1.5" {'disabled' if p else ''} title="Сгенерировать случайный код" aria-label="Сгенерировать случайный код"><i class="fa-solid fa-dice" aria-hidden="true"></i>Случайный</button>
            </div>
          </label>
          <label class="form-control w-full"><span class="label-text font-medium">Тип</span>
            <select class="select select-bordered select-sm h-9 min-h-9 text-sm" name="promo_type">
            {type_options}
          </select></label>
          <label class="form-control w-full"><span class="label-text font-medium">Награда (число)</span>
            <input class="input input-bordered input-sm h-9 min-h-9 text-sm" name="value" value="{_esc(p.value if p else '')}" /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Лимит активаций (число или '-')</span>
            <input class="input input-bordered input-sm h-9 min-h-9 text-sm" name="max_uses" value="{_esc(p.max_uses if p and p.max_uses is not None else '-')}" />
            <span class="label-text-alt text-xs opacity-70 mt-1">Если ниже выбраны пользователи — лимит игнорируется, каждый из списка может активировать 1 раз.</span>
          </label>
          <div class="form-control w-full rounded-lg border border-base-content/10 bg-base-200/30 p-4 gap-3">
            <span class="label-text font-medium">Условия активации</span>
            <span class="label-text-alt text-xs opacity-70">Повторная активация одним пользователем всегда запрещена.</span>
            <label class="label cursor-pointer justify-start gap-3 py-1">
              <input type="checkbox" name="require_no_active_subscription" value="1" class="checkbox checkbox-sm" {'checked' if req_no_active else ''} />
              <span class="label-text text-sm">Только без активной подписки (в т.ч. триал)</span>
            </label>
            <label class="form-control w-full max-w-xs">
              <span class="label-text text-sm">Не покупали подписку, месяцев</span>
              <input class="input input-bordered input-sm h-9 min-h-9 text-sm" name="require_no_paid_subscription_months" type="number" min="1" max="24" step="1" placeholder="пусто = не проверять" value="{_esc(req_months_val)}" />
              <span class="label-text-alt text-xs opacity-70">Например <code class="text-xs">2</code> для TEST3.</span>
            </label>
          </div>
          <div class="form-control w-full">
            <span class="label-text font-medium">Срок действия</span>
            <span class="label-text-alt text-xs opacity-70 mb-1">Выберите дату в календаре или отметьте «без срока». Пустая дата без галочки тоже означает без ограничения по времени.</span>
            <div class="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
              <input type="date" id="promo-expires-date" name="expires_at_date" class="input input-bordered input-sm h-9 min-h-9 text-sm w-full max-w-[12rem]" value="{_esc(_promo_expires_date_input_value(p.expires_at if p else None))}" {'disabled' if (p is not None and p.expires_at is None) else ''} />
              <label class="label cursor-pointer justify-start gap-2 py-0 w-fit">
                <input type="checkbox" name="expires_unlimited" value="1" class="checkbox checkbox-sm" {'checked' if (p is not None and p.expires_at is None) else ''} onchange="document.getElementById('promo-expires-date').disabled=this.checked;if(this.checked)document.getElementById('promo-expires-date').value=''" />
                <span class="label-text text-sm">Без срока</span>
              </label>
            </div>
          </div>
          <label class="form-control w-full"><span class="label-text font-medium">Активен</span>
            <select class="select select-bordered select-sm h-9 min-h-9 text-sm" name="is_active">
            <option value="true" {'selected' if (p is None or p.is_active) else ''}>да</option>
            <option value="false" {'selected' if p is not None and not p.is_active else ''}>нет</option>
          </select></label>

          <div class="form-control w-full">
            <span class="label-text font-medium">Доступно пользователям (опционально)</span>
            <span class="label-text-alt text-xs opacity-70 mb-1">Если пусто — промокод доступен всем. Начните вводить @username, имя, telegram ID или ID в боте.</span>
            <div id="allowed-users-chips" class="flex flex-wrap gap-1.5 mb-2 min-h-[28px]">{chips_initial_html}</div>
            <div class="relative">
              <input id="allowed-users-search" type="text" autocomplete="off" class="input input-bordered input-sm h-9 min-h-9 text-sm w-full" placeholder="Поиск пользователя…" />
              <div id="allowed-users-dropdown" class="absolute left-0 right-0 top-full mt-1 z-[120] hidden max-h-72 overflow-auto rounded-lg border border-base-content/10 bg-base-100 shadow-xl"></div>
            </div>
            <input type="hidden" name="allowed_user_ids" id="allowed-user-ids" value="{_esc(selected_ids_csv)}" />
          </div>

          <button class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5 w-fit" type="submit"><i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Сохранить</button>
        </form>
      </div>
    </div>
    </div>
    <script>(function(){{
      // --- Генератор случайного кода (8 символов, A-Z и 0-9, без 0/O/1/I чтобы не путать) ---
      var ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
      var dice = document.getElementById('promo-code-dice');
      var codeInput = document.getElementById('promo-code-input');
      if (dice && codeInput && !dice.disabled) {{
        dice.addEventListener('click', function(){{
          var s = '';
          var arr = (window.crypto && crypto.getRandomValues) ? new Uint32Array(8) : null;
          if (arr) crypto.getRandomValues(arr);
          for (var i = 0; i < 8; i++) {{
            var idx = arr ? (arr[i] % ALPHABET.length) : Math.floor(Math.random() * ALPHABET.length);
            s += ALPHABET.charAt(idx);
          }}
          codeInput.value = s;
        }});
      }}

      // --- Виджет выбора пользователей (chips + autocomplete) ---
      var chipsBox = document.getElementById('allowed-users-chips');
      var searchInp = document.getElementById('allowed-users-search');
      var dropdown = document.getElementById('allowed-users-dropdown');
      var hiddenIds = document.getElementById('allowed-user-ids');
      if (chipsBox && searchInp && dropdown && hiddenIds) {{
        var selected = new Map();
        chipsBox.querySelectorAll('[data-allowed-user-chip]').forEach(function(el){{
          var id = el.getAttribute('data-user-id');
          if (id) selected.set(String(id), el);
        }});

        function syncHidden() {{
          hiddenIds.value = Array.from(selected.keys()).join(',');
        }}
        function bindRemoveButtons() {{
          chipsBox.querySelectorAll('[data-allowed-user-remove]').forEach(function(btn){{
            if (btn.dataset.bound) return;
            btn.dataset.bound = '1';
            btn.addEventListener('click', function(){{
              var chip = btn.closest('[data-allowed-user-chip]');
              if (!chip) return;
              var id = chip.getAttribute('data-user-id');
              chip.remove();
              if (id) selected.delete(String(id));
              syncHidden();
            }});
          }});
        }}
        bindRemoveButtons();

        function addUserChip(u) {{
          var id = String(u.id);
          if (selected.has(id)) return;
          var label = u.label || ('#' + id);
          var span = document.createElement('span');
          span.className = 'badge badge-primary gap-1 py-3 pl-3 pr-1';
          span.setAttribute('data-allowed-user-chip', '');
          span.setAttribute('data-user-id', id);
          span.innerHTML = "<span class='text-xs'></span><button type='button' class='btn btn-ghost btn-xs btn-circle' data-allowed-user-remove aria-label='Убрать'><i class='fa-solid fa-xmark text-[10px]' aria-hidden='true'></i></button>";
          span.firstElementChild.textContent = label;
          chipsBox.appendChild(span);
          selected.set(id, span);
          syncHidden();
          bindRemoveButtons();
        }}

        function closeDropdown() {{
          dropdown.classList.add('hidden');
          dropdown.innerHTML = '';
        }}
        function renderResults(items) {{
          if (!items || !items.length) {{
            dropdown.innerHTML = "<div class='px-3 py-2 text-xs opacity-60'>Ничего не найдено</div>";
            dropdown.classList.remove('hidden');
            return;
          }}
          var html = '';
          for (var i = 0; i < items.length; i++) {{
            var u = items[i];
            var disabled = selected.has(String(u.id));
            html += "<button type='button' class='block w-full text-left px-3 py-2 hover:bg-base-200 text-sm" + (disabled ? " opacity-40 cursor-not-allowed" : "") + "' " +
                    "data-pick-id='" + u.id + "' data-pick-label='" + (u.label || '').replace(/'/g, "&#39;") + "' " +
                    (disabled ? "disabled" : "") + ">" + (u.label_html || u.label || '#' + u.id) + "</button>";
          }}
          dropdown.innerHTML = html;
          dropdown.classList.remove('hidden');
          dropdown.querySelectorAll('button[data-pick-id]').forEach(function(b){{
            b.addEventListener('click', function(){{
              var id = b.getAttribute('data-pick-id');
              var lbl = b.getAttribute('data-pick-label');
              if (id) addUserChip({{id: id, label: lbl}});
              searchInp.value = '';
              closeDropdown();
              searchInp.focus();
            }});
          }});
        }}

        var searchTimer = null;
        var activeReq = 0;
        function runSearch(q) {{
          var myReq = ++activeReq;
          var url = '/admin/api/users-search?q=' + encodeURIComponent(q || '') + '&limit=20';
          fetch(url, {{credentials: 'same-origin'}}).then(function(r){{
            if (!r.ok) throw new Error('http ' + r.status);
            return r.json();
          }}).then(function(d){{
            if (myReq !== activeReq) return;
            renderResults(d.users || []);
          }}).catch(function(){{
            if (myReq !== activeReq) return;
            dropdown.innerHTML = "<div class='px-3 py-2 text-xs text-error'>Ошибка поиска</div>";
            dropdown.classList.remove('hidden');
          }});
        }}

        searchInp.addEventListener('input', function(){{
          var q = searchInp.value.trim();
          if (searchTimer) clearTimeout(searchTimer);
          if (q.length === 0) {{
            searchTimer = setTimeout(function(){{ runSearch(''); }}, 120);
            return;
          }}
          searchTimer = setTimeout(function(){{ runSearch(q); }}, 180);
        }});
        searchInp.addEventListener('focus', function(){{
          if (searchInp.value.trim().length === 0) runSearch('');
        }});
        document.addEventListener('click', function(ev){{
          if (ev.target === searchInp) return;
          if (dropdown.contains(ev.target)) return;
          closeDropdown();
        }});
        // Не отправлять форму, если фокус в поле поиска и нажат Enter — это попытка найти, а не сабмит
        searchInp.addEventListener('keydown', function(ev){{
          if (ev.key === 'Enter') {{ ev.preventDefault(); }}
        }});
      }}
    }})();</script>
    """


def _modal_shell(*, modal_id: str, title: str, inner: str) -> str:
    return f"""
    <div id="{_esc(modal_id)}" class="fixed inset-0 z-[140] hidden items-center justify-center bg-base-content/45 backdrop-blur-sm p-4" role="dialog" aria-modal="true" aria-labelledby="{_esc(modal_id)}-title">
      <div class="relative max-h-[90vh] w-full max-w-4xl overflow-auto rounded-2xl border border-base-content/10 bg-base-100 shadow-2xl">
        <button type="button" class="btn btn-sm btn-circle btn-ghost absolute right-3 top-3 z-10" data-remna-modal-close="{_esc(modal_id)}" aria-label="Закрыть">
          <i class="fa-solid fa-xmark" aria-hidden="true"></i>
        </button>
        <div class="px-5 pt-5">
          <h3 id="{_esc(modal_id)}-title" class="text-xl font-semibold">{_esc(title)}</h3>
        </div>
        <div class="px-2 pb-2">
          {inner}
        </div>
      </div>
    </div>
    """


@router.get("/promos/new")
async def admin_promos_new(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    return _layout("New Promo", _promo_form(action="/admin/promos/new"), request=request, back_href="/admin/promos")


def _parse_allowed_user_ids_csv(raw: str) -> list[int]:
    """Парсит CSV id'ов пользователей (id в БД бота) в уникальный отсортированный список."""
    if not raw:
        return []
    out: set[int] = set()
    for piece in raw.split(","):
        s = piece.strip()
        if not s:
            continue
        try:
            v = int(s)
        except ValueError:
            continue
        if v > 0:
            out.add(v)
    return sorted(out)


async def _load_promo_allowed_users(
    session: AsyncSession, promo_id: int
) -> list[dict]:
    """Загружает выбранных пользователей промокода в виде словарей для шаблона."""
    rows = (
        await session.execute(
            select(User)
            .join(PromoCodeAllowedUser, PromoCodeAllowedUser.user_id == User.id)
            .where(PromoCodeAllowedUser.promo_id == promo_id)
            .order_by(User.id.asc())
        )
    ).scalars().all()
    return [
        {
            "id": int(u.id),
            "telegram_id": int(u.telegram_id or 0),
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
        }
        for u in rows
    ]


async def _sync_promo_allowed_users(
    session: AsyncSession, *, promo_id: int, user_ids: list[int]
) -> tuple[list[int], list[int]]:
    """Приводит allow-list промокода к указанному списку, возвращает (added, removed)."""
    valid_ids: list[int] = []
    if user_ids:
        rows = (
            await session.execute(
                select(User.id).where(User.id.in_(user_ids))
            )
        ).scalars().all()
        valid_ids = sorted({int(x) for x in rows})

    current = set(
        (
            await session.execute(
                select(PromoCodeAllowedUser.user_id).where(
                    PromoCodeAllowedUser.promo_id == promo_id
                )
            )
        ).scalars().all()
    )
    desired = set(valid_ids)
    to_add = sorted(desired - current)
    to_remove = sorted(current - desired)

    for uid in to_add:
        session.add(PromoCodeAllowedUser(promo_id=promo_id, user_id=uid))
    if to_remove:
        await session.execute(
            PromoCodeAllowedUser.__table__.delete().where(
                PromoCodeAllowedUser.promo_id == promo_id,
                PromoCodeAllowedUser.user_id.in_(to_remove),
            )
        )
    return to_add, to_remove


@router.post("/promos/new")
async def admin_promos_new_post(
    request: Request,
    code: str = Form(""),
    promo_type: str = Form(""),
    value: str = Form(""),
    fallback_value_rub: str = Form(""),
    max_uses: str = Form("-"),
    expires_at_date: str = Form(""),
    expires_unlimited: str = Form(""),
    is_active: str = Form("true"),
    allowed_user_ids: str = Form(""),
    require_no_active_subscription: str = Form(""),
    require_no_paid_subscription_months: str = Form(""),
):
    denied = _require_login(request)
    if denied is not None:
        return denied
    try:
        c = code.strip().upper()
        if not c:
            raise ValueError("Код обязателен")
        if promo_type not in _PROMO_TYPES_SELECTABLE:
            raise ValueError("Неверный тип")
        val = Decimal(value.strip().replace(",", "."))
        if val <= 0:
            raise ValueError("Награда должна быть > 0")
        if promo_type == "discount_percent" and (val <= 0 or val >= 100):
            raise ValueError("discount_percent должен быть в диапазоне (0,100)")
        if promo_type == "extra_days" and val != val.to_integral_value():
            raise ValueError("Для дней подписки нужно целое число")
        if promo_type == "extra_days" and int(val) > 3650:
            raise ValueError("Слишком много дней")
        req_no_active, req_months = _parse_promo_eligibility_form(
            require_no_active_subscription, require_no_paid_subscription_months
        )
        mu: int | None = None
        if max_uses.strip() != "-":
            if not max_uses.strip().isdigit():
                raise ValueError("Лимит должен быть целым числом")
            mu = int(max_uses.strip())
            if mu <= 0:
                raise ValueError("Лимит должен быть > 0")
        exp = _promo_expires_from_form(expires_unlimited, expires_at_date)
        active = is_active == "true"
        allow_ids = _parse_allowed_user_ids_csv(allowed_user_ids)
    except (ValueError, InvalidOperation) as e:
        return _layout(
            "New Promo Error",
            _promo_form(action="/admin/promos/new", error=str(e)),
            request=request,
            back_href="/admin/promos",
        )
    auth = request.session.get("wauth") or {}
    raw_tg_id = auth.get("id") or auth.get("telegram_id")
    admin_db_id: int | None = None
    try:
        tg_id = int(raw_tg_id) if raw_tg_id is not None else 0
    except (TypeError, ValueError):
        tg_id = 0

    async with await _session() as session:
        if tg_id > 0:
            admin_user = (
                await session.execute(select(User).where(User.telegram_id == tg_id).limit(1))
            ).scalar_one_or_none()
            if admin_user is not None:
                admin_db_id = int(admin_user.id)
        actor_user = await _web_admin_actor_user(session, request)
        actor_label = _web_admin_actor_label(request)
        promo = PromoCode(
            code=c,
            type=promo_type,
            value=val,
            fallback_value_rub=None,
            max_uses=mu,
            expires_at=exp,
            is_active=active,
            require_no_active_subscription=req_no_active,
            require_no_paid_subscription_months=req_months,
            created_by_user_id=admin_db_id,
        )
        session.add(promo)
        await session.flush()
        added, _removed = await _sync_promo_allowed_users(
            session, promo_id=int(promo.id), user_ids=allow_ids
        )
        await session.commit()
        await notify_admin(
            get_settings(),
            title="🎁 Промокод создан (web-admin)",
            lines=[
                f"Код: {md_esc(promo.code)}",
                f"Тип: {md_esc(_promo_type_ru(promo.type))}",
                f"Награда: {md_esc(_promo_reward_caption(promo))}",
                f"Срок (до): {md_esc(_fmt_expires(promo.expires_at))}",
                f"Лимит: {md_esc('∞' if promo.max_uses is None else str(promo.max_uses))}",
                f"Активен: {md_esc('да' if promo.is_active else 'нет')}",
                f"Привязан к: {md_esc(str(len(added)) if added else 'все пользователи')}",
                f"Условия: {md_esc(_promo_eligibility_summary(promo))}",
                web_admin_actor_notify_line(),
            ],
            event_type="promo_create_web",
            topic=AdminLogTopic.PROMO,
            subject_user=actor_user,
            session=session,
        )
    return RedirectResponse("/admin/promos", status_code=303)


@router.get("/promos/{promo_id}")
async def admin_promos_detail(request: Request, promo_id: int) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        promo = await session.get(PromoCode, promo_id)
        if promo is None:
            return _layout(
                "Promo not found",
                "<div class='alert alert-warning shadow-lg'>Промокод не найден</div>",
                request=request,
                back_href="/admin/promos",
            )
        usages = (
            await session.execute(
                select(PromoUsage, User)
                .join(User, User.id == PromoUsage.user_id)
                .where(PromoUsage.promo_id == promo_id)
                .order_by(desc(PromoUsage.id))
                .limit(300)
            )
        ).all()
        allowed = await _load_promo_allowed_users(session, promo_id)
    usage_rows = "".join(
        f"<tr><td>{pu.id}</td><td><a href='/admin/users/{u.id}'>#{u.id}</a></td>"
        f"<td><code>{u.telegram_id}</code></td><td class='whitespace-nowrap text-xs'>{_fmt_dt_msk(pu.used_at)}</td></tr>"
        for pu, u in usages
    )

    if allowed:
        chips = "".join(
            "<a class='badge badge-primary badge-lg gap-1 hover:underline' "
            f"href='/admin/users/{int(u['id'])}'>"
            f"<span class='text-xs'>{_esc(_user_chip_label(int(u['id']), int(u['telegram_id'] or 0), u.get('username'), u.get('first_name'), u.get('last_name')))}</span>"
            "</a>"
            for u in allowed
        )
        allowed_block = (
            "<div class='card bg-base-100 border border-base-content/10 shadow-lg mt-4'><div class='card-body gap-3'>"
            f"<h3 class='text-lg font-semibold'>Доступен только пользователям ({len(allowed)})</h3>"
            f"<div class='flex flex-wrap gap-1.5'>{chips}</div>"
            "</div></div>"
        )
        scope_caption = f"только {len(allowed)} польз."
    else:
        allowed_block = ""
        scope_caption = "все пользователи"

    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <h2 class="card-title text-2xl font-mono">Промокод <span class="text-primary">{_esc(promo.code)}</span></h2>
        <p>Тип: <span class="badge badge-ghost">{_esc(_promo_type_ru(promo.type))}</span> · Награда: <b>{_esc(_promo_reward_caption(promo))}</b></p>
        <p>Срок: <b>{_esc(_fmt_expires(promo.expires_at))}</b> · Лимит: <b>{_esc(promo.max_uses if promo.max_uses is not None else '∞')}</b></p>
        <p>Активен: <b>{'да' if promo.is_active else 'нет'}</b> · Использований: <b>{promo.used_count}</b> · Доступен: <b>{_esc(scope_caption)}</b></p>
        <p>Условия активации: <b>{_esc(_promo_eligibility_summary(promo))}</b></p>
        <div class="flex flex-wrap gap-2">
          <a class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5" href="/admin/promos/{promo.id}/edit"><i class="fa-solid fa-pen" aria-hidden="true"></i>Редактировать</a>
          <form method="post" action="/admin/promos/{promo.id}/delete" data-remna-confirm-msg="Удалить промокод?">
            <button class="btn btn-error btn-outline btn-sm h-9 min-h-9 gap-1.5" type="submit"><i class="fa-solid fa-trash" aria-hidden="true"></i>Удалить</button>
          </form>
        </div>
      </div>
    </div>
    {allowed_block}
    <div class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold">История активаций ({len(usages)})</h3>
        <div class="overflow-x-auto rounded-lg border border-base-content/10"><table class="table table-zebra table-sm"><thead><tr><th>ID usage</th><th>User ID</th><th>Telegram ID</th><th>Дата</th></tr></thead>
        <tbody>{usage_rows or '<tr><td colspan="4" class="opacity-50">Нет активаций</td></tr>'}</tbody></table></div>
      </div>
    </div>
    """
    return _layout(f"Promo {promo_id}", body, request=request, back_href="/admin/promos")


@router.get("/promos/{promo_id}/edit")
async def admin_promos_edit(request: Request, promo_id: int) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        promo = await session.get(PromoCode, promo_id)
        if promo is None:
            return _layout(
                "Promo not found",
                "<div class='alert alert-warning shadow-lg'>Промокод не найден</div>",
                request=request,
                back_href="/admin/promos",
            )
        selected_users = await _load_promo_allowed_users(session, promo_id)
    return _layout(
        "Edit Promo",
        _promo_form(
            action=f"/admin/promos/{promo_id}/edit",
            promo=promo,
            selected_users=selected_users,
        ),
        request=request,
        back_href=f"/admin/promos/{promo_id}",
    )


@router.post("/promos/{promo_id}/edit")
async def admin_promos_edit_post(
    request: Request,
    promo_id: int,
    promo_type: str = Form(""),
    value: str = Form(""),
    fallback_value_rub: str = Form(""),
    max_uses: str = Form("-"),
    expires_at_date: str = Form(""),
    expires_unlimited: str = Form(""),
    is_active: str = Form("true"),
    allowed_user_ids: str = Form(""),
    require_no_active_subscription: str = Form(""),
    require_no_paid_subscription_months: str = Form(""),
):
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        promo = await session.get(PromoCode, promo_id)
        if promo is None:
            return _layout(
                "Promo not found",
                "<div class='alert alert-warning shadow-lg'>Промокод не найден</div>",
                request=request,
                back_href="/admin/promos",
            )
        try:
            allowed_types = set(_PROMO_TYPES_SELECTABLE) | {promo.type, "bonus_rub"}
            if promo_type not in allowed_types:
                raise ValueError("Неверный тип")
            val = Decimal(value.strip().replace(",", "."))
            if val <= 0:
                raise ValueError("Награда должна быть > 0")
            if promo_type == "discount_percent" and (val <= 0 or val >= 100):
                raise ValueError("discount_percent должен быть в диапазоне (0,100)")
            if promo_type == "extra_days" and val != val.to_integral_value():
                raise ValueError("Для дней подписки нужно целое число")
            if promo_type == "extra_days" and int(val) > 3650:
                raise ValueError("Слишком много дней")
            req_no_active, req_months = _parse_promo_eligibility_form(
                require_no_active_subscription, require_no_paid_subscription_months
            )
            mu: int | None = None
            if max_uses.strip() != "-":
                if not max_uses.strip().isdigit():
                    raise ValueError("Лимит должен быть целым числом")
                mu = int(max_uses.strip())
                if mu <= 0:
                    raise ValueError("Лимит должен быть > 0")
            exp = _promo_expires_from_form(expires_unlimited, expires_at_date)
            active = is_active == "true"
            allow_ids = _parse_allowed_user_ids_csv(allowed_user_ids)
        except (ValueError, InvalidOperation) as e:
            selected_users = await _load_promo_allowed_users(session, promo_id)
            return _layout(
                "Edit Promo Error",
                _promo_form(
                    action=f"/admin/promos/{promo_id}/edit",
                    promo=promo,
                    error=str(e),
                    selected_users=selected_users,
                ),
                request=request,
                back_href=f"/admin/promos/{promo_id}",
            )
        actor_user = await _web_admin_actor_user(session, request)
        actor_label = _web_admin_actor_label(request)
        before_type = promo.type
        before_value = promo.value
        before_max_uses = promo.max_uses
        before_expires_at = promo.expires_at
        before_is_active = promo.is_active
        before_allowed = set(
            (
                await session.execute(
                    select(PromoCodeAllowedUser.user_id).where(
                        PromoCodeAllowedUser.promo_id == promo_id
                    )
                )
            ).scalars().all()
        )
        promo.type = promo_type
        promo.value = val
        promo.fallback_value_rub = None
        promo.max_uses = mu
        promo.expires_at = exp
        promo.is_active = active
        promo.require_no_active_subscription = req_no_active
        promo.require_no_paid_subscription_months = req_months
        added, removed = await _sync_promo_allowed_users(
            session, promo_id=int(promo.id), user_ids=allow_ids
        )
        await session.commit()
        after_allowed = before_allowed.union(added).difference(removed)
        await notify_admin(
            get_settings(),
            title="✏️ Промокод изменён (web-admin)",
            lines=[
                f"Код: {md_esc(promo.code)}",
                f"Тип: {md_esc(_promo_type_ru(before_type))} → {md_esc(_promo_type_ru(promo.type))}",
                f"Награда: {md_esc(str(before_value))} → {md_esc(str(promo.value))}",
                f"Срок: {md_esc(_fmt_expires(before_expires_at))} → {md_esc(_fmt_expires(promo.expires_at))}",
                f"Лимит: {md_esc('∞' if before_max_uses is None else str(before_max_uses))} → {md_esc('∞' if promo.max_uses is None else str(promo.max_uses))}",
                f"Активен: {md_esc('да' if before_is_active else 'нет')} → {md_esc('да' if promo.is_active else 'нет')}",
                f"Привязан к: {md_esc(str(len(before_allowed)) if before_allowed else 'все')} → {md_esc(str(len(after_allowed)) if after_allowed else 'все')}",
                f"Условия: {md_esc(_promo_eligibility_summary(promo))}",
                web_admin_actor_notify_line(),
            ],
            event_type="promo_edit_web",
            topic=AdminLogTopic.PROMO,
            subject_user=actor_user,
            session=session,
        )
    return RedirectResponse(f"/admin/promos/{promo_id}", status_code=303)


@router.post("/promos/{promo_id}/delete")
async def admin_promos_delete(request: Request, promo_id: int):
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        actor_user = await _web_admin_actor_user(session, request)
        actor_label = _web_admin_actor_label(request)
        promo = await session.get(PromoCode, promo_id)
        if promo is not None:
            deleted_code = str(promo.code)
            await session.delete(promo)
            await session.commit()
            await notify_admin(
                get_settings(),
                title="🗑 Промокод удалён (web-admin)",
                lines=[
                    f"Код: {md_esc(deleted_code)}",
                    web_admin_actor_notify_line(),
                ],
                event_type="promo_delete_web",
                topic=AdminLogTopic.PROMO,
                subject_user=actor_user,
                session=session,
            )
    return RedirectResponse("/admin/promos", status_code=303)


def _parse_plan_opt_int(raw: str) -> int | None:
    t = (raw or "").strip()
    if not t or t in "-—":
        return None
    return int(t)


def _admin_tariff_transition_card(settings: Settings, tdays: str, *, base_month: Decimal) -> str:
    tip = ""
    try:
        d = int((tdays or "").strip())
        if d > 0:
            cred = transition_credit_for_remaining_legacy_rub(
                settings, remaining_days=d, base_month_rub=base_month
            )
            fee = settings.billing_transition_fee_percent
            tip = f"""
            <div class="mt-3 rounded-lg border border-primary/25 bg-primary/5 p-3 text-sm">
              <p><b>Остаток срока:</b> {_esc(d)}</p>
              <p>База месяца: <b>{_esc(base_month)} ₽</b> · комиссия: <b>{_esc(fee)}%</b></p>
              <p class="text-lg font-semibold mt-2">Рекомендуемый кредит на баланс: <span class="text-primary">{_esc(cred)} ₽</span></p>
              <p class="text-xs opacity-70 mt-1">Только для ориентира, автоначисления нет.</p>
            </div>
            """
    except ValueError:
        tip = ""
    return f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-calculator text-primary mr-2" aria-hidden="true"></i>Калькулятор перехода с legacy</h3>
        <p class="text-sm opacity-80">Остаток старой подписки по сроку → сумма на баланс после вычета комиссии. База месяца: минимальный активный тариф ~30 дней из БД; если нет — <code class="text-xs bg-base-300 px-1 rounded">BILLING_TRANSITION_BASE_MONTH_RUB</code>. Комиссия: <code class="text-xs bg-base-300 px-1 rounded">BILLING_TRANSITION_FEE_PERCENT</code>.</p>
        <form method="get" class="flex flex-wrap items-end gap-2">
          <label class="form-control w-full max-w-xs"><span class="label-text text-xs">Остаток срока</span>
            <input class="input input-bordered input-sm h-9 min-h-9" name="tdays" value="{_esc((tdays or '').strip())}" placeholder="30"/></label>
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9">Посчитать</button>
        </form>
        {tip}
      </div>
    </div>
    """


def _admin_plan_form(
    *,
    action: str,
    settings: Settings,
    plan: Plan | None = None,
    error: str | None = None,
    plan_id: int | None = None,
    base_month_rub_hint: Decimal | None = None,
    price_editable: bool = True,
) -> str:
    p = plan
    e = f"<div class='alert alert-error text-sm'>{_esc(error)}</div>" if error else ""
    name_extra = 'readonly class="input input-bordered input-sm h-9 min-h-9 bg-base-200"' if p and p.name in _RESERVED_PLAN_NAMES else 'class="input input-bordered input-sm h-9 min-h-9"'
    pkg_yes = p is not None and p.is_package_monthly
    pkg_no = p is None or not p.is_package_monthly
    act_yes = p is None or p.is_active
    act_no = p is not None and not p.is_active
    dd = str(p.duration_days) if p else "30"
    pr = str(p.price_rub) if p else "179"
    dsc = str(p.discount_percent) if p else "0"
    price_field = (
        f"""<label class="form-control w-full"><span class="label-text font-medium">Цена за 1 месяц, ₽</span>
              <input class="input input-bordered input-sm h-9 min-h-9" name="price_rub" id="f_price_rub" value="{_esc(pr)}" /></label>
          <p class="text-xs opacity-70 -mt-2">Только для тарифа «1 месяц». Остальные пакеты считаются автоматически.</p>"""
        if price_editable
        else """<input type="hidden" name="price_rub" value="0" />
          <p class="text-sm opacity-80">Цена рассчитывается автоматически от тарифа «1 месяц» и скидки ниже.</p>"""
    )
    tgb = "" if p is None or p.traffic_limit_gb is None else str(p.traffic_limit_gb)
    dlim = "" if p is None or p.device_limit is None else str(p.device_limit)
    mgbl = "" if p is None or p.monthly_gb_limit is None else str(p.monthly_gb_limit)
    so = str(p.sort_order) if p else "0"
    nm = p.name if p else ""
    daily = str(settings.billing_device_daily_rub)
    gbstep = str(settings.billing_gb_step_rub)
    mobx = str(settings.billing_mobile_gb_extra_rub)
    base_month_attr = str(base_month_rub_hint) if base_month_rub_hint is not None else ""
    delete_block = ""
    if plan_id is not None and p is not None and p.name not in _RESERVED_PLAN_NAMES:
        delete_block = f"""
        <form method="post" action="/admin/tariffs/{plan_id}/delete" class="mt-2" data-remna-confirm-msg="{_esc_attr(f'Удалить тариф «{p.name}»? Это возможно только если нет записей подписок с этим plan_id.')}">
          <button type="submit" class="btn btn-error btn-outline btn-sm h-9 min-h-9 gap-1.5"><i class="fa-solid fa-trash" aria-hidden="true"></i>Удалить тариф</button>
        </form>
        """
    return f"""
    <div class="flex w-full flex-col items-center justify-center py-6 min-h-[min(70vh,calc(100vh-10rem))]">
    <div class="card bg-base-100 border border-base-content/10 shadow-lg w-full max-w-2xl">
      <div class="card-body gap-4">
        <h2 class="card-title text-xl"><i class="fa-solid fa-tags text-primary mr-2" aria-hidden="true"></i>{'Редактирование тарифа' if p else 'Новый тариф'}</h2>
        {e}
        <div id="tariff-ppu-config" class="hidden" data-daily="{_esc_attr(daily)}" data-gbstep="{_esc_attr(gbstep)}" data-mobile="{_esc_attr(mobx)}" data-basemonth="{_esc_attr(base_month_attr)}"></div>
        <form method="post" action="{_esc(action)}" class="flex flex-col gap-4">
          <label class="form-control w-full"><span class="label-text font-medium">Название</span>
            <input type="text" name="name" id="f_name" value="{_esc(nm)}" {name_extra} /></label>
          <p class="text-xs opacity-70 -mt-2">Имена «{_esc(BASE_SUBSCRIPTION_PLAN_NAME)}» и «Триал» нельзя переименовать (системные).</p>
          <label class="form-control w-full"><span class="label-text font-medium">Срок, дней</span>
            <input class="input input-bordered input-sm h-9 min-h-9" name="duration_days" id="f_duration_days" type="number" min="1" value="{_esc(dd)}" /></label>
          {price_field}
          <label class="form-control w-full"><span class="label-text font-medium">Скидка плана, %</span>
            <input class="input input-bordered input-sm h-9 min-h-9" name="discount_percent" id="f_discount_percent" value="{_esc(dsc)}" /></label>
          <p class="text-xs opacity-70 -mt-2">Для 2 и 3 месяцев укажите только скидку — цена считается от тарифа «1 месяц» автоматически.</p>
          <p id="f_discount_price_preview" class="text-sm font-mono opacity-80"></p>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label class="form-control w-full"><span class="label-text font-medium">Лимит трафика, ГБ (пусто = без лимита в записи)</span>
              <input class="input input-bordered input-sm h-9 min-h-9" name="traffic_limit_gb" id="f_traffic_gb" value="{_esc(tgb)}" placeholder="—" /></label>
            <label class="form-control w-full"><span class="label-text font-medium">Лимит устройств (пусто = не задано)</span>
              <input class="input input-bordered input-sm h-9 min-h-9" name="device_limit" id="f_device_limit" value="{_esc(dlim)}" placeholder="—" /></label>
          </div>
          <label class="form-control w-full"><span class="label-text font-medium">Пакетный месячный тариф</span>
            <select class="select select-bordered select-sm h-9 min-h-9 text-sm" name="is_package_monthly" id="f_is_pkg">
              <option value="false" {'selected' if pkg_no else ''}>нет (классический)</option>
              <option value="true" {'selected' if pkg_yes else ''}>да (включены лимиты пакета в v2)</option>
            </select></label>
          <label class="form-control w-full"><span class="label-text font-medium">ГБ в пакете / месяц (для пакетного)</span>
            <input class="input input-bordered input-sm h-9 min-h-9" name="monthly_gb_limit" id="f_monthly_gb" value="{_esc(mgbl)}" placeholder="—" /></label>
          <div class="rounded-xl border border-base-content/10 bg-base-200/60 p-4 text-sm">
            <p class="font-semibold mb-1">Сравнение с pay-per-use за период 30</p>
            <p class="text-xs opacity-70 mb-2">Оценка по полям выше: {daily} ₽/день за устройство, {gbstep} ₽ за шаг ГБ, +{mobx} ₽/ГБ «мобильный интернет» (оценка моб. трафика — вручную).</p>
            <label class="form-control w-full max-w-xs"><span class="label-text text-xs">Моб. интернет, ГБ (оценка)</span>
              <input class="input input-bordered input-sm h-9 min-h-9" type="number" min="0" id="f_ppu_mobile_gb" value="0" /></label>
            <p id="f_ppu_result" class="mt-2 font-mono text-sm"></p>
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label class="form-control w-full"><span class="label-text font-medium">Активен в магазине</span>
              <select class="select select-bordered select-sm h-9 min-h-9 text-sm" name="is_active" id="f_is_active">
                <option value="true" {'selected' if act_yes else ''}>да</option>
                <option value="false" {'selected' if act_no else ''}>нет</option>
              </select></label>
            <label class="form-control w-full"><span class="label-text font-medium">Порядок сортировки</span>
              <input class="input input-bordered input-sm h-9 min-h-9" name="sort_order" id="f_sort_order" type="number" value="{_esc(so)}" /></label>
          </div>
          <button class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5 w-fit" type="submit"><i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Сохранить</button>
        </form>
        {delete_block}
        <script>
        (function(){{
          var cfg=document.getElementById('tariff-ppu-config');
          var out=document.getElementById('f_ppu_result');
          if(!cfg||!out)return;
          var DAILY=parseFloat(cfg.dataset.daily||'0');
          var GBSTEP=parseFloat(cfg.dataset.gbstep||'0');
          var MOB=parseFloat(cfg.dataset.mobile||'0');
          function recalc(){{
            var dev=parseInt(document.getElementById('f_device_limit').value,10);
            if(!dev||dev<1)dev=1;
            var pkg=document.getElementById('f_is_pkg').value==='true';
            var gb=0;
            if(pkg){{ gb=parseInt(document.getElementById('f_monthly_gb').value,10)||0; }}
            else {{ gb=parseInt(document.getElementById('f_traffic_gb').value,10)||0; }}
            var mob=parseInt(document.getElementById('f_ppu_mobile_gb').value,10)||0;
            var dr=(30*DAILY*dev).toFixed(2);
            var tr=(GBSTEP*gb).toFixed(2);
            var mr=(MOB*mob).toFixed(2);
            var sum=(parseFloat(dr)+parseFloat(tr)+parseFloat(mr)).toFixed(2);
            out.textContent='Устройства: '+dev+' × 30 × '+DAILY+' = '+dr+' ₽ · ГБ: '+gb+' × '+GBSTEP+' = '+tr+' ₽ · Моб.: '+mob+' × '+MOB+' = '+mr+' ₽ · Всего ≈ '+sum+' ₽';
          }}
          ['f_device_limit','f_monthly_gb','f_traffic_gb','f_is_pkg','f_ppu_mobile_gb'].forEach(function(id){{
            var el=document.getElementById(id);
            if(el){{ el.addEventListener('input',recalc); el.addEventListener('change',recalc); }}
          }});
          recalc();
        }})();
        (function(){{
          var cfg=document.getElementById('tariff-ppu-config');
          var out=document.getElementById('f_discount_price_preview');
          var priceEl=document.getElementById('f_price_rub');
          if(!cfg||!out)return;
          var BASE=parseFloat(cfg.dataset.basemonth||'0');
          function previewDiscount(){{
            var disc=parseFloat(document.getElementById('f_discount_percent').value)||0;
            var days=parseInt(document.getElementById('f_duration_days').value,10)||30;
            if(disc<=0||!BASE||BASE<=0){{ out.textContent=''; return; }}
            var months=days/30;
            var raw=BASE*months*(1-disc/100);
            var floored=Math.floor(raw);
            out.textContent='Расчётная цена при сохранении: '+floored+' ₽ (база '+BASE+' ₽/мес × '+months.toFixed(2)+' мес × '+(100-disc)+'%)';
            if(priceEl && priceEl.type!=='hidden') priceEl.value=String(floored);
          }}
          ['f_discount_percent','f_duration_days'].forEach(function(id){{
            var el=document.getElementById(id);
            if(el){{ el.addEventListener('input',previewDiscount); el.addEventListener('change',previewDiscount); }}
          }});
          previewDiscount();
        }})();
        </script>
      </div>
    </div>
    </div>
    """


@router.get("/tariffs")
async def admin_tariffs(request: Request, tdays: str = "") -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    shop_on = await tariff_purchases_enabled(settings)
    badge_cls = "badge-success" if shop_on else "badge-warning"
    badge_txt = "да" if shop_on else "нет"
    toggle_val = "0" if shop_on else "1"
    toggle_btn = "Выключить продажу тарифов в боте" if shop_on else "Включить продажу тарифов в боте"
    shop_toggle_card = (
        "<div class='card bg-base-100 border border-base-content/15 shadow-lg'>"
        "<div class='card-body gap-3'>"
        "<h3 class='text-lg font-semibold'><i class='fa-solid fa-robot mr-2' aria-hidden='true'></i>Продажа тарифов в Telegram-боте</h3>"
        "<p class='text-sm opacity-80'>При выключении пользователи не видят кнопки «Тарифы» и не могут списать баланс за пакет. PAYG без изменений.</p>"
        f"<p class='text-sm'>Сейчас: <span class='badge badge-sm {badge_cls}'>{badge_txt}</span>"
        " (совпадает с <code class='text-xs bg-base-300 px-1 rounded'>BOT_TARIFF_PURCHASES_ENABLED</code> и Redis).</p>"
        '<form method="post" action="/admin/tariffs/toggle-shop" class="flex flex-wrap gap-2 mt-2">'
        f'<input type="hidden" name="enabled" value="{toggle_val}" />'
        f'<button type="submit" class="btn btn-sm btn-outline">{_esc(toggle_btn)}</button>'
        "</form>"
        "</div></div>"
    )
    async with await _session() as session:
        dyn_base = await resolve_legacy_transition_base_month_rub(session, settings)
        ref_plan = await get_one_month_reference_plan(session)
        plans = list(
            (await session.execute(select(Plan).order_by(Plan.sort_order.asc(), Plan.id.asc()))).scalars().all()
        )
        plan_prices = {pl.id: await resolve_plan_price_rub(session, pl) for pl in plans}
    rows: list[str] = []
    for pl in plans:
        dev, gb = plan_fields_for_ppu_estimate(pl)
        est = estimate_pay_per_use_30d_rub(settings, device_count=dev, gb_per_month=gb, mobile_gb_per_month=0)
        pkg = "да" if pl.is_package_monthly else "нет"
        act = "да" if pl.is_active else "нет"
        rows.append(
            f"<tr class='remna-row-link cursor-pointer' data-row-href='/admin/tariffs/{pl.id}/edit' tabindex='0' role='link'>"
            f"<td class='font-medium'>{_esc(pl.name)}</td>"
            f"<td>{pl.duration_days}</td><td>{_esc(plan_prices.get(pl.id, pl.price_rub))}</td>"
            f"<td class='text-xs'>{_esc(pl.traffic_limit_gb if pl.traffic_limit_gb is not None else '—')}</td>"
            f"<td class='text-xs'>{_esc(pl.device_limit if pl.device_limit is not None else '—')}</td>"
            f"<td class='text-xs'>{_esc(pl.monthly_gb_limit if pl.monthly_gb_limit is not None else '—')}</td>"
            f"<td>{_esc(pkg)}</td><td>{_esc(act)}</td>"
            f"<td class='whitespace-nowrap text-xs' title='устройства {dev}, ГБ {gb}'>{_esc(est['total_rub'])} ₽</td>"
            f"<td>{pl.sort_order}</td></tr>"
        )
    trans = _admin_tariff_transition_card(settings, tdays, base_month=dyn_base)
    create_modal = _modal_shell(
        modal_id="tariff-create-modal",
        title="Новый тариф",
        inner=_admin_plan_form(
            action="/admin/tariffs/new",
            settings=settings,
            base_month_rub_hint=dyn_base,
            price_editable=ref_plan is None,
        ),
    )
    body = (
        "<div class='flex flex-col gap-4'>"
        f"{shop_toggle_card}"
        f"{trans}"
        "<div class='card bg-base-100 border border-base-content/10 shadow-lg'><div class='card-body gap-4'>"
        "<div class='flex flex-wrap items-center justify-between gap-2'><h2 class='card-title text-2xl mb-0'><i class='fa-solid fa-tags text-primary mr-2' aria-hidden='true'></i>Тарифы</h2>"
        "<button type='button' class='btn btn-primary btn-sm h-9 min-h-9 gap-1.5' data-remna-modal-open='tariff-create-modal'><i class='fa-solid fa-plus' aria-hidden='true'></i>Новый тариф</button></div>"
        "<div class='overflow-x-auto rounded-xl border border-base-content/10'><table class='table table-zebra table-sm'>"
        "<thead><tr><th>Название</th><th>Срок</th><th>Цена ₽</th><th>ГБ лимит</th><th>Устр.</th><th>ГБ/мес пакет</th><th>Пакет</th><th>Активен</th>"
        "<th title='Эквивалент pay-per-use за период 30'>≈ PPU 30</th><th>Сорт.</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"10\" class=\"opacity-50\">Нет тарифов</td></tr>'}</tbody></table></div>"
        "<p class='text-xs opacity-60'>Строка ведёт в редактирование. «Базовый» и «Триал» нельзя удалить и переименовать. "
        "Кнопки тарифов в боте строятся из БД при каждом открытии списка; при снятии с продажи или удалении устаревшее сообщение "
        "можно закрыть и открыть «Тарифы» снова.</p>"
        "</div></div></div>"
        f"{create_modal}"
        "<script>(function(){document.querySelectorAll('[data-remna-modal-open]').forEach(function(b){b.addEventListener('click',function(){var id=b.getAttribute('data-remna-modal-open');var m=document.getElementById(id);if(m)m.classList.remove('hidden');if(m)m.classList.add('flex');});});document.querySelectorAll('[data-remna-modal-close]').forEach(function(b){b.addEventListener('click',function(){var id=b.getAttribute('data-remna-modal-close');var m=document.getElementById(id);if(m)m.classList.add('hidden');if(m)m.classList.remove('flex');});});document.querySelectorAll('[role=\"dialog\"]').forEach(function(m){m.addEventListener('click',function(e){if(e.target===m){m.classList.add('hidden');m.classList.remove('flex');}});});})();</script>"
    )
    return _layout("Тарифы", body, request=request)


@router.get("/tariffs/new")
async def admin_tariffs_new(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        base_hint = await default_one_month_tariff_price_rub(session)
        ref_plan = await get_one_month_reference_plan(session)
    return _layout(
        "Новый тариф",
        _admin_plan_form(
            action="/admin/tariffs/new",
            settings=settings,
            base_month_rub_hint=base_hint,
            price_editable=ref_plan is None,
        ),
        request=request,
        back_href="/admin/tariffs",
    )


@router.post("/tariffs/new")
async def admin_tariffs_new_post(
    request: Request,
    name: str = Form(""),
    duration_days: str = Form(""),
    price_rub: str = Form(""),
    discount_percent: str = Form("0"),
    traffic_limit_gb: str = Form(""),
    device_limit: str = Form(""),
    monthly_gb_limit: str = Form(""),
    is_package_monthly: str = Form("false"),
    is_active: str = Form("true"),
    sort_order: str = Form("0"),
):
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    try:
        nm = name.strip()
        if not nm:
            raise ValueError("Название обязательно")
        if nm in _RESERVED_PLAN_NAMES:
            raise ValueError("Создайте тариф с другим имени; «Базовый» и «Триал» уже зарезервированы")
        dd = int((duration_days or "").strip())
        if dd < 1:
            raise ValueError("Срок должен быть ≥ 1 дня")
        pr = Decimal((price_rub or "0").strip().replace(",", "."))
        if pr < 0:
            raise ValueError("Цена не может быть отрицательной")
        dsc = Decimal((discount_percent or "0").strip().replace(",", "."))
        if dsc < 0 or dsc >= 100:
            raise ValueError("Скидка плана должна быть в [0, 100)")
        tgb = _parse_plan_opt_int(traffic_limit_gb)
        dlim = _parse_plan_opt_int(device_limit)
        mgbl = _parse_plan_opt_int(monthly_gb_limit)
        pkg = is_package_monthly == "true"
        active = is_active == "true"
        so = int((sort_order or "0").strip())
    except (ValueError, InvalidOperation) as e:
        return _layout(
            "Тариф — ошибка",
            _admin_plan_form(action="/admin/tariffs/new", settings=settings, error=str(e)),
            request=request,
            back_href="/admin/tariffs",
        )
    async with await _session() as session:
        dup = (
            await session.execute(select(Plan.id).where(Plan.name == nm).limit(1))
        ).scalar_one_or_none()
        if dup is not None:
            return _layout(
                "Тариф — ошибка",
                _admin_plan_form(
                    action="/admin/tariffs/new",
                    settings=settings,
                    error="Тариф с таким названием уже есть",
                ),
                request=request,
                back_href="/admin/tariffs",
            )
        ref_before = await get_one_month_reference_plan(session)
        is_ref_target = ref_before is None and is_one_month_duration(dd)
        if is_ref_target and pr <= 0:
            return _layout(
                "Тариф — ошибка",
                _admin_plan_form(
                    action="/admin/tariffs/new",
                    settings=settings,
                    error="Укажите цену за 1 месяц (больше 0 ₽)",
                    base_month_rub_hint=await default_one_month_tariff_price_rub(session),
                    price_editable=True,
                ),
                request=request,
                back_href="/admin/tariffs",
            )
        new_plan = Plan(
            name=nm,
            duration_days=dd,
            price_rub=pr if is_ref_target else Decimal("0"),
            discount_percent=dsc,
            traffic_limit_gb=tgb,
            device_limit=dlim,
            monthly_gb_limit=mgbl,
            is_package_monthly=pkg,
            is_active=active,
            sort_order=so,
        )
        session.add(new_plan)
        await session.flush()
        ref = await get_one_month_reference_plan(session)
        is_ref = ref is not None and new_plan.id == ref.id
        await assign_plan_catalog_price(
            session, new_plan, submitted_price_rub=pr if is_ref else None
        )
        if is_ref:
            await refresh_all_derived_plan_prices(session)
        plan_label = f"{new_plan.name} · {new_plan.duration_days} дн. · {new_plan.price_rub} ₽"
        await session.commit()
        await notify_admin(
            settings,
            title="📦 " + bold("Тариф создан (web-admin)"),
            lines=[
                plain("Тариф: ") + bold(plan_label),
                plain("Активен: ") + bold("да" if new_plan.is_active else "нет"),
                web_admin_actor_notify_line(),
            ],
            event_type="tariff_create_web",
            topic=AdminLogTopic.SUBSCRIPTIONS,
        )
    return RedirectResponse("/admin/tariffs", status_code=303)


@router.get("/tariffs/{plan_id}/edit")
async def admin_tariffs_edit(request: Request, plan_id: int) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        plan = await session.get(Plan, plan_id)
        base_hint = await default_one_month_tariff_price_rub(session)
        ref_plan = await get_one_month_reference_plan(session)
        price_editable = ref_plan is None or (plan is not None and plan.id == ref_plan.id)
    if plan is None:
        return _layout(
            "Тариф не найден",
            "<div class='alert alert-warning shadow-lg'>Тариф не найден</div>",
            request=request,
            back_href="/admin/tariffs",
        )
    return _layout(
        "Редактирование тарифа",
        _admin_plan_form(
            action=f"/admin/tariffs/{plan_id}/edit",
            settings=settings,
            plan=plan,
            plan_id=plan_id,
            base_month_rub_hint=base_hint,
            price_editable=price_editable,
        ),
        request=request,
        back_href="/admin/tariffs",
    )


@router.post("/tariffs/{plan_id}/edit")
async def admin_tariffs_edit_post(
    request: Request,
    plan_id: int,
    name: str = Form(""),
    duration_days: str = Form(""),
    price_rub: str = Form(""),
    discount_percent: str = Form("0"),
    traffic_limit_gb: str = Form(""),
    device_limit: str = Form(""),
    monthly_gb_limit: str = Form(""),
    is_package_monthly: str = Form("false"),
    is_active: str = Form("true"),
    sort_order: str = Form("0"),
):
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            return _layout(
                "Тариф не найден",
                "<div class='alert alert-warning shadow-lg'>Тариф не найден</div>",
                request=request,
                back_href="/admin/tariffs",
            )
        try:
            nm = name.strip()
            if not nm:
                raise ValueError("Название обязательно")
            if plan.name in _RESERVED_PLAN_NAMES and nm != plan.name:
                raise ValueError("Системный тариф нельзя переименовать")
            dd = int((duration_days or "").strip())
            if dd < 1:
                raise ValueError("Срок должен быть ≥ 1 дня")
            pr = Decimal((price_rub or "0").strip().replace(",", "."))
            if pr < 0:
                raise ValueError("Цена не может быть отрицательной")
            dsc = Decimal((discount_percent or "0").strip().replace(",", "."))
            if dsc < 0 or dsc >= 100:
                raise ValueError("Скидка плана должна быть в [0, 100)")
            tgb = _parse_plan_opt_int(traffic_limit_gb)
            dlim = _parse_plan_opt_int(device_limit)
            mgbl = _parse_plan_opt_int(monthly_gb_limit)
            pkg = is_package_monthly == "true"
            active = is_active == "true"
            so = int((sort_order or "0").strip())
        except (ValueError, InvalidOperation) as e:
            return _layout(
                "Тариф — ошибка",
                _admin_plan_form(
                    action=f"/admin/tariffs/{plan_id}/edit",
                    settings=settings,
                    plan=plan,
                    plan_id=plan_id,
                    error=str(e),
                ),
                request=request,
                back_href="/admin/tariffs",
            )
        if nm != plan.name:
            dup = (
                await session.execute(select(Plan.id).where(Plan.name == nm, Plan.id != plan_id).limit(1))
            ).scalar_one_or_none()
            if dup is not None:
                return _layout(
                    "Тариф — ошибка",
                    _admin_plan_form(
                        action=f"/admin/tariffs/{plan_id}/edit",
                        settings=settings,
                        plan=plan,
                        plan_id=plan_id,
                        error="Тариф с таким названием уже есть",
                    ),
                    request=request,
                    back_href="/admin/tariffs",
                )
        ref = await get_one_month_reference_plan(session)
        is_ref = ref is not None and plan.id == ref.id
        if is_ref and pr <= 0:
            return _layout(
                "Тариф — ошибка",
                _admin_plan_form(
                    action=f"/admin/tariffs/{plan_id}/edit",
                    settings=settings,
                    plan=plan,
                    plan_id=plan_id,
                    error="Укажите цену за 1 месяц (больше 0 ₽)",
                    base_month_rub_hint=await default_one_month_tariff_price_rub(session),
                    price_editable=True,
                ),
                request=request,
                back_href="/admin/tariffs",
            )
        plan.name = nm
        plan.duration_days = dd
        plan.discount_percent = dsc
        plan.traffic_limit_gb = tgb
        plan.device_limit = dlim
        plan.monthly_gb_limit = mgbl
        plan.is_package_monthly = pkg
        plan.is_active = active
        plan.sort_order = so
        await assign_plan_catalog_price(
            session, plan, submitted_price_rub=pr if is_ref else None
        )
        if is_ref:
            await refresh_all_derived_plan_prices(session)
        plan_label = f"{plan.name} · {plan.duration_days} дн. · {plan.price_rub} ₽"
        await session.commit()
        await notify_admin(
            settings,
            title="✏️ " + bold("Тариф изменён (web-admin)"),
            lines=[
                plain("Тариф: ") + bold(plan_label),
                plain("Активен: ") + bold("да" if plan.is_active else "нет"),
                web_admin_actor_notify_line(),
            ],
            event_type="tariff_edit_web",
            topic=AdminLogTopic.SUBSCRIPTIONS,
        )
    return RedirectResponse("/admin/tariffs", status_code=303)


@router.post("/tariffs/{plan_id}/delete")
async def admin_tariffs_delete(request: Request, plan_id: int):
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            return RedirectResponse("/admin/tariffs", status_code=303)
        if plan.name in _RESERVED_PLAN_NAMES:
            return _layout(
                "Тариф",
                f"<div class='alert alert-error shadow-lg'>Нельзя удалить системный тариф «{_esc(plan.name)}».</div>"
                "<p class='mt-2'><a class='link' href='/admin/tariffs'>К списку</a></p>",
                request=request,
                back_href="/admin/tariffs",
            )
        cnt = (
            await session.execute(
                select(func.count()).select_from(Subscription).where(Subscription.plan_id == plan_id)
            )
        ).scalar_one()
        if int(cnt or 0) > 0:
            return _layout(
                "Тариф",
                "<div class='alert alert-error shadow-lg'>У тарифа есть записи подписок в истории — удаление запрещено. Отключите тариф (неактивен) или замените план у подписок.</div>"
                "<p class='mt-2'><a class='link' href='/admin/tariffs'>К списку</a></p>",
                request=request,
                back_href="/admin/tariffs",
            )
        deleted_name = str(plan.name)
        deleted_days = int(plan.duration_days)
        await session.delete(plan)
        await session.commit()
        await notify_admin(
            get_settings(),
            title="🗑 " + bold("Тариф удалён (web-admin)"),
            lines=[
                plain("Тариф: ") + bold(f"{deleted_name} · {deleted_days} дн."),
                web_admin_actor_notify_line(),
            ],
            event_type="tariff_delete_web",
            topic=AdminLogTopic.SUBSCRIPTIONS,
        )
    return RedirectResponse("/admin/tariffs", status_code=303)

