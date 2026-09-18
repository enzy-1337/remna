"""Личный кабинет — главная: подписка, баланс, трафик, статистика, быстрые действия."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from shared.config import get_settings
from shared.database import get_session_factory
from shared.integrations.rw_traffic import extract_traffic_gb_from_rw_user
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.device_slots_pricing import is_admin_unlimited_devices
from shared.services.hwid_devices_service import connected_devices_count, fetch_panel_hwid_context, panel_devices_unlimited
from shared.services.promo_service import apply_promo_code_for_user_v2
from shared.services.referral_service import count_invited_users, sum_referrer_bonus_rub
from shared.services.site_session_service import load_site_user, touch_session
from shared.services.subscription_service import get_active_subscription, subscription_days_left
from shared.services.topup_service import create_topup_payment

from api.routers.site_theme import (
    app_topbar,
    bot_deep_link,
    esc,
    fmt_money,
    icon,
    page,
    site_footer,
    tg_logo_svg,
)

router = APIRouter()

_TXN_KIND = {
    "topup": ("wallet", "var(--success)", "Пополнение баланса"),
    "admin_balance_add": ("wallet", "var(--success)", "Пополнение (администратор)"),
    "purchase_plan": ("shield", "var(--text-2)", "Покупка подписки"),
    "subscription": ("shield", "var(--text-2)", "Подписка"),
    "subscription_autorenew": ("shield", "var(--text-2)", "Автопродление подписки"),
    "referral_signup": ("people", "var(--success)", "Реферальный бонус · регистрация"),
    "referral_signup_invited": ("people", "var(--success)", "Бонус за вход по приглашению"),
    "referral_payment_percent": ("people", "var(--success)", "Реферальный кэшбэк"),
    "promo_apply": ("coupon", "var(--warn)", "Активирован промокод"),
    "device_slots_purchased": ("devices", "var(--text-2)", "Слот устройства"),
    "trial_activate": ("gift", "var(--success)", "Пробный период"),
    "usage_charge": ("bar-chart", "var(--text-2)", "Списание по трафику"),
    "debit": ("bar-chart", "var(--text-2)", "Списание"),
    "purchase_refund": ("wallet", "var(--success)", "Возврат средств"),
}


def _txn_row_html(t: Transaction) -> str:
    ic, color, label = _TXN_KIND.get(t.type, ("bar-chart", "var(--text-2)", t.type.replace("_", " ").capitalize()))
    positive = t.amount >= 0
    sign = "+" if positive else ""
    amt_color = "var(--success)" if positive else "var(--text-2)"
    dt = t.created_at.strftime("%-d %B %Y, %H:%M") if t.created_at else ""
    return f"""
<div style="display:flex;align-items:center;gap:14px;padding:12px 0;border-bottom:1px solid var(--line);">
  <div style="width:32px;height:32px;border-radius:9px;background:rgba(123,92,255,.1);display:flex;align-items:center;justify-content:center;flex-shrink:0;">
    {icon(ic, size=15, color=color)}
  </div>
  <div style="flex:1;min-width:0;">
    <div style="font:700 13.5px Manrope;color:var(--text-1);">{esc(label)}</div>
    <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">{esc(dt)}</div>
  </div>
  <span class="mono" style="font:500 14px 'JetBrains Mono';color:{amt_color};">{sign}{esc(fmt_money(t.amount))} ₽</span>
</div>"""


@router.get("/app")
async def dashboard(request: Request) -> HTMLResponse:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)

        sub = await get_active_subscription(session, user.id)
        days_left = subscription_days_left(sub.expires_at if sub else None)

        devices_used = 0
        devices_max = sub.devices_count if sub else 0
        devices_max_label = str(devices_max)
        traffic_used_gb: float | None = None
        traffic_limit_gb: float | None = None
        if user.remnawave_uuid is not None:
            uinf, hw_devices, _err = await fetch_panel_hwid_context(user, settings)
            devices_used = connected_devices_count(uinf, hw_devices)
            is_admin = is_admin_unlimited_devices(user, settings)
            if is_admin or panel_devices_unlimited(uinf, is_bot_admin=is_admin):
                devices_max_label = "∞"
            if uinf:
                from api.routers.miniapp import _lifetime_used_gb

                traffic_used_gb = _lifetime_used_gb(uinf)
                _cur, traffic_limit_gb = extract_traffic_gb_from_rw_user(uinf)

        txns = list(
            (
                await session.execute(
                    select(Transaction).where(Transaction.user_id == user.id).order_by(Transaction.id.desc()).limit(4)
                )
            ).scalars()
        )
        topped_up_total = (
            await session.execute(
                select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.user_id == user.id, Transaction.type == "topup", Transaction.status == "completed"
                )
            )
        ).scalar_one()
        subs_count = (
            await session.execute(select(func.count()).select_from(Subscription).where(Subscription.user_id == user.id))
        ).scalar_one()
        invited_count = await count_invited_users(session, user.id)
        earned_rub = await sum_referrer_bonus_rub(session, user.id)

    initial = (user.first_name or user.username or "U")[:1].upper()
    name_display = esc(user.first_name or (f"@{user.username}" if user.username else f"#{user.id}"))

    if sub is not None:
        pct = min(100, max(0, int(round(days_left / 30 * 100)))) if days_left else 0
        ring = f"""
        <div class="ring" style="width:108px;height:108px;background:conic-gradient(var(--accent) 0 {pct}%,#1A2129 {pct}% 100%);">
          <div class="ring-inner" style="inset:9px;">
            <div style="font:800 26px Manrope;color:var(--text-1);">{days_left}</div>
            <div style="font:600 10px Manrope;color:var(--text-3);">дней</div>
          </div>
        </div>"""
        status_badge = (
            '<span class="badge badge-success">Активна</span>'
            if sub.status in ("active", "trial")
            else '<span class="badge badge-warn">Истекает</span>'
        )
        traffic_html = ""
        if traffic_used_gb is not None:
            lim_label = f"{traffic_limit_gb:g} ГБ" if traffic_limit_gb else "∞"
            traffic_pct = (
                min(100, int(round(traffic_used_gb / traffic_limit_gb * 100))) if traffic_limit_gb else 0
            )
            traffic_html = f"""
            <div style="display:flex;justify-content:space-between;font:600 11px Manrope;color:var(--text-3);margin-top:16px;">
              <span>Трафик {traffic_used_gb:g} / {esc(lim_label)}</span><span style="color:var(--text-2);">{devices_used} из {esc(devices_max_label)} устройств</span>
            </div>
            <div class="bar-track" style="margin-top:7px;"><div class="bar-fill" style="width:{traffic_pct}%;"></div></div>
            """
        sub_card = f"""
        <div class="card card-accent">
          <div style="display:flex;align-items:center;gap:9px;">{icon('shield', size=16, color='#7B5CFF')}<span class="section-label">ВАША ПОДПИСКА</span></div>
          <div style="display:flex;align-items:center;gap:22px;margin-top:18px;flex-wrap:wrap;">
            {ring}
            <div style="flex:1;min-width:200px;">
              <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap;"><span style="font:800 19px Manrope;color:var(--text-1);">{esc(sub.plan.name if sub.plan else 'Подписка')}</span>{status_badge}</div>
              <div style="font:500 13px Manrope;color:var(--text-3);margin-top:6px;">Продление {esc(sub.expires_at.strftime('%-d %B %Y') if sub.expires_at else '—')} · автоплатёж {'включён' if sub.auto_renew else 'выключен'}</div>
              {traffic_html}
              <div style="display:flex;gap:10px;margin-top:18px;flex-wrap:wrap;">
                <a href="/app/subscription" class="btn btn-primary btn-sm">Продлить</a>
                <a href="/app/subscription" class="btn btn-outline btn-sm">Ключ и устройства</a>
              </div>
            </div>
          </div>
        </div>"""
    else:
        sub_card = f"""
        <div class="card card-accent" style="text-align:center;">
          <div style="width:44px;height:44px;border-radius:13px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;margin:0 auto;">{icon('shield', size=20, color='#7B5CFF')}</div>
          <div style="font:800 17px Manrope;color:var(--text-1);margin-top:14px;">Подписки нет</div>
          <div style="font:500 13px Manrope;color:var(--text-3);margin-top:6px;">Выберите тариф, чтобы открыть доступ ко всем локациям</div>
          <a href="/app/subscription" class="btn btn-primary" style="margin-top:16px;">Выбрать тариф</a>
        </div>"""

    txns_html = "".join(_txn_row_html(t) for t in txns) or '<div style="opacity:.5;font:500 13px Manrope;padding:12px 0;">Пока нет операций</div>'

    channel = (settings.required_channel_username or "").strip().lstrip("@")
    channel_url = f"https://t.me/{channel}" if channel else bot_deep_link(settings)

    body = f"""
<div class="cabinet-bg" style="min-height:100vh;">
  <div class="shell-wide">
    {app_topbar(active="home", balance_rub=fmt_money(user.balance), unread_tickets=0, initial=initial)}

    <div class="card fade-up" style="margin-top:6px;padding:16px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
      <div style="width:38px;height:38px;border-radius:11px;background:linear-gradient(140deg,#2AABEE,#229ED9);display:flex;align-items:center;justify-content:center;flex-shrink:0;">{tg_logo_svg(19,'#fff')}</div>
      <div style="flex:1;min-width:200px;">
        <div style="display:flex;align-items:center;gap:9px;"><span class="badge badge-danger">ВАЖНО</span><span style="font:600 11px Manrope;color:var(--text-4);">@{esc(channel or 'fluxvpn')}</span></div>
        <div style="font:500 14px Manrope;color:var(--text-2);margin-top:6px;"><b style="color:var(--text-1);">Подпишитесь на наш Telegram-канал</b> — все важные новости, обновления и анонсы сервиса выходят там первыми.</div>
      </div>
      <a href="{esc(channel_url)}" class="btn btn-primary btn-sm">Перейти {icon('chevron-right', size=13, color='#fff')}</a>
    </div>

    <div class="grid-auto fade-up d1 cols-2" style="grid-template-columns:1fr 400px;margin-top:20px;">
      <div style="display:flex;flex-direction:column;gap:20px;min-width:0;">
        {sub_card}

        <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));">
          <button class="card" style="text-align:left;border:0;cursor:pointer;" data-open-promo>
            <div style="width:30px;height:30px;border-radius:9px;background:rgba(245,181,68,.12);display:flex;align-items:center;justify-content:center;">{icon('coupon', size=15, color='#F5B544')}</div>
            <div style="font:700 13px Manrope;color:var(--text-1);margin-top:10px;">Купон</div>
            <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">Активировать промокод</div>
          </button>
          <a class="card" href="/app/tickets" style="text-align:left;">
            <div style="width:30px;height:30px;border-radius:9px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;">{icon('chat', size=15, color='#7B5CFF')}</div>
            <div style="font:700 13px Manrope;color:var(--text-1);margin-top:10px;">Чат</div>
            <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">Общение и поддержка</div>
          </a>
          <a class="card" href="{esc(channel_url)}" style="text-align:left;">
            <div style="width:30px;height:30px;border-radius:9px;background:rgba(34,158,217,.14);display:flex;align-items:center;justify-content:center;">{tg_logo_svg(15,'#6AB3F3')}</div>
            <div style="font:700 13px Manrope;color:var(--text-1);margin-top:10px;">Новости</div>
            <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">Канал в Telegram</div>
          </a>
        </div>

        <div class="card">
          <div style="display:flex;align-items:center;justify-content:space-between;">
            <div style="display:flex;align-items:center;gap:9px;">{icon('bar-chart', size=16, color='#7B5CFF')}<span class="section-label">ПОСЛЕДНИЕ ОПЕРАЦИИ</span></div>
          </div>
          <div style="margin-top:14px;">{txns_html}</div>
        </div>
      </div>

      <div style="display:flex;flex-direction:column;gap:20px;min-width:0;">
        <div class="card card-accent">
          <div style="display:flex;align-items:center;gap:14px;">
            <div class="avatar-circle" style="width:46px;height:46px;border-radius:14px;font-size:17px;">{esc(initial)}</div>
            <div style="min-width:0;">
              <div style="font:800 16px Manrope;color:var(--text-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{name_display}</div>
              <div class="mono" style="font:600 11px Manrope;color:var(--text-4);margin-top:2px;">ID: {user.id}</div>
            </div>
          </div>
        </div>

        <div class="card">
          <div style="display:flex;align-items:center;gap:9px;">{icon('bar-chart', size=16, color='#7B5CFF')}<span class="section-label">СТАТИСТИКА ЗА ВСЁ ВРЕМЯ</span></div>
          <div style="margin-top:6px;">
            {_stat_row('Пополнено всего', f'{fmt_money(topped_up_total)} ₽')}
            {_stat_row('Баланс', f'{fmt_money(user.balance)} ₽', color='var(--accent-soft)')}
            {_stat_row('Трафик за всё время', f'{traffic_used_gb:g} ГБ' if traffic_used_gb is not None else '—')}
            {_stat_row('Подписок оформлено', str(subs_count))}
            {_stat_row('Аккаунт с', user.created_at.strftime('%d.%m.%Y') if user.created_at else '—', last=True)}
          </div>
        </div>

        <div class="card">
          <div style="display:flex;align-items:center;justify-content:space-between;">
            <div style="display:flex;align-items:center;gap:9px;">{icon('people', size=16, color='#7B5CFF')}<span class="section-label">РЕФЕРАЛЬНАЯ ПРОГРАММА</span></div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px;">
            <div style="background:var(--card-4);border-radius:12px;padding:14px;text-align:center;">
              <div style="font:800 20px Manrope;color:var(--text-1);">{invited_count}</div>
              <div style="font:600 11px Manrope;color:var(--text-4);margin-top:2px;">Приглашено</div>
            </div>
            <div style="background:var(--card-4);border-radius:12px;padding:14px;text-align:center;">
              <div style="font:800 20px Manrope;color:var(--success);">{fmt_money(earned_rub)} ₽</div>
              <div style="font:600 11px Manrope;color:var(--text-4);margin-top:2px;">Заработано</div>
            </div>
          </div>
          <a href="/app/referrals" class="btn btn-outline btn-block" style="margin-top:14px;">Подробнее {icon('chevron-right', size=13)}</a>
        </div>
      </div>
    </div>

    {site_footer()}
  </div>
</div>

<div id="promo-modal" class="modal-overlay">
  <div class="fade-in" style="width:100%;max-width:380px;background:var(--card-2);border:1px solid var(--line-2);border-radius:18px;padding:24px;">
    <div style="font:800 17px Manrope;color:var(--text-1);">Активировать промокод</div>
    <form method="post" action="/app/promo" style="margin-top:16px;">
      <input class="input mono" name="code" placeholder="FLUX20" autocomplete="off" style="text-transform:uppercase;" required/>
      <div style="display:flex;gap:10px;margin-top:14px;">
        <button type="button" class="btn btn-outline btn-block" data-close-promo>Отмена</button>
        <button type="submit" class="btn btn-primary btn-block">Активировать</button>
      </div>
    </form>
  </div>
</div>
<script>
document.querySelectorAll('[data-open-promo]').forEach(function(b){{ b.addEventListener('click', function(){{ document.getElementById('promo-modal').classList.add('open'); }}); }});
document.querySelectorAll('[data-close-promo]').forEach(function(b){{ b.addEventListener('click', function(){{ document.getElementById('promo-modal').classList.remove('open'); }}); }});
document.getElementById('promo-modal').addEventListener('click', function(e){{ if (e.target === this) this.classList.remove('open'); }});
</script>
"""
    resp_body = page(title="Личный кабинет — Flux Network", body=body)
    return HTMLResponse(resp_body)


def _stat_row(label: str, value: str, *, color: str = "var(--text-1)", last: bool = False) -> str:
    border = "" if last else "border-bottom:1px solid var(--line);"
    return f"""
    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;{border}">
      <span style="font:600 13px Manrope;color:var(--text-2);">{esc(label)}</span>
      <span class="mono" style="font:600 14px 'JetBrains Mono';color:{color};">{esc(value)}</span>
    </div>"""


@router.post("/app/promo")
async def activate_promo(request: Request, code: str = Form("")) -> RedirectResponse:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        ok, _msg, _data = await apply_promo_code_for_user_v2(session, settings=settings, user=user, raw_code=code)
        await session.commit()
    return RedirectResponse("/app?n=" + ("promo_ok" if ok else "promo_err"), status_code=303)


@router.post("/app/topup")
async def create_topup(request: Request, amount: str = Form("")) -> RedirectResponse:
    settings = get_settings()
    try:
        amt = Decimal((amount or "").strip().replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return RedirectResponse(f"/app?err={quote_plus('Некорректная сумма')}", status_code=303)
    if amt <= 0:
        return RedirectResponse(f"/app?err={quote_plus('Некорректная сумма')}", status_code=303)

    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        try:
            _txn, pay_url = await create_topup_payment(
                session,
                user=user,
                telegram_id=int(user.telegram_id),
                amount_rub=amt,
                provider_name="platega",
                settings=settings,
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            return RedirectResponse(
                f"/app?err={quote_plus('Не удалось создать платёж: ' + str(exc)[:160])}", status_code=303
            )
    return RedirectResponse(pay_url, status_code=303)
