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


def _layout(title: str, body: str, *, request: Request | None = None, show_nav: bool = True) -> HTMLResponse:
    nav_auth = ""
    if request is not None and _auth_label(request):
        user_label = _auth_label(request) or "admin"
        nav_auth = (
            "<div class='nav-right'>"
            f"<img src='{_esc(_auth_avatar(request))}' class='avatar me' alt='me'/>"
            f"<span class='muted me-label'>{_esc(user_label)}</span>"
            "<form method='post' action='/admin/logout' style='display:inline'>"
            "<button type='submit' class='btn ghost'>Выйти</button></form>"
            "</div>"
        )
    nav_block = ""
    if show_nav:
        nav_block = f"""
    <header class="topbar">
      <div class="brand">Remna Web Admin</div>
      <input type="checkbox" id="menu-toggle" class="menu-toggle" />
      <label for="menu-toggle" class="menu-btn"><span></span><span></span><span></span></label>
      <nav class="nav">
        <a href="/admin/dashboard">Дашборд</a>
        <a href="/admin/users">Пользователи</a>
        <a href="/admin/promos">Промокоды</a>
        <a href="/admin/settings">Настройки</a>
      </nav>
      {nav_auth}
    </header>
"""
    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_esc(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {{ --bg:#0b1020; --bg2:#101936; --card:#131c33cc; --stroke:#2a3b66; --txt:#e9edf7; --muted:#a7b4d0; --acc:#7aa2ff; --ok:#60d394; --bad:#ff6b6b; --warn:#ffd166; }}
    * {{ box-sizing:border-box; }}
    body {{ font-family: Inter, Segoe UI, Arial, sans-serif; margin:0; color:var(--txt); background: radial-gradient(circle at 20% -20%, #2d4fa222, transparent 40%), linear-gradient(150deg, var(--bg), var(--bg2)); min-height:100vh; }}
    .wrap {{ max-width:1160px; margin:0 auto; padding:16px; }}
    .topbar {{ position:sticky; top:0; z-index:10; display:flex; gap:14px; align-items:center; margin-bottom:16px; padding:12px; border:1px solid var(--stroke); border-radius:16px; background:#0d162de6; backdrop-filter: blur(8px); }}
    .brand {{ font-weight:700; }}
    .nav {{ display:flex; gap:8px; align-items:center; }}
    .nav a {{ color:#d7e2ff; text-decoration:none; padding:8px 10px; border-radius:10px; border:1px solid transparent; transition:.2s; }}
    .nav a:hover {{ border-color:var(--stroke); background:#1a2748; }}
    .nav-right {{ margin-left:auto; display:flex; align-items:center; gap:8px; }}
    .menu-toggle, .menu-btn {{ display:none; }}
    .card {{ background:var(--card); border:1px solid var(--stroke); border-radius:16px; padding:16px; margin-bottom:14px; box-shadow: 0 10px 30px #02061155; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ text-align:left; padding:8px; border-bottom:1px solid #2a2f3a; vertical-align:top; }}
    input, select {{ background:#0f162b; border:1px solid var(--stroke); color:var(--txt); padding:10px; border-radius:10px; }}
    button, .btn {{ background:linear-gradient(135deg, #5989ff, #6f68ff); border:none; color:white; padding:9px 12px; border-radius:10px; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; gap:6px; }}
    .btn.ghost {{ background:#1a2748; border:1px solid var(--stroke); }}
    .muted {{ color:var(--muted); }}
    .row {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
    .ok {{ color:var(--ok); }}
    .bad {{ color:var(--bad); }}
    .warn {{ color:var(--warn); }}
    code {{ background:#0f162b; padding:2px 6px; border-radius:6px; }}
    .avatar {{ border-radius:50%; object-fit:cover; background:#0f162b; }}
    .avatar.me {{ width:28px; height:28px; border:2px solid #6f8dff; }}
    .stat-grid {{ display:grid; grid-template-columns:repeat(3, minmax(170px,1fr)); gap:10px; }}
    .donut {{ --deg:180deg; width:118px; height:118px; border-radius:50%; background:conic-gradient(#86a6ff var(--deg), #1c2b50 0); display:grid; place-items:center; margin:8px 0; }}
    .donut::after {{ content:""; width:78px; height:78px; border-radius:50%; background:#101936; box-shadow:inset 0 0 0 1px #334a7b; }}
    .donut-wrap {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
    .login-wrap {{ min-height:80vh; display:grid; place-items:center; }}
    .login-card {{ width:min(100%, 460px); }}
    .me-label {{ font-weight:600; }}
    .identity {{ display:flex; align-items:center; gap:10px; }}
    .status-ring {{ padding:2px; border-radius:999px; display:inline-block; }}
    .status-ring.ok {{ background:var(--ok); }}
    .status-ring.bad {{ background:var(--bad); }}
    .user-cell {{ display:flex; gap:10px; align-items:center; }}
    @media (max-width: 860px) {{
      .topbar {{ flex-wrap:wrap; }}
      .menu-btn {{ display:flex; width:38px; height:34px; border:1px solid var(--stroke); border-radius:10px; align-items:center; justify-content:center; flex-direction:column; gap:4px; cursor:pointer; margin-left:auto; }}
      .menu-btn span {{ display:block; width:16px; height:2px; background:#dce7ff; }}
      .nav {{ display:none; width:100%; flex-direction:column; align-items:stretch; }}
      .menu-toggle:checked ~ .nav {{ display:flex; }}
      .nav-right {{ width:100%; justify-content:flex-end; }}
      .stat-grid {{ grid-template-columns:1fr; }}
      th, td {{ font-size:13px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    {nav_block}
    {body}
  </div>
</body>
</html>
"""
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
    telegram_block = "<p class='muted'>Для входа через Telegram задайте BOT_USERNAME в .env.</p>"
    if bot_username:
        telegram_block = f"""
      <script async src="https://telegram.org/js/telegram-widget.js?22" data-telegram-login="{_esc(bot_username)}" data-size="large" data-radius="8" data-auth-url="/admin/login/telegram/widget" data-request-access="write"></script>
"""
    body = f"""
    <div class="login-wrap">
      <div class="card login-card">
        <h2>Вход</h2>
        <div class="row">{telegram_block}</div>
        <div style="height:10px"></div>
        <a class="btn" href="/admin/login/github/start">Войти через GitHub</a>
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
    <div class="card"><h2>Доход</h2>
      <p>За все время: <b>{_esc(total_income)} ₽</b> · За месяц: <b>{_esc(month_income)} ₽</b> · За день: <b>{_esc(day_income)} ₽</b></p>
      <div class="stat-grid">
        <div class="card">
          <div class="muted">Месяц от всего оборота</div>
          <div class="donut-wrap"><div class="donut" style="--deg:{month_pct * 3.6}deg"></div><div><b>{month_pct}%</b></div></div>
        </div>
        <div class="card">
          <div class="muted">День от месяца</div>
          <div class="donut-wrap"><div class="donut" style="--deg:{day_pct * 3.6}deg"></div><div><b>{day_pct}%</b></div></div>
        </div>
        <div class="card">
          <div class="muted">Пользователи / промокоды</div>
          <p><b>{users_count}</b> / <b>{promos_count}</b></p>
        </div>
      </div>
      <p class="muted">Учитываются только платежи ({'<code>type=topup,status=completed</code>'}).</p>
      <pre>{_esc(chr(10).join(bars))}</pre>
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
        display = u.first_name or u.username or "-"
        username = f"@{u.username}" if u.username else "-"
        rows.append(
            "<tr>"
            f"<td><div class='user-cell'><span class='status-ring {ring}'><img class='avatar' src='{_esc(_user_avatar_url(u))}' alt='u' width='34' height='34'/></span>"
            f"<a href='/admin/users/{u.id}'>{_esc(display)}</a></div></td>"
            f"<td>{_esc(username)}</td><td><code>{u.telegram_id}</code></td><td>{u.id}</td><td>{_esc(u.balance)}</td>"
            f"<td>{'Заблокирован' if u.is_blocked else 'Активен'}</td></tr>"
        )
    pages = []
    if total_pages > 1:
        for p in range(1, total_pages + 1):
            cls = "btn ghost" if p != page else "btn"
            pages.append(f"<a class='{cls}' href='/admin/users?q={_esc(needle)}&page={p}'>{p}</a>")
    body = (
        "<div class='card'><h2>Пользователи</h2>"
        "<form method='get' class='row'>"
        f"<input name='q' value='{_esc(needle)}' placeholder='ID, username, имя'/>"
        "<button type='submit'>Поиск</button></form><br/>"
        "<table><thead><tr><th>Пользователь</th><th>Username</th><th>Telegram ID</th><th>ID в боте</th><th>Баланс</th><th>Статус</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=6 class=muted>Нет данных</td></tr>'}</tbody></table>"
        f"<div class='row' style='margin-top:10px'><span class='muted'>Страница {page} из {total_pages}</span>{''.join(pages)}</div></div>"
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
            return _layout("User not found", "<div class='card'><h2>Пользователь не найден</h2></div>", request=request)
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
    body = f"""
    <div class="card">
      <div class="user-cell">
        <span class='status-ring {ring}'><img class='avatar' src='{_esc(_user_avatar_url(user))}' alt='u' width='68' height='68'/></span>
        <div><h2>Пользователь #{user.id}</h2><div class="muted">{_esc(user.first_name or user.username or '-')}</div></div>
      </div>
      <p>Имя: <b>{_esc((user.first_name or '') + ' ' + (user.last_name or ''))}</b></p>
      <p>Username: <b>{_esc(user.username or '-')}</b> · Telegram ID: <code>{user.telegram_id}</code></p>
      <p>Баланс: <b>{_esc(user.balance)} ₽</b> · RemnaWave UUID: <code>{_esc(user.remnawave_uuid or '-')}</code></p>
      <p>Регистрация: <b>{_esc(user.created_at)}</b></p>
      <p>Всего оплатил (без админ-бонусов): <b>{_esc(payments_total)} ₽</b></p>
    </div>
    <div class="card">
      <h3>История подписок ({len(subs)})</h3>
      <table><thead><tr><th>ID</th><th>Статус</th><th>Старт</th><th>До</th><th>Устройства</th></tr></thead>
      <tbody>{subs_rows or '<tr><td colspan=5 class=muted>Нет подписок</td></tr>'}</tbody></table>
    </div>
    <div class="card">
      <h3>История транзакций ({len(txs)})</h3>
      <table><thead><tr><th>ID</th><th>Тип</th><th>Сумма</th><th>Статус</th><th>Провайдер</th><th>Дата</th></tr></thead>
      <tbody>{tx_rows or '<tr><td colspan=6 class=muted>Нет транзакций</td></tr>'}</tbody></table>
    </div>
    """
    return _layout(f"User {user_id}", body, request=request)


@router.get("/settings")
async def admin_settings(request: Request) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    body = """
    <div class="card">
      <h2>Админские настройки / сброс БД</h2>
      <p class="warn">Внимание: полный сброс удалит пользователей, подписки, транзакции, промокоды и прочие данные.</p>
      <form method="post" action="/admin/settings/factory-reset" class="row">
        <input name="confirm_text" placeholder="Введите WIPE ALL" />
        <button type="submit">Сделать factory reset</button>
      </form>
      <p class="muted">Повторяет функцию сброса из Telegram-админки, но с веб-подтверждением.</p>
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
            "<div class='card'><h2>Сброс отменен</h2><p>Неверная фраза подтверждения.</p></div>",
            request=request,
        )
    async with await _session() as session:
        await wipe_all_application_data(session)
        await session.commit()
    return _layout(
        "Reset done",
        "<div class='card'><h2>База очищена</h2><p>Factory reset выполнен успешно.</p></div>",
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
        status_cls = "bad" if is_expired else ("ok" if p.is_active else "warn")
        rows.append(
            f"<tr><td><a href='/admin/promos/{p.id}'>{_esc(p.code)}</a></td>"
            f"<td>{_esc(p.type)}</td><td>{_esc(_promo_reward_caption(p))}</td>"
            f"<td>{p.used_count}/{_esc(p.max_uses if p.max_uses is not None else '∞')}</td>"
            f"<td>{_esc(_fmt_expires(p.expires_at))}</td><td class='{status_cls}'>{status}</td></tr>"
        )
    body = (
        "<div class='card'><h2>Промокоды</h2>"
        "<div class='row'><a href='/admin/promos/new'><button type='button'>Создать промокод</button></a></div><br/>"
        "<form method='get' class='row'>"
        f"<input name='q' value='{_esc(needle)}' placeholder='Поиск по коду'/>"
        "<button type='submit'>Искать</button></form><br/>"
        "<table><thead><tr><th>Код</th><th>Тип</th><th>Награда</th><th>Активации</th><th>Срок</th><th>Статус</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=6 class=muted>Нет промокодов</td></tr>'}</tbody></table></div>"
    )
    return _layout("Web-admin Promos", body, request=request)


def _promo_form(*, action: str, promo: PromoCode | None = None, error: str | None = None) -> str:
    p = promo
    e = f"<p class='bad'>{_esc(error)}</p>" if error else ""
    return f"""
    <div class="card">
      <h2>{'Редактирование промокода' if p else 'Создание промокода'}</h2>
      {e}
      <form method="post" action="{_esc(action)}">
        <p>Код<br/><input name="code" value="{_esc(p.code if p else '')}" {'readonly' if p else ''} /></p>
        <p>Тип<br/>
          <select name="promo_type">
            <option value="subscription_days" {'selected' if p and p.type == 'subscription_days' else ''}>subscription_days</option>
            <option value="balance_rub" {'selected' if p and p.type == 'balance_rub' else ''}>balance_rub</option>
            <option value="topup_bonus_percent" {'selected' if p and p.type == 'topup_bonus_percent' else ''}>topup_bonus_percent</option>
          </select>
        </p>
        <p>Награда (число)<br/><input name="value" value="{_esc(p.value if p else '')}" /></p>
        <p>Фолбэк в ₽ (для subscription_days)<br/><input name="fallback_value_rub" value="{_esc(p.fallback_value_rub if p and p.fallback_value_rub is not None else '')}" /></p>
        <p>Лимит активаций (число или '-')<br/><input name="max_uses" value="{_esc(p.max_uses if p and p.max_uses is not None else '-')}" /></p>
        <p>Срок до (YYYY-MM-DD или DD.MM.YYYY или '-')<br/><input name="expires_at" value="{_esc(_fmt_expires(p.expires_at) if p else '-')}" /></p>
        <p>Активен
          <select name="is_active">
            <option value="true" {'selected' if (p is None or p.is_active) else ''}>да</option>
            <option value="false" {'selected' if p is not None and not p.is_active else ''}>нет</option>
          </select>
        </p>
        <button type="submit">Сохранить</button>
      </form>
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
            return _layout("Promo not found", "<div class='card'><h2>Промокод не найден</h2></div>", request=request)
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
    <div class="card">
      <h2>Промокод <code>{_esc(promo.code)}</code></h2>
      <p>Тип: <b>{_esc(promo.type)}</b> · Награда: <b>{_esc(_promo_reward_caption(promo))}</b></p>
      <p>Срок: <b>{_esc(_fmt_expires(promo.expires_at))}</b> · Лимит: <b>{_esc(promo.max_uses if promo.max_uses is not None else '∞')}</b></p>
      <p>Активен: <b>{'да' if promo.is_active else 'нет'}</b> · Использований: <b>{promo.used_count}</b></p>
      <div class="row">
        <a href="/admin/promos/{promo.id}/edit"><button type="button">Редактировать</button></a>
        <form method="post" action="/admin/promos/{promo.id}/delete" onsubmit="return confirm('Удалить промокод?');">
          <button type="submit">Удалить</button>
        </form>
      </div>
    </div>
    <div class="card">
      <h3>История активаций ({len(usages)})</h3>
      <table><thead><tr><th>ID usage</th><th>User ID</th><th>Telegram ID</th><th>Дата</th></tr></thead>
      <tbody>{usage_rows or '<tr><td colspan=4 class=muted>Нет активаций</td></tr>'}</tbody></table>
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
        return _layout("Promo not found", "<div class='card'><h2>Промокод не найден</h2></div>", request=request)
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
            return _layout("Promo not found", "<div class='card'><h2>Промокод не найден</h2></div>", request=request)
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

