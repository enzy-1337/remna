"""Web-admin: аналитика, пользователи и управление промокодами."""

from __future__ import annotations

import asyncio
import base64
import hmac
import html
import json
import time
from hashlib import sha256
from base64 import urlsafe_b64encode
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from secrets import token_urlsafe
from types import SimpleNamespace
from urllib.parse import quote as url_quote
from urllib.parse import quote_plus

import httpx
import re
import redis.asyncio as redis_async
from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, desc, distinct, extract, exists, func, or_, select, text
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
from shared.models.plan import Plan
from shared.models.promo import PromoCode, PromoUsage
from shared.models.remnawave_webhook_event import RemnawaveWebhookEvent
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.factory_reset_service import wipe_all_application_data
from shared.services.referral_service import count_invited_users, list_invited_users
from shared.services.subscription_service import (
    admin_disable_subscription_record,
    admin_enable_subscription_record,
    count_devices,
    get_active_subscription,
    get_base_subscription_plan,
    remove_hwid_device_from_panel,
    remove_device_slot,
    set_subscription_auto_renew,
    unlink_hwid_device_keep_slots,
    update_rw_user_respecting_hwid_limit,
)
from shared.subscription_qr import subscription_url_qr_png

router = APIRouter(tags=["web-admin"])

_MSK_TZ = ZoneInfo("Europe/Moscow")


def _fmt_dt_msk(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_MSK_TZ).strftime("%d.%m.%Y %H:%M") + " МСК"


_AVATAR_CACHE: dict[int, tuple[float, bytes, str]] = {}
_AVATAR_TTL_SEC = 3600.0
_avatar_fetch_locks: dict[int, asyncio.Lock] = {}
_DASHBOARD_HTML_CACHE: tuple[float, str] | None = None
_DASHBOARD_HTML_TTL_SEC = 20.0
_USERS_HTML_CACHE: dict[tuple[str, int, str, str, str], tuple[float, str]] = {}
_USERS_HTML_TTL_SEC = 15.0


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


def _pagination_bar(*, page: int, total_pages: int, base_path: str, query_extra: dict[str, str]) -> str:
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
        return f"<a class='{tw}' href='{_esc(href)}'>{_esc(label)}</a>"

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
    return str(auth.get("label") or "") or None


def _auth_avatar(request: Request) -> str:
    auth = _auth_data(request)
    avatar_url = str(auth.get("avatar_url") or "").strip()
    if avatar_url:
        return avatar_url
    return "https://ui-avatars.com/api/?background=2563eb&color=f0f9ff&bold=true&name=Admin"


def _head_common(title: str, *, favicon_url: str | None = None) -> str:
    fav = ""
    u = (favicon_url or "").strip()
    if u.startswith(("http://", "https://")):
        fav = f'  <link rel="icon" href="{_esc(u)}" />\n'
    return f"""  <meta charset="utf-8" />
  <script>try{{var t=localStorage.getItem('remna-admin-theme');if(t==='light'||t==='night')document.documentElement.setAttribute('data-theme',t);}}catch(e){{}}</script>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
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
    body {{ font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
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
    base = "flex w-full items-center gap-0 rounded-xl px-1.5 py-2 text-sm font-medium transition-colors no-underline"
    h = href.rstrip("/")
    c = cur.rstrip("/") or "/"
    active = False
    if h == "/admin/dashboard":
        active = c in ("/admin/dashboard", "/admin")
    elif c == h or c.startswith(h + "/"):
        active = True
    if active:
        return f"{base} bg-primary/20 text-primary shadow-sm border border-primary/20"
    return f"{base} text-base-content/75 hover:bg-base-200 hover:text-base-content border border-transparent"


def _sidebar_nav_item(href: str, icon_class: str, label: str, cur: str) -> str:
    cls = _nav_link_class(href, cur)
    return f"""<div class="shrink-0 px-1.5">
    <a href="{href}" class="{cls}">
      <i class="{icon_class} fa-fw w-6 shrink-0 text-center text-[15px] opacity-90" aria-hidden="true"></i>
      <span class="nav-label ml-1 max-w-0 overflow-hidden whitespace-nowrap opacity-0 transition-all duration-300 ease-out group-hover/sidebar:max-w-[12rem] group-hover/sidebar:opacity-100">{_esc(label)}</span>
    </a></div>"""


def _mob_nav_cls(href: str, cur: str) -> str:
    h = href.rstrip("/")
    c = cur.rstrip("/") or "/"
    if h == "/admin/dashboard":
        act = c in ("/admin/dashboard", "/admin")
    else:
        act = c == h or c.startswith(h + "/")
    return "text-primary font-semibold" if act else "text-base-content/55"


def _layout(
    title: str,
    body: str,
    *,
    request: Request | None = None,
    show_nav: bool = True,
    back_href: str | None = None,
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
    main_cls = "min-h-screen bg-base-200 bg-gradient-to-br from-base-200 via-base-200/80 to-secondary/5 px-3 py-5 pt-16 pb-28 sm:px-5 md:pt-[4.75rem] md:pb-8 md:pl-[4.5rem] md:pr-6 lg:pr-8"

    if show_nav and request is not None:
        user_label = _auth_label(request) or "admin"
        avatar = _esc(_auth_avatar(request))
        logo_inner = _brand_logo_mark(settings)
        desktop_sidebar = f"""
    <aside class="group/sidebar fixed left-0 top-0 z-[60] hidden h-screen w-14 flex-col overflow-x-hidden border-r border-base-content/10 bg-base-300 shadow-xl transition-[width] duration-300 ease-out hover:w-56 md:flex">
      <div class="flex shrink-0 items-center gap-0 px-1.5 pb-3 pt-2.5">
        <span class="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-primary/20 text-primary">
          {logo_inner}
        </span>
        <span class="nav-label ml-2 max-w-0 overflow-hidden whitespace-nowrap text-sm font-bold tracking-tight text-base-content opacity-0 transition-all duration-300 ease-out group-hover/sidebar:max-w-[10rem] group-hover/sidebar:opacity-100">{_esc(brand_title)}</span>
      </div>
      <nav class="flex flex-1 flex-col gap-0.5 overflow-y-auto overflow-x-hidden px-0">
        {_sidebar_nav_item("/admin/dashboard", "fa-solid fa-chart-pie", "Дашборд", cur)}
        {_sidebar_nav_item("/admin/status", "fa-solid fa-heart-pulse", "Статус", cur)}
        {_sidebar_nav_item("/admin/users", "fa-solid fa-users", "Пользователи", cur)}
        {_sidebar_nav_item("/admin/tickets", "fa-solid fa-headset", "Тикеты", cur)}
        {_sidebar_nav_item("/admin/subscriptions", "fa-solid fa-clock-rotate-left", "Подписки", cur)}
        {_sidebar_nav_item("/admin/promos", "fa-solid fa-ticket", "Промокоды", cur)}
        {_sidebar_nav_item("/admin/broadcast", "fa-solid fa-bullhorn", "Рассылка", cur)}
        {_sidebar_nav_item("/admin/settings", "fa-solid fa-gear", "Настройки", cur)}
      </nav>
      <div class="mt-auto border-t border-base-content/10 p-1.5">
        <div class="flex w-full min-h-10 items-center gap-1">
          <a href="/admin/profile" class="shrink-0 rounded-full ring-2 ring-base-100 transition-transform hover:scale-105 focus:outline-none focus:ring-2 focus:ring-primary" title="Мой профиль">
            <img src="{avatar}" alt="" class="h-9 w-9 rounded-full border-2 border-primary/40 object-cover remna-avatar-img" width="36" height="36" loading="lazy" decoding="async" data-remna-avatar="1" />
          </a>
          <a href="/admin/profile" class="nav-label min-w-0 flex-1 truncate text-center text-sm font-semibold text-base-content no-underline opacity-0 max-w-0 overflow-hidden transition-all duration-300 ease-out group-hover/sidebar:max-w-none group-hover/sidebar:opacity-100 hover:text-primary" title="Мой профиль">{_esc(user_label)}</a>
          <form method="post" action="/admin/logout" class="nav-label shrink-0 opacity-0 max-w-0 overflow-hidden transition-all duration-300 ease-out group-hover/sidebar:max-w-none group-hover/sidebar:opacity-100">
            <button type="submit" class="btn btn-ghost btn-square btn-sm h-9 w-9 min-h-9 min-w-9 p-0 text-error hover:bg-error/10" title="Выйти" aria-label="Выйти">
              <i class="fa-solid fa-right-from-bracket" aria-hidden="true"></i>
            </button>
          </form>
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
        mobile_nav = f"""
    <nav class="fixed bottom-0 left-0 right-0 z-30 flex h-[4.25rem] items-center justify-between gap-0 border-t border-base-content/10 bg-base-300/95 px-2 py-1.5 backdrop-blur-md md:hidden" aria-label="Мобильное меню">
      <a href="/admin/dashboard" class="flex min-w-0 flex-1 flex-col items-center gap-0.5 p-1 text-[9px] leading-tight {_mob_nav_cls('/admin/dashboard', cur)}"><i class="fa-solid fa-chart-pie text-base"></i><span>Дашборд</span></a>
      <a href="/admin/status" class="flex min-w-0 flex-1 flex-col items-center gap-0.5 p-1 text-[9px] leading-tight {_mob_nav_cls('/admin/status', cur)}"><i class="fa-solid fa-heart-pulse text-base"></i><span>Статус</span></a>
      <a href="/admin/users" class="flex min-w-0 flex-1 flex-col items-center gap-0.5 p-1 text-[9px] leading-tight {_mob_nav_cls('/admin/users', cur)}"><i class="fa-solid fa-users text-base"></i><span>Юзеры</span></a>
      <a href="/admin/tickets" class="flex min-w-0 flex-1 flex-col items-center gap-0.5 p-1 text-[9px] leading-tight {_mob_nav_cls('/admin/tickets', cur)}"><i class="fa-solid fa-headset text-base"></i><span>Тикеты</span></a>
      <a href="/admin/subscriptions" class="flex min-w-0 flex-1 flex-col items-center gap-0.5 p-1 text-[9px] leading-tight {_mob_nav_cls('/admin/subscriptions', cur)}"><i class="fa-solid fa-clock-rotate-left text-base"></i><span>Подписки</span></a>
      <a href="/admin/promos" class="flex min-w-0 flex-1 flex-col items-center gap-0.5 p-1 text-[9px] leading-tight {_mob_nav_cls('/admin/promos', cur)}"><i class="fa-solid fa-ticket text-base"></i><span>Промо</span></a>
      <a href="/admin/broadcast" class="flex min-w-0 flex-1 flex-col items-center gap-0.5 p-1 text-[9px] leading-tight {_mob_nav_cls('/admin/broadcast', cur)}"><i class="fa-solid fa-bullhorn text-base"></i><span>Рассыл.</span></a>
      <a href="/admin/settings" class="flex min-w-0 flex-1 flex-col items-center gap-0.5 p-1 text-[9px] leading-tight {_mob_nav_cls('/admin/settings', cur)}"><i class="fa-solid fa-gear text-base"></i><span>Настр.</span></a>
      <a href="/admin/profile" class="flex min-w-0 flex-1 flex-col items-center gap-0.5 p-1 text-[9px] leading-tight {_mob_nav_cls('/admin/profile', cur)}"><i class="fa-solid fa-user text-base"></i><span>Профиль</span></a>
      <form method="post" action="/admin/logout" class="flex min-w-0 flex-1 flex-col items-center justify-center p-1"><button type="submit" class="text-error" title="Выйти"><i class="fa-solid fa-right-from-bracket text-base"></i></button></form>
    </nav>"""
        theme_toggle = """
    <button type="button" id="remna-theme-toggle" onclick="remnaToggleTheme()" class="btn btn-square fixed right-2 top-2 z-[52] h-8 w-8 min-h-8 min-w-8 shrink-0 border border-base-content/15 bg-base-300/90 p-0 shadow-md backdrop-blur-md md:right-7 md:top-6 md:h-10 md:w-10 md:min-h-10 md:min-w-10 md:shadow-lg" aria-label="Тема"></button>"""
        nav_blocks = desktop_sidebar + mobile_brand_bar + mobile_nav + theme_toggle
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
            <button type="submit" class="btn btn-outline btn-primary w-full" data-remna-hwid-mode="keep_slots">Только с панели</button>
            <p class="text-xs opacity-60 mt-1">Снимет HWID с Remnawave; оплаченные слоты не меняются.</p>
          </div>
          <div>
            <button type="submit" class="btn btn-error w-full" data-remna-hwid-mode="decrease_slot">Отвязать и убрать слот</button>
            <p class="text-xs opacity-60 mt-1">Минус один оплаченный слот и обновление лимита в панели.</p>
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
        <h3 id="remna-hwid-json-title" class="font-bold text-lg mb-3 pr-10">Данные устройства (JSON)</h3>
        <pre id="remna-hwid-json-pre" class="flex-1 overflow-auto rounded-lg border border-base-content/10 bg-base-300 p-3 text-[11px] leading-relaxed whitespace-pre-wrap font-mono"></pre>
      </div>
    </div>"""
    elif not show_nav:
        main_cls = "min-h-screen bg-base-200 bg-gradient-to-br from-base-200 via-base-200 to-secondary/10 flex items-center justify-center p-4 w-full"

    back_fixed = ""
    if back_href and show_nav and request is not None:
        back_fixed = f"""
    <a href="{_esc(back_href)}" class="btn btn-square btn-ghost fixed left-2 top-2 z-40 h-8 w-8 min-h-8 min-w-8 shrink-0 border border-base-content/15 bg-base-300/90 p-0 shadow-md backdrop-blur-md md:left-[calc(3.5rem+0.75rem)] md:top-6 md:h-10 md:w-10 md:min-h-10 md:min-w-10 md:shadow-lg" title="Назад" aria-label="Назад"><i class="fa-solid fa-arrow-left text-sm md:text-base" aria-hidden="true"></i></a>"""
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
      if(href && !href.startsWith('#') && !nav.hasAttribute('data-no-loading')){
        if(!(e.ctrlKey||e.metaKey||e.shiftKey||e.altKey) && nav.getAttribute('target')!=='_blank'){
          window.remnaShowLoading&&window.remnaShowLoading();
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
      var p=document.getElementById('remna-hwid-json-pre');
      if(p)p.textContent='';
    }
    window.remnaCloseAllModals=function(){remnaCloseHwid();remnaCloseSlot();remnaCloseSubdis();remnaCloseHwidJson();};
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
        var pre=document.getElementById('remna-hwid-json-pre');
        if(pre)pre.textContent=txt;
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
      if(e.key==='Escape')window.remnaCloseAllModals();
    });
    document.addEventListener('submit',function(e){
      var f=e.target;
      if(!f || !(f instanceof HTMLFormElement))return;
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
        var map={hwid_keep:'Устройство отвязано от панели. Оплаченные слоты не менялись.',hwid_slot:'Устройство отвязано, слот подписки уменьшен.',db_slot:'Слот снят: запись в БД удалена, лимит в панели обновлён.',sub_off:'Подписка отключена (БД и панель).',sub_on:'Подписка снова включена.',ar_on:'Авто-продление включено.',ar_off:'Авто-продление выключено.',days_ok:'Дни к подписке добавлены.',bal_ok:'Баланс пополнен.'};
        if(n&&map[n])window.remnaToast('success',map[n]);
        if(err)window.remnaToast('error',err);
        if(n||err){
          u.searchParams.delete('n');
          u.searchParams.delete('err');
          var qs=u.searchParams.toString();
          window.history.replaceState({},'',u.pathname+(qs?'?'+qs:''));
        }
      }catch(x){}
    }
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',remnaConsumeUrlNotify);
    else remnaConsumeUrlNotify();
  })();
  </script>"""

    page = f"""<!DOCTYPE html>
<html lang="ru" data-theme="night">
<head>
{_head_common(title, favicon_url=favicon_for_head)}
</head>
<body class="text-base-content antialiased">
  {nav_blocks}{back_fixed}{remna_chrome}
  <div class="{main_cls} remna-page w-full min-w-0">
    {inner}
  </div>
{theme_script if show_nav and request is not None else ""}
</body>
</html>"""
    return HTMLResponse(page, headers={"Cache-Control": "private, no-store"})


def _is_logged(request: Request) -> bool:
    return bool(request.session.get("wauth"))


def _require_login(request: Request) -> RedirectResponse | None:
    if not _is_logged(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


async def _session() -> AsyncSession:
    factory = get_session_factory()
    return factory()


async def _linked_bot_user_for_admin(request: Request) -> User | None:
    """Пользователь бота по Telegram ID из сессии web-admin (если вход через Telegram)."""
    if not _is_logged(request):
        return None
    auth = _auth_data(request)
    if str(auth.get("kind")) != "telegram":
        return None
    try:
        tid = int(auth.get("id"))
    except (TypeError, ValueError):
        return None
    async with await _session() as session:
        r = await session.execute(select(User).where(User.telegram_id == tid))
        return r.scalar_one_or_none()


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
    return f"+{v}"


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


def _admin_allowed_by_tg(tg_id: int) -> bool:
    return tg_id in get_settings().admin_telegram_ids


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


@router.get("/login")
async def admin_login_page(request: Request) -> HTMLResponse:
    if _is_logged(request):
        return RedirectResponse("/admin/dashboard", status_code=303)
    bot_username = (get_settings().bot_username or "").strip()
    telegram_block = "<p class='text-sm opacity-60'>Для входа через Telegram задайте BOT_USERNAME в .env.</p>"
    base = (get_settings().public_site_url or "").strip().rstrip("/")
    auth_url = "/admin/login/telegram/widget"
    if base:
        auth_url = f"{base}/admin/login/telegram/widget"
    if bot_username:
        telegram_block = f"""
      <script async src="https://telegram.org/js/telegram-widget.js?22" data-telegram-login="{_esc(bot_username)}" data-size="large" data-radius="8" data-auth-url="{_esc(auth_url)}" data-request-access="write"></script>
"""
    body = f"""
    <div class="card bg-base-100 w-full max-w-md border border-base-content/10 shadow-2xl">
      <div class="card-body items-center gap-6 text-center">
        <h2 class="card-title justify-center text-2xl font-bold">
          <i class="fa-solid fa-right-to-bracket text-primary" aria-hidden="true"></i>
          <span>Вход</span>
        </h2>
        <div class="flex w-full flex-col items-center gap-4">
          <div class="flex flex-wrap justify-center">{telegram_block}</div>
          <a class="btn btn-primary gap-2" href="/admin/login/github/start">
            <i class="fa-brands fa-github text-lg" aria-hidden="true"></i>
            Войти через GitHub
          </a>
        </div>
      </div>
    </div>
    """
    return _layout("Вход", body, request=request, show_nav=False)


@router.get("/login/telegram/widget")
async def admin_login_telegram_widget(
    request: Request,
    id: str = "",
    first_name: str = "",
    last_name: str = "",
    username: str = "",
    photo_url: str = "",
    auth_date: str = "",
    hash: str = "",
):
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
    if not _verify_telegram_login(payload, get_settings().bot_token):
        return RedirectResponse("/admin/login", status_code=303)
    tid = int(payload["id"])
    if not _admin_allowed_by_tg(tid):
        return RedirectResponse("/admin/login", status_code=303)
    label = payload["first_name"] or payload["username"] or f"tg:{tid}"
    request.session["wauth"] = {
        "kind": "telegram",
        "id": tid,
        "label": label,
        "avatar_url": payload["photo_url"],
        "username": payload["username"],
    }
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.get("/login/github/start")
async def admin_login_github_start(request: Request):
    settings = get_settings()
    if not settings.web_admin_github_client_id or not settings.web_admin_github_redirect_uri:
        return RedirectResponse("/admin/login", status_code=303)
    state = urlsafe_b64encode(token_urlsafe(24).encode("utf-8")).decode("ascii")[:40]
    request.session["gh_oauth_state"] = state
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
    if not login or not _admin_allowed_by_gh(login):
        return RedirectResponse("/admin/login", status_code=303)
    request.session["wauth"] = {
        "kind": "github",
        "login": login,
        "label": str(me.get("name") or login),
        "avatar_url": str(me.get("avatar_url") or f"https://github.com/{login}.png"),
        "username": login,
    }
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.post("/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


async def _admin_broadcast_job(text: str) -> None:
    import logging

    from aiogram import Bot

    from shared.services.broadcast_service import broadcast_to_users

    log = logging.getLogger("api.broadcast")
    settings = get_settings()
    tok = (settings.bot_token or "").strip()
    if not tok:
        log.error("фоновая рассылка: BOT_TOKEN пуст — пропуск")
        return
    try:
        log.info("фоновая рассылка из web-admin: длина текста=%s симв.", len((text or "").strip()))
        async with Bot(token=tok) as bot:
            ok, failed = await broadcast_to_users(bot, text)
        log.info("фоновая рассылка завершена: доставлено=%s ошибок=%s", ok, failed)
    except Exception:
        log.exception("фоновая рассылка: необработанная ошибка")


@router.get("/broadcast")
async def admin_broadcast_page(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
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
    body = f"""
    <div class="mx-auto flex w-full max-w-5xl justify-center">
    <div class="card bg-base-100 border border-base-content/10 shadow-lg w-full max-w-3xl">
      <div class="card-body gap-4">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-bullhorn text-primary mr-2" aria-hidden="true"></i>Рассылка в Telegram</h2>
        <p class="text-sm opacity-70">Сообщение уходит всем пользователям из БД (как «Рассылка всем» в боте). В Telegram используется <strong>HTML</strong>, не Markdown: жирный — <code class="bg-base-300 px-1 rounded text-xs">&lt;b&gt;текст&lt;/b&gt;</code> (закрывающий тег со слэшем: <code class="bg-base-300 px-1 rounded text-xs">&lt;/b&gt;</code>, не второй <code class="bg-base-300 px-1 rounded text-xs">&lt;b&gt;</code>). Упрощённо: <code class="bg-base-300 px-1 rounded text-xs">**текст**</code> автоматически превращается в жирный; <code class="bg-base-300 px-1 rounded text-xs">__текст__</code> — в подчёркнутый. Также: <code class="bg-base-300 px-1 rounded text-xs">&lt;i&gt;</code>, <code class="bg-base-300 px-1 rounded text-xs">&lt;a href=&quot;…&quot;&gt;</code>.</p>
        <p class="text-xs opacity-60">Шаблоны: клик — вставить; <b>ПКМ</b> по кнопке — сохранить текущий текст в шаблон (хранится в браузере).</p>
        {alert}
        <div class="grid gap-2 md:grid-cols-3">
          <button type="button" class="btn btn-outline btn-sm" id="bc-s1">Шаблон 1</button>
          <button type="button" class="btn btn-outline btn-sm" id="bc-s2">Шаблон 2</button>
          <button type="button" class="btn btn-outline btn-sm" id="bc-s3">Шаблон 3</button>
        </div>
        <form method="post" action="/admin/broadcast" class="flex flex-col gap-3">
          <textarea name="text" id="bc-text" class="textarea textarea-bordered min-h-[200px]" placeholder="Текст рассылки..." required></textarea>
          <div class="flex flex-wrap gap-2">
            <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5"><i class="fa-solid fa-paper-plane" aria-hidden="true"></i>Отправить в фоне</button>
            <button type="button" class="btn btn-ghost btn-sm h-9 min-h-9" id="bc-preview">Предпросмотр (экранированный)</button>
          </div>
        </form>
        <div id="bc-prev" class="hidden rounded-xl border border-base-content/10 bg-base-200/50 p-4 text-sm"></div>
      </div>
    </div>
    </div>
    <script>
    (function(){{
      var key='remna_broadcast_tpls';
      var ta=document.getElementById('bc-text');
      var prev=document.getElementById('bc-prev');
      function load(){{
        try{{ return JSON.parse(localStorage.getItem(key)||'[]'); }}catch(e){{ return []; }}
      }}
      function save(arr){{ localStorage.setItem(key, JSON.stringify(arr)); }}
      var arr=load();
      while(arr.length<3) arr.push('');
      for(var i=1;i<=3;i++){{
        (function(n){{
          var b=document.getElementById('bc-s'+n);
          if(!b) return;
          b.addEventListener('click', function(){{
            var a=load(); ta.value=a[n-1]||''; ta.focus();
          }});
          b.addEventListener('contextmenu', function(e){{
            e.preventDefault();
            var a=load(); a[n-1]=ta.value||''; save(a); alert('Шаблон '+n+' сохранён');
          }});
        }})(i);
      }}
      var pv=document.getElementById('bc-preview');
      if(pv) pv.addEventListener('click', function(){{
        var v=(ta.value||'').trim();
        if(!v){{ prev.classList.add('hidden'); return; }}
        prev.innerHTML='<div class="font-semibold mb-2 opacity-70\">Предпросмотр (как текст, теги экранированы)</div><div class="whitespace-pre-wrap break-words">'+v.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</div>';
        prev.classList.remove('hidden');
      }});
    }})();
    </script>
    """
    return _layout("Рассылка", body, request=request)


@router.post("/broadcast")
async def admin_broadcast_post(request: Request, background: BackgroundTasks, text: str = Form("")) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    body = (text or "").strip()
    if not body:
        return RedirectResponse("/admin/broadcast?err=empty", status_code=303)
    settings = get_settings()
    if not (settings.bot_token or "").strip():
        return RedirectResponse("/admin/broadcast?err=no_bot_token", status_code=303)
    background.add_task(_admin_broadcast_job, body)
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
        session.add(
            Transaction(
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
        )
        await session.commit()
    return RedirectResponse(f"/admin/tickets/{ticket_id}?n=bal_ok", status_code=303)


@router.post("/tickets/{ticket_id}/user/add-days")
async def admin_ticket_user_add_days(
    request: Request, ticket_id: int, subscription_id: int = Form(...), days: int = Form(...)
) -> RedirectResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    if days < 1 or days > 3650:
        return RedirectResponse(f"/admin/tickets/{ticket_id}?err={quote_plus('Дней: от 1 до 3650')}", status_code=303)
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
        sub.expires_at = sub.expires_at + timedelta(days=days)
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
    return RedirectResponse(f"/admin/tickets/{ticket_id}?n=days_ok", status_code=303)


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
    settings = get_settings()
    rw = RemnaWaveClient(settings)
    panel_ok, panel_msg, panel_ms = await rw.ping_api()
    node_rows, nodes_catalog_ms, nodes_list_err = await rw.list_nodes_with_latency(ping_each=False)
    panel_lat = f"Задержка API: {panel_ms} мс" if panel_ms is not None else None

    bot_ok = False
    bot_msg = "—"
    bot_lat: str | None = None
    tok = (settings.bot_token or "").strip()
    if not tok:
        bot_msg = "BOT_TOKEN не задан в окружении"
    else:
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get(f"https://api.telegram.org/bot{tok}/getMe")
            ms = round((time.perf_counter() - t0) * 1000, 1)
            bot_lat = f"Задержка: {ms} мс"
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    j = {}
                res = j.get("result") if isinstance(j, dict) else None
                if j.get("ok") and isinstance(res, dict):
                    bot_ok = True
                    un = str(res.get("username") or "")
                    bot_msg = f"@{un}" if un else "бот отвечает (getMe OK)"
                else:
                    bot_msg = str(j)[:220]
            else:
                bot_msg = f"HTTP {r.status_code}"
        except Exception as e:
            bot_msg = str(e)[:220]

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

    nodes_table_html = ""
    if nodes_list_err:
        nodes_table_html = f"<div class='alert alert-warning text-sm mt-4'>{_esc(nodes_list_err)}</div>"
    elif node_rows:
        cat_note = f"загрузка списка: {nodes_catalog_ms} мс · " if nodes_catalog_ms is not None else ""
        trs = []
        for n in node_rows:
            pms = n.get("ping_ms")
            ms_s = f"{pms} мс" if pms is not None else "—"
            trs.append(
                f"<tr><td class='max-w-[14rem] truncate' title='{_esc_attr(n.get('name'))}'>{_esc(n.get('name'))}</td>"
                f"<td><code class='text-xs bg-base-300 px-1 rounded'>{_esc(n.get('uuid'))}</code></td>"
                f"<td>{_esc(n.get('status'))}</td><td class='font-mono text-sm'>{_esc(ms_s)}</td>"
                f"<td class='text-xs opacity-80 max-w-xs'>{_esc(n.get('ping_note'))}</td></tr>"
            )
        nodes_table_html = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <div class="card-body gap-3">
        <h3 class="card-title text-lg"><i class="fa-solid fa-network-wired text-primary mr-2" aria-hidden="true"></i>Ноды Remnawave</h3>
        <p class="text-sm opacity-60">{_esc(cat_note)}статус и UUID из списка нод панели; отдельный замер к каждой ноде отключён.</p>
        <div class="overflow-x-auto rounded-lg border border-base-content/10">
          <table class="table table-zebra table-sm">
            <thead><tr><th>Имя</th><th>UUID</th><th>Статус (API)</th><th>Задержка</th><th>Примечание</th></tr></thead>
            <tbody>{''.join(trs)}</tbody>
          </table>
        </div>
      </div>
    </div>
    """

    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg mb-4">
      <div class="card-body gap-2">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-heart-pulse text-primary mr-2" aria-hidden="true"></i>Состояние сервисов</h2>
        <p class="text-sm opacity-70">Проверки при каждой загрузке страницы: API панели Remnawave, список нод (без отдельного запроса к каждой), Telegram <code class="bg-base-300 px-1 rounded text-xs">getMe</code>, БД <code class="bg-base-300 px-1 rounded text-xs">SELECT 1</code>, Redis <code class="bg-base-300 px-1 rounded text-xs">PING</code>.</p>
      </div>
    </div>
    <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {_status_service_card(title="Панель Remnawave (API)", icon="fa-solid fa-server", ok=panel_ok, detail=panel_msg, latency=panel_lat)}
      {_status_service_card(title="Telegram-бот", icon="fa-brands fa-telegram", ok=bot_ok, detail=bot_msg, latency=bot_lat)}
      {_status_service_card(title="База данных", icon="fa-solid fa-database", ok=db_ok, detail=db_msg, latency=db_lat)}
      {_status_service_card(title="Redis", icon="fa-solid fa-bolt", ok=redis_ok, detail=redis_msg, latency=redis_lat)}
    </div>
    {nodes_table_html}
    """
    return _layout("Статус сервисов", body, request=request)


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


@router.get("/tickets")
async def admin_tickets(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    body = """
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="card-title text-2xl"><i class="fa-solid fa-headset text-primary mr-2" aria-hidden="true"></i>Тикеты</h2>
          <a class="btn btn-outline btn-sm h-9 min-h-9 gap-1.5" href="/admin/tickets" title="Сбросить фильтры"><i class="fa-solid fa-rotate" aria-hidden="true"></i>Сброс</a>
        </div>
        <div class="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          <label class="form-control"><span class="label-text text-xs opacity-70">Статус</span>
            <select id="tk-status" class="select select-bordered select-sm h-9 min-h-9 text-sm">
              <option value="">Все</option>
              <option value="open">Открыт</option>
              <option value="in_progress">В работе</option>
              <option value="closed">Закрыт</option>
            </select>
          </label>
          <label class="form-control"><span class="label-text text-xs opacity-70">Дата с</span>
            <input id="tk-from" type="date" class="input input-bordered input-sm h-9 min-h-9 text-sm" />
          </label>
          <label class="form-control"><span class="label-text text-xs opacity-70">Дата по</span>
            <input id="tk-to" type="date" class="input input-bordered input-sm h-9 min-h-9 text-sm" />
          </label>
          <label class="form-control"><span class="label-text text-xs opacity-70">Поиск</span>
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
        var ass=t.assigned_admin_id?('#'+t.assigned_admin_id):'—';
        return ''
          +'<div class=\"card bg-base-100 border border-base-content/10 shadow-md hover:shadow-lg transition-shadow\">'
          +'<div class=\"card-body gap-3\">'
          +'<div class=\"flex items-start justify-between gap-2\"><h3 class=\"card-title text-lg\">Тикет #'+t.id+'</h3>'+statusBadge(t.status)+'</div>'
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
      df.addEventListener('change',loadTickets);
      dt.addEventListener('change',loadTickets);
      sort.addEventListener('change',loadTickets);
      loadTickets();
    })();
    </script>
    """
    return _layout("Web-admin Tickets", body, request=request)


@router.get("/tickets/{ticket_id}")
async def admin_ticket_detail_stub(request: Request, ticket_id: int) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    admin_opts: list[dict[str, object]] = []
    tg_admins = [int(x) for x in (settings.admin_telegram_ids or [])]
    if tg_admins:
        async with await _session() as session:
            rows = (
                await session.execute(select(User).where(User.telegram_id.in_(tg_admins)).order_by(User.id))
            ).scalars().all()
        by_tg = {int(u.telegram_id): u for u in rows}
        for tg_id in tg_admins:
            u = by_tg.get(tg_id)
            label = f"#{u.id} {((u.first_name or u.username or '').strip() or f'admin:{tg_id}')}" if u else f"admin:{tg_id}"
            admin_opts.append({"db_id": int(u.id) if u else None, "tg_id": tg_id, "label": label})
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
              <textarea id="tk-text" class="textarea textarea-bordered min-h-[44px] h-[44px] max-h-56 resize-none flex-1" placeholder="Введите ответ пользователю или заметку (Enter = отправить, Shift+Enter = новая строка)"></textarea>
              <input id="tk-file-input" type="file" accept="image/*,video/*" class="hidden"/>
              <div class="flex flex-col gap-2">
                <button id="tk-send-reply" class="btn btn-primary btn-sm btn-square h-10 min-h-10" title="Отправить ответ"><i class="fa-solid fa-paper-plane" aria-hidden="true"></i></button>
                <button id="tk-send-note" class="btn btn-outline btn-sm btn-square h-10 min-h-10" title="Добавить внутреннюю заметку"><i class="fa-solid fa-note-sticky" aria-hidden="true"></i></button>
                <button id="tk-attach" class="btn btn-ghost btn-sm btn-square h-10 min-h-10" title="Прикрепить фото/видео"><i class="fa-solid fa-paperclip" aria-hidden="true"></i></button>
              </div>
            </div>
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
      var loadInFlight=false;
      function esc(s){{return String(s||'').replace(/[&<>\"']/g,function(ch){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[ch]||ch;}});}}
      function modelSig(m){{
        var t=(m&&m.ticket)||{{}};
        var msgs=(m&&m.messages)||[];
        var last=msgs.length?msgs[msgs.length-1]:null;
        return [
          String(t.status||''),
          String(t.last_activity||''),
          String(msgs.length),
          String(last&&last.id||''),
          String(last&&last.created_at||''),
          String(last&&last.text||''),
          String(last&&last.photo_file_id||''),
          String(last&&last.video_file_id||'')
        ].join('|');
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
          html+='<form method="post" action="'+base+'/add-days" class="flex flex-wrap gap-2 items-end mt-2">'
            +'<input type="hidden" name="subscription_id" value="'+s.id+'"/>'
            +'<label class="form-control"><span class="label-text text-xs">Подписка +дн.</span>'
            +'<input type="number" name="days" min="1" max="3650" class="input input-bordered input-sm w-24" value="30" required/></label>'
            +'<button type="submit" class="btn btn-outline btn-sm">Добавить дни</button>'
            +'</form>';
          var ar=!!s.auto_renew;
          var nxt=ar?'0':'1';
          var lbl=ar?'Выключить авто-продление':'Включить авто-продление';
          html+='<form method="post" action="'+base+'/subscription/auto-renew" class="mt-2">'
            +'<input type="hidden" name="enabled" value="'+nxt+'"/>'
            +'<button type="submit" class="btn btn-ghost btn-xs">'+esc(lbl)+'</button>'
            +'</form>';
          html+='<form method="post" action="'+base+'/subscription/disable" class="mt-2" onsubmit="return confirm(&quot;Отключить подписку пользователя?&quot;);">'
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
          var mediaHtml='';
          if(m.photo_file_id){{
            var psrc='/api/tickets/'+ticketId+'/messages/'+m.id+'/photo';
            mediaHtml+='<div class="mt-2 relative"><img src="'+psrc+'" alt="" title="Нажмите, чтобы открыть крупно" data-kind="image" class="tk-ticket-thumb max-h-64 max-w-full rounded-lg border border-base-content/10 object-contain bg-base-300/30 cursor-pointer hover:opacity-90 transition-opacity" loading="lazy" decoding="async"/><a href="'+psrc+'" download class="btn btn-xs btn-circle absolute top-2 right-2" title="Скачать"><i class="fa-solid fa-download"></i></a></div>';
          }}
          if(m.video_file_id){{
            var vsrc='/api/tickets/'+ticketId+'/messages/'+m.id+'/video';
            mediaHtml+='<div class="mt-2 relative"><video src="'+vsrc+'" class="max-h-64 max-w-full rounded-lg border border-base-content/10 bg-base-300/20" controls playsinline preload="metadata"></video><a href="'+vsrc+'" download class="btn btn-xs btn-circle absolute top-2 right-2" title="Скачать"><i class="fa-solid fa-download"></i></a></div>';
          }}
          return ''
            +'<div class="flex w-full '+row+'">'
            +'<div class="max-w-[88%] rounded-xl border px-3 py-2 '+cls+' tk-msg-enter">'
            +'<div class="text-xs opacity-70 mb-1">'+esc(who)+' · '+esc(m.created_at||'')+'</div>'
            +'<div class="whitespace-pre-wrap break-words text-sm">'+esc(m.text||'')+'</div>'
            +mediaHtml
            +'</div></div>';
        }}).join('');
        if(shouldStickBottom) chat.scrollTop=chat.scrollHeight;
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
          var sig=modelSig(nextModel);
          if(sig===lastSig) return;
          model=nextModel;
          renderMeta(); renderUserPanel(); renderMgmt(); renderChat(wasNearBottom);
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
        try{{await sendJson('/api/tickets/'+ticketId+'/reply','POST',{{text:v}}); txt.value=''; await load();}}catch(e){{alert('Ошибка отправки ответа');}}
      }});
      document.getElementById('tk-send-note').addEventListener('click', async function(){{
        var v=(txt.value||'').trim(); if(!v) return;
        try{{await sendJson('/api/tickets/'+ticketId+'/note','POST',{{text:v}}); txt.value=''; await load();}}catch(e){{alert('Ошибка добавления заметки');}}
      }});
      document.getElementById('tk-set-open').addEventListener('click', async function(){{try{{await sendJson('/api/tickets/'+ticketId+'/status','PATCH',{{status:'open'}});await load();}}catch(e){{alert('Не удалось сменить статус');}}}});
      document.getElementById('tk-set-progress').addEventListener('click', async function(){{try{{await sendJson('/api/tickets/'+ticketId+'/status','PATCH',{{status:'in_progress'}});await load();}}catch(e){{alert('Не удалось сменить статус');}}}});
      document.getElementById('tk-set-closed').addEventListener('click', async function(){{if(!confirm("Закрыть тикет?"))return;try{{await sendJson('/api/tickets/'+ticketId+'/status','PATCH',{{status:'closed'}});await load();}}catch(e){{alert("Не удалось закрыть тикет");}}}});
      document.getElementById('tk-assign-save').addEventListener('click', async function(){{
        var tg=assign.value||'';
        var db=assign.options[assign.selectedIndex] ? (assign.options[assign.selectedIndex].dataset.dbId||'') : '';
        try{{await sendJson('/api/tickets/'+ticketId+'/assign','PATCH',{{assigned_admin_id:db?parseInt(db,10):null,telegram_assigned_admin_id:tg?parseInt(tg,10):null}});await load();}}catch(e){{alert('Не удалось сохранить назначение');}}
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
      var attachBtn=document.getElementById('tk-attach');
      if(attachBtn&&fileInput){{
        attachBtn.addEventListener('click', function(){{ fileInput.click(); }});
        fileInput.addEventListener('change', async function(){{
          if(!fileInput.files||!fileInput.files.length) return;
          try{{await sendMedia(false); await load();}}catch(e){{alert('Ошибка отправки файла');}}
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
      initAssign(); load(); loadTicketCount(); startLive();
    }})();
    </script>
    """
    return _layout(f"Ticket {ticket_id}", body, request=request, back_href="/admin/tickets")


@router.get("/users")
async def admin_users(
    request: Request, q: str = "", page: int = 1, sub: str = "", blocked: str = "", risk: str = ""
) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    needle = q.strip()
    sub_f = (sub or "").strip().lower()
    blk_f = (blocked or "").strip()
    risk_f = (risk or "").strip().lower()
    page = max(1, page)
    cache_key = (needle.casefold(), page, sub_f, blk_f, risk_f)
    now_m = time.monotonic()
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
        query = select(User).order_by(desc(risk_priority), desc(User.id))
        count_query = select(func.count()).select_from(User)
        if needle:
            if needle.isdigit():
                tid = int(needle)
                query = query.where(or_(User.telegram_id == tid, User.id == tid))
                count_query = count_query.where(or_(User.telegram_id == tid, User.id == tid))
            else:
                search_filter = or_(
                    User.username.ilike(f"%{needle}%"),
                    User.first_name.ilike(f"%{needle}%"),
                    User.last_name.ilike(f"%{needle}%"),
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
        rows.append(
            f"<tr class='remna-row-link cursor-pointer' data-row-href='/admin/users/{u.id}' tabindex='0' role='link' aria-label='Открыть пользователя'>"
            f"<td><div class='flex items-center gap-3'>{av}"
            f"<span class='link link-primary font-medium'>{_esc(display)}</span></div></td>"
            f"<td>{_esc(username)}</td><td><code class='bg-base-300 px-1.5 py-0.5 rounded text-xs'>{u.telegram_id}</code></td><td>{u.id}</td><td class='font-medium'>{_esc(u.balance)}</td>"
            f"<td><span class='badge {sub_badge} badge-sm'>{_esc(sub_lbl)}</span></td>"
            f"<td>{risk_badge}</td></tr>"
        )
    pager = _pagination_bar(
        page=page,
        total_pages=total_pages,
        base_path="/admin/users",
        query_extra={"q": needle, "sub": sub_f, "blocked": blk_f, "risk": risk_f},
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
    body = (
        "<div class='card bg-base-100 border border-base-content/10 shadow-lg'><div class='card-body gap-4'>"
        "<h2 class='card-title text-2xl'><i class='fa-solid fa-users text-primary mr-2' aria-hidden='true'></i>Пользователи</h2>"
        "<form id='us-form' method='get' class='flex flex-wrap items-end gap-2'>"
        f"<input id='us-q' class='input input-bordered input-sm h-9 min-h-9 w-full max-w-md text-sm' name='q' value='{_esc(needle)}' placeholder='ID, username, имя'/>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Подписка</span>"
        f"<select id='us-sub' name='sub' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{sub_opts}</select></label>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Аккаунт</span>"
        f"<select id='us-blocked' name='blocked' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{blk_opts}</select></label>"
        f"<label class='form-control'><span class='label-text text-xs opacity-70'>Риск минуса</span>"
        f"<select id='us-risk' name='risk' class='select select-bordered select-sm h-9 min-h-9 text-sm'>{risk_opts}</select></label>"
        "<button id='us-apply' class='btn btn-primary btn-sm h-9 min-h-9 gap-1.5' type='submit'><i class='fa-solid fa-magnifying-glass' aria-hidden='true'></i>Применить</button></form>"
        "<div class='overflow-x-auto rounded-xl border border-base-content/10'>"
        "<table class='table table-zebra table-sm'><thead><tr><th>Пользователь</th><th>Username</th><th>Telegram ID</th><th>ID в боте</th><th>Баланс</th><th>Подписка</th><th>Риск</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"7\" class=\"opacity-50\">Нет данных</td></tr>'}</tbody></table></div>"
        f"{pager}</div></div>"
        "<script>(function(){"
        "var form=document.getElementById('us-form'); if(!form)return;"
        "var q=document.getElementById('us-q'); var sub=document.getElementById('us-sub'); var blk=document.getElementById('us-blocked'); var risk=document.getElementById('us-risk');"
        "var timer=null;"
        "function submitLater(ms){ if(timer)clearTimeout(timer); timer=setTimeout(function(){ form.submit(); }, ms); }"
        "if(q){ q.addEventListener('input', function(){ submitLater(320); }); q.addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); form.submit(); } }); }"
        "if(sub)sub.addEventListener('change', function(){ form.submit(); });"
        "if(blk)blk.addEventListener('change', function(){ form.submit(); });"
        "if(risk)risk.addEventListener('change', function(){ form.submit(); });"
        "})();</script>"
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
    hwid_devices: list[dict] = []
    hwid_err: str | None = None
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
                           (SELECT tr.rating FROM ticket_ratings tr WHERE tr.ticket_id = t.id ORDER BY tr.id DESC LIMIT 1) AS rating
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
        active_sub = await get_active_subscription(session, user.id)
        n_db_dev_active = await count_devices(session, active_sub.id) if active_sub else 0
        today = datetime.now(UTC).date()
        spend_rows = list(
            (
                await session.execute(
                    select(BillingDailySummary).where(
                        BillingDailySummary.user_id == user.id,
                        BillingDailySummary.day >= today - timedelta(days=3),
                        BillingDailySummary.day <= today,
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

        ud = SimpleNamespace(
            id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
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
        sub_summary_html = f"""
    <div class="rounded-2xl border border-primary/30 bg-gradient-to-br from-primary/15 via-base-200/90 to-base-100 p-4 shadow-md backdrop-blur-sm">
      <h3 class="mb-3 text-xs font-bold uppercase tracking-wider text-primary">Активная подписка</h3>
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
    tx_rows = "".join(
        f"<tr><td>{tid}</td><td>{_esc(tt)}</td><td>{_esc(ta)}</td><td>{_esc(ts)}</td>"
        f"<td>{_esc(tp or '-')}</td><td>{_fmt_dt_msk(tc)}</td></tr>"
        for tid, tt, ta, ts, tp, tc in txs_tuples
    )
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
        if exp_chk is not None and exp_chk > now_check:
            ar_on = active_snap["auto_renew"]
            nxt = "0" if ar_on else "1"
            lbl = "Выключить авто-продление" if ar_on else "Включить авто-продление"
            tip = "После срока списание не произойдёт." if ar_on else "За ~1 ч до конца — попытка продлить с баланса."
            mgmt_html = f"""
    <div class="card bg-base-100 border border-warning/35 shadow-lg">
      <div class="card-body gap-4">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-sliders text-warning mr-2" aria-hidden="true"></i>Управление подпиской</h3>
        <form method="post" action="/admin/users/{user_id}/subscription/auto-renew" class="flex flex-wrap items-center gap-3">
          <input type="hidden" name="enabled" value="{nxt}"/>
          <button type="submit" class="btn btn-outline btn-warning btn-sm h-9 min-h-9">{_esc(lbl)}</button>
          <span class="text-xs opacity-60 max-w-xs">{_esc(tip)}</span>
        </form>
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
            f'data-user-id="{user_id}" data-hwid="{_esc_attr(hwid)}" data-title="{_esc_attr(title)}">Отвязать</button></td></tr>'
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
        <p class="text-xs opacity-60">«Отвязать»: в модальном окне — только снять HWID с панели или также уменьшить оплаченный слот.</p>
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
          </div>
          {_copy_line(label="ID в боте", value=str(ud.id))}
          {_copy_line(label="Telegram ID", value=str(ud.telegram_id))}
          {_copy_line(label="UUID в панели Remnawave", value=str(ud.remnawave_uuid) if ud.remnawave_uuid else "—")}
          {_copy_line(label="Реф. код", value=str(ud.referral_code))}
          <div class="flex flex-wrap items-end gap-2">
            <p class="m-0">Баланс: <b class="text-primary">{_esc(ud.balance)} ₽</b></p>
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
          </div>
          {negative_risk_block}
          <p>Регистрация: <b>{_fmt_dt_msk(ud.created_at)}</b></p>
          <p>Всего оплатил (без админ-бонусов): <b>{_esc(payments_total)} ₽</b></p>
          {ref_block}
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
          <div class="overflow-x-auto rounded-lg border border-base-content/10"><table class="table table-zebra table-sm"><thead><tr><th>ID</th><th>Тип</th><th>Сумма</th><th>Статус</th><th>Провайдер</th><th>Дата</th></tr></thead>
          <tbody>{tx_rows or '<tr><td colspan="6" class="opacity-50">Нет транзакций</td></tr>'}</tbody></table></div>
        </div>
      </div>
    </div>
    {tickets_block}
    """
    return _layout(f"User {user_id}", body, request=request, back_href="/admin/users")


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
        session.add(
            Transaction(
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
        )
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse(f"/admin/users/{user_id}?n=bal_ok", status_code=303)


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
            ok, msg = await unlink_hwid_device_keep_slots(session, user=user, hwid=hwid, settings=settings)
            ncode = "hwid_keep"
        else:
            ok, msg = await remove_hwid_device_from_panel(session, user=user, hwid=hwid, settings=settings)
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
        ok, msg = await remove_device_slot(session, user=user, device_id=device_id, settings=settings)
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
    display = (settings.web_admin_profile_display_name or "").strip() or str(auth.get("label") or "Администратор")
    kind = str(auth.get("kind") or "")
    avatar = _esc(_auth_avatar(request))
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
            in_list = "да" if tid_int in admin_ids else "нет"
            parts.append(
                f"<p>Telegram ID: <code class='bg-base-300 px-1.5 py-0.5 rounded text-xs'>{tid_int}</code>"
                f" · в <code class='text-xs'>ADMIN_TELEGRAM_IDS</code>: <b>{in_list}</b></p>"
            )
        if un:
            tg_url = f"https://t.me/{url_quote(un)}"
            parts.append(
                f"<p>Профиль: <a class='link link-primary font-medium' href=\"{_esc(tg_url)}\" target=\"_blank\" rel=\"noopener\">@{_esc(un)}</a></p>"
            )
    elif kind == "github":
        login = str(auth.get("login") or auth.get("username") or "").strip()
        parts.append("<p class='text-base'>Вход через <b>GitHub</b>.</p>")
        if login:
            allowed_gh = {x.casefold() for x in settings.web_admin_github_logins}
            in_list = "да" if login.casefold() in allowed_gh else "нет"
            parts.append(
                f"<p>Аккаунт: <a class='link link-primary font-medium' href=\"https://github.com/{_esc(login)}\" target=\"_blank\" rel=\"noopener\">{_esc(login)}</a>"
                f" · в <code class='text-xs'>WEB_ADMIN_GITHUB_LOGINS</code>: <b>{in_list}</b></p>"
            )
    else:
        parts.append("<p class='opacity-70'>Способ входа не определён.</p>")

    panel_raw = (settings.remnawave_public_url or settings.remnawave_api_url or "").strip().rstrip("/")
    if panel_raw:
        parts.append(
            f"<p><a class='link link-secondary font-medium' href=\"{_esc(panel_raw)}\" target=\"_blank\" rel=\"noopener\">"
            "Открыть панель Remnawave</a></p>"
        )

    ties = "\n".join(parts)

    linked = await _linked_bot_user_for_admin(request)
    profile_notice = ""
    ncode = (request.query_params.get("n") or "").strip()
    err = (request.query_params.get("err") or "").strip()
    if ncode == "bal_ok":
        profile_notice = "<div class='alert alert-success shadow-sm'><span>Баланс успешно пополнен.</span></div>"
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
    if linked is not None:
        profile_balance_block = f"""
    <div class="card bg-base-100 border border-primary/25 shadow-lg">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-wallet text-primary mr-2" aria-hidden="true"></i>Баланс в боте</h3>
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
        <p class="text-xs opacity-60 pt-2">Права в админке задаются в .env (<code class='bg-base-300 px-1 rounded text-[10px]'>ADMIN_TELEGRAM_IDS</code>, <code class='bg-base-300 px-1 rounded text-[10px]'>WEB_ADMIN_GITHUB_LOGINS</code>).</p>
      </div>
    </div>
    {profile_balance_block}
    {vpn_block}
    </div>
    """
    return _layout("Мой профиль", body, request=request, back_href="/admin/dashboard")


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
        session.add(
            Transaction(
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
        )
        await session.commit()
    _USERS_HTML_CACHE.clear()
    return RedirectResponse("/admin/profile?n=bal_ok", status_code=303)


@router.get("/settings")
async def admin_settings(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    vals = read_whitelist_values()
    tab_buttons: list[str] = []
    tab_panels: list[str] = []
    for idx, (sec_id, sec_title, fields) in enumerate(WEB_ADMIN_ENV_SECTIONS):
        active = "btn-primary" if idx == 0 else "btn-ghost"
        tab_buttons.append(
            f"<button type=\"button\" data-env-tab=\"{_esc(sec_id)}\" class=\"btn btn-sm h-9 min-h-9 shrink-0 gap-1.5 {active}\">"
            f"{_esc(sec_title)}</button>"
        )
        hidden = "" if idx == 0 else " hidden"
        flds: list[str] = []
        for key, label, _getter, help_text in fields:
            v = vals.get(key, "")
            flds.append(
                f"<label class=\"form-control w-full border-b border-base-content/5 pb-4 last:border-0 last:pb-0\">"
                f"<div class=\"label\"><span class=\"label-text font-medium\">{_esc(label)}</span>"
                f"<code class=\"label-text-alt text-[10px] opacity-50\">{_esc(key)}</code></div>"
                f"<p class=\"text-xs leading-snug text-base-content/70 mb-2 max-w-3xl\">{_esc(help_text)}</p>"
                f"<input class=\"input input-bordered input-sm h-9 min-h-9 w-full font-mono text-xs\" name=\"{key}\" "
                f'value="{_esc(v)}" autocomplete="off" /></label>'
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
    env_tabs_script = """
    <script>
    (function(){
      function show(id){
        document.querySelectorAll('[data-env-panel]').forEach(function(p){
          p.classList.toggle('hidden', p.getAttribute('data-env-panel')!==id);
        });
        document.querySelectorAll('[data-env-tab]').forEach(function(b){
          var on=b.getAttribute('data-env-tab')===id;
          b.classList.toggle('btn-primary',on);
          b.classList.toggle('btn-ghost',!on);
        });
      }
      document.querySelectorAll('[data-env-tab]').forEach(function(b){
        b.addEventListener('click',function(){show(b.getAttribute('data-env-tab'));});
      });
    })();
    </script>"""
    body = f"""
    <div class="tabs-env card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-sliders text-primary mr-2" aria-hidden="true"></i>Настройки .env</h2>
        <p class="text-sm opacity-70">Редактируются только безопасные ключи (пояснения — как в комментариях к <code class="bg-base-300 px-1 rounded text-xs">.env.example</code>). Токены, <code class="bg-base-300 px-1 rounded text-xs">DATABASE_URL</code>, секреты платежей и GitHub — правьте на сервере вручную.</p>
        {saved_note}
        <div role="tablist" class="flex flex-wrap gap-2 border-b border-base-content/10 pb-3">
          {''.join(tab_buttons)}
        </div>
        <form method="post" action="/admin/settings/env" class="flex flex-col gap-4">
          {''.join(tab_panels)}
          <button class="btn btn-primary btn-sm h-9 min-h-9 w-fit gap-1.5" type="submit"><i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Сохранить в .env</button>
        </form>
      </div>
    </div>
    <div role="tabpanel" class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <div class="card-body gap-4">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-triangle-exclamation text-error mr-2" aria-hidden="true"></i>Опасная зона</h2>
        <div class="alert alert-warning shadow-sm"><i class="fa-solid fa-triangle-exclamation mr-2" aria-hidden="true"></i><span>Полный сброс удалит пользователей, подписки, транзакции, промокоды и прочие данные.</span></div>
        <form method="post" action="/admin/settings/factory-reset" class="flex flex-wrap items-end gap-2">
          <input class="input input-bordered input-sm h-9 min-h-9 w-full max-w-md" name="confirm_text" placeholder="Введите WIPE ALL" autocomplete="off" />
          <button class="btn btn-error btn-sm h-9 min-h-9 gap-1.5" type="submit"><i class="fa-solid fa-bomb" aria-hidden="true"></i>Сделать factory reset</button>
        </form>
        <p class="text-sm opacity-60">То же, что сброс из Telegram-админки, с подтверждением в браузере.</p>
      </div>
    </div>
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
            f"<td><code class='text-xs bg-base-300 px-1 rounded'>{_esc(p.type)}</code></td><td>{_esc(_promo_reward_caption(p))}</td>"
            f"<td>{p.used_count}/{_esc(p.max_uses if p.max_uses is not None else '∞')}</td>"
            f"<td>{_esc(_fmt_expires(p.expires_at))}</td><td class='{tw}'>{status}</td></tr>"
        )
    body = (
        "<div class='card bg-base-100 border border-base-content/10 shadow-lg'><div class='card-body gap-4'>"
        "<div class='flex flex-wrap items-center justify-between gap-2'><h2 class='card-title text-2xl mb-0'><i class='fa-solid fa-ticket text-primary mr-2' aria-hidden='true'></i>Промокоды</h2>"
        "<a class='btn btn-primary btn-sm h-9 min-h-9 gap-1.5' href='/admin/promos/new'><i class='fa-solid fa-plus' aria-hidden='true'></i>Создать промокод</a></div>"
        "<form method='get' class='flex flex-wrap items-end gap-2'>"
        f"<input class='input input-bordered input-sm h-9 min-h-9 w-full max-w-md font-mono text-sm uppercase' name='q' value='{_esc(needle)}' placeholder='Поиск по коду'/>"
        "<button class='btn btn-primary btn-sm h-9 min-h-9 gap-1.5' type='submit'><i class='fa-solid fa-magnifying-glass' aria-hidden='true'></i>Искать</button></form>"
        "<div class='overflow-x-auto rounded-xl border border-base-content/10'><table class='table table-zebra table-sm'><thead><tr><th>Код</th><th>Тип</th><th>Награда</th><th>Активации</th><th>Срок</th><th>Статус</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"6\" class=\"opacity-50\">Нет промокодов</td></tr>'}</tbody></table></div></div></div>"
    )
    return _layout("Web-admin Promos", body, request=request)


def _promo_form(*, action: str, promo: PromoCode | None = None, error: str | None = None) -> str:
    p = promo
    e = f"<div class='alert alert-error text-sm'>{_esc(error)}</div>" if error else ""
    ro = "readonly" if p else ""
    return f"""
    <div class="flex w-full flex-col items-center justify-center py-6 min-h-[min(70vh,calc(100vh-10rem))]">
    <div class="card bg-base-100 border border-base-content/10 shadow-lg w-full max-w-2xl">
      <div class="card-body gap-4">
        <h2 class="card-title text-xl"><i class="fa-solid fa-pen-to-square text-primary mr-2" aria-hidden="true"></i>{'Редактирование промокода' if p else 'Создание промокода'}</h2>
        {e}
        <form method="post" action="{_esc(action)}" class="flex flex-col gap-4">
          <label class="form-control w-full"><span class="label-text font-medium">Код</span>
            <input class="input input-bordered input-sm h-9 min-h-9 font-mono text-sm uppercase" name="code" value="{_esc(p.code if p else '')}" {ro} /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Тип</span>
            <select class="select select-bordered select-sm h-9 min-h-9 text-sm" name="promo_type">
            <option value="discount_percent" {'selected' if p and p.type == 'discount_percent' else ''}>discount_percent</option>
            <option value="balance_rub" {'selected' if p and p.type == 'balance_rub' else ''}>balance_rub</option>
            <option value="topup_bonus_percent" {'selected' if p and p.type == 'topup_bonus_percent' else ''}>topup_bonus_percent</option>
            <option value="extra_gb" {'selected' if p and p.type == 'extra_gb' else ''}>extra_gb</option>
            <option value="extra_devices" {'selected' if p and p.type == 'extra_devices' else ''}>extra_devices</option>
          </select></label>
          <label class="form-control w-full"><span class="label-text font-medium">Награда (число)</span>
            <input class="input input-bordered input-sm h-9 min-h-9 text-sm" name="value" value="{_esc(p.value if p else '')}" /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Фолбэк в ₽ (устарело, не используется)</span>
            <input class="input input-bordered input-sm h-9 min-h-9 text-sm" name="fallback_value_rub" value="{_esc(p.fallback_value_rub if p and p.fallback_value_rub is not None else '')}" /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Лимит активаций (число или '-')</span>
            <input class="input input-bordered input-sm h-9 min-h-9 text-sm" name="max_uses" value="{_esc(p.max_uses if p and p.max_uses is not None else '-')}" /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Срок до (YYYY-MM-DD или DD.MM.YYYY или '-')</span>
            <input class="input input-bordered input-sm h-9 min-h-9 text-sm" name="expires_at" value="{_esc(_fmt_expires(p.expires_at) if p else '-')}" /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Активен</span>
            <select class="select select-bordered select-sm h-9 min-h-9 text-sm" name="is_active">
            <option value="true" {'selected' if (p is None or p.is_active) else ''}>да</option>
            <option value="false" {'selected' if p is not None and not p.is_active else ''}>нет</option>
          </select></label>
          <button class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5 w-fit" type="submit"><i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Сохранить</button>
        </form>
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


@router.post("/promos/new")
async def admin_promos_new_post(
    request: Request,
    code: str = Form(""),
    promo_type: str = Form(""),
    value: str = Form(""),
    fallback_value_rub: str = Form(""),
    max_uses: str = Form("-"),
    expires_at: str = Form("-"),
    is_active: str = Form("true"),
):
    denied = _require_login(request)
    if denied is not None:
        return denied
    try:
        c = code.strip().upper()
        if not c:
            raise ValueError("Код обязателен")
        if promo_type not in {"discount_percent", "balance_rub", "topup_bonus_percent", "extra_gb", "extra_devices"}:
            raise ValueError("Неверный тип")
        val = Decimal(value.strip().replace(",", "."))
        if val <= 0:
            raise ValueError("Награда должна быть > 0")
        if promo_type == "discount_percent" and (val <= 0 or val >= 100):
            raise ValueError("discount_percent должен быть в диапазоне (0,100)")
        mu: int | None = None
        if max_uses.strip() != "-":
            if not max_uses.strip().isdigit():
                raise ValueError("Лимит должен быть целым числом")
            mu = int(max_uses.strip())
            if mu <= 0:
                raise ValueError("Лимит должен быть > 0")
        exp = _parse_date_any(expires_at)
        active = is_active == "true"
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
        promo = PromoCode(
            code=c,
            type=promo_type,
            value=val,
            fallback_value_rub=None,
            max_uses=mu,
            expires_at=exp,
            is_active=active,
            created_by_user_id=admin_db_id,
        )
        session.add(promo)
        await session.commit()
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
    usage_rows = "".join(
        f"<tr><td>{pu.id}</td><td><a href='/admin/users/{u.id}'>#{u.id}</a></td>"
        f"<td><code>{u.telegram_id}</code></td><td class='whitespace-nowrap text-xs'>{_fmt_dt_msk(pu.used_at)}</td></tr>"
        for pu, u in usages
    )
    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <h2 class="card-title text-2xl font-mono">Промокод <span class="text-primary">{_esc(promo.code)}</span></h2>
        <p>Тип: <code class="bg-base-300 px-1.5 py-0.5 rounded text-sm">{_esc(promo.type)}</code> · Награда: <b>{_esc(_promo_reward_caption(promo))}</b></p>
        <p>Срок: <b>{_esc(_fmt_expires(promo.expires_at))}</b> · Лимит: <b>{_esc(promo.max_uses if promo.max_uses is not None else '∞')}</b></p>
        <p>Активен: <b>{'да' if promo.is_active else 'нет'}</b> · Использований: <b>{promo.used_count}</b></p>
        <div class="flex flex-wrap gap-2">
          <a class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5" href="/admin/promos/{promo.id}/edit"><i class="fa-solid fa-pen" aria-hidden="true"></i>Редактировать</a>
          <form method="post" action="/admin/promos/{promo.id}/delete" onsubmit="return confirm('Удалить промокод?');">
            <button class="btn btn-error btn-outline btn-sm h-9 min-h-9 gap-1.5" type="submit"><i class="fa-solid fa-trash" aria-hidden="true"></i>Удалить</button>
          </form>
        </div>
      </div>
    </div>
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
    return _layout(
        "Edit Promo",
        _promo_form(action=f"/admin/promos/{promo_id}/edit", promo=promo),
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
    expires_at: str = Form("-"),
    is_active: str = Form("true"),
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
            if promo_type not in {"discount_percent", "balance_rub", "topup_bonus_percent", "extra_gb", "extra_devices"}:
                raise ValueError("Неверный тип")
            val = Decimal(value.strip().replace(",", "."))
            if val <= 0:
                raise ValueError("Награда должна быть > 0")
            if promo_type == "discount_percent" and (val <= 0 or val >= 100):
                raise ValueError("discount_percent должен быть в диапазоне (0,100)")
            mu: int | None = None
            if max_uses.strip() != "-":
                if not max_uses.strip().isdigit():
                    raise ValueError("Лимит должен быть целым числом")
                mu = int(max_uses.strip())
                if mu <= 0:
                    raise ValueError("Лимит должен быть > 0")
            exp = _parse_date_any(expires_at)
            active = is_active == "true"
        except (ValueError, InvalidOperation) as e:
            return _layout(
                "Edit Promo Error",
                _promo_form(action=f"/admin/promos/{promo_id}/edit", promo=promo, error=str(e)),
                request=request,
                back_href=f"/admin/promos/{promo_id}",
            )
        promo.type = promo_type
        promo.value = val
        promo.fallback_value_rub = None
        promo.max_uses = mu
        promo.expires_at = exp
        promo.is_active = active
        await session.commit()
    return RedirectResponse(f"/admin/promos/{promo_id}", status_code=303)


@router.post("/promos/{promo_id}/delete")
async def admin_promos_delete(request: Request, promo_id: int):
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        promo = await session.get(PromoCode, promo_id)
        if promo is not None:
            await session.delete(promo)
            await session.commit()
    return RedirectResponse("/admin/promos", status_code=303)

