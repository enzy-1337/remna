"""Публичные страницы возврата после оплаты."""

from __future__ import annotations

import asyncio
import html
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from shared.config import get_settings
from shared.database import get_session_factory
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.subscription import Subscription
from shared.models.user import User
from shared.services.billing_v2.balance_runway_service import compute_balance_runway
from shared.services.billing_v2.detail_service import user_has_tariff_subscription_charges
from shared.services.topup_service import create_topup_payment
from shared.services.subscription_service import get_active_subscription

router = APIRouter(tags=["public-pages"])
logger = logging.getLogger(__name__)


def _esc(s: str) -> str:
    return html.escape(s)


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y")


def _days_left(exp: datetime | None) -> int:
    if exp is None:
        return 0
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    left = exp - datetime.now(timezone.utc)
    return max(0, int(left.total_seconds() // 86400))


def _ru_days_phrase(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} дн."
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return f"{n} дн."
    return f"{n} дн."


def _token_from_subscription_url(url: str | None) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    path = (parsed.path or "").rstrip("/")
    if "/sub/" not in path:
        return None
    return path.split("/sub/", 1)[-1].strip() or None


def _balance_status(balance: Decimal | int | float | None) -> tuple[str, str]:
    try:
        b = Decimal(str(balance or "0"))
    except Exception:
        b = Decimal("0")
    if b > 0:
        return "POSITIVE", "Положительный"
    return "EMPTY", "Низкий"


def _format_rub(balance: Decimal | int | float | None) -> str:
    try:
        b = Decimal(str(balance or "0")).quantize(Decimal("0.01"))
    except Exception:
        b = Decimal("0.00")
    return f"{b} ₽"


def _subscription_page(
    *,
    token: str,
    headline_value: str,
    headline_hint: str | None,
    expires_at: datetime | None,
    created_at: datetime | None,
    balance_rub: Decimal | int | float | None,
    bot_open_url: str | None,
) -> HTMLResponse:
    bot_href = _esc((bot_open_url or "").strip()) or "#"
    bot_btn_disabled = " opacity-60 pointer-events-none" if not (bot_open_url or "").strip() else ""
    topup_href = f"/sub/{_esc(token)}/topup"
    active_badge = "Активна"
    hint_html = f'<div class="subid">{_esc(headline_hint or "")}</div>' if headline_hint else ""
    page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Flux Network — подписка</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #060a1b;
      --card: #0b1328;
      --card2: #0f1a34;
      --line: rgba(148, 163, 184, 0.15);
      --text: #e8edf6;
      --muted: #95a3bf;
      --blue: #2b78ff;
      --blue2: #1f68e8;
      --green: #22c55e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      background: radial-gradient(circle at top, #0d1733 0%, var(--bg) 45%);
      color: var(--text);
      min-height: 100vh;
    }}
    .wrap {{
      max-width: 430px;
      margin: 0 auto;
      padding: 20px 14px 24px;
    }}
    .brand {{
      font-size: 30px;
      font-weight: 700;
      margin: 4px 0 14px;
      letter-spacing: 0.2px;
    }}
    .brand span {{ color: #f4cc44; margin-right: 6px; }}
    .card {{
      background: linear-gradient(180deg, var(--card), #091021);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      margin-bottom: 12px;
    }}
    .badge {{
      display: inline-block;
      background: rgba(34, 197, 94, 0.17);
      color: #7ff0a8;
      border: 1px solid rgba(34, 197, 94, 0.3);
      padding: 6px 11px;
      border-radius: 999px;
      font-size: 14px;
      margin-bottom: 10px;
    }}
    .days {{
      font-size: 44px;
      line-height: 1;
      font-weight: 700;
      margin: 4px 0 6px;
    }}
    .subid {{ color: var(--muted); font-size: 16px; }}
    .title {{
      color: var(--muted);
      font-size: 14px;
      letter-spacing: 0.9px;
      text-transform: uppercase;
      margin-bottom: 8px;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 0;
      border-top: 1px solid var(--line);
      font-size: 20px;
    }}
    .row:first-of-type {{ border-top: 0; }}
    .label {{ color: var(--muted); }}
    .value {{ text-align: right; font-weight: 600; }}
    .actions {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }}
    .btn {{
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 52px;
      border-radius: 12px;
      text-decoration: none;
      font-size: 22px;
      font-weight: 600;
      border: 1px solid rgba(255,255,255,0.12);
    }}
    .btn-primary {{
      background: linear-gradient(180deg, var(--blue), var(--blue2));
      color: #fff;
      border: 0;
    }}
    .btn-outline {{
      background: transparent;
      color: #d8e3fa;
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <h1 class="brand"><span>⚡</span>Flux Network</h1>
    <section class="card">
      <div class="badge">{_esc(active_badge)}</div>
      <div class="days">{_esc(headline_value)}</div>
      {hint_html}
    </section>
    <section class="card">
      <div class="title">Детализация</div>
      <div class="row"><div class="label">Действует до</div><div class="value">{_esc(_fmt_dt(expires_at))}</div></div>
      <div class="row"><div class="label">Создана</div><div class="value">{_esc(_fmt_dt(created_at))}</div></div>
      <div class="row"><div class="label">Баланс</div><div class="value">{_esc(_format_rub(balance_rub))}</div></div>
    </section>
    <section class="actions">
      <a class="btn btn-outline" href="{topup_href}">Пополнить баланс</a>
      <a class="btn btn-primary{bot_btn_disabled}" href="{bot_href}">Перейти в бота</a>
    </section>
  </main>
</body>
</html>"""
    return HTMLResponse(page)


def _topup_page(
    *,
    token: str,
    balance_rub: Decimal | int | float | None,
    min_topup_rub: Decimal,
    error_message: str | None = None,
) -> HTMLResponse:
    form_action = f"/sub/{_esc(token)}/topup"
    back_href = f"/sub/{_esc(token)}"
    error_html = (
        f'<div class="error-box">{_esc(error_message or "")}</div>'
        if error_message
        else ""
    )
    page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Flux Network — пополнение</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #060a1b;
      --card: #0b1328;
      --line: rgba(148, 163, 184, 0.15);
      --text: #e8edf6;
      --muted: #95a3bf;
      --blue: #2b78ff;
      --blue2: #1f68e8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      background: radial-gradient(circle at top, #0d1733 0%, var(--bg) 45%);
      color: var(--text);
      min-height: 100vh;
    }}
    .wrap {{ max-width: 430px; margin: 0 auto; padding: 20px 14px 24px; }}
    .brand {{ font-size: 30px; font-weight: 700; margin: 4px 0 14px; }}
    .brand span {{ color: #f4cc44; margin-right: 6px; }}
    .card {{
      background: linear-gradient(180deg, var(--card), #091021);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      margin-bottom: 12px;
    }}
    .title {{ color: var(--muted); font-size: 14px; letter-spacing: 0.9px; text-transform: uppercase; margin-bottom: 8px; }}
    .balance {{ font-size: 34px; font-weight: 700; }}
    .amounts {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 10px; }}
    .amount-chip {{
      appearance: none; border: 1px solid var(--line); background: #0c1730; color: #d9e5ff;
      border-radius: 10px; padding: 10px 6px; text-align: center; font-size: 16px; cursor: pointer;
    }}
    .amount-chip.active {{ border-color: #4e8bff; background: #10244d; color: #fff; }}
    .field {{
      width: 100%; border: 1px solid var(--line); background: #0c1730; color: #fff;
      border-radius: 10px; padding: 12px; font-size: 18px; margin-top: 10px;
    }}
    .btn {{
      display: flex; justify-content: center; align-items: center; min-height: 52px; border-radius: 12px;
      text-decoration: none; font-size: 22px; font-weight: 600; border: 0; width: 100%; margin-top: 10px;
      background: linear-gradient(180deg, var(--blue), var(--blue2)); color: #fff; cursor: pointer;
    }}
    .btn-back {{
      display: flex; justify-content: center; align-items: center; min-height: 46px;
      border-radius: 12px; text-decoration: none; font-size: 18px; font-weight: 600;
      width: 100%; margin-top: 8px; border: 1px solid var(--line); color: #d8e3fa; background: #0c1730;
    }}
    .error-box {{
      margin-bottom: 10px; border: 1px solid rgba(239,68,68,.45); background: rgba(239,68,68,.12);
      color: #ffc5c5; border-radius: 10px; padding: 10px 12px; font-size: 14px;
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <h1 class="brand"><span>⚡</span>Flux Network</h1>
    <section class="card">
      <div class="title">Текущий баланс</div>
      <div class="balance">{_esc(_format_rub(balance_rub))}</div>
    </section>
    <section class="card">
      {error_html}
      <form method="post" action="{form_action}">
        <div class="title">Выберите сумму</div>
        <input type="hidden" name="preset_amount" id="preset_amount" value="100">
        <div class="amounts">
          <button type="button" class="amount-chip active" data-amount="100">100 ₽</button>
          <button type="button" class="amount-chip" data-amount="300">300 ₽</button>
          <button type="button" class="amount-chip" data-amount="500">500 ₽</button>
        </div>
        <input class="field" type="number" min="{_esc(str(min_topup_rub))}" step="1" name="custom_amount" placeholder="Или введите сумму вручную">
        <button type="submit" class="btn">Оплатить</button>
      </form>
      <a class="btn-back" href="{back_href}">Назад</a>
    </section>
  </main>
  <script>
    const chips = document.querySelectorAll('.amount-chip');
    const preset = document.getElementById('preset_amount');
    chips.forEach((chip) => {{
      chip.addEventListener('click', () => {{
        chips.forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        preset.value = chip.dataset.amount || '100';
      }});
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(page)


async def _resolve_subscription_context(subscription_key: str) -> tuple[dict | None, User | None, Subscription | None]:
    token = (subscription_key or "").strip()
    if not token:
        return None, None, None
    settings = get_settings()
    rw = RemnaWaveClient(settings)
    fast_timeout = max(12.0, float(settings.remnawave_request_timeout) + 3.0)
    deep_timeout = max(40.0, fast_timeout * 3.0)
    panel_user: dict | None = None
    try:
        users = await asyncio.wait_for(rw.list_users(limit=1000), timeout=fast_timeout)
        logger.info("public-sub: fast scan users=%s token=%s", len(users), token)
        for item in users:
            url_token = _token_from_subscription_url(str(item.get("subscriptionUrl") or ""))
            if url_token and url_token == token:
                panel_user = item
                break
            if str(item.get("shortUuid") or "").strip() == token:
                panel_user = item
                break
            if str(item.get("uuid") or "").strip() == token:
                panel_user = item
                break
        if panel_user is None:
            users_all = await asyncio.wait_for(
                rw.list_all_users(page_size=500, max_items=50000, max_pages=400),
                timeout=deep_timeout,
            )
            logger.info("public-sub: deep scan users=%s token=%s", len(users_all), token)
            for item in users_all:
                url_token = _token_from_subscription_url(str(item.get("subscriptionUrl") or ""))
                if url_token and url_token == token:
                    panel_user = item
                    break
                if str(item.get("shortUuid") or "").strip() == token:
                    panel_user = item
                    break
                if str(item.get("uuid") or "").strip() == token:
                    panel_user = item
                    break
        if panel_user is None:
            try:
                uuid.UUID(token)
                one = await asyncio.wait_for(rw.get_user(token), timeout=4.0)
                if isinstance(one, dict) and one:
                    panel_user = one
                    logger.info("public-sub: direct get_user hit token=%s", token)
            except Exception:
                pass
    except RemnaWaveError:
        logger.exception("public-sub: remnawave error token=%s", token)
        panel_user = None
    except asyncio.TimeoutError:
        logger.warning("public-sub: timeout token=%s", token)
        panel_user = None
    if panel_user is None:
        return None, None, None

    db_user: User | None = None
    sub: Subscription | None = None
    raw_tg = panel_user.get("telegramId")
    raw_uuid = str(panel_user.get("uuid") or "").strip()
    factory = get_session_factory()
    async with factory() as session:
        if raw_tg not in (None, ""):
            try:
                tg_id = int(str(raw_tg))
                db_user = (
                    await session.execute(select(User).where(User.telegram_id == tg_id).limit(1))
                ).scalar_one_or_none()
            except Exception:
                db_user = None
        if db_user is None and raw_uuid:
            try:
                db_user = (
                    await session.execute(
                        select(User).where(User.remnawave_uuid == uuid.UUID(raw_uuid)).limit(1)
                    )
                ).scalar_one_or_none()
            except Exception:
                db_user = None
        if db_user is not None:
            sub = await get_active_subscription(session, db_user.id)
            if sub is None:
                sub = (
                    await session.execute(
                        select(Subscription)
                        .where(Subscription.user_id == db_user.id)
                        .order_by(Subscription.expires_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
    return panel_user, db_user, sub


def _page(title: str, message: str, *, variant: str, badge: str, footer: str) -> HTMLResponse:
    """variant: success | error | neutral"""
    if variant == "success":
        icon = "fa-circle-check text-success"
        accent = "from-success/25 via-primary/20 to-secondary/25"
        alert = "alert-success"
    elif variant == "error":
        icon = "fa-circle-xmark text-error"
        accent = "from-error/25 via-primary/20 to-secondary/25"
        alert = "alert-error"
    else:
        icon = "fa-wave-square text-primary"
        accent = "from-primary/25 via-secondary/20 to-accent/25"
        alert = "alert-info"
    page = f"""<!DOCTYPE html>
<html lang="ru" data-theme="night">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_esc(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" crossorigin="anonymous" referrerpolicy="no-referrer" />
  <link href="https://cdn.jsdelivr.net/npm/daisyui@4.12.14/dist/full.min.css" rel="stylesheet" type="text/css" />
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen overflow-hidden bg-[#0a0715] text-base-content antialiased">
  <div class="pointer-events-none fixed inset-0">
    <div class="absolute inset-0 bg-[radial-gradient(circle_at_top,rgba(146,112,255,0.22),transparent_38%),radial-gradient(circle_at_bottom_right,rgba(67,97,238,0.18),transparent_30%),linear-gradient(145deg,#0a0715,#120d26_45%,#1f133f)]"></div>
    <div class="absolute left-[8%] top-[12%] h-40 w-40 rounded-full bg-white/6 blur-3xl"></div>
    <div class="absolute right-[10%] top-[18%] h-56 w-56 rounded-full bg-secondary/20 blur-3xl"></div>
    <div class="absolute bottom-[10%] left-[12%] h-52 w-52 rounded-full bg-primary/20 blur-3xl"></div>
  </div>
  <main class="relative flex min-h-screen items-center justify-center p-6">
    <div class="card w-full max-w-xl overflow-hidden border border-white/10 bg-base-100/90 shadow-[0_30px_80px_-30px_rgba(0,0,0,0.75)] backdrop-blur">
      <div class="h-1.5 w-full bg-gradient-to-r {accent}"></div>
      <div class="card-body items-center gap-5 px-7 py-8 text-center">
        <div class="badge badge-outline badge-lg border-white/15 bg-base-300/50 px-4 py-3 font-medium">{_esc(badge)}</div>
        <i class="fa-solid {icon} text-5xl" aria-hidden="true"></i>
        <h1 class="text-2xl font-bold sm:text-3xl">{_esc(title)}</h1>
        <p class="max-w-lg text-sm leading-relaxed text-base-content/70 sm:text-base">{_esc(message)}</p>
        <div class="alert {alert} border border-white/10 bg-base-200/60 text-sm">{_esc(footer)}</div>
      </div>
    </div>
  </main>
</body>
</html>
"""
    return HTMLResponse(page)


def render_public_stub_page() -> HTMLResponse:
    return _page(
        "Flux Network Web Admin",
        "Сервис доступен, но публичной страницы здесь нет. Этот адрес используется как точка входа для служебных и административных сценариев.",
        variant="neutral",
        badge="WEBA",
        footer="Откройте только нужный вам адрес или вернитесь туда, откуда пришли.",
    )


def render_not_found_page(path: str) -> HTMLResponse:
    shown = path if path.startswith("/") else f"/{path}"
    page = _page(
        "Страница не найдена",
        f"Адрес {shown} не существует или был перемещен. Проверьте путь и попробуйте снова.",
        variant="error",
        badge="404",
        footer="По этому адресу ничего нет.",
    )
    page.status_code = 404
    return page


@router.get("/")
async def public_stub(_request: Request) -> HTMLResponse:
    return render_public_stub_page()


@router.get("/payment/success")
async def payment_success() -> HTMLResponse:
    return _page(
        "Оплата прошла успешно",
        "Платеж подтвержден. Вернитесь в Telegram-бот, чтобы продолжить работу с подпиской.",
        variant="success",
        badge="SUCCESS",
        footer="Вернитесь в Telegram-бот.",
    )


@router.get("/payment/fail")
async def payment_fail() -> HTMLResponse:
    return _page(
        "Оплата не завершена",
        "Платеж не был подтвержден. Попробуйте снова или выберите другой способ оплаты в боте.",
        variant="error",
        badge="ERROR",
        footer="Вернитесь в Telegram-бот и попробуйте еще раз.",
    )


@router.get("/sub/{subscription_key}")
@router.head("/sub/{subscription_key}")
async def public_subscription_card(subscription_key: str) -> HTMLResponse:
    token = (subscription_key or "").strip()
    if not token:
        return render_not_found_page("/sub/<empty>")
    logger.info("public-sub: request token=%s", token)
    settings = get_settings()
    panel_user, db_user, sub = await _resolve_subscription_context(token)
    if panel_user is None:
        logger.info("public-sub: not found token=%s", token)
        return render_not_found_page(f"/sub/{token}")

    expires_at = sub.expires_at if sub is not None else None
    created_at = sub.created_at if sub is not None else None
    _balance_code, _balance_human = _balance_status(db_user.balance if db_user is not None else 0)
    headline_value = _ru_days_phrase(_days_left(expires_at))
    headline_hint: str | None = None
    if db_user is not None and settings.billing_v2_enabled and db_user.billing_mode == "hybrid":
        factory = get_session_factory()
        async with factory() as session:
            user_fresh = await session.get(User, db_user.id)
            if user_fresh is not None:
                has_tariff_charges = await user_has_tariff_subscription_charges(session, user_fresh.id)
                if not has_tariff_charges:
                    runway = await compute_balance_runway(session, user=user_fresh, settings=settings)
                    if runway is not None:
                        headline_value = f"~{runway.estimated_days_int} дн."
                        headline_hint = "PAYG: ориентировочно по текущим списаниям"
                    else:
                        headline_value = "~н/д"
                        headline_hint = "PAYG: прогноз появится после накопления статистики"
    bot_open_url: str | None = None
    bot_username = (settings.bot_username or "").strip().lstrip("@")
    if bot_username:
        bot_open_url = f"https://t.me/{bot_username}"
    return _subscription_page(
        token=token,
        headline_value=headline_value,
        headline_hint=headline_hint,
        expires_at=expires_at,
        created_at=created_at,
        balance_rub=db_user.balance if db_user is not None else Decimal("0"),
        bot_open_url=bot_open_url,
    )


@router.get("/sub/{subscription_key}/topup")
async def public_subscription_topup_page(subscription_key: str) -> HTMLResponse:
    token = (subscription_key or "").strip()
    if not token:
        return render_not_found_page("/sub/<empty>")
    settings = get_settings()
    panel_user, db_user, _sub = await _resolve_subscription_context(token)
    if panel_user is None:
        return render_not_found_page(f"/sub/{token}")
    return _topup_page(
        token=token,
        balance_rub=db_user.balance if db_user is not None else Decimal("0"),
        min_topup_rub=settings.billing_min_topup_rub,
    )


@router.post("/sub/{subscription_key}/topup")
async def public_subscription_topup(
    subscription_key: str,
    preset_amount: str = Form("100"),
    custom_amount: str = Form(""),
) -> Response:
    token = (subscription_key or "").strip()
    if not token:
        return render_not_found_page("/sub/<empty>")
    settings = get_settings()
    panel_user, db_user, sub = await _resolve_subscription_context(token)
    if panel_user is None:
        return render_not_found_page(f"/sub/{token}")
    if db_user is None:
        return _topup_page(
            token=token,
            balance_rub=Decimal("0"),
            min_topup_rub=settings.billing_min_topup_rub,
            error_message="Не удалось связать подписку с пользователем в БД.",
        )
    platega_ready = settings.platega_stub or bool(
        (settings.platega_merchant_id or "").strip() and (settings.platega_secret_key or "").strip()
    )
    if not platega_ready:
        return _topup_page(
            token=token,
            balance_rub=db_user.balance,
            min_topup_rub=settings.billing_min_topup_rub,
            error_message="Platega не настроена на сервере.",
        )

    amount_raw = (custom_amount or "").strip() or (preset_amount or "").strip()
    try:
        amount = Decimal(amount_raw)
    except Exception:
        amount = Decimal("0")
    if amount < settings.billing_min_topup_rub:
        return _topup_page(
            token=token,
            balance_rub=db_user.balance,
            min_topup_rub=settings.billing_min_topup_rub,
            error_message=f"Минимальная сумма пополнения: {settings.billing_min_topup_rub} ₽",
        )

    factory = get_session_factory()
    try:
        async with factory() as session:
            user = await session.get(User, db_user.id)
            if user is None:
                raise ValueError("Пользователь не найден.")
            _txn, pay_url = await create_topup_payment(
                session,
                user=user,
                telegram_id=int(user.telegram_id),
                amount_rub=amount,
                provider_name="platega",
                settings=settings,
            )
            await session.commit()
    except Exception as exc:
        logger.exception("public-sub: create topup failed token=%s", token)
        return _topup_page(
            token=token,
            balance_rub=db_user.balance,
            min_topup_rub=settings.billing_min_topup_rub,
            error_message=f"Не удалось создать платеж: {str(exc)[:160]}",
        )
    return RedirectResponse(url=pay_url, status_code=status.HTTP_303_SEE_OTHER)
