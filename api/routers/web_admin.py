"""Web-admin: аналитика, пользователи и управление промокодами."""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

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


def _layout(title: str, body: str, *, user_label: str | None = None) -> HTMLResponse:
    nav_auth = ""
    if user_label:
        nav_auth = (
            f"<span class='muted'>Вход: {_esc(user_label)}</span>"
            "<form method='post' action='/admin/logout' style='display:inline'>"
            "<button type='submit'>Выйти</button></form>"
        )
    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_esc(title)}</title>
  <style>
    body {{ font-family: Inter, Segoe UI, Arial, sans-serif; margin:0; background:#0f1115; color:#e6e8eb; }}
    .wrap {{ max-width:1100px; margin:0 auto; padding:20px; }}
    .nav {{ display:flex; gap:12px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }}
    .nav a {{ color:#90caf9; text-decoration:none; }}
    .card {{ background:#171a21; border:1px solid #2a2f3a; border-radius:10px; padding:14px; margin-bottom:14px; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ text-align:left; padding:8px; border-bottom:1px solid #2a2f3a; vertical-align:top; }}
    input, select {{ background:#0f1115; border:1px solid #2a2f3a; color:#e6e8eb; padding:7px; border-radius:8px; }}
    button {{ background:#2d6cdf; border:none; color:white; padding:8px 12px; border-radius:8px; cursor:pointer; }}
    .muted {{ color:#9aa4b2; }}
    .row {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
    .ok {{ color:#74d69a; }}
    .bad {{ color:#ff8a80; }}
    .warn {{ color:#ffd180; }}
    code {{ background:#0f1115; padding:2px 6px; border-radius:6px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="nav">
      <a href="/admin/dashboard">Дашборд</a>
      <a href="/admin/users">Пользователи</a>
      <a href="/admin/promos">Промокоды</a>
      <a href="/admin/settings">Настройки/сброс</a>
      {nav_auth}
    </div>
    {body}
  </div>
</body>
</html>
"""
    return HTMLResponse(page)


def _is_logged(request: Request) -> bool:
    return bool(request.session.get("wauth"))


def _auth_label(request: Request) -> str | None:
    auth = request.session.get("wauth")
    if not isinstance(auth, dict):
        return None
    return str(auth.get("label") or "admin")


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


@router.get("/login")
async def admin_login_page(request: Request) -> HTMLResponse:
    if _is_logged(request):
        return RedirectResponse("/admin/dashboard", status_code=303)
    body = """
    <div class="card"><h2>Вход в web-admin</h2>
      <p class="muted">Разрешен только вход через Telegram ID или GitHub login (из allowlist).</p>
      <div class="row">
        <form method="post" action="/admin/login/telegram">
          <input name="telegram_id" placeholder="Telegram ID" />
          <button type="submit">Войти через Telegram</button>
        </form>
      </div>
      <br />
      <div class="row">
        <form method="post" action="/admin/login/github">
          <input name="github_login" placeholder="GitHub login" />
          <button type="submit">Войти через GitHub</button>
        </form>
      </div>
    </div>
    """
    return _layout("Web-admin login", body)


@router.post("/login/telegram")
async def admin_login_telegram(request: Request, telegram_id: str = Form("")):
    if not telegram_id.strip().isdigit():
        return RedirectResponse("/admin/login", status_code=303)
    tid = int(telegram_id.strip())
    if not _admin_allowed_by_tg(tid):
        return RedirectResponse("/admin/login", status_code=303)
    request.session["wauth"] = {"kind": "telegram", "id": tid, "label": f"tg:{tid}"}
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.post("/login/github")
async def admin_login_github(request: Request, github_login: str = Form("")):
    login = github_login.strip()
    if not login or not _admin_allowed_by_gh(login):
        return RedirectResponse("/admin/login", status_code=303)
    request.session["wauth"] = {"kind": "github", "login": login, "label": f"gh:{login}"}
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
    body = f"""
    <div class="card"><h2>Доход</h2>
      <p>За все время: <b>{_esc(total_income)} ₽</b> · За месяц: <b>{_esc(month_income)} ₽</b> · За день: <b>{_esc(day_income)} ₽</b></p>
      <p class="muted">Учитываются только платежи ({'<code>type=topup,status=completed</code>'}).</p>
      <pre>{_esc(chr(10).join(bars))}</pre>
    </div>
    <div class="card"><h2>Сводка</h2>
      <p>Пользователи: <b>{users_count}</b></p>
      <p>Промокоды: <b>{promos_count}</b></p>
      <p class="muted">Разделы: пользователи, детали пользователя, промокоды, настройки/сброс БД.</p>
    </div>
    """
    return _layout("Web-admin Dashboard", body, user_label=_auth_label(request))


@router.get("/users")
async def admin_users(request: Request, q: str = "") -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    needle = q.strip()
    async with await _session() as session:
        query = select(User).order_by(desc(User.id)).limit(200)
        if needle:
            if needle.isdigit():
                tid = int(needle)
                query = query.where(or_(User.telegram_id == tid, User.id == tid))
            else:
                query = query.where(
                    or_(
                        User.username.ilike(f"%{needle}%"),
                        User.first_name.ilike(f"%{needle}%"),
                        User.last_name.ilike(f"%{needle}%"),
                    )
                )
        users = list((await session.execute(query)).scalars().all())
    rows = []
    for u in users:
        rows.append(
            f"<tr><td>{u.id}</td><td><a href='/admin/users/{u.id}'>{_esc(u.first_name or u.username or '-')}</a></td>"
            f"<td>{_esc(u.username or '-')}</td><td><code>{u.telegram_id}</code></td><td>{_esc(u.balance)}</td>"
            f"<td>{'🚫' if u.is_blocked else '✅'}</td></tr>"
        )
    body = (
        "<div class='card'><h2>Пользователи</h2>"
        "<form method='get' class='row'>"
        f"<input name='q' value='{_esc(needle)}' placeholder='ID, username, имя'/>"
        "<button type='submit'>Поиск</button></form><br/>"
        "<table><thead><tr><th>ID</th><th>Профиль</th><th>Username</th><th>Telegram ID</th><th>Баланс</th><th>Статус</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=6 class=muted>Нет данных</td></tr>'}</tbody></table></div>"
    )
    return _layout("Web-admin Users", body, user_label=_auth_label(request))


@router.get("/users/{user_id}")
async def admin_user_detail(request: Request, user_id: int) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return _layout("User not found", "<div class='card'><h2>Пользователь не найден</h2></div>")
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
    body = f"""
    <div class="card">
      <h2>Пользователь #{user.id}</h2>
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
    return _layout(f"User {user_id}", body, user_label=_auth_label(request))


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
    return _layout("Web-admin Settings", body, user_label=_auth_label(request))


@router.post("/settings/factory-reset")
async def admin_factory_reset(request: Request, confirm_text: str = Form("")) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    if confirm_text.strip() != "WIPE ALL":
        return _layout(
            "Reset rejected",
            "<div class='card'><h2>Сброс отменен</h2><p>Неверная фраза подтверждения.</p></div>",
            user_label=_auth_label(request),
        )
    async with await _session() as session:
        await wipe_all_application_data(session)
        await session.commit()
    return _layout(
        "Reset done",
        "<div class='card'><h2>База очищена</h2><p>Factory reset выполнен успешно.</p></div>",
        user_label=_auth_label(request),
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
    return _layout("Web-admin Promos", body, user_label=_auth_label(request))


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
    return _layout("New Promo", _promo_form(action="/admin/promos/new"), user_label=_auth_label(request))


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
            user_label=_auth_label(request),
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
            return _layout("Promo not found", "<div class='card'><h2>Промокод не найден</h2></div>")
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
    return _layout(f"Promo {promo_id}", body, user_label=_auth_label(request))


@router.get("/promos/{promo_id}/edit")
async def admin_promos_edit(request: Request, promo_id: int) -> HTMLResponse:
    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        promo = await session.get(PromoCode, promo_id)
    if promo is None:
        return _layout("Promo not found", "<div class='card'><h2>Промокод не найден</h2></div>")
    return _layout(
        "Edit Promo",
        _promo_form(action=f"/admin/promos/{promo_id}/edit", promo=promo),
        user_label=_auth_label(request),
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
            return _layout("Promo not found", "<div class='card'><h2>Промокод не найден</h2></div>")
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
                user_label=_auth_label(request),
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

