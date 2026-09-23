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


def _price(p: Plan, prices: dict[int, Decimal] | None) -> Decimal:
    """Актуальная цена тарифа — та же, что в боте (resolve_plan_price_rub), иначе поле плана."""
    if prices and p.id in prices:
        return prices[p.id]
    return p.price_rub


def _pricing_html(plans: list[Plan], prices: dict[int, Decimal] | None = None) -> str:
    if not plans:
        return ""
    best_id = max(plans, key=lambda p: p.discount_percent).id if any(p.discount_percent > 0 for p in plans) else None
    tiles = []
    for p in plans:
        months = _plan_months(p.duration_days)
        per_month = (_price(p, prices) / months).quantize(Decimal("1"))
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


def _ru_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def _monthly_price(plans: list[Plan], prices: dict[int, Decimal] | None = None) -> Decimal | None:
    """Цена месячного тарифа (по нему считается «затем N ₽/мес»)."""
    monthly = [p for p in plans if _plan_months(p.duration_days) == 1]
    if monthly:
        return min(_price(p, prices) for p in monthly)
    return None


def _trial_block_html(*, code: str, days: int, price: Decimal | None, logged_in: bool) -> str:
    then = f"затем {fmt_money(price.quantize(Decimal('1')))} ₽/месяц" if price is not None else "затем по тарифу"
    days_s = _ru_days(days)
    if logged_in:
        # код попадает в страницу только для вошедших
        reveal = f"""
        <div id="trial-code-box" hidden style="margin-top:22px;">
          <div style="font:600 12px Manrope;color:var(--text-4);text-transform:uppercase;letter-spacing:.08em;">Ваш промокод</div>
          <div style="display:flex;align-items:center;justify-content:center;gap:10px;margin-top:10px;flex-wrap:wrap;">
            <div class="mono" style="font:800 30px 'JetBrains Mono',monospace;letter-spacing:.18em;color:#fff;background:rgba(123,92,255,.16);border:1px dashed rgba(157,133,255,.6);border-radius:14px;padding:12px 22px;">{esc(code)}</div>
            <button type="button" class="btn btn-outline" data-copy-field data-copy-value="{esc(code)}" onclick="var s=this.querySelector('span');s.textContent='Скопировано';setTimeout(function(){{s.textContent='Копировать';}},1500);">{icon('copy', size=15)}<span>Копировать</span></button>
          </div>
          <div style="font:500 13px Manrope;color:var(--text-3);margin-top:14px;">{esc(days_s)} бесплатно, {esc(then)}. Активируйте в личном кабинете → «Купон».</div>
          <a href="/app?promo={esc(code)}" class="btn btn-primary" style="margin-top:16px;display:inline-flex;">Активировать сейчас {icon('arrow-right', size=15, color='#fff', stroke=2.4)}</a>
        </div>"""
        button = f'<button type="button" class="btn btn-primary btn-lg" id="trial-reveal-btn">{icon("gift", size=17, color="#fff")}<span>Получить промокод</span></button>'
    else:
        reveal = ""
        button = (
            f'<button type="button" class="btn btn-primary btn-lg" data-open-login data-login-next="/?trial=1#free-trial">'
            f'{icon("gift", size=17, color="#fff")}<span>Получить промокод</span></button>'
            '<div style="font:500 12px Manrope;color:var(--text-4);margin-top:10px;">Нужно войти — это займёт пару секунд</div>'
        )
    return f"""
    <div id="free-trial" class="card fade-up" style="margin:60px 0 0;padding:38px 28px;text-align:center;position:relative;overflow:hidden;
      background:radial-gradient(120% 140% at 50% 0%,rgba(123,92,255,.22),rgba(17,24,32,.95) 60%);border:1px solid rgba(123,92,255,.35);">
      <div style="width:52px;height:52px;border-radius:16px;margin:0 auto;background:linear-gradient(140deg,var(--accent),var(--accent-2));display:flex;align-items:center;justify-content:center;box-shadow:0 16px 40px -14px rgba(123,92,255,.9);">{icon('gift', size=24, color='#fff')}</div>
      <div style="font:800 clamp(24px,4vw,34px) Manrope;color:var(--text-1);margin-top:18px;letter-spacing:-.02em;">Получи {esc(days_s)} подписки бесплатно</div>
      <div style="font:500 15px Manrope;color:var(--text-3);margin:10px auto 0;max-width:520px;line-height:1.55;">
        Попробуйте Flux VPN без оплаты: {esc(days_s)} полного доступа на всех устройствах, {esc(then)}. Без привязки карты — продление можно отключить в любой момент.
      </div>
      <div id="trial-cta" style="margin-top:24px;">{button}</div>
      {reveal}
    </div>"""


_TRIAL_JS = """
(function(){
  var btn = document.getElementById('trial-reveal-btn');
  var box = document.getElementById('trial-code-box');
  function reveal(){
    if (!box) return;
    box.hidden = false;
    var cta = document.getElementById('trial-cta'); if (cta) cta.hidden = true;
  }
  if (btn) btn.addEventListener('click', reveal);
  // вернулись сюда после входа — сразу показываем код
  if (/[?&]trial=1/.test(location.search) && box) {
    reveal();
    var el = document.getElementById('free-trial');
    if (el) setTimeout(function(){ el.scrollIntoView({behavior:'smooth', block:'center'}); }, 150);
    history.replaceState({}, '', location.pathname + '#free-trial');
  }
})();
"""


def render_landing_page(
    *,
    ref_code: str = "",
    plans: list[Plan] | None = None,
    logged_in: bool = False,
    prices: dict[int, Decimal] | None = None,
    month_price: Decimal | None = None,
) -> str:
    from api.routers.site_auth import login_modal_html

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

    cheapest = min((_price(p, prices) / _plan_months(p.duration_days) for p in plans), default=None)
    price_line = (
        f"Дальше — от {fmt_money(cheapest.quantize(Decimal('1')))} ₽ в месяц, оплата картой, СБП или криптовалютой."
        if cheapest is not None
        else "Оплата картой, СБП или криптовалютой."
    )
    pricing_html = _pricing_html(plans, prices)
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

    trial_code = (settings.landing_trial_promo_code or "").strip()
    trial_block = (
        _trial_block_html(
            code=trial_code,
            days=int(settings.landing_trial_days),
            price=month_price if month_price is not None else _monthly_price(plans, prices),
            logged_in=logged_in,
        )
        if trial_code
        else ""
    )
    hero_cta = (
        f'<a href="/app" class="btn btn-primary btn-lg">Личный кабинет {icon("arrow-right", size=16, color="#fff", stroke=2.4)}</a>'
        if logged_in
        else f'<a href="/login" class="btn btn-primary btn-lg">Войти и попробовать {icon("arrow-right", size=16, color="#fff", stroke=2.4)}</a>'
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
        {hero_cta}
        <a href="{esc(bot_url)}" class="btn btn-outline btn-lg">{tg_logo_svg(16, '#8A96A3')}<span>Открыть в Telegram</span></a>
      </div>
    </div>

    <div id="features" class="grid-auto fade-up d2" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin:100px 0 0;">
      {features_html}
    </div>

    {pricing_block}

    {trial_block}

    <div id="apps" style="margin:60px 0 0;"></div>
    {site_footer()}
  </div>
</div>
{'' if logged_in else login_modal_html()}
<script>{COPY_JS}</script>
<script>{_TRIAL_JS}</script>
"""
    return page(title="Flux Network — VPN без границ", body=body)


_LEGAL_PAGES = {
    "refund": (
        "Политика возврата",
        "Если сервис не заработал по нашей вине и вопрос не удалось решить через поддержку, мы возвращаем "
        "средства за неиспользованный период. Обратитесь в тикет поддержки в личном кабинете — рассмотрим "
        "обращение в течение 24 часов.",
    ),
}


_LEGAL_CSS = """
.legal-body{font:500 15px/1.75 Manrope;color:var(--text-2);}
.legal-body p{margin:0 0 14px;}
.legal-body strong,.legal-body b{color:var(--text-1);font-weight:800;}
.legal-body h3,.legal-body h4{font:800 18px Manrope;color:var(--text-1);margin:26px 0 10px;}
.legal-body blockquote{margin:16px 0;padding:12px 16px;border-left:3px solid var(--accent);background:rgba(123,92,255,.07);border-radius:0 10px 10px 0;color:var(--text-2);}
.legal-body a{color:var(--accent-softer);text-decoration:underline;}
.legal-body ul,.legal-body ol{padding-left:22px;margin:0 0 14px;}
@media (max-width:640px){ .legal-card{padding:22px 18px !important;margin:24px auto !important;} .legal-body{font-size:14.5px;} }
"""


def _legal_page_html(title: str, body_html: str, *, updated: str = "") -> str:
    return f"""
<style>{_LEGAL_CSS}</style>
<div class="hero-bg" style="min-height:100vh;">
  <div class="shell" style="padding-top:20px;">
    {public_topbar()}
    <div class="card fade-up legal-card" style="max-width:820px;margin:40px auto;padding:40px 44px;">
      <h1 style="font:800 clamp(24px,4vw,32px) Manrope;color:var(--text-1);margin:0;letter-spacing:-.02em;">{esc(title)}</h1>
      {f'<div style="font:500 12px Manrope;color:var(--text-4);margin-top:8px;">Редакция от {esc(updated)}</div>' if updated else ''}
      <div class="legal-body" style="margin-top:26px;">{body_html}</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:24px;border-top:1px solid var(--line);padding-top:20px;">
        <a href="/legal/privacy" class="btn btn-outline btn-sm">Политика конфиденциальности</a>
        <a href="/legal/terms" class="btn btn-outline btn-sm">Пользовательское соглашение</a>
        <a href="/" class="btn btn-outline btn-sm">На главную</a>
      </div>
    </div>
    {site_footer()}
  </div>
</div>
{_login_modal()}
"""


@router.get("/legal/{slug}")
async def legal_page(slug: str) -> HTMLResponse:
    from fastapi.responses import RedirectResponse

    from shared.database import get_session_factory
    from shared.datetime_msk import fmt_dt_msk
    from shared.services.legal_docs import DOCS, get_or_import_doc

    settings = get_settings()
    if slug == "offer":
        # раньше была короткая «оферта» — теперь полноценное пользовательское соглашение
        return RedirectResponse("/legal/terms", status_code=301)
    if slug in DOCS:
        factory = get_session_factory()
        async with factory() as session:
            doc = await get_or_import_doc(session, slug, settings)
        if doc is not None:
            updated = fmt_dt_msk(doc.get("updated_at"), with_suffix=False) if doc.get("updated_at") else ""
            return HTMLResponse(page(title=f"{doc['title']} — Flux Network", body=_legal_page_html(doc["title"], doc["content_html"], updated=updated.split(" ")[0])))
        support = (settings.support_username or settings.bot_username or "").strip().lstrip("@")
        return HTMLResponse(
            page(
                title=DOCS[slug][0],
                body=_legal_page_html(
                    DOCS[slug][0],
                    f"<p>Документ временно недоступен. Напишите в поддержку{(' — @' + esc(support)) if support else ''}, и мы пришлём актуальную редакцию.</p>",
                ),
            )
        )
    item = _LEGAL_PAGES.get(slug)
    if item is None:
        return HTMLResponse(page(title="Документ не найден", body=_legal_page_html("Документ не найден", "<p>Такой страницы нет.</p>")), status_code=404)
    title, body_text = item
    support = (settings.support_username or settings.bot_username or "").strip().lstrip("@")
    body_text = body_text.format(support_line=f" — @{support}" if support else "")
    return HTMLResponse(page(title=title, body=_legal_page_html(title, f"<p>{esc(body_text)}</p>")))


def _login_modal() -> str:
    from api.routers.site_auth import login_modal_html

    return login_modal_html()
