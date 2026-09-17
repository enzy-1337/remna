"""Реферальная программа: ссылки (сайт + бот), сводка, список друзей. Без уровней/XP (см. план MVP)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import get_settings
from shared.database import get_session_factory
from shared.services.referral_service import (
    count_invited_users,
    list_referrer_rewards_with_referred,
    sum_referrer_bonus_rub,
)
from shared.services.site_session_service import load_site_user, touch_session

from api.routers.site_theme import app_topbar, copy_field, esc, fmt_money, icon, page, site_footer, COPY_JS

router = APIRouter()


@router.get("/app/referrals")
async def referrals_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)

        invited_count = await count_invited_users(session, user.id)
        earned_rub = await sum_referrer_bonus_rub(session, user.id)
        rewards = await list_referrer_rewards_with_referred(session, user.id, limit=20)

    initial = (user.first_name or user.username or "U")[:1].upper()
    bot_username = (settings.bot_username or "").strip().lstrip("@")
    base_url = (settings.public_site_url or "").strip().rstrip("/")
    site_link = f"{base_url or ''}/?ref={user.referral_code}"
    bot_link = f"https://t.me/{bot_username}?start=ref_{user.referral_code}" if bot_username else ""
    pct = settings.referral_payment_percent

    friends_rows = "".join(
        f"""
        <div style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);">
          <div class="avatar-circle" style="width:34px;height:34px;background:#1A2129;font-size:13px;">{esc((ru.first_name or ru.username or '?')[:1].upper())}</div>
          <div style="flex:1;min-width:0;">
            <div style="font:700 13px Manrope;color:var(--text-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{esc(('@' + ru.username) if ru.username else (ru.first_name or f'#{ru.id}'))}</div>
          </div>
          <span class="mono" style="font:600 13px 'JetBrains Mono';color:var(--success);">+{fmt_money(rw.bonus_rub)} ₽</span>
        </div>"""
        for rw, ru in rewards[:12]
    ) or '<div style="opacity:.5;font:500 13px Manrope;padding:12px 0;">Пока нет начислений</div>'

    body = f"""
<div class="cabinet-bg" style="min-height:100vh;">
  <div class="shell-wide">
    {app_topbar(active="referrals", balance_rub=fmt_money(user.balance), unread_tickets=0, initial=initial)}

    <div class="fade-up" style="display:flex;align-items:center;gap:14px;margin-top:12px;">
      <div style="width:34px;height:34px;border-radius:11px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;">{icon('people', size=17, color='#7B5CFF')}</div>
      <div>
        <div style="font:800 22px Manrope;color:var(--text-1);">Реферальная программа</div>
        <div style="font:500 13px Manrope;color:var(--text-3);">Кэшбэк {esc(pct)}% с каждого пополнения приглашённых друзей</div>
      </div>
    </div>

    <div class="grid-auto fade-up d1 cols-2" style="grid-template-columns:1fr 360px;margin-top:20px;">
      <div style="display:flex;flex-direction:column;gap:16px;min-width:0;">
        <div class="card">
          <div class="section-label">Приглашайте друзей</div>
          <div style="font:500 12.5px Manrope;color:var(--text-4);margin-top:4px;">Две ссылки с одним кодом — делитесь любой</div>
          <div style="margin-top:16px;">{copy_field(value=site_link, label='ССЫЛКА НА САЙТ')}</div>
          <div style="margin-top:12px;">{copy_field(value=bot_link, label='ССЫЛКА НА TELEGRAM-БОТА')}</div>
          <div style="display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;align-items:center;">
            <button class="btn btn-primary" data-copy-field data-copy-value="{esc(site_link)}">{icon('copy', size=15, color='#fff')}<span>Копировать ссылку</span></button>
            <span class="mono badge badge-neutral" style="padding:8px 12px;">Код: {esc(user.referral_code)}</span>
          </div>
        </div>

        <div class="grid-auto" style="grid-template-columns:repeat(2,1fr);">
          <div class="card" style="text-align:center;">
            <div style="font:800 22px Manrope;color:var(--text-1);">{invited_count}</div>
            <div style="font:600 11px Manrope;color:var(--text-4);margin-top:4px;">Приглашено друзей</div>
          </div>
          <div class="card" style="text-align:center;">
            <div style="font:800 22px Manrope;color:var(--success);">{fmt_money(earned_rub)} ₽</div>
            <div style="font:600 11px Manrope;color:var(--text-4);margin-top:4px;">Заработано всего</div>
          </div>
        </div>

        <div class="card">
          <div class="section-label">ВАШИ ДРУЗЬЯ · {invited_count}</div>
          <div style="margin-top:8px;">{friends_rows}</div>
        </div>
      </div>

      <div style="display:flex;flex-direction:column;gap:16px;min-width:0;">
        <div class="card">
          <div style="display:flex;align-items:center;gap:9px;">{icon('bolt', size=16, color='#7B5CFF')}<span style="font:800 15px Manrope;color:var(--text-1);">Как зарабатывать с Flux</span></div>
          <div style="font:500 13px Manrope;color:var(--text-3);margin-top:8px;line-height:1.55;">Приглашайте друзей и получайте {esc(pct)}% с каждого их пополнения — без лимита по времени.</div>
          <div style="display:flex;flex-direction:column;gap:14px;margin-top:16px;">
            {"".join(_step(i, t, d) for i, (t, d) in enumerate([
              ("Поделитесь ссылкой", "Отправьте другу ссылку, код или откройте в Telegram"),
              ("Друг регистрируется", "На сайте или в Telegram-боте — аккаунт общий"),
              ("Друг пополняет баланс", f"С каждого пополнения вам начисляется {pct}% кэшбэка"),
              ("Получайте кэшбэк", "Бонус сразу зачисляется на баланс — тратьте на подписку"),
            ], start=1))}
          </div>
        </div>
      </div>
    </div>
    {site_footer()}
  </div>
</div>
<script>{COPY_JS}</script>
"""
    return HTMLResponse(page(title="Рефералка — Flux Network", body=body))


def _step(i: int, title: str, desc: str) -> str:
    return f"""
    <div style="display:flex;gap:12px;">
      <div style="width:26px;height:26px;border-radius:8px;background:rgba(123,92,255,.16);color:var(--accent-soft);display:flex;align-items:center;justify-content:center;font:800 12px Manrope;flex-shrink:0;">{i}</div>
      <div><div style="font:700 13px Manrope;color:var(--text-1);">{esc(title)}</div><div style="font:500 12px Manrope;color:var(--text-4);margin-top:2px;">{esc(desc)}</div></div>
    </div>"""
