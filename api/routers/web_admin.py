"""Web-admin: аналитика, пользователи и управление промокодами."""

from __future__ import annotations

import hmac
import html
from hashlib import sha256
from base64 import urlsafe_b64encode
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from secrets import token_urlsafe
from urllib.parse import quote_plus

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.database import get_session_factory
from shared.models.promo import PromoCode, PromoUsage
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.factory_reset_service import wipe_all_application_data

router = APIRouter(tags=["web-admin"])


def _esc(v: object) -> str:
    return html.escape(str(v))


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
    return "https://ui-avatars.com/api/?background=1f2430&color=e6e8eb&name=Admin"


def _head_common(title: str) -> str:
    return f"""  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_esc(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
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
  </style>"""


def _nav_link_class(href: str, cur: str) -> str:
    base = "flex items-center gap-0 rounded-xl px-2.5 py-2.5 text-sm font-medium transition-colors no-underline"
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
    return f"""<a href="{href}" class="{cls}">
      <i class="{icon_class} fa-fw w-7 shrink-0 text-center text-base opacity-90" aria-hidden="true"></i>
      <span class="nav-label ml-1 max-w-0 overflow-hidden whitespace-nowrap opacity-0 transition-all duration-300 ease-out group-hover/sidebar:max-w-[12rem] group-hover/sidebar:opacity-100">{_esc(label)}</span>
    </a>"""


def _mob_nav_cls(href: str, cur: str) -> str:
    h = href.rstrip("/")
    c = cur.rstrip("/") or "/"
    act = (h == "/admin/dashboard" and c in ("/admin/dashboard", "/admin")) or (
        h != "/admin/dashboard" and (c == h or c.startswith(h + "/"))
    )
    return "text-primary font-semibold" if act else "text-base-content/55"


def _layout(title: str, body: str, *, request: Request | None = None, show_nav: bool = True) -> HTMLResponse:
    cur = ""
    if request is not None:
        cur = request.url.path.rstrip("/") or "/"

    nav_blocks = ""
    main_cls = "min-h-screen bg-base-200 bg-gradient-to-br from-base-200 via-base-200/80 to-secondary/5 p-4 pb-24 md:pb-8 md:pl-[4.75rem]"

    if show_nav and request is not None:
        user_label = _auth_label(request) or "admin"
        avatar = _esc(_auth_avatar(request))
        desktop_sidebar = f"""
    <aside class="group/sidebar fixed left-0 top-0 z-40 hidden h-screen w-[4.75rem] flex-col overflow-x-hidden border-r border-base-content/10 bg-base-300 shadow-xl transition-[width] duration-300 ease-out hover:w-60 md:flex">
      <div class="flex shrink-0 items-center gap-0 px-2 pb-4 pt-3">
        <span class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary/20 text-primary">
          <i class="fa-solid fa-shield-halved text-lg" aria-hidden="true"></i>
        </span>
        <span class="nav-label ml-2 max-w-0 overflow-hidden whitespace-nowrap text-base font-bold tracking-tight text-base-content opacity-0 transition-all duration-300 ease-out group-hover/sidebar:max-w-[10rem] group-hover/sidebar:opacity-100">Remna</span>
      </div>
      <nav class="flex flex-1 flex-col gap-1 overflow-y-auto overflow-x-hidden px-2">
        {_sidebar_nav_item("/admin/dashboard", "fa-solid fa-chart-pie", "Дашборд", cur)}
        {_sidebar_nav_item("/admin/users", "fa-solid fa-users", "Пользователи", cur)}
        {_sidebar_nav_item("/admin/promos", "fa-solid fa-ticket", "Промокоды", cur)}
        {_sidebar_nav_item("/admin/settings", "fa-solid fa-gear", "Настройки", cur)}
      </nav>
      <div class="mt-auto border-t border-base-content/10 p-2">
        <div class="flex items-center gap-0">
          <img src="{avatar}" alt="" class="h-10 w-10 shrink-0 rounded-full border-2 border-primary/40 object-cover ring-2 ring-base-100" width="40" height="40" />
          <div class="nav-label flex min-w-0 flex-1 flex-col gap-1 pl-2 max-w-0 overflow-hidden opacity-0 transition-all duration-300 ease-out group-hover/sidebar:max-w-[11rem] group-hover/sidebar:opacity-100">
            <span class="truncate text-sm font-semibold text-base-content">{_esc(user_label)}</span>
            <form method="post" action="/admin/logout">
              <button type="submit" class="btn btn-ghost btn-xs gap-1 px-0 normal-case text-error hover:bg-error/10">
                <i class="fa-solid fa-right-from-bracket" aria-hidden="true"></i> Выйти
              </button>
            </form>
          </div>
        </div>
      </div>
    </aside>"""
        mobile_nav = f"""
    <nav class="fixed bottom-0 left-0 right-0 z-30 flex h-16 items-center justify-around border-t border-base-content/10 bg-base-300/95 px-1 py-2 backdrop-blur-md md:hidden" aria-label="Мобильное меню">
      <a href="/admin/dashboard" class="flex flex-col items-center gap-0.5 p-2 text-[10px] {_mob_nav_cls('/admin/dashboard', cur)}"><i class="fa-solid fa-chart-pie text-lg"></i><span>Дашборд</span></a>
      <a href="/admin/users" class="flex flex-col items-center gap-0.5 p-2 text-[10px] {_mob_nav_cls('/admin/users', cur)}"><i class="fa-solid fa-users text-lg"></i><span>Юзеры</span></a>
      <a href="/admin/promos" class="flex flex-col items-center gap-0.5 p-2 text-[10px] {_mob_nav_cls('/admin/promos', cur)}"><i class="fa-solid fa-ticket text-lg"></i><span>Промо</span></a>
      <a href="/admin/settings" class="flex flex-col items-center gap-0.5 p-2 text-[10px] {_mob_nav_cls('/admin/settings', cur)}"><i class="fa-solid fa-gear text-lg"></i><span>Настр.</span></a>
      <form method="post" action="/admin/logout" class="flex flex-col items-center justify-center p-2"><button type="submit" class="text-error" title="Выйти"><i class="fa-solid fa-right-from-bracket text-lg"></i></button></form>
    </nav>"""
        nav_blocks = desktop_sidebar + mobile_nav
    elif not show_nav:
        main_cls = "min-h-screen bg-base-200 bg-gradient-to-br from-base-200 via-base-200 to-secondary/10 flex items-center justify-center p-4 w-full"

    page = f"""<!DOCTYPE html>
<html lang="ru" data-theme="night">
<head>
{_head_common(title)}
</head>
<body class="text-base-content antialiased">
  {nav_blocks}
  <div class="{main_cls} max-w-[1400px] mx-auto w-full">
    {body}
  </div>
</body>
</html>"""
    return HTMLResponse(page)


def _is_logged(request: Request) -> bool:
    return bool(request.session.get("wauth"))


def _require_login(request: Request) -> RedirectResponse | None:
    if not _is_logged(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


async def _session() -> AsyncSession:
    factory = get_session_factory()
    return factory()


def _promo_reward_caption(promo: PromoCode) -> str:
    v = promo.value
    if promo.type in ("balance_rub", "bonus_rub"):
        return f"+{v} ₽"
    if promo.type == "subscription_days":
        return f"+{v} дн."
    if promo.type == "topup_bonus_percent":
        return f"+{v}%"
    return f"+{v}"


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


def _user_avatar_url(user: User) -> str:
    if (user.username or "").strip():
        return f"https://t.me/i/userpic/320/{user.username}.jpg"
    seed = quote_plus((user.first_name or user.username or f"U{user.id}" or "User"))
    return f"https://ui-avatars.com/api/?background=1f2430&color=e6e8eb&name={seed}"


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


@router.get("")
async def admin_root():
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.get("/dashboard")
async def admin_dashboard(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with await _session() as session:
        total_income = (
            await session.execute(
                select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.type == "topup",
                    Transaction.status == "completed",
                )
            )
        ).scalar_one()
        month_income = (
            await session.execute(
                select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.type == "topup",
                    Transaction.status == "completed",
                    Transaction.created_at >= month_start,
                )
            )
        ).scalar_one()
        day_income = (
            await session.execute(
                select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.type == "topup",
                    Transaction.status == "completed",
                    Transaction.created_at >= day_start,
                )
            )
        ).scalar_one()
        users_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        promos_count = (await session.execute(select(func.count()).select_from(PromoCode))).scalar_one()
        tx_last_14 = (
            await session.execute(
                select(Transaction.created_at, Transaction.amount).where(
                    Transaction.type == "topup",
                    Transaction.status == "completed",
                    Transaction.created_at >= day_start - timedelta(days=13),
                )
            )
        ).all()
    by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for created_at, amount in tx_last_14:
        if created_at is None:
            continue
        by_day[created_at.date()] += Decimal(amount)
    labels: list[str] = []
    max_val = Decimal("1")
    for i in range(13, -1, -1):
        d = (day_start - timedelta(days=i)).date()
        val = by_day[d]
        if val > max_val:
            max_val = val
        labels.append(f"{d.strftime('%d.%m')}: {val}")
    bars = []
    for i in range(13, -1, -1):
        d = (day_start - timedelta(days=i)).date()
        val = by_day[d]
        width = int((val / max_val) * 30) if max_val > 0 else 0
        bars.append(f"{d.strftime('%d.%m')} {'█' * max(1, width) if val > 0 else '·'} {val}")
    safe_total = float(total_income or 0)
    safe_month = float(month_income or 0)
    safe_day = float(day_income or 0)
    month_pct = int(min(100, round((safe_month / safe_total) * 100))) if safe_total > 0 else 0
    day_pct = int(min(100, round((safe_day / safe_month) * 100))) if safe_month > 0 else 0
    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-6">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-sack-dollar text-primary mr-2" aria-hidden="true"></i>Доход</h2>
        <p class="text-base-content/80">За все время: <span class="font-bold text-primary">{_esc(total_income)} ₽</span>
        · За месяц: <span class="font-bold">{_esc(month_income)} ₽</span>
        · За день: <span class="font-bold">{_esc(day_income)} ₽</span></p>
        <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
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
          <div class="card bg-base-200/50 border border-base-content/5 shadow-md sm:col-span-2 xl:col-span-1">
            <div class="card-body justify-center">
              <p class="text-sm opacity-60 mb-2">Пользователи / промокоды</p>
              <p class="text-2xl font-bold"><span class="text-primary">{users_count}</span> <span class="opacity-40">/</span> <span>{promos_count}</span></p>
            </div>
          </div>
        </div>
        <p class="text-sm opacity-60">Учитываются только платежи (<code class="bg-base-300 px-1.5 py-0.5 rounded text-xs">type=topup,status=completed</code>).</p>
        <pre class="bg-base-300/80 rounded-xl p-4 text-xs overflow-x-auto font-mono border border-base-content/10">{_esc(chr(10).join(bars))}</pre>
      </div>
    </div>
    """
    return _layout("Web-admin Dashboard", body, request=request)


@router.get("/users")
async def admin_users(request: Request, q: str = "", page: int = 1) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    needle = q.strip()
    page = max(1, page)
    per_page = 10
    async with await _session() as session:
        query = select(User).order_by(desc(User.id))
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
    rows = []
    for u in users:
        ring = "bad" if u.is_blocked else "ok"
        ring_tw = "ring-success" if ring == "ok" else "ring-error"
        badge_tw = "badge-error" if u.is_blocked else "badge-success"
        display = u.first_name or u.username or "-"
        username = f"@{u.username}" if u.username else "-"
        rows.append(
            "<tr>"
            f"<td><div class='flex items-center gap-3'><span class='rounded-full p-0.5 ring-2 ring-offset-2 ring-offset-base-100 {ring_tw}'><img class='rounded-full object-cover w-9 h-9' src='{_esc(_user_avatar_url(u))}' alt='' width='36' height='36'/></span>"
            f"<a class='link link-primary font-medium' href='/admin/users/{u.id}'>{_esc(display)}</a></div></td>"
            f"<td>{_esc(username)}</td><td><code class='bg-base-300 px-1.5 py-0.5 rounded text-xs'>{u.telegram_id}</code></td><td>{u.id}</td><td class='font-medium'>{_esc(u.balance)}</td>"
            f"<td><span class='badge {badge_tw} badge-sm'>{'Заблокирован' if u.is_blocked else 'Активен'}</span></td></tr>"
        )
    pages = []
    if total_pages > 1:
        for p in range(1, total_pages + 1):
            cls = "btn btn-ghost btn-sm" if p != page else "btn btn-primary btn-sm"
            pages.append(f"<a class='{cls}' href='/admin/users?q={_esc(needle)}&page={p}'>{p}</a>")
    body = (
        "<div class='card bg-base-100 border border-base-content/10 shadow-lg'><div class='card-body gap-4'>"
        "<h2 class='card-title text-2xl'><i class='fa-solid fa-users text-primary mr-2' aria-hidden='true'></i>Пользователи</h2>"
        "<form method='get' class='flex flex-wrap items-end gap-2'>"
        f"<input class='input input-bordered w-full max-w-md' name='q' value='{_esc(needle)}' placeholder='ID, username, имя'/>"
        "<button class='btn btn-primary btn-sm gap-1' type='submit'><i class='fa-solid fa-magnifying-glass' aria-hidden='true'></i>Поиск</button></form>"
        "<div class='overflow-x-auto rounded-xl border border-base-content/10'>"
        "<table class='table table-zebra table-sm'><thead><tr><th>Пользователь</th><th>Username</th><th>Telegram ID</th><th>ID в боте</th><th>Баланс</th><th>Статус</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"6\" class=\"opacity-50\">Нет данных</td></tr>'}</tbody></table></div>"
        f"<div class='flex flex-wrap items-center gap-2'><span class='text-sm opacity-60'>Страница {page} из {total_pages}</span>{''.join(pages)}</div></div></div>"
    )
    return _layout("Web-admin Users", body, request=request)


@router.get("/users/{user_id}")
async def admin_user_detail(request: Request, user_id: int) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return _layout(
                "User not found",
                "<div class='alert alert-warning shadow-lg'><i class='fa-solid fa-user-slash mr-2' aria-hidden='true'></i><span>Пользователь не найден</span></div>",
                request=request,
            )
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
        payments_total = (
            await session.execute(
                select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.user_id == user_id,
                    Transaction.type == "topup",
                    Transaction.status == "completed",
                )
            )
        ).scalar_one()
    subs_rows = "".join(
        f"<tr><td>{s.id}</td><td>{_esc(s.status)}</td><td>{_esc(s.started_at)}</td><td>{_esc(s.expires_at)}</td><td>{s.devices_count}</td></tr>"
        for s in subs
    )
    tx_rows = "".join(
        f"<tr><td>{t.id}</td><td>{_esc(t.type)}</td><td>{_esc(t.amount)}</td><td>{_esc(t.status)}</td><td>{_esc(t.payment_provider or '-')}</td><td>{_esc(t.created_at)}</td></tr>"
        for t in txs
    )
    ring = "bad" if user.is_blocked else "ok"
    ring_tw = "ring-success" if ring == "ok" else "ring-error"
    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <div class="flex flex-wrap items-start gap-4">
          <span class='rounded-full p-1 ring-2 ring-offset-4 ring-offset-base-100 {ring_tw}'><img class='rounded-full object-cover w-16 h-16' src='{_esc(_user_avatar_url(user))}' alt='' width='64' height='64'/></span>
          <div><h2 class="text-2xl font-bold">Пользователь #{user.id}</h2><p class="text-sm opacity-60">{_esc(user.first_name or user.username or '-')}</p></div>
        </div>
        <div class="divider my-0"></div>
        <p>Имя: <b>{_esc((user.first_name or '') + ' ' + (user.last_name or ''))}</b></p>
        <p>Username: <b>{_esc(user.username or '-')}</b> · Telegram ID: <code class="bg-base-300 px-1.5 py-0.5 rounded text-sm">{user.telegram_id}</code></p>
        <p>Баланс: <b class="text-primary">{_esc(user.balance)} ₽</b> · RemnaWave UUID: <code class="bg-base-300 px-1.5 py-0.5 rounded text-xs">{_esc(user.remnawave_uuid or '-')}</code></p>
        <p>Регистрация: <b>{_esc(user.created_at)}</b></p>
        <p>Всего оплатил (без админ-бонусов): <b>{_esc(payments_total)} ₽</b></p>
      </div>
    </div>
    <div class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-clock-rotate-left text-secondary mr-2" aria-hidden="true"></i>История подписок ({len(subs)})</h3>
        <div class="overflow-x-auto rounded-lg border border-base-content/10"><table class="table table-zebra table-sm"><thead><tr><th>ID</th><th>Статус</th><th>Старт</th><th>До</th><th>Устройства</th></tr></thead>
        <tbody>{subs_rows or '<tr><td colspan="5" class="opacity-50">Нет подписок</td></tr>'}</tbody></table></div>
      </div>
    </div>
    <div class="card bg-base-100 border border-base-content/10 shadow-lg mt-4">
      <div class="card-body gap-3">
        <h3 class="text-lg font-semibold"><i class="fa-solid fa-receipt text-accent mr-2" aria-hidden="true"></i>История транзакций ({len(txs)})</h3>
        <div class="overflow-x-auto rounded-lg border border-base-content/10"><table class="table table-zebra table-sm"><thead><tr><th>ID</th><th>Тип</th><th>Сумма</th><th>Статус</th><th>Провайдер</th><th>Дата</th></tr></thead>
        <tbody>{tx_rows or '<tr><td colspan="6" class="opacity-50">Нет транзакций</td></tr>'}</tbody></table></div>
      </div>
    </div>
    """
    return _layout(f"User {user_id}", body, request=request)


@router.get("/settings")
async def admin_settings(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    body = """
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <h2 class="card-title text-2xl"><i class="fa-solid fa-gear text-primary mr-2" aria-hidden="true"></i>Админские настройки / сброс БД</h2>
        <div class="alert alert-warning shadow-sm"><i class="fa-solid fa-triangle-exclamation mr-2" aria-hidden="true"></i><span>Внимание: полный сброс удалит пользователей, подписки, транзакции, промокоды и прочие данные.</span></div>
        <form method="post" action="/admin/settings/factory-reset" class="flex flex-wrap items-end gap-2">
          <input class="input input-bordered w-full max-w-md" name="confirm_text" placeholder="Введите WIPE ALL" autocomplete="off" />
          <button class="btn btn-error btn-sm gap-1" type="submit"><i class="fa-solid fa-bomb" aria-hidden="true"></i>Сделать factory reset</button>
        </form>
        <p class="text-sm opacity-60">Повторяет функцию сброса из Telegram-админки, но с веб-подтверждением.</p>
      </div>
    </div>
    """
    return _layout("Web-admin Settings", body, request=request)


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
            f"<tr><td><a class='link link-primary font-mono font-semibold' href='/admin/promos/{p.id}'>{_esc(p.code)}</a></td>"
            f"<td><code class='text-xs bg-base-300 px-1 rounded'>{_esc(p.type)}</code></td><td>{_esc(_promo_reward_caption(p))}</td>"
            f"<td>{p.used_count}/{_esc(p.max_uses if p.max_uses is not None else '∞')}</td>"
            f"<td>{_esc(_fmt_expires(p.expires_at))}</td><td class='{tw}'>{status}</td></tr>"
        )
    body = (
        "<div class='card bg-base-100 border border-base-content/10 shadow-lg'><div class='card-body gap-4'>"
        "<div class='flex flex-wrap items-center justify-between gap-2'><h2 class='card-title text-2xl mb-0'><i class='fa-solid fa-ticket text-primary mr-2' aria-hidden='true'></i>Промокоды</h2>"
        "<a class='btn btn-primary btn-sm gap-1' href='/admin/promos/new'><i class='fa-solid fa-plus' aria-hidden='true'></i>Создать промокод</a></div>"
        "<form method='get' class='flex flex-wrap items-end gap-2'>"
        f"<input class='input input-bordered w-full max-w-md font-mono uppercase' name='q' value='{_esc(needle)}' placeholder='Поиск по коду'/>"
        "<button class='btn btn-primary btn-sm gap-1' type='submit'><i class='fa-solid fa-magnifying-glass' aria-hidden='true'></i>Искать</button></form>"
        "<div class='overflow-x-auto rounded-xl border border-base-content/10'><table class='table table-zebra table-sm'><thead><tr><th>Код</th><th>Тип</th><th>Награда</th><th>Активации</th><th>Срок</th><th>Статус</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"6\" class=\"opacity-50\">Нет промокодов</td></tr>'}</tbody></table></div></div></div>"
    )
    return _layout("Web-admin Promos", body, request=request)


def _promo_form(*, action: str, promo: PromoCode | None = None, error: str | None = None) -> str:
    p = promo
    e = f"<div class='alert alert-error text-sm'>{_esc(error)}</div>" if error else ""
    ro = "readonly" if p else ""
    return f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg max-w-2xl">
      <div class="card-body gap-4">
        <h2 class="card-title text-xl"><i class="fa-solid fa-pen-to-square text-primary mr-2" aria-hidden="true"></i>{'Редактирование промокода' if p else 'Создание промокода'}</h2>
        {e}
        <form method="post" action="{_esc(action)}" class="flex flex-col gap-4">
          <label class="form-control w-full"><span class="label-text font-medium">Код</span>
            <input class="input input-bordered font-mono uppercase" name="code" value="{_esc(p.code if p else '')}" {ro} /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Тип</span>
            <select class="select select-bordered" name="promo_type">
            <option value="subscription_days" {'selected' if p and p.type == 'subscription_days' else ''}>subscription_days</option>
            <option value="balance_rub" {'selected' if p and p.type == 'balance_rub' else ''}>balance_rub</option>
            <option value="topup_bonus_percent" {'selected' if p and p.type == 'topup_bonus_percent' else ''}>topup_bonus_percent</option>
          </select></label>
          <label class="form-control w-full"><span class="label-text font-medium">Награда (число)</span>
            <input class="input input-bordered" name="value" value="{_esc(p.value if p else '')}" /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Фолбэк в ₽ (для subscription_days)</span>
            <input class="input input-bordered" name="fallback_value_rub" value="{_esc(p.fallback_value_rub if p and p.fallback_value_rub is not None else '')}" /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Лимит активаций (число или '-')</span>
            <input class="input input-bordered" name="max_uses" value="{_esc(p.max_uses if p and p.max_uses is not None else '-')}" /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Срок до (YYYY-MM-DD или DD.MM.YYYY или '-')</span>
            <input class="input input-bordered" name="expires_at" value="{_esc(_fmt_expires(p.expires_at) if p else '-')}" /></label>
          <label class="form-control w-full"><span class="label-text font-medium">Активен</span>
            <select class="select select-bordered" name="is_active">
            <option value="true" {'selected' if (p is None or p.is_active) else ''}>да</option>
            <option value="false" {'selected' if p is not None and not p.is_active else ''}>нет</option>
          </select></label>
          <button class="btn btn-primary gap-2 w-fit" type="submit"><i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Сохранить</button>
        </form>
      </div>
    </div>
    """


@router.get("/promos/new")
async def admin_promos_new(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    return _layout("New Promo", _promo_form(action="/admin/promos/new"), request=request)


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
        if promo_type not in {"subscription_days", "balance_rub", "topup_bonus_percent"}:
            raise ValueError("Неверный тип")
        val = Decimal(value.strip().replace(",", "."))
        if val <= 0:
            raise ValueError("Награда должна быть > 0")
        fb: Decimal | None = None
        if promo_type == "subscription_days":
            fb_raw = fallback_value_rub.strip().replace(",", ".")
            fb = Decimal(fb_raw) if fb_raw else None
            if fb is None or fb <= 0:
                raise ValueError("Для subscription_days нужен fallback > 0")
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
        )
    async with await _session() as session:
        promo = PromoCode(
            code=c,
            type=promo_type,
            value=val,
            fallback_value_rub=fb if promo_type == "subscription_days" else None,
            max_uses=mu,
            expires_at=exp,
            is_active=active,
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
        f"<td><code>{u.telegram_id}</code></td><td>{_esc(pu.used_at)}</td></tr>"
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
          <a class="btn btn-primary btn-sm gap-1" href="/admin/promos/{promo.id}/edit"><i class="fa-solid fa-pen" aria-hidden="true"></i>Редактировать</a>
          <form method="post" action="/admin/promos/{promo.id}/delete" onsubmit="return confirm('Удалить промокод?');">
            <button class="btn btn-error btn-outline btn-sm gap-1" type="submit"><i class="fa-solid fa-trash" aria-hidden="true"></i>Удалить</button>
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
    return _layout(f"Promo {promo_id}", body, request=request)


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
        )
    return _layout(
        "Edit Promo",
        _promo_form(action=f"/admin/promos/{promo_id}/edit", promo=promo),
        request=request,
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
            )
        try:
            if promo_type not in {"subscription_days", "balance_rub", "topup_bonus_percent"}:
                raise ValueError("Неверный тип")
            val = Decimal(value.strip().replace(",", "."))
            if val <= 0:
                raise ValueError("Награда должна быть > 0")
            fb: Decimal | None = None
            if promo_type == "subscription_days":
                fb_raw = fallback_value_rub.strip().replace(",", ".")
                fb = Decimal(fb_raw) if fb_raw else None
                if fb is None or fb <= 0:
                    raise ValueError("Для subscription_days нужен fallback > 0")
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
            )
        promo.type = promo_type
        promo.value = val
        promo.fallback_value_rub = fb if promo_type == "subscription_days" else None
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

