"""Публичный лендинг Flux Network (замена стаб-страницы public_pages.public_stub)."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shared.config import get_settings
from shared.models.plan import Plan
from api.routers.site_theme import (
    COPY_JS,
    esc,
    fmt_money,
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
    ("lock", "Без логов", "Не храним историю подключений и трафик. Оплата в том числе криптовалютой."),
    ("bolt", "Быстрое подключение", "Современное шифрование трафика и оптимизированные маршруты без разрывов."),
    ("devices", "5 платформ", "iOS, Android, Windows, macOS и Linux — один аккаунт и Telegram Mini App впридачу."),
    ("wallet", "Гибкая оплата", "Карта, СБП или криптовалюта. Автопродление можно отключить в один клик."),
]


def _plan_months(duration_days: int) -> int:
    return max(1, round(duration_days / 30))


def _pricing_html(plans: list[Plan]) -> str:
    if not plans:
        return ""
    best_id = max(plans, key=lambda p: p.discount_percent).id if any(p.discount_percent > 0 for p in plans) else None
    tiles = []
    for p in plans:
        months = _plan_months(p.duration_days)
        per_month = (p.price_rub / months).quantize(Decimal("1"))
        is_best = p.id == best_id
        badge = (
            '<div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);font:800 9px Manrope;'
            'color:#fff;background:var(--accent);padding:3px 8px;border-radius:6px;white-space:nowrap;">ВЫГОДНО</div>'
            if is_best
            else ""
        )
        box_style = (
            "background:rgba(123,92,255,.1);border:1px solid rgba(123,92,255,.35);"
            if is_best
            else "background:var(--card-4);border:1px solid var(--line-2);"
        )
        label_color = "var(--accent-softer)" if is_best else "var(--text-3)"
        price_color = "#fff" if is_best else "var(--text-1)"
        mo_color = "var(--accent-mut)" if is_best else "var(--text-4)"
        suffix = f'<span style="font:600 11px Manrope;color:{mo_color};">/мес</span>' if months > 1 else ""
        tiles.append(
            f"""
        <div style="{box_style}border-radius:16px;padding:18px 24px;text-align:center;position:relative;">
          {badge}
          <div style="font:600 12px Manrope;color:{label_color};">{esc(p.name)}</div>
          <div style="font:800 22px Manrope;color:{price_color};margin-top:6px;">{fmt_money(per_month)} ₽{suffix}</div>
        </div>"""
        )
    return "".join(tiles)


def render_landing_page(*, ref_code: str = "", plans: list[Plan] | None = None) -> str:
    settings = get_settings()
    plans = plans or []
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

    cheapest = min((p.price_rub / _plan_months(p.duration_days) for p in plans), default=None)
    price_line = (
        f"Дальше — от {fmt_money(cheapest.quantize(Decimal('1')))} ₽ в месяц, оплата картой, СБП или криптовалютой."
        if cheapest is not None
        else "Оплата картой, СБП или криптовалютой."
    )
    pricing_html = _pricing_html(plans)
    pricing_block = (
        f"""
    <div id="pricing" class="card fade-up d3" style="margin:80px 0 0;padding:34px 40px;display:flex;align-items:center;justify-content:space-between;gap:24px;flex-wrap:wrap;">
      <div>
        <div style="font:800 24px Manrope;color:var(--text-1);">Пробный период — бесплатно</div>
        <div style="font:500 14px Manrope;color:var(--text-3);margin-top:8px;">Без привязки карты. {esc(price_line)}</div>
      </div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;">{pricing_html}</div>
    </div>"""
        if pricing_html
        else ""
    )

    body = f"""
<div class="hero-bg">
  <div class="shell-wide">
    {public_topbar(active="features")}

    <div style="text-align:center;padding:96px 0 0;" class="fade-up d1">
      <div style="display:inline-flex;align-items:center;gap:8px;background:rgba(123,92,255,.1);border:1px solid rgba(123,92,255,.28);border-radius:999px;padding:7px 16px;">
        <div class="pulse" style="width:7px;height:7px;border-radius:50%;background:var(--accent);"></div>
        <span style="font:700 12px Manrope;color:var(--accent-softer);">Без логов · один аккаунт на все устройства</span>
      </div>
      <div style="font:800 clamp(34px,6vw,68px) Manrope;color:var(--text-1);letter-spacing:-.03em;line-height:1.08;margin-top:26px;">
        Интернет без границ<br>и без наблюдения
      </div>
      <div style="font:500 19px Manrope;color:var(--text-3);margin:22px auto 0;max-width:620px;line-height:1.55;">
        Одна подписка на все устройства: iOS, Android, Windows, macOS, Linux и Telegram Mini App. Оформление и продление — в боте или на сайте, пополнение картой, СБП или криптовалютой.
      </div>
      <div style="display:flex;align-items:center;justify-content:center;gap:14px;margin-top:38px;flex-wrap:wrap;">
        <a href="/login" class="btn btn-primary btn-lg">Войти и попробовать {icon('arrow-right', size=16, color='#fff', stroke=2.4)}</a>
        <a href="{esc(bot_url)}" class="btn btn-outline btn-lg">{tg_logo_svg(16, '#8A96A3')}<span>Открыть в Telegram</span></a>
      </div>
    </div>

    <div id="features" class="grid-auto fade-up d2" style="grid-template-columns:repeat(4,1fr);margin:100px 0 0;">
      {features_html}
    </div>

    {pricing_block}

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
        "законом. Полный текст политики уточняйте у поддержки{support_line}.",
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
    support = (settings.support_username or settings.bot_username or "").strip().lstrip("@")
    item = _LEGAL_PAGES.get(slug)
    if item is None:
        title, text = "Документ не найден", "Такой страницы нет."
    else:
        title, text = item
        support_line = f" — @{support}" if support else ""
        text = text.format(support_line=support_line)
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
