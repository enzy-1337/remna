"""Помощь: контакты поддержки + FAQ (контент — реальные особенности сервиса)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shared.config import get_settings
from shared.database import get_session_factory
from shared.services.site_session_service import load_site_user, touch_session

from api.routers.site_theme import (
    ACCORDION_JS,
    app_topbar,
    esc,
    fmt_money,
    icon,
    page,
    public_topbar,
    site_footer,
    tg_logo_svg,
)

router = APIRouter()

_FAQ = [
    (
        "shield",
        "Безопасно ли пользоваться вашим сервисом?",
        "Да. Мы не ведём логи подключений и не передаём данные третьим лицам. Вход в личный кабинет можно "
        "дополнительно защитить двухфакторной аутентификацией (Google Authenticator) в разделе «Профиль».",
    ),
    (
        "link",
        "Как подключиться?",
        "Войдите в личный кабинет, оформите тариф и откройте бота Flux Network в Telegram — он выдаст ссылку "
        "подписки и подключаемую конфигурацию для вашего устройства в один тап.",
    ),
    (
        "devices",
        "Какое приложение использовать?",
        "iOS, Android, Windows, macOS и Linux — для каждой платформы есть отдельное приложение Flux Network "
        "с единым интерфейсом, плюс Telegram Mini App. Ссылки на загрузку доступны в боте после активации подписки.",
    ),
    (
        "wallet",
        "Как пополнить баланс?",
        "В личном кабинете нажмите на баланс в шапке сайта или откройте бота — доступна оплата картой, СБП "
        "или криптовалютой.",
    ),
    (
        "people",
        "Сколько устройств поддерживает подписка?",
        "По умолчанию — 2 устройства одновременно на одну подписку, слоты можно докупить в боте вплоть до 15.",
    ),
    (
        "wifi",
        "Можно ли настроить на роутер?",
        "Да, если роутер поддерживает клиент для соответствующего протокола. Конфигурацию можно получить в "
        "разделе подписки — обратитесь в тикет поддержки, если нужна помощь с настройкой конкретной модели.",
    ),
    (
        "clock",
        "Как отключить автопродление?",
        "В карточке подписки в личном кабинете или в боте — переключатель автопродления отключает списание "
        "за следующий период, доступ сохраняется до конца уже оплаченного срока.",
    ),
    (
        "coupon",
        "Как активировать купон?",
        "На главной странице личного кабинета нажмите «Купон» в быстрых действиях и введите промокод — "
        "бонус зачислится сразу.",
    ),
]


def _faq_accordion() -> str:
    items = ""
    for i, (ic, q, a) in enumerate(_FAQ):
        open_cls = " open" if i == 0 else ""
        items += f"""
        <div class="acc-item{open_cls}" data-acc-item>
          <div class="acc-head" data-acc-head>
            {icon(ic, size=16, color='var(--accent-soft)')}
            <span class="acc-title">{esc(q)}</span>
            <span class="acc-chevron">{icon('chevron-down', size=15)}</span>
          </div>
          <div class="acc-body"><p>{esc(a)}</p></div>
        </div>"""
    return items


def _contact_cards(settings) -> str:
    support = (settings.support_username or settings.bot_username or "").strip().lstrip("@")
    channel = (settings.required_channel_username or "").strip().lstrip("@")
    support_href = f"https://t.me/{support}" if support else "/app/tickets"
    channel_href = f"https://t.me/{channel}" if channel else "#"
    return f"""
    <div class="grid-auto" style="grid-template-columns:1fr 1fr;">
      <a href="{esc(support_href)}" class="card" style="display:flex;align-items:center;gap:14px;">
        <div style="width:44px;height:44px;border-radius:13px;background:linear-gradient(140deg,var(--accent),var(--accent-2));display:flex;align-items:center;justify-content:center;flex-shrink:0;">{icon('headset', size=20, color='#fff')}</div>
        <div style="flex:1;"><div style="font:700 14px Manrope;color:var(--text-1);">Техподдержка</div><div style="font:500 12px Manrope;color:var(--text-4);margin-top:2px;">{('@' + esc(support)) if support else 'Тикет в кабинете'}</div></div>
        {icon('chevron-right', size=15, color='var(--text-4)')}
      </a>
      <a href="{esc(channel_href)}" class="card" style="display:flex;align-items:center;gap:14px;">
        <div style="width:44px;height:44px;border-radius:13px;background:linear-gradient(140deg,#2AABEE,#229ED9);display:flex;align-items:center;justify-content:center;flex-shrink:0;">{tg_logo_svg(20,'#fff')}</div>
        <div style="flex:1;"><div style="font:700 14px Manrope;color:var(--text-1);">Чат Flux</div><div style="font:500 12px Manrope;color:var(--text-4);margin-top:2px;">Обсуждения и новости</div></div>
        {icon('chevron-right', size=15, color='var(--text-4)')}
      </a>
    </div>"""


def _docs_card() -> str:
    docs = [("Политика конфиденциальности", "/legal/privacy"), ("Публичная оферта", "/legal/offer"), ("Политика возврата", "/legal/refund")]
    rows = "".join(
        f"""
        <a href="{href}" style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);">
          {icon('file', size=15, color='var(--accent-soft)')}
          <span style="flex:1;font:600 13px Manrope;color:var(--text-2);">{esc(title)}</span>
          {icon('chevron-right', size=14, color='var(--text-4)')}
        </a>"""
        for title, href in docs
    )
    return f'<div class="card"><div class="section-label">{icon("file", size=14)}<span>Документы</span></div><div style="margin-top:6px;">{rows}</div></div>'


def render_help_body(*, authed: bool, balance_rub: str = "0", initial: str = "") -> str:
    settings = get_settings()
    topbar = app_topbar(active="help", balance_rub=balance_rub, unread_tickets=0, initial=initial) if authed else public_topbar(active="help")
    return f"""
<div class="{'cabinet-bg' if authed else 'hero-bg'}" style="min-height:100vh;">
  <div class="shell-wide">
    {topbar}
    <div class="fade-up" style="display:flex;align-items:center;gap:14px;margin-top:12px;">
      <div style="width:34px;height:34px;border-radius:11px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;">{icon('help', size=17, color='#7B5CFF')}</div>
      <div><div style="font:800 22px Manrope;color:var(--text-1);">Помощь</div><div style="font:500 13px Manrope;color:var(--text-3);">Ответы на вопросы и поддержка</div></div>
    </div>

    <div class="grid-auto fade-up d1 cols-2" style="grid-template-columns:1fr 360px;margin-top:20px;">
      <div style="display:flex;flex-direction:column;gap:16px;min-width:0;">
        {_contact_cards(settings)}
        <div class="card">
          <div class="section-label">{icon('bolt', size=14)}<span>Частые вопросы</span></div>
          <div style="margin-top:10px;">{_faq_accordion()}</div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:16px;min-width:0;">
        {_docs_card()}
      </div>
    </div>
    {site_footer()}
  </div>
</div>
<script>{ACCORDION_JS}</script>
"""


@router.get("/app/help")
async def help_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is not None:
            user, sess_row = auth
            await touch_session(session, sess_row)
            initial = (user.first_name or user.username or "U")[:1].upper()
            body = render_help_body(authed=True, balance_rub=fmt_money(user.balance), initial=initial)
            return HTMLResponse(page(title="Помощь — Flux Network", body=body))
    return HTMLResponse(page(title="Помощь — Flux Network", body=render_help_body(authed=False)))
