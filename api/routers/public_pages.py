"""Публичные страницы возврата после оплаты."""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sqlalchemy import text

from shared.config import get_settings
from shared.database import get_session_factory
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.subscription import Subscription
from shared.models.user import User
from shared.tickets_db_compat import (
    ticket_messages_has_document_columns,
    ticket_messages_has_photo_file_id_column,
    ticket_messages_has_video_file_id_column,
)
from shared.services.billing_v2.balance_runway_service import compute_balance_runway
from shared.services.billing_v2.detail_service import user_has_tariff_subscription_charges
from shared.models.plan import Plan
from shared.services.feature_flags import tariff_purchases_enabled
from shared.services.topup_service import create_topup_payment
from shared.services.subscription_service import (
    can_renew_subscription_with_tariff,
    get_active_subscription,
    list_paid_plans,
    plan_tariff_button_label,
    purchase_plan_with_balance,
    resolve_plan_price_rub,
    subscription_days_left,
)
from tickets.config import config as tickets_config
from tickets.services import (
    add_ticket_message,
    bump_ticket_activity,
    create_ticket,
    get_active_ticket_id,
    open_ticket_forum_topic,
)

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


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")


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
    show_renew_button: bool = False,
) -> HTMLResponse:
    bot_href = _esc((bot_open_url or "").strip()) or "#"
    bot_btn_disabled = " opacity-60 pointer-events-none" if not (bot_open_url or "").strip() else ""
    topup_href = f"/sub/{_esc(token)}/topup"
    renew_href = f"/sub/{_esc(token)}/renew"
    renew_btn_html = (
        f'<a class="btn btn-primary" href="{renew_href}">Продлить подписку</a>'
        if show_renew_button
        else ""
    )
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
      margin-bottom: 90px;
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
    .hotbar {{
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      border-top: 1px solid var(--line);
      background: rgba(7, 12, 28, 0.95);
      backdrop-filter: blur(8px);
      padding: 10px 16px calc(10px + env(safe-area-inset-bottom));
    }}
    .hotbar-inner {{
      max-width: 430px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }}
    .hotbtn {{
      text-decoration: none;
      color: #c8d5ef;
      border: 1px solid var(--line);
      border-radius: 12px;
      min-height: 46px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 15px;
      font-weight: 600;
      background: rgba(15, 26, 52, 0.55);
    }}
    .hotbtn.active {{
      color: #fff;
      border-color: rgba(86, 135, 255, 0.45);
      background: rgba(39, 101, 224, 0.35);
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
      {renew_btn_html}
      <a class="btn btn-outline" href="{topup_href}">Пополнить баланс</a>
      <a class="btn btn-primary{bot_btn_disabled}" href="{bot_href}">Перейти в бота</a>
    </section>
  </main>
  <nav class="hotbar">
    <div class="hotbar-inner">
      <a class="hotbtn active" href="/sub/{_esc(token)}">Моя подписка</a>
      <a class="hotbtn" href="/sub/{_esc(token)}/support">Поддержка</a>
    </div>
  </nav>
</body>
</html>"""
    return HTMLResponse(page)


def _renew_page(
    *,
    token: str,
    plans: list[tuple[Plan, str]],
    can_renew: bool,
    days_left: int,
    window_days: int,
    balance_rub: Decimal | int | float | None,
    error_message: str | None = None,
) -> HTMLResponse:
    back_href = f"/sub/{_esc(token)}"
    form_action = f"/sub/{_esc(token)}/renew"
    error_html = (
        f'<div class="error-box">{_esc(error_message or "")}</div>'
        if error_message
        else ""
    )
    if can_renew:
        hint = "Выберите тариф (оплата с баланса)."
    else:
        hint = (
            f"Продление доступно только за {window_days} дн. до окончания подписки. "
            f"Сейчас осталось {days_left} дн."
        )
    plan_buttons: list[str] = []
    for p, label in plans:
        label_esc = _esc(label)
        if can_renew:
            plan_buttons.append(
                f'<button type="submit" name="plan_id" value="{p.id}" class="btn btn-tariff">{label_esc}</button>'
            )
        else:
            plan_buttons.append(
                f'<button type="button" class="btn btn-tariff btn-tariff-disabled" disabled>{label_esc}</button>'
            )
    plans_html = "\n".join(plan_buttons) or '<p class="muted">Нет доступных тарифов.</p>'
    page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Flux Network — продление</title>
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
    .wrap {{ max-width: 430px; margin: 0 auto; padding: 20px 14px 90px; }}
    .brand {{ font-size: 28px; font-weight: 700; margin: 4px 0 14px; }}
    .brand span {{ color: #f4cc44; margin-right: 6px; }}
    .card {{
      background: linear-gradient(180deg, var(--card), #091021);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      margin-bottom: 12px;
    }}
    .muted {{ color: var(--muted); font-size: 15px; line-height: 1.45; }}
    .error-box {{
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.35);
      color: #fecaca;
      padding: 10px 12px;
      border-radius: 10px;
      margin-bottom: 12px;
      font-size: 14px;
    }}
    .tariffs {{ display: grid; gap: 10px; margin-top: 14px; }}
    .btn-tariff {{
      width: 100%;
      min-height: 52px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(15, 26, 52, 0.85);
      color: #e8edf6;
      font-size: 18px;
      font-weight: 600;
      cursor: pointer;
    }}
    .btn-tariff-disabled {{
      opacity: 0.45;
      cursor: not-allowed;
      color: #8b9bb8;
      background: rgba(30, 40, 60, 0.5);
    }}
    .btn-back {{
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 48px;
      margin-top: 14px;
      border-radius: 12px;
      text-decoration: none;
      color: #d8e3fa;
      border: 1px solid var(--line);
      font-size: 18px;
      font-weight: 600;
    }}
    .hotbar {{
      position: fixed; left: 0; right: 0; bottom: 0;
      border-top: 1px solid var(--line);
      background: rgba(7, 12, 28, 0.95);
      padding: 10px 16px calc(10px + env(safe-area-inset-bottom));
    }}
    .hotbar-inner {{
      max-width: 430px; margin: 0 auto;
      display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
    }}
    .hotbtn {{
      text-decoration: none; color: #c8d5ef;
      border: 1px solid var(--line); border-radius: 12px;
      min-height: 46px; display: flex; align-items: center; justify-content: center;
      font-size: 15px; font-weight: 600;
      background: rgba(15, 26, 52, 0.55);
    }}
    .hotbtn.active {{
      color: #fff;
      border-color: rgba(86, 135, 255, 0.45);
      background: rgba(39, 101, 224, 0.35);
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <h1 class="brand"><span>⚡</span>Flux Network</h1>
    <section class="card">
      <h2 style="margin:0 0 8px;font-size:22px;">Продлить подписку</h2>
      <p class="muted">{_esc(hint)}</p>
      <p class="muted" style="margin-top:8px;">Баланс: {_esc(_format_rub(balance_rub))}</p>
      {error_html}
      <form method="post" action="{form_action}" class="tariffs">
        {plans_html}
      </form>
      <a class="btn-back" href="{back_href}">Назад</a>
    </section>
  </main>
  <nav class="hotbar">
    <div class="hotbar-inner">
      <a class="hotbtn" href="{back_href}">Моя подписка</a>
      <a class="hotbtn" href="/sub/{_esc(token)}/support">Поддержка</a>
    </div>
  </nav>
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


def _support_page(token: str) -> HTMLResponse:
    token_esc = _esc(token)
    page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Flux Network — поддержка</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg:#060a1b; --line:rgba(148,163,184,.16); --text:#e8edf6; --muted:#95a3bf;
      --accent:#7c6cff; --chip:#111a30; --my:#1a2440; --admin:#1f3569;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0; min-height:100vh; color:var(--text);
      font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
      background: radial-gradient(circle at top, #0d1733 0%, var(--bg) 45%);
    }}
    .wrap {{ max-width:430px; margin:0 auto; min-height:100vh; display:flex; flex-direction:column; }}
    .head {{
      position:sticky;
      top:0;
      z-index:20;
      padding:14px 14px 8px;
      background:linear-gradient(180deg, rgba(6,10,27,.96), rgba(6,10,27,.82));
      backdrop-filter: blur(6px);
      border-bottom:1px solid var(--line);
    }}
    .brand {{ font-weight:700; font-size:22px; }}
    .sub {{ display:flex; align-items:center; justify-content:space-between; margin-top:4px; }}
    .subtitle {{ font-size:14px; color:var(--muted); }}
    .online {{ border:1px solid rgba(34,197,94,.45); color:#7ff0a8; border-radius:999px; padding:4px 10px; font-size:12px; display:flex; align-items:center; gap:6px; }}
    .dot {{ width:8px; height:8px; border-radius:50%; background:#22c55e; }}
    .chat {{ flex:1; overflow:auto; padding:8px 14px 150px; }}
    .msg-row {{ display:flex; margin:8px 0; }}
    .msg-row.me {{ justify-content:flex-end; }}
    .bubble {{ max-width:84%; border:1px solid var(--line); border-radius:12px; padding:10px 12px; font-size:14px; background:var(--admin); }}
    .msg-row.me .bubble {{ background:var(--my); }}
    .ts {{ margin-top:4px; font-size:11px; color:var(--muted); }}
    .msg-media {{ margin-top:8px; }}
    .msg-media img {{
      display:block;
      max-width:100%;
      max-height:320px;
      border-radius:10px;
      border:1px solid var(--line);
      object-fit:contain;
      background:rgba(255,255,255,.03);
      cursor:zoom-in;
    }}
    .lb {{
      position:fixed; inset:0; z-index:60; display:none; align-items:center; justify-content:center;
      background:rgba(0,0,0,.85); padding:20px;
    }}
    .lb.open {{ display:flex; }}
    .lb img {{
      max-width:min(95vw,1280px); max-height:92vh; border-radius:12px; border:1px solid var(--line);
      background:rgba(255,255,255,.03); object-fit:contain;
    }}
    .lb-close {{
      position:absolute; top:14px; right:14px; width:38px; height:38px; border-radius:10px;
      border:1px solid var(--line); background:rgba(15,26,52,.75); color:#fff; cursor:pointer;
    }}
    .composer {{
      position:fixed; left:0; right:0; bottom:62px; background:rgba(7,12,28,.95); border-top:1px solid var(--line);
      padding:10px 14px calc(10px + env(safe-area-inset-bottom));
      z-index:30;
    }}
    .composer-inner {{ max-width:430px; margin:0 auto; display:grid; grid-template-columns:44px 1fr 44px; gap:8px; align-items:center; }}
    .iconbtn {{ width:44px; height:44px; border-radius:12px; border:1px solid var(--line); background:#0d1730; color:#dbe6ff; display:flex; align-items:center; justify-content:center; cursor:pointer; }}
    .input {{ width:100%; height:44px; border-radius:12px; border:1px solid var(--line); background:#0b142b; color:#fff; padding:10px 12px; }}
    .attach-info {{ max-width:430px; margin:6px auto 0; color:var(--muted); font-size:12px; }}
    .hotbar {{
      position:fixed; left:0; right:0; bottom:0; border-top:1px solid var(--line);
      background:rgba(7,12,28,.95); padding:8px 14px calc(8px + env(safe-area-inset-bottom));
      z-index:35;
    }}
    .hotbar-inner {{ max-width:430px; margin:0 auto; display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
    .hotbtn {{ text-decoration:none; color:#c8d5ef; border:1px solid var(--line); border-radius:12px; min-height:44px; display:flex; align-items:center; justify-content:center; font-size:14px; font-weight:600; background:rgba(15,26,52,.55); }}
    .hotbtn.active {{ color:#fff; border-color:rgba(124,108,255,.55); background:rgba(124,108,255,.25); }}
  </style>
</head>
<body>
  <main class="wrap">
    <header class="head">
      <div class="brand">Flux Network</div>
      <div class="sub">
        <div class="subtitle">Поддержка</div>
        <div class="online"><span class="dot"></span>Онлайн</div>
      </div>
    </header>
    <section id="chat" class="chat"><div class="ts">Загрузка чата...</div></section>
  </main>
  <div class="composer">
    <div class="composer-inner">
      <button id="pick" class="iconbtn" type="button" title="Прикрепить фото">📎</button>
      <input id="txt" class="input" type="text" placeholder="Написать сообщение..." maxlength="4000" />
      <button id="send" class="iconbtn" type="button">➤</button>
      <input id="file" type="file" accept="image/*" style="display:none" />
    </div>
    <div id="attach-info" class="attach-info"></div>
  </div>
  <nav class="hotbar">
    <div class="hotbar-inner">
      <a class="hotbtn" href="/sub/{token_esc}">Моя подписка</a>
      <a class="hotbtn active" href="/sub/{token_esc}/support">Поддержка</a>
    </div>
  </nav>
  <div id="img-lb" class="lb" role="dialog" aria-modal="true" aria-label="Просмотр фото">
    <button id="img-lb-close" class="lb-close" type="button">✕</button>
    <img id="img-lb-src" src="" alt="Фото"/>
  </div>
  <script>
    const token = {token_esc!r};
    const chat = document.getElementById('chat');
    const txt = document.getElementById('txt');
    const send = document.getElementById('send');
    const pick = document.getElementById('pick');
    const file = document.getElementById('file');
    const attachInfo = document.getElementById('attach-info');
    const lb = document.getElementById('img-lb');
    const lbImg = document.getElementById('img-lb-src');
    const lbClose = document.getElementById('img-lb-close');
    let lastSig = '';
    let notifyInit = false;
    let lastCount = 0;
    function esc(s) {{ return String(s||'').replace(/[&<>"]/g, c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c])); }}
    function playNotifyTone() {{
      try {{
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        const ctx = new Ctx();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = 880;
        gain.gain.value = 0.0001;
        osc.connect(gain); gain.connect(ctx.destination);
        const now = ctx.currentTime;
        gain.gain.exponentialRampToValueAtTime(0.07, now + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.22);
        osc.start(now);
        osc.stop(now + 0.24);
      }} catch(_e) {{}}
    }}
    function openImageModal(src) {{
      if (!lb || !lbImg || !src) return;
      lbImg.src = src;
      lb.classList.add('open');
      document.body.style.overflow = 'hidden';
    }}
    function closeImageModal() {{
      if (!lb || !lbImg) return;
      lb.classList.remove('open');
      lbImg.removeAttribute('src');
      document.body.style.overflow = '';
    }}
    function render(data) {{
      const msgs = data.messages || [];
      const sig = JSON.stringify(msgs.map(m=>[m.id,m.created_at,m.text,m.sender_role,m.photo_file_id,m.document_file_id]));
      if (sig === lastSig) return;
      const hadNew = notifyInit && msgs.length > lastCount;
      if (hadNew) {{
        const last = msgs[msgs.length - 1];
        if (last && last.sender_role !== 'user' && !document.hidden) {{
          playNotifyTone();
        }}
      }}
      lastCount = msgs.length;
      notifyInit = true;
      lastSig = sig;
      if (!msgs.length) {{
        chat.innerHTML = '<div class="ts">Пока нет сообщений. Напишите первым.</div>'; return;
      }}
      chat.innerHTML = msgs.map(m => {{
        const me = m.sender_role === 'user';
        const text = m.text ? '<div>'+esc(m.text).replace(/\\n/g,'<br>')+'</div>' : '';
        const photo = m.photo_file_id
          ? '<div class="msg-media"><img data-photo-src="/sub/'+token+'/support/media/'+m.id+'/photo" src="/sub/'+token+'/support/media/'+m.id+'/photo" alt="Фото" loading="lazy" decoding="async"></div>'
          : '';
        const doc = m.document_file_id ? '<div class="ts"><a target="_blank" href="/sub/'+token+'/support/media/'+m.id+'/document">'+esc(m.document_file_name||'Документ')+'</a></div>' : '';
        return '<div class="msg-row '+(me?'me':'')+'"><div class="bubble">'+text+photo+doc+'<div class="ts">'+esc(m.created_at||'')+'</div></div></div>';
      }}).join('');
      chat.scrollTop = chat.scrollHeight;
    }}
    async function load() {{
      try {{
        const r = await fetch('/sub/'+token+'/support/messages');
        if (!r.ok) return;
        render(await r.json());
      }} catch (_e) {{}}
    }}
    function updateAttachInfo() {{
      if (!attachInfo || !file) return;
      attachInfo.textContent = file.files && file.files[0] ? ('Файл: ' + file.files[0].name) : '';
    }}
    async function sendMsg() {{
      const t = (txt.value||'').trim();
      const f = file && file.files ? file.files[0] : null;
      if (!t && !f) return;
      const fd = new FormData();
      fd.append('text', t || '');
      if (f) fd.append('file', f);
      send.disabled = true;
      if (pick) pick.disabled = true;
      try {{
        const r = await fetch('/sub/'+token+'/support/send', {{ method:'POST', body:fd }});
        if (!r.ok) throw new Error('send failed');
        txt.value='';
        if (file) file.value = '';
        updateAttachInfo();
        await load();
      }} finally {{
        send.disabled = false;
        if (pick) pick.disabled = false;
      }}
    }}
    if (chat) {{
      chat.addEventListener('click', (e) => {{
        const t = e.target;
        if (!t || !t.closest) return;
        const img = t.closest('img[data-photo-src]');
        if (!img) return;
        e.preventDefault();
        openImageModal(img.getAttribute('data-photo-src') || '');
      }});
    }}
    if (lb) {{
      lb.addEventListener('click', (e) => {{
        if (e.target === lb || e.target === lbClose) closeImageModal();
      }});
    }}
    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape') closeImageModal();
    }});
    if (pick && file) {{
      pick.addEventListener('click', () => file.click());
      file.addEventListener('change', updateAttachInfo);
    }}
    send.addEventListener('click', sendMsg);
    txt.addEventListener('keydown', (e)=>{{ if (e.key==='Enter') {{ e.preventDefault(); sendMsg(); }} }});
    load(); setInterval(load, 2500);
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


def _web_support_display_name(db_user: User) -> str:
    parts = [p for p in (db_user.first_name, db_user.last_name) if p]
    if parts:
        return " ".join(parts).strip()
    if db_user.username:
        return f"@{db_user.username}"
    return f"ID {int(db_user.telegram_id)}"


async def _get_active_support_ticket(*, db_user: User) -> tuple[int, int] | None:
    """Активный тикет (id, topic_id) или None — без автосоздания."""
    factory = get_session_factory()
    async with factory() as session:
        active_id = await get_active_ticket_id(session, user_id=db_user.id)
        if active_id is None:
            return None
        row = (
            await session.execute(
                text("SELECT topic_id FROM tickets WHERE id=:tid LIMIT 1"),
                {"tid": int(active_id)},
            )
        ).mappings().first()
        topic_id = int(row["topic_id"] or 0) if row else 0
        return int(active_id), topic_id


async def _start_web_support_ticket(*, db_user: User, message_text: str) -> tuple[int, int]:
    """Новый тикет из web: тема форума как в боте поддержки."""
    factory = get_session_factory()
    async with factory() as session:
        tid = await create_ticket(
            session,
            user=db_user,
            telegram_user_id=int(db_user.telegram_id),
            text_body=message_text,
        )
        topic_id = 0
        if tickets_config.bot_token and tickets_config.support_group_id:
            settings = get_settings()
            disp = _web_support_display_name(db_user)
            async with Bot(token=tickets_config.bot_token) as bot:
                topic_id = await open_ticket_forum_topic(
                    bot,
                    session,
                    ticket_id=tid,
                    db_user=db_user,
                    message_text=message_text,
                    display_name=disp,
                    telegram_user_id=int(db_user.telegram_id),
                    username=db_user.username,
                    settings=settings,
                )
        await session.commit()
        return tid, topic_id


def _web_support_topic_text(*, ticket_id: int, db_user: User, msg: str) -> str:
    disp = _web_support_display_name(db_user)
    user_line = f'<a href="tg://user?id={int(db_user.telegram_id)}">{_esc(disp)}</a>'
    topic_text = f"<b>✉️ Новое сообщение в тикете #{ticket_id}</b>\nОт: {user_line}"
    if msg:
        topic_text += f"\n\n<blockquote>{_esc(msg)}</blockquote>"
    return topic_text


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
    show_renew = False
    if db_user is not None and sub is not None and await tariff_purchases_enabled(settings):
        show_renew = True
    return _subscription_page(
        token=token,
        headline_value=headline_value,
        headline_hint=headline_hint,
        expires_at=expires_at,
        created_at=created_at,
        balance_rub=db_user.balance if db_user is not None else Decimal("0"),
        bot_open_url=bot_open_url,
        show_renew_button=show_renew,
    )


@router.get("/sub/{subscription_key}/support")
async def public_subscription_support_page(subscription_key: str) -> HTMLResponse:
    token = (subscription_key or "").strip()
    if not token:
        return render_not_found_page("/sub/<empty>/support")
    panel_user, db_user, _sub = await _resolve_subscription_context(token)
    if panel_user is None or db_user is None:
        return render_not_found_page(f"/sub/{token}/support")
    return _support_page(token)


@router.get("/sub/{subscription_key}/support/messages")
async def public_subscription_support_messages(subscription_key: str) -> dict[str, object]:
    token = (subscription_key or "").strip()
    panel_user, db_user, _sub = await _resolve_subscription_context(token)
    if panel_user is None or db_user is None:
        raise HTTPException(status_code=404, detail="Not found")
    active = await _get_active_support_ticket(db_user=db_user)
    if active is None:
        return {"ticket_id": None, "messages": []}
    ticket_id, _topic_id = active
    factory = get_session_factory()
    async with factory() as session:
        has_photo = await ticket_messages_has_photo_file_id_column(session)
        has_video = await ticket_messages_has_video_file_id_column(session)
        has_document = await ticket_messages_has_document_columns(session)
        msg_cols = (
            "id,sender_role,text,created_at,photo_file_id,video_file_id,document_file_id,document_file_name"
            if (has_photo and has_video and has_document)
            else "id,sender_role,text,created_at,photo_file_id,video_file_id"
            if (has_photo and has_video)
            else "id,sender_role,text,created_at,photo_file_id"
            if has_photo
            else "id,sender_role,text,created_at"
        )
        rows = (
            await session.execute(
                text(f"SELECT {msg_cols} FROM ticket_messages WHERE ticket_id=:tid AND COALESCE(is_internal,false)=false ORDER BY id ASC"),
                {"tid": ticket_id},
            )
        ).mappings().all()
    return {
        "ticket_id": ticket_id,
        "messages": [
            {
                "id": int(r["id"]),
                "sender_role": r["sender_role"],
                "text": r.get("text"),
                "created_at": _to_iso(r.get("created_at")) if r.get("created_at") is not None else None,
                "photo_file_id": r.get("photo_file_id") if has_photo else None,
                "video_file_id": r.get("video_file_id") if has_video else None,
                "document_file_id": r.get("document_file_id") if has_document else None,
                "document_file_name": r.get("document_file_name") if has_document else None,
            }
            for r in rows
        ],
    }


@router.post("/sub/{subscription_key}/support/send")
async def public_subscription_support_send(
    subscription_key: str,
    text_value: str = Form(default="", alias="text"),
    file: UploadFile | None = File(default=None),
) -> dict[str, object]:
    token = (subscription_key or "").strip()
    panel_user, db_user, _sub = await _resolve_subscription_context(token)
    if panel_user is None or db_user is None:
        raise HTTPException(status_code=404, detail="Not found")
    msg = (text_value or "").strip()
    if not msg and file is None:
        raise HTTPException(status_code=400, detail="Message is empty")
    active = await _get_active_support_ticket(db_user=db_user)
    is_new_ticket = active is None
    if is_new_ticket:
        ticket_id, topic_id = await _start_web_support_ticket(
            db_user=db_user,
            message_text=msg or "📎 Вложение",
        )
    else:
        ticket_id, topic_id = active
    photo_file_id: str | None = None
    video_file_id: str | None = None
    document_file_id: str | None = None
    document_name: str | None = None
    if file is not None:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file")
        if len(raw) > int(tickets_config.media_max_mb) * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File too large (max {tickets_config.media_max_mb} MB)")
        ctype = (file.content_type or "").lower()
        safe_name = (file.filename or "").strip() or "attachment.bin"
        if not tickets_config.bot_token:
            raise HTTPException(status_code=503, detail="Tickets bot not configured")
        async with Bot(token=tickets_config.bot_token) as bot:
            upload = BufferedInputFile(file=raw, filename=safe_name)
            if ctype.startswith("image/"):
                try:
                    sent = await bot.send_photo(chat_id=int(db_user.telegram_id), photo=upload, caption=msg[:1024] or None)
                    photo_file_id = sent.photo[-1].file_id if sent.photo else None
                except TelegramBadRequest as e:
                    err = str(e)
                    # Некоторые изображения Telegram не принимает как photo (например, некорректные размеры).
                    # В этом случае отправляем тот же файл как document, чтобы сообщение не падало с 500.
                    if "PHOTO_INVALID_DIMENSIONS" in err or "IMAGE_PROCESS_FAILED" in err:
                        sent = await bot.send_document(chat_id=int(db_user.telegram_id), document=upload, caption=msg[:1024] or None)
                        document_file_id = sent.document.file_id if sent.document else None
                        document_name = safe_name
                    else:
                        raise
            elif ctype.startswith("video/"):
                sent = await bot.send_video(chat_id=int(db_user.telegram_id), video=upload, caption=msg[:1024] or None)
                video_file_id = sent.video.file_id if sent.video else None
            else:
                sent = await bot.send_document(chat_id=int(db_user.telegram_id), document=upload, caption=msg[:1024] or None)
                document_file_id = sent.document.file_id if sent.document else None
                document_name = safe_name
            if topic_id and file is not None:
                if is_new_ticket:
                    cap = msg[:1024] if msg else None
                else:
                    cap = _web_support_topic_text(ticket_id=ticket_id, db_user=db_user, msg=msg)[:1024]
                if photo_file_id:
                    await bot.send_photo(
                        chat_id=tickets_config.support_group_id,
                        message_thread_id=topic_id,
                        photo=photo_file_id,
                        caption=cap,
                        parse_mode=ParseMode.HTML if cap and not is_new_ticket else None,
                    )
                elif video_file_id:
                    await bot.send_video(
                        chat_id=tickets_config.support_group_id,
                        message_thread_id=topic_id,
                        video=video_file_id,
                        caption=cap,
                        parse_mode=ParseMode.HTML if cap and not is_new_ticket else None,
                    )
                elif document_file_id:
                    await bot.send_document(
                        chat_id=tickets_config.support_group_id,
                        message_thread_id=topic_id,
                        document=document_file_id,
                        caption=cap,
                        parse_mode=ParseMode.HTML if cap and not is_new_ticket else None,
                    )
    elif topic_id and tickets_config.bot_token and not is_new_ticket:
        async with Bot(token=tickets_config.bot_token) as bot:
            await bot.send_message(
                chat_id=tickets_config.support_group_id,
                message_thread_id=topic_id,
                text=_web_support_topic_text(ticket_id=ticket_id, db_user=db_user, msg=msg),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    factory = get_session_factory()
    async with factory() as session:
        if is_new_ticket and file is None:
            await bump_ticket_activity(session, ticket_id=ticket_id, status_to_in_progress=False)
            await session.commit()
            return {"ok": True, "ticket_id": ticket_id}
        now = datetime.now(timezone.utc)
        has_photo = await ticket_messages_has_photo_file_id_column(session)
        has_video = await ticket_messages_has_video_file_id_column(session)
        has_document = await ticket_messages_has_document_columns(session)
        if has_photo and has_video and has_document:
            await session.execute(
                text(
                    """
                    INSERT INTO ticket_messages (ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal,photo_file_id,video_file_id,document_file_id,document_file_name)
                    VALUES (:tid,:sid,'user',:stg,:txt,:now,false,:photo,:video,:doc,:dname)
                    """
                ),
                {"tid": ticket_id, "sid": db_user.id, "stg": int(db_user.telegram_id), "txt": msg, "now": now, "photo": photo_file_id, "video": video_file_id, "doc": document_file_id, "dname": document_name},
            )
        elif has_photo and has_video:
            await session.execute(
                text(
                    """
                    INSERT INTO ticket_messages (ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal,photo_file_id,video_file_id)
                    VALUES (:tid,:sid,'user',:stg,:txt,:now,false,:photo,:video)
                    """
                ),
                {"tid": ticket_id, "sid": db_user.id, "stg": int(db_user.telegram_id), "txt": msg, "now": now, "photo": photo_file_id, "video": video_file_id},
            )
        elif has_photo:
            await session.execute(
                text(
                    """
                    INSERT INTO ticket_messages (ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal,photo_file_id)
                    VALUES (:tid,:sid,'user',:stg,:txt,:now,false,:photo)
                    """
                ),
                {"tid": ticket_id, "sid": db_user.id, "stg": int(db_user.telegram_id), "txt": msg, "now": now, "photo": photo_file_id},
            )
        else:
            await session.execute(
                text(
                    """
                    INSERT INTO ticket_messages (ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal)
                    VALUES (:tid,:sid,'user',:stg,:txt,:now,false)
                    """
                ),
                {"tid": ticket_id, "sid": db_user.id, "stg": int(db_user.telegram_id), "txt": msg, "now": now},
            )
        await session.execute(
            text(
                "UPDATE tickets SET status=CASE WHEN status='open' THEN 'in_progress' ELSE status END, updated_at=:now, last_activity=:now WHERE id=:tid"
            ),
            {"tid": ticket_id, "now": now},
        )
        await session.commit()
    return {"ok": True, "ticket_id": ticket_id}


@router.get("/sub/{subscription_key}/support/media/{msg_id}/photo")
async def public_subscription_support_media_photo(subscription_key: str, msg_id: int) -> Response:
    token = (subscription_key or "").strip()
    panel_user, db_user, _sub = await _resolve_subscription_context(token)
    if panel_user is None or db_user is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not tickets_config.bot_token:
        raise HTTPException(status_code=503, detail="Tickets bot not configured")
    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT tm.photo_file_id
                    FROM ticket_messages tm
                    JOIN tickets t ON t.id=tm.ticket_id
                    WHERE tm.id=:mid AND t.user_id=:uid
                    """
                ),
                {"mid": msg_id, "uid": db_user.id},
            )
        ).mappings().first()
    if not row or not row.get("photo_file_id"):
        raise HTTPException(status_code=404, detail="Photo not found")
    async with Bot(token=tickets_config.bot_token) as bot:
        f = await bot.get_file(str(row["photo_file_id"]))
        if not f.file_path:
            raise HTTPException(status_code=404, detail="File path unavailable")
        url = f"https://api.telegram.org/file/bot{tickets_config.bot_token}/{f.file_path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return Response(content=r.content, media_type="image/jpeg")


@router.get("/sub/{subscription_key}/support/media/{msg_id}/document")
async def public_subscription_support_media_document(subscription_key: str, msg_id: int) -> Response:
    token = (subscription_key or "").strip()
    panel_user, db_user, _sub = await _resolve_subscription_context(token)
    if panel_user is None or db_user is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not tickets_config.bot_token:
        raise HTTPException(status_code=503, detail="Tickets bot not configured")
    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT tm.document_file_id, tm.document_file_name
                    FROM ticket_messages tm
                    JOIN tickets t ON t.id=tm.ticket_id
                    WHERE tm.id=:mid AND t.user_id=:uid
                    """
                ),
                {"mid": msg_id, "uid": db_user.id},
            )
        ).mappings().first()
    if not row or not row.get("document_file_id"):
        raise HTTPException(status_code=404, detail="Document not found")
    async with Bot(token=tickets_config.bot_token) as bot:
        f = await bot.get_file(str(row["document_file_id"]))
        if not f.file_path:
            raise HTTPException(status_code=404, detail="File path unavailable")
        url = f"https://api.telegram.org/file/bot{tickets_config.bot_token}/{f.file_path}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return Response(
            content=r.content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{_esc(str(row.get("document_file_name") or "document"))}"'},
        )


@router.get("/sub/{subscription_key}/renew")
async def public_subscription_renew_page(subscription_key: str) -> HTMLResponse:
    token = (subscription_key or "").strip()
    if not token:
        return render_not_found_page("/sub/<empty>/renew")
    settings = get_settings()
    panel_user, db_user, sub = await _resolve_subscription_context(token)
    if panel_user is None or db_user is None:
        return render_not_found_page(f"/sub/{token}/renew")
    if not await tariff_purchases_enabled(settings):
        return render_not_found_page(f"/sub/{token}/renew")
    window = int(settings.subscription_renewal_window_days)
    days_left = subscription_days_left(sub.expires_at if sub else None)
    can_renew = can_renew_subscription_with_tariff(sub, window_days=window)
    factory = get_session_factory()
    async with factory() as session:
        raw_plans = await list_paid_plans(session)
        plan_rows: list[tuple[Plan, str]] = []
        for p in raw_plans:
            eff = await resolve_plan_price_rub(session, p)
            plan_rows.append((p, plan_tariff_button_label(p, price_rub=eff)))
    return _renew_page(
        token=token,
        plans=plan_rows,
        can_renew=can_renew,
        days_left=days_left,
        window_days=window,
        balance_rub=db_user.balance,
    )


@router.post("/sub/{subscription_key}/renew")
async def public_subscription_renew_post(
    subscription_key: str,
    plan_id: str = Form(""),
) -> HTMLResponse:
    token = (subscription_key or "").strip()
    if not token:
        return render_not_found_page("/sub/<empty>/renew")
    settings = get_settings()
    panel_user, db_user, sub = await _resolve_subscription_context(token)
    if panel_user is None or db_user is None:
        return render_not_found_page(f"/sub/{token}/renew")
    if not await tariff_purchases_enabled(settings):
        return render_not_found_page(f"/sub/{token}/renew")
    window = int(settings.subscription_renewal_window_days)
    days_left = subscription_days_left(sub.expires_at if sub else None)
    can_renew = can_renew_subscription_with_tariff(sub, window_days=window)

    factory = get_session_factory()
    async with factory() as session:
        raw_plans = await list_paid_plans(session)
        plan_rows: list[tuple[Plan, str]] = []
        for p in raw_plans:
            eff = await resolve_plan_price_rub(session, p)
            plan_rows.append((p, plan_tariff_button_label(p, price_rub=eff)))

    def _renew_response(*, error: str | None = None) -> HTMLResponse:
        return _renew_page(
            token=token,
            plans=plan_rows,
            can_renew=can_renew,
            days_left=days_left,
            window_days=window,
            balance_rub=db_user.balance,
            error_message=error,
        )

    if not can_renew:
        return _renew_response(
            error=f"Продление доступно только за {window} дн. до окончания. Сейчас осталось {days_left} дн."
        )
    try:
        pid = int((plan_id or "").strip())
    except ValueError:
        return _renew_response(error="Выберите тариф.")
    if not any(p.id == pid for p, _lbl in plan_rows):
        return _renew_response(error="Тариф недоступен.")

    idem = secrets.token_urlsafe(12)
    async with factory() as session:
        user = await session.get(User, db_user.id)
        if user is None:
            return _renew_response(error="Пользователь не найден.")
        ok, msg, kind = await purchase_plan_with_balance(
            session,
            user=user,
            plan_id=pid,
            telegram_id=int(user.telegram_id),
            settings=settings,
            save_to_cart_if_insufficient=False,
            idempotency_key=f"webrenew:{user.id}:{idem}",
        )
        if ok:
            await session.commit()
            return RedirectResponse(f"/sub/{token}", status_code=303)
        await session.rollback()
    plain_msg = msg.replace("*", "").replace("_", "").replace("`", "") if isinstance(msg, str) else str(msg)
    if kind == "insufficient":
        plain_msg = f"{plain_msg} Пополните баланс и повторите."
    return _renew_response(error=plain_msg[:500])


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
