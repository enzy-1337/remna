"""Подписка в личном кабинете сайта: продление/покупка тарифа с баланса, ключ подписки,
авто-продление, устройства — те же сервисы, что использует бот и Telegram Mini App."""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from urllib.parse import quote_plus

from shared.config import get_settings
from shared.database import get_session_factory
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError, subscription_url_for_telegram
from shared.integrations.rw_traffic import extract_traffic_gb_from_rw_user
from shared.services.device_slots_pricing import (
    device_slot_cap,
    is_admin_unlimited_devices,
    price_for_extra_device_slots,
    slots_available_to_buy,
)
from shared.services.hwid_devices_service import (
    connected_devices_count,
    device_display_title,
    fetch_panel_hwid_context,
    panel_devices_unlimited,
)
from shared.services.promo_service import get_pending_purchase_discount_info
from shared.services.site_session_service import load_site_user, touch_session
from shared.services.subscription_service import (
    add_paid_device_slots,
    get_active_subscription,
    get_one_month_reference_plan,
    list_paid_plans,
    purchase_plan_with_balance,
    resolve_user_plan_price_rub,
    set_subscription_auto_renew,
    subscription_days_left,
    tariff_duration_months,
    unbind_hwid_device_keep_slot,
    user_custom_month_price_rub,
)

from api.routers.miniapp import _classify_device, _md2_to_plain
from api.routers.site_theme import app_topbar, esc, fmt_money, icon, page, site_footer

router = APIRouter()

_DEVICE_ICON = {"phone": "devices", "tv": "monitor", "computer": "monitor", "unknown": "devices"}


@router.get("/app/subscription")
async def subscription_page(request: Request) -> HTMLResponse:
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
        is_bot_admin = is_admin_unlimited_devices(user, settings)

        ref_plan = await get_one_month_reference_plan(session)
        custom_month = user_custom_month_price_rub(user)
        base_month = custom_month if custom_month is not None else (ref_plan.price_rub if ref_plan else Decimal("0"))
        _promo_code, promo_discount_pct = await get_pending_purchase_discount_info(session, user_id=user.id)

        plans = await list_paid_plans(session)
        plan_cards: list[dict] = []
        for p in plans:
            price_after_plan = await resolve_user_plan_price_rub(session, user, p)
            final_price = price_after_plan
            if promo_discount_pct > 0:
                final_price = (price_after_plan * (Decimal("100") - promo_discount_pct) / Decimal("100")).quantize(Decimal("1"))
            months = tariff_duration_months(p.duration_days)
            original_price = (base_month * months).quantize(Decimal("1")) if base_month > 0 else final_price
            if original_price < final_price:
                original_price = final_price
            plan_cards.append({"plan": p, "price": final_price, "original": original_price})

        subscription_url = ""
        traffic_used_gb: float | None = None
        traffic_limit_gb: float | None = None
        devices_out: list[dict] = []
        devices_used = 0
        devices_unlimited = False
        if user.remnawave_uuid is not None:
            uinf, devices, _err = await fetch_panel_hwid_context(user, settings)
            devices_used = connected_devices_count(uinf, devices)
            devices_unlimited = panel_devices_unlimited(uinf, is_bot_admin=is_bot_admin)
            for i, d in enumerate(devices):
                devices_out.append(
                    {
                        "hwid": d.get("hwid") or d.get("hwId") or "",
                        "title": device_display_title(d, i),
                        "platform": d.get("platform") or "",
                        "dtype": _classify_device(d),
                        "updated_at": d.get("updatedAt") or d.get("createdAt") or "",
                    }
                )
            if uinf:
                traffic_used_gb = None
                try:
                    from api.routers.miniapp import _lifetime_used_gb

                    traffic_used_gb = _lifetime_used_gb(uinf)
                except Exception:
                    pass
                _cur, traffic_limit_gb = extract_traffic_gb_from_rw_user(uinf)
                subscription_url = subscription_url_for_telegram(uinf.get("subscriptionUrl"), settings) or ""

        cap = device_slot_cap(user, settings, is_bot_admin=is_bot_admin)
        devices_total = sub.devices_count if sub else 0
        available_to_buy = slots_available_to_buy(devices_total, cap)

    initial = (user.first_name or user.username or "U")[:1].upper()
    notice = request.query_params.get("n") or ""
    err = request.query_params.get("err") or ""
    notice_html = _notice_html(notice, err)

    body = f"""
<div class="cabinet-bg" style="min-height:100vh;">
  <div class="shell-wide">
    {app_topbar(active="subscription", balance_rub=fmt_money(user.balance), unread_tickets=0, initial=initial)}

    <div class="fade-up" style="display:flex;align-items:center;gap:14px;margin-top:12px;">
      <div style="width:34px;height:34px;border-radius:11px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;">{icon('shield', size=17, color='#7B5CFF')}</div>
      <div>
        <div style="font:800 22px Manrope;color:var(--text-1);">Подписка</div>
        <div style="font:500 13px Manrope;color:var(--text-3);">Тариф, ключ, авто-продление и устройства</div>
      </div>
    </div>

    {notice_html}

    {_sub_card_html(sub, days_left, subscription_url, traffic_used_gb, traffic_limit_gb, devices_used, devices_total, devices_unlimited)}

    <div class="card fade-up d1" style="margin-top:20px;">
      <div class="section-label">{icon('coupon', size=14)}<span>{'ПРОДЛИТЬ ТАРИФ' if sub else 'ВЫБРАТЬ ТАРИФ'}</span></div>
      <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));margin-top:14px;">
        {"".join(_plan_card_html(pc, has_sub=sub is not None) for pc in plan_cards) or '<div style="opacity:.5;font:500 13px Manrope;padding:12px 0;">Тарифы не найдены</div>'}
      </div>
    </div>

    <div class="card fade-up d2" style="margin-top:20px;">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;">
        <div class="section-label">{icon('devices', size=14)}<span>УСТРОЙСТВА</span></div>
        <span style="font:600 12px Manrope;color:var(--text-3);">{devices_used} из {'∞' if devices_unlimited else devices_total} подключено</span>
      </div>
      <div style="margin-top:12px;">{_devices_list_html(devices_out)}</div>
      {_buy_slot_html(settings, available_to_buy, devices_unlimited) if sub else ''}
    </div>

    {site_footer()}
  </div>
</div>
<script>
document.querySelectorAll('[data-copy]').forEach(function(b){{
  b.addEventListener('click', function(){{
    var val = b.getAttribute('data-copy');
    navigator.clipboard.writeText(val).then(function(){{
      var old = b.textContent; b.textContent = 'Скопировано'; setTimeout(function(){{ b.textContent = old; }}, 1500);
    }});
  }});
}});
document.querySelectorAll('form[data-confirm]').forEach(function(f){{
  f.addEventListener('submit', function(e){{ if (!confirm(f.getAttribute('data-confirm'))) e.preventDefault(); }});
}});
document.querySelectorAll('form[data-disable-on-submit]').forEach(function(f){{
  f.addEventListener('submit', function(){{ var btn = f.querySelector('button[type=submit]'); if (btn) btn.disabled = true; }});
}});
var arToggle = document.getElementById('auto-renew-toggle');
if (arToggle) {{
  arToggle.addEventListener('click', function(e){{
    e.preventDefault();
    var enabled = arToggle.classList.contains('on');
    fetch('/app/subscription/auto-renew', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}}, body:'enabled=' + (enabled ? '0' : '1')}})
      .then(function(){{ arToggle.classList.toggle('on'); }});
  }});
}}
</script>
"""
    return HTMLResponse(page(title="Подписка — Flux Network", body=body))


def _notice_html(notice: str, err: str) -> str:
    if err:
        return f'<div class="card-danger fade-up" style="margin-top:14px;padding:12px 16px;font:600 13px Manrope;color:var(--danger-soft);border-radius:12px;">{esc(err)}</div>'
    messages = {
        "buy_ok": ("success", "Тариф активирован."),
        "buy_insufficient": ("warn", "Недостаточно средств на балансе — пополните и попробуйте снова."),
        "buy_err": ("danger", "Не удалось выполнить покупку."),
        "autorenew_on": ("success", "Авто-продление включено."),
        "autorenew_off": ("success", "Авто-продление выключено."),
        "device_unbound": ("success", "Устройство отключено."),
        "slot_bought": ("success", "Слот устройства добавлен."),
        "reissue_ok": ("success", "Ключ подписки обновлён."),
    }
    if notice not in messages:
        return ""
    kind, text = messages[notice]
    color = {"success": "var(--success)", "warn": "var(--warn)", "danger": "var(--danger-soft)"}.get(kind, "var(--text-2)")
    return f'<div class="card fade-up" style="margin-top:14px;padding:12px 16px;font:600 13px Manrope;color:{color};">{esc(text)}</div>'


def _sub_card_html(
    sub, days_left: int, subscription_url: str, traffic_used_gb, traffic_limit_gb,
    devices_used: int, devices_total: int, devices_unlimited: bool,
) -> str:
    if sub is None:
        return f"""
        <div class="card card-accent fade-up d1" style="margin-top:20px;text-align:center;">
          <div style="width:44px;height:44px;border-radius:13px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;margin:0 auto;">{icon('shield', size=20, color='#7B5CFF')}</div>
          <div style="font:800 17px Manrope;color:var(--text-1);margin-top:14px;">Подписки нет</div>
          <div style="font:500 13px Manrope;color:var(--text-3);margin-top:6px;">Выберите тариф ниже, чтобы открыть доступ ко всем локациям</div>
        </div>"""

    pct = min(100, max(0, int(round(days_left / 30 * 100)))) if days_left else 0
    ring = f"""
    <div class="ring" style="width:96px;height:96px;background:conic-gradient(var(--accent) 0 {pct}%,#1A2129 {pct}% 100%);">
      <div class="ring-inner" style="inset:8px;">
        <div style="font:800 24px Manrope;color:var(--text-1);">{days_left}</div>
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
        traffic_pct = min(100, int(round(traffic_used_gb / traffic_limit_gb * 100))) if traffic_limit_gb else 0
        traffic_html = f"""
        <div style="display:flex;justify-content:space-between;font:600 11px Manrope;color:var(--text-3);margin-top:14px;">
          <span>Трафик {traffic_used_gb:g} / {esc(lim_label)}</span><span style="color:var(--text-2);">{devices_used} из {'∞' if devices_unlimited else devices_total} устройств</span>
        </div>
        <div class="bar-track" style="margin-top:7px;"><div class="bar-fill" style="width:{traffic_pct}%;"></div></div>"""

    key_row = ""
    if subscription_url:
        key_row = f"""
        <div style="margin-top:16px;">
          <div style="font:600 11px Manrope;color:var(--text-4);margin-bottom:6px;">КЛЮЧ ПОДПИСКИ</div>
          <div style="display:flex;gap:8px;align-items:center;">
            <input class="input mono" readonly value="{esc(subscription_url)}" style="flex:1;min-width:0;font-size:12px;" onclick="this.select();"/>
            <button type="button" class="btn btn-outline btn-sm" data-copy="{esc(subscription_url)}">{icon('copy', size=13)}</button>
          </div>
          <div style="display:flex;gap:10px;margin-top:10px;flex-wrap:wrap;">
            <form method="post" action="/app/subscription/reissue" data-confirm="Старый ключ перестанет работать на всех устройствах. Обновить?" data-disable-on-submit>
              <button type="submit" class="btn btn-outline btn-sm">Обновить ключ</button>
            </form>
          </div>
        </div>"""
    elif sub is not None:
        key_row = '<div style="margin-top:16px;font:500 12px Manrope;color:var(--text-4);">Ключ подписки появится после первого подключения к панели.</div>'

    auto_renew_row = f"""
    <div style="display:flex;align-items:center;justify-content:space-between;margin-top:16px;padding-top:14px;border-top:1px solid var(--line);">
      <div>
        <div style="font:700 13px Manrope;color:var(--text-1);">Авто-продление</div>
        <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">Списывать с баланса автоматически при истечении</div>
      </div>
      <a href="#" id="auto-renew-toggle" class="toggle{' on' if sub.auto_renew else ''}"><span class="knob"></span></a>
    </div>"""

    return f"""
    <div class="card card-accent fade-up d1" style="margin-top:20px;">
      <div style="display:flex;align-items:center;gap:9px;">{icon('shield', size=16, color='#7B5CFF')}<span class="section-label">ВАША ПОДПИСКА</span></div>
      <div style="display:flex;align-items:center;gap:22px;margin-top:18px;flex-wrap:wrap;">
        {ring}
        <div style="flex:1;min-width:200px;">
          <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap;"><span style="font:800 19px Manrope;color:var(--text-1);">{esc(sub.plan.name if sub.plan else 'Подписка')}</span>{status_badge}</div>
          <div style="font:500 13px Manrope;color:var(--text-3);margin-top:6px;">Действует до {esc(sub.expires_at.strftime('%-d %B %Y') if sub.expires_at else '—')}</div>
          {traffic_html}
        </div>
      </div>
      {key_row}
      {auto_renew_row}
    </div>"""


def _plan_card_html(pc: dict, *, has_sub: bool) -> str:
    p = pc["plan"]
    price = pc["price"]
    original = pc["original"]
    discounted = original > price
    price_row = (
        f'<span style="font:800 20px Manrope;color:var(--text-1);">{fmt_money(price)} ₽</span>'
        + (f'<span style="font:600 13px Manrope;color:var(--text-5);text-decoration:line-through;margin-left:6px;">{fmt_money(original)} ₽</span>' if discounted else "")
    )
    traffic_label = f"{p.traffic_limit_gb:g} ГБ" if p.traffic_limit_gb else "Безлимит"
    return f"""
    <form method="post" action="/app/subscription/buy" class="card" style="display:flex;flex-direction:column;gap:10px;" data-disable-on-submit>
      <input type="hidden" name="plan_id" value="{p.id}"/>
      <input type="hidden" name="idempotency_key" value="{uuid.uuid4()}"/>
      <div style="font:800 15px Manrope;color:var(--text-1);">{esc(p.name)}</div>
      <div>{price_row}</div>
      <div style="font:500 11.5px Manrope;color:var(--text-4);display:flex;flex-direction:column;gap:2px;">
        <span>{p.duration_days} дней</span>
        <span>Трафик: {esc(traffic_label)}</span>
        <span>Устройств: {p.device_limit}</span>
      </div>
      <button type="submit" class="btn btn-primary btn-block" style="margin-top:auto;">{'Продлить' if has_sub else 'Купить'}</button>
    </form>"""


def _devices_list_html(devices: list[dict]) -> str:
    if not devices:
        return '<div style="opacity:.5;font:500 13px Manrope;padding:16px 0;text-align:center;">Нет подключённых устройств</div>'
    rows = []
    for i, d in enumerate(devices):
        ic = _DEVICE_ICON.get(d["dtype"], "devices")
        rows.append(f"""
        <div style="display:flex;align-items:center;gap:12px;padding:12px 0;{'border-bottom:1px solid var(--line);' if i < len(devices) - 1 else ''}">
          <div style="width:34px;height:34px;border-radius:10px;background:var(--card-3);display:flex;align-items:center;justify-content:center;flex-shrink:0;">{icon(ic, size=15, color='var(--text-3)')}</div>
          <div style="flex:1;min-width:0;">
            <div style="font:700 13px Manrope;color:var(--text-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{esc(d['title'])}</div>
            <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">{esc(d['platform'] or '—')}</div>
          </div>
          <form method="post" action="/app/subscription/devices/unbind" data-confirm="Отключить это устройство? К слоту можно будет привязать новое." data-disable-on-submit>
            <input type="hidden" name="hwid" value="{esc(d['hwid'])}"/>
            <button type="submit" class="link-btn" style="color:var(--danger-soft);">Отключить</button>
          </form>
        </div>""")
    return "".join(rows)


def _buy_slot_html(settings, available_to_buy: int, unlimited: bool) -> str:
    if unlimited or available_to_buy <= 0:
        return ""
    total, unit, _pct = price_for_extra_device_slots(settings, 1)
    return f"""
    <form method="post" action="/app/subscription/devices/buy-slot" style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;" data-disable-on-submit>
      <div style="font:500 12px Manrope;color:var(--text-4);">Дополнительный слот устройства — {fmt_money(total)} ₽</div>
      <button type="submit" class="btn btn-outline btn-sm">{icon('plus', size=13)}<span>Докупить слот</span></button>
    </form>"""


@router.post("/app/subscription/buy")
async def subscription_buy(request: Request, plan_id: int = Form(...), idempotency_key: str = Form("")) -> RedirectResponse:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        ok, message, kind = await purchase_plan_with_balance(
            session,
            user=user,
            plan_id=plan_id,
            telegram_id=int(user.telegram_id),
            settings=settings,
            idempotency_key=idempotency_key or None,
        )
        await session.commit()
    if ok:
        return RedirectResponse("/app/subscription?n=buy_ok", status_code=303)
    if kind == "insufficient":
        return RedirectResponse("/app/subscription?n=buy_insufficient", status_code=303)
    return RedirectResponse(f"/app/subscription?err={quote_plus(_md2_to_plain(message) or 'Ошибка покупки')}", status_code=303)


@router.post("/app/subscription/auto-renew")
async def subscription_auto_renew(request: Request, enabled: str = Form("0")) -> RedirectResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        await set_subscription_auto_renew(session, user.id, enabled == "1")
        await session.commit()
    return RedirectResponse("/app/subscription?n=" + ("autorenew_on" if enabled == "1" else "autorenew_off"), status_code=303)


@router.post("/app/subscription/devices/unbind")
async def subscription_device_unbind(request: Request, hwid: str = Form(...)) -> RedirectResponse:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        ok, message = await unbind_hwid_device_keep_slot(session, user=user, hwid=hwid, settings=settings, initiator="site")
        await session.commit()
    if ok:
        return RedirectResponse("/app/subscription?n=device_unbound", status_code=303)
    return RedirectResponse(f"/app/subscription?err={quote_plus(_md2_to_plain(message) or 'Не удалось отключить устройство')}", status_code=303)


@router.post("/app/subscription/devices/buy-slot")
async def subscription_buy_slot(request: Request) -> RedirectResponse:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        ok, message = await add_paid_device_slots(session, user=user, settings=settings, quantity=1, idempotency_key=str(uuid.uuid4()))
        await session.commit()
    if ok:
        return RedirectResponse("/app/subscription?n=slot_bought", status_code=303)
    return RedirectResponse(f"/app/subscription?err={quote_plus(_md2_to_plain(message) or 'Не удалось купить слот')}", status_code=303)


@router.post("/app/subscription/reissue")
async def subscription_reissue(request: Request) -> RedirectResponse:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        if user.remnawave_uuid is None:
            return RedirectResponse(f"/app/subscription?err={quote_plus('Нет активной подписки')}", status_code=303)
        rw_uuid = str(user.remnawave_uuid)
    rw = RemnaWaveClient(settings)
    try:
        await rw.reset_user_subscription_credentials(rw_uuid, revoke_only_passwords=False)
    except RemnaWaveError as e:
        return RedirectResponse(f"/app/subscription?err={quote_plus(str(e)[:160])}", status_code=303)
    return RedirectResponse("/app/subscription?n=reissue_ok", status_code=303)
