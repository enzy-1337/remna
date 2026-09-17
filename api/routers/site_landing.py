"""Публичный лендинг Flux Network (замена стаб-страницы public_pages.public_stub)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shared.config import get_settings
from api.routers.site_theme import (
    COPY_JS,
    esc,
    icon,
    page,
    public_topbar,
    site_footer,
    tg_logo_svg,
)

router = APIRouter()

REF_COOKIE = "flux_ref"
REF_COOKIE_MAX_AGE = 60 * 60 * 24 * 30

_FEATURES = [
    ("globe", "64 локации", "Европа, США, Азия и СНГ. Переключение сервера без разрыва соединения."),
    ("bolt", "До 1 Гбит/с", "WireGuard и собственные линии. 4K-стримы и звонки без фризов."),
    ("lock", "Без логов", "Мы не храним историю подключений и трафик. Оплата в том числе криптой."),
    ("devices", "5 платформ", "Единый интерфейс на iOS, Android, Windows, macOS и Linux. До 5 устройств."),
]


def render_landing_page(*, ref_code: str = "") -> str:
    settings = get_settings()
    features_html = "".join(
        f"""
    <div class="card fade-up" style="padding:24px;">
      <div style="width:38px;height:38px;border-radius:11px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;">
        {icon(ic, size=19, color='#7B5CFF')}
      </div>
      <div style="font:800 16px Manrope;color:var(--text-1);margin-top:16px;">{esc(title)}</div>
      <div style="font:500 13px Manrope;color:var(--text-3);margin-top:7px;line-height:1.55;">{esc(desc)}</div>
    </div>"""
        for ic, title, desc in _FEATURES
    )
    bot_username = (settings.bot_username or "").strip().lstrip("@")
    bot_url = f"https://t.me/{bot_username}" if bot_username else "#"

    body = f"""
<div class="hero-bg">
  <div class="shell-wide">
    {public_topbar(active="features")}

    <div style="text-align:center;padding:96px 0 0;" class="fade-up d1">
      <div style="display:inline-flex;align-items:center;gap:8px;background:rgba(123,92,255,.1);border:1px solid rgba(123,92,255,.28);border-radius:999px;padding:7px 16px;">
        <div class="pulse" style="width:7px;height:7px;border-radius:50%;background:var(--accent);"></div>
        <span style="font:700 12px Manrope;color:var(--accent-softer);">64 локации · WireGuard · без логов</span>
      </div>
      <div style="font:800 clamp(34px,6vw,68px) Manrope;color:var(--text-1);letter-spacing:-.03em;line-height:1.08;margin-top:26px;">
        Интернет без границ<br>и без наблюдения
      </div>
      <div style="font:500 19px Manrope;color:var(--text-3);margin:22px auto 0;max-width:620px;line-height:1.55;">
        Одна подписка на все устройства: iOS, Android, Windows, macOS, Linux и Telegram Mini App. Подключение в один тап, скорость до 1 Гбит/с.
      </div>
      <div style="display:flex;align-items:center;justify-content:center;gap:14px;margin-top:38px;flex-wrap:wrap;">
        <a href="/login" class="btn btn-primary btn-lg">Войти и попробовать {icon('arrow-right', size=16, color='#fff', stroke=2.4)}</a>
        <a href="{esc(bot_url)}" class="btn btn-outline btn-lg">{tg_logo_svg(16, '#8A96A3')}<span>Открыть в Telegram</span></a>
      </div>
      <div style="display:flex;align-items:center;justify-content:center;gap:56px;margin-top:52px;flex-wrap:wrap;">
        <div><div class="mono" style="font:500 30px 'JetBrains Mono';color:var(--accent);">99.9%</div><div style="font:600 12px Manrope;color:var(--text-4);margin-top:4px;letter-spacing:.05em;">UPTIME</div></div>
        <div class="vdivider hide-mobile" style="height:40px;"></div>
        <div><div class="mono" style="font:500 30px 'JetBrains Mono';color:var(--accent);">64</div><div style="font:600 12px Manrope;color:var(--text-4);margin-top:4px;letter-spacing:.05em;">ЛОКАЦИИ</div></div>
        <div class="vdivider hide-mobile" style="height:40px;"></div>
        <div><div class="mono" style="font:500 30px 'JetBrains Mono';color:var(--accent);">18 ms</div><div style="font:600 12px Manrope;color:var(--text-4);margin-top:4px;letter-spacing:.05em;">СРЕДНИЙ ПИНГ</div></div>
      </div>
    </div>

    <div id="features" class="grid-auto fade-up d2" style="grid-template-columns:repeat(4,1fr);margin:100px 0 0;">
      {features_html}
    </div>

    <div id="pricing" class="card fade-up d3" style="margin:80px 0 0;padding:34px 40px;display:flex;align-items:center;justify-content:space-between;gap:24px;flex-wrap:wrap;">
      <div>
        <div style="font:800 24px Manrope;color:var(--text-1);">Первый день — бесплатно</div>
        <div style="font:500 14px Manrope;color:var(--text-3);margin-top:8px;">Тестовый период без привязки карты. Дальше — от 90 ₽ в месяц, оплата картой, СБП или USDT.</div>
      </div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;">
        <div style="background:var(--card-4);border:1px solid var(--line-2);border-radius:16px;padding:18px 24px;text-align:center;">
          <div style="font:600 12px Manrope;color:var(--text-3);">1 месяц</div>
          <div style="font:800 22px Manrope;color:var(--text-1);margin-top:6px;">149 ₽</div>
        </div>
        <div style="background:rgba(123,92,255,.1);border:1px solid rgba(123,92,255,.35);border-radius:16px;padding:18px 24px;text-align:center;position:relative;">
          <div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);font:800 9px Manrope;color:#fff;background:var(--accent);padding:3px 8px;border-radius:6px;white-space:nowrap;">ВЫГОДНО</div>
          <div style="font:600 12px Manrope;color:var(--accent-softer);">6 месяцев</div>
          <div style="font:800 22px Manrope;color:#fff;margin-top:6px;">119 ₽<span style="font:600 11px Manrope;color:var(--accent-mut);">/мес</span></div>
        </div>
        <div style="background:var(--card-4);border:1px solid var(--line-2);border-radius:16px;padding:18px 24px;text-align:center;">
          <div style="font:600 12px Manrope;color:var(--text-3);">12 месяцев</div>
          <div style="font:800 22px Manrope;color:var(--text-1);margin-top:6px;">90 ₽<span style="font:600 11px Manrope;color:var(--text-4);">/мес</span></div>
        </div>
      </div>
    </div>

    <div id="apps" style="margin:60px 0 0;"></div>
    {site_footer()}
  </div>
</div>
<script>{COPY_JS}</script>
"""
    return page(title="Flux Network — VPN без границ", body=body)


_LEGAL_PAGES = {
    "privacy": (
        "Политика конфиденциальности",
        "Мы обрабатываем только данные, необходимые для работы сервиса (Telegram ID, платёжные метаданные, "
        "техническую статистику подключений) и не передаём их третьим лицам, кроме случаев, предусмотренных "
        "законом. Полный текст политики уточняйте у поддержки — @{support}.",
    ),
    "offer": (
        "Публичная оферта",
        "Оплачивая подписку Flux Network, вы соглашаетесь с условиями предоставления доступа к VPN-сервису: "
        "оплата за выбранный период, автопродление можно отключить в личном кабинете, лимиты трафика и устройств "
        "указаны на странице тарифа.",
    ),
    "refund": (
        "Политика возврата",
        "Если сервис не заработал по нашей вине и вопрос не удалось решить через поддержку, мы возвращаем "
        "средства за неиспользованный период. Обратитесь в тикет поддержки в личном кабинете — рассмотрим "
        "обращение в течение 24 часов.",
    ),
}


@router.get("/legal/{slug}")
async def legal_page(slug: str) -> HTMLResponse:
    settings = get_settings()
    support = (settings.bot_username or "fluxvpn_support").strip().lstrip("@")
    item = _LEGAL_PAGES.get(slug)
    if item is None:
        title, text = "Документ не найден", "Такой страницы нет."
    else:
        title, text = item
        text = text.format(support=support)
    body = f"""
<div class="hero-bg" style="min-height:100vh;">
  <div class="shell" style="padding-top:40px;">
    {public_topbar()}
    <div class="card fade-up" style="max-width:760px;margin:48px auto;padding:36px;">
      <h1 style="font:800 26px Manrope;color:var(--text-1);margin:0 0 16px;">{esc(title)}</h1>
      <p style="font:500 14.5px Manrope;color:var(--text-3);line-height:1.7;">{esc(text)}</p>
      <a href="/" class="btn btn-outline" style="margin-top:20px;">{icon('arrow-right', size=14)}<span>На главную</span></a>
    </div>
    {site_footer()}
  </div>
</div>
"""
    return HTMLResponse(page(title=title, body=body))
