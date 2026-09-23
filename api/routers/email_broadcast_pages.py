"""Веб-админка: рассылка по почте (новости/акции) — только пользователям, давшим согласие.

Тот же паттерн, что mass_grant_pages.py: общий _layout/_require_login из web_admin (ленивый импорт).
"""

from __future__ import annotations

import html as html_lib
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import get_settings
from shared.datetime_msk import fmt_dt_msk
from shared.services import email_marketing
from shared.services.email_code_service import normalize_email
from shared.services.email_sender import email_sending_configured, render_email, site_url

router = APIRouter(tags=["web-admin-email-broadcast"])

_BASE = "/admin/broadcast/email"


def _esc(s: object) -> str:
    return html_lib.escape(str(s or ""), quote=True)


def _campaign_from_form(kind: str, subject: str, title: str, text: str, button_text: str, button_url: str):
    return email_marketing.EmailCampaign(
        subject=subject.strip(),
        title=title.strip(),
        text=text.strip(),
        button_text=button_text.strip(),
        button_url=button_url.strip(),
        kind="promo" if kind == "promo" else "info",
    )


def _validate(c: email_marketing.EmailCampaign) -> str | None:
    if not c.subject:
        return "Укажите тему письма"
    if not c.text:
        return "Укажите текст письма"
    if bool(c.button_text) != bool(c.button_url):
        return "Для кнопки нужны и текст, и ссылка (или оставьте оба поля пустыми)"
    if c.button_url and not c.button_url.startswith(("https://", "http://")):
        return "Ссылка кнопки должна начинаться с https://"
    return None


def _progress_html() -> str:
    p = email_marketing.PROGRESS
    if p.started_at is None:
        return ""
    done = p.sent + p.failed
    pct = int(done * 100 / p.total) if p.total else (0 if p.running else 100)
    state = (
        "<span class='badge badge-info gap-1'><span class='loading loading-spinner loading-xs'></span>Идёт отправка</span>"
        if p.running
        else "<span class='badge badge-success'>Завершена</span>"
    )
    errors = ""
    if p.errors:
        errors = (
            "<details class='text-xs opacity-80'><summary class='cursor-pointer'>Ошибки (первые 20)</summary>"
            "<ul class='list-disc list-inside mt-1 font-mono'>"
            + "".join(f"<li>{_esc(e)}</li>" for e in p.errors)
            + "</ul></details>"
        )
    stop = (
        f"<form method='post' action='{_BASE}/stop' data-remna-confirm-msg='Остановить рассылку?'>"
        "<button class='btn btn-warning btn-sm h-9 min-h-9 gap-1.5'><i class='fa-solid fa-stop'></i>Остановить</button></form>"
        if p.running
        else ""
    )
    refresh = "<script>setTimeout(function(){location.reload();},5000);</script>" if p.running else ""
    return f"""
    <div class="card bg-base-100 border border-primary/25 shadow">
      <div class="card-body gap-3">
        <div class="flex flex-wrap items-center gap-3">
          <h2 class="text-lg font-semibold">Последняя рассылка</h2>{state}
        </div>
        <p class="text-sm">Тема: <b>{_esc(p.subject)}</b> · запустил {_esc(p.started_by or '—')} · {_esc(fmt_dt_msk(p.started_at))}</p>
        <progress class="progress progress-primary w-full" value="{pct}" max="100"></progress>
        <p class="text-sm">Отправлено: <b>{p.sent}</b> · Ошибок: <b>{p.failed}</b> · Всего получателей: <b>{p.total}</b></p>
        {f"<p class='text-sm text-error'>{_esc(p.last_error)}</p>" if p.last_error else ""}
        {errors}
        {stop}
      </div>
    </div>{refresh}"""


@router.get("/broadcast/email")
async def admin_email_broadcast_page(request: Request) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        consent, total = await email_marketing.count_marketing_recipients(session)

    ok_msg = (request.query_params.get("ok") or "").strip()
    err_msg = (request.query_params.get("err") or "").strip()
    alerts = ""
    if ok_msg:
        alerts += f"<div class='alert alert-success'><span>{_esc(ok_msg)}</span></div>"
    if err_msg:
        alerts += f"<div class='alert alert-error'><span>{_esc(err_msg)}</span></div>"
    smtp_ok = email_sending_configured(settings)
    smtp_badge = (
        "<span class='badge badge-success'>SMTP настроен</span>"
        if smtp_ok
        else "<a href='/admin/settings' class='badge badge-warning'>SMTP не настроен — Настройки → «Почта и Google»</a>"
    )
    default_test = _esc((settings.smtp_user or "").strip())

    body = f"""
    <div class="flex flex-col gap-4">
      <div class="flex flex-wrap items-center gap-3">
        <h1 class="text-xl font-bold"><i class="fa-solid fa-envelope-open-text text-primary mr-2"></i>Рассылка на почту</h1>
        {smtp_badge}
        <a href="/admin/broadcast" class="btn btn-ghost btn-sm h-8 min-h-8 gap-1.5"><i class="fa-brands fa-telegram"></i>Рассылка в Telegram</a>
      </div>
      {alerts}
      <div class="grid gap-3 sm:grid-cols-2">
        <div class="stat rounded-2xl border border-base-content/10 bg-base-100">
          <div class="stat-title">Получат рассылку</div>
          <div class="stat-value text-primary">{consent}</div>
          <div class="stat-desc">дали согласие на новости и акции</div>
        </div>
        <div class="stat rounded-2xl border border-base-content/10 bg-base-100">
          <div class="stat-title">Всего с подтверждённой почтой</div>
          <div class="stat-value">{total}</div>
          <div class="stat-desc">остальные получают только чеки и уведомления о подписке</div>
        </div>
      </div>
      {_progress_html()}
      <details class="card bg-base-100 border border-base-content/10 shadow">
        <summary class="card-body cursor-pointer select-none py-4"><span class="font-semibold"><i class="fa-solid fa-circle-info text-info mr-2"></i>Как это работает</span></summary>
        <div class="card-body pt-0 text-sm leading-relaxed opacity-85 flex flex-col gap-2">
          <p><b>Кому уходит.</b> Только пользователям с подтверждённой почтой, которые при привязке поставили галочку «Получать новости и акции»
            (или включили её потом в профиле на сайте / в боте). Заблокированным не отправляется.</p>
          <p><b>Что приходит всегда, без согласия.</b> Код подтверждения, чек о пополнении баланса, «подписка продлена», напоминания за 3 дня и 6 часов до конца подписки.</p>
          <p><b>Отписка.</b> В каждом письме рассылки есть ссылка «Отписаться» (и кнопка отписки в Gmail/Яндекс) — она выключает только рассылку.</p>
          <p><b>Лимиты Gmail.</b> Около 500 писем в сутки с обычного ящика. Письма уходят по одному раз в ~1,5 секунды; при упоре в дневной лимит рассылка остановится сама —
            продолжите на следующий день. Для больших баз подключите Resend или SMTP на своём домене.</p>
          <p><b>Совет.</b> Сначала нажмите «Предпросмотр» и «Тест на адрес», проверьте письмо у себя, и только потом «Отправить всем».</p>
        </div>
      </details>
      <div class="card bg-base-100 border border-base-content/10 shadow">
        <div class="card-body gap-4">
          <form id="eb-form" method="post" action="{_BASE}/test" class="flex flex-col gap-4">
            <div class="flex flex-wrap gap-4">
              <label class="flex items-center gap-2 cursor-pointer"><input type="radio" name="kind" value="info" class="radio radio-sm" checked/><span>Информационная (новости)</span></label>
              <label class="flex items-center gap-2 cursor-pointer"><input type="radio" name="kind" value="promo" class="radio radio-sm"/><span>Рекламная (акция)</span></label>
            </div>
            <label class="form-control"><span class="label-text text-xs opacity-70">Тема письма (видна в списке входящих)</span>
              <input name="subject" required maxlength="150" class="input input-bordered input-sm h-9 min-h-9" placeholder="🔥 Скидка 30% на подписку до воскресенья"/></label>
            <label class="form-control"><span class="label-text text-xs opacity-70">Заголовок внутри письма (пусто — как тема)</span>
              <input name="title" maxlength="150" class="input input-bordered input-sm h-9 min-h-9" placeholder="Скидка 30% на любой тариф"/></label>
            <label class="form-control"><span class="label-text text-xs opacity-70">Текст (переносы строк сохраняются, HTML не поддерживается)</span>
              <textarea name="text" required rows="7" class="textarea textarea-bordered text-sm" placeholder="Привет! ..."></textarea></label>
            <div class="grid gap-3 md:grid-cols-2">
              <label class="form-control"><span class="label-text text-xs opacity-70">Текст кнопки (необязательно)</span>
                <input name="button_text" maxlength="60" class="input input-bordered input-sm h-9 min-h-9" placeholder="Продлить со скидкой"/></label>
              <label class="form-control"><span class="label-text text-xs opacity-70">Ссылка кнопки</span>
                <input name="button_url" class="input input-bordered input-sm h-9 min-h-9 font-mono text-xs" placeholder="{_esc(site_url(settings, '/app/subscription') or 'https://')}"/></label>
            </div>
            <div class="flex flex-wrap items-end gap-2 border-t border-base-content/10 pt-4">
              <button type="submit" formaction="{_BASE}/preview" formtarget="_blank" formnovalidate class="btn btn-ghost btn-sm h-9 min-h-9 gap-1.5 border border-base-content/15"><i class="fa-solid fa-eye"></i>Предпросмотр</button>
              <label class="form-control"><span class="label-text text-xs opacity-70">Тестовый адрес</span>
                <input name="test_email" type="email" value="{default_test}" class="input input-bordered input-sm h-9 min-h-9 w-60 font-mono text-xs"/></label>
              <button type="submit" formaction="{_BASE}/test" class="btn btn-outline btn-sm h-9 min-h-9 gap-1.5"><i class="fa-solid fa-paper-plane"></i>Тест на адрес</button>
            </div>
          </form>
          <form id="eb-send-form" method="post" action="{_BASE}/send" class="flex justify-end border-t border-base-content/10 pt-4"
            data-remna-confirm-msg="Отправить письмо {consent} получателям? Отменить уже отправленные письма нельзя.">
            <input type="hidden" name="kind"/><input type="hidden" name="subject"/><input type="hidden" name="title"/>
            <input type="hidden" name="text"/><input type="hidden" name="button_text"/><input type="hidden" name="button_url"/>
            <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5" {'disabled' if not smtp_ok or consent == 0 else ''}>
              <i class="fa-solid fa-bullhorn"></i>Отправить всем ({consent})</button>
          </form>
        </div>
      </div>
    </div>
    <script>
    (function(){{
      var f=document.getElementById('eb-form'); if(!f) return;
      var KEY='remna-email-broadcast-draft', names=['subject','title','text','button_text','button_url','kind'];
      try {{
        var d=JSON.parse(localStorage.getItem(KEY)||'{{}}');
        names.forEach(function(n){{
          if(d[n]==null) return;
          if(n==='kind'){{ var r=f.querySelector('input[name=kind][value="'+d[n]+'"]'); if(r) r.checked=true; }}
          else if(f.elements[n]) f.elements[n].value=d[n];
        }});
      }} catch(e) {{}}
      var sf=document.getElementById('eb-send-form');
      if(sf) sf.addEventListener('submit',function(){{
        ['subject','title','text','button_text','button_url'].forEach(function(n){{ sf.elements[n].value=f.elements[n].value; }});
        sf.elements['kind'].value=(f.querySelector('input[name=kind]:checked')||{{}}).value||'info';
      }});
      f.addEventListener('input',function(){{
        try {{
          var d={{}}; names.forEach(function(n){{ d[n]= n==='kind' ? (f.querySelector('input[name=kind]:checked')||{{}}).value : f.elements[n].value; }});
          localStorage.setItem(KEY, JSON.stringify(d));
        }} catch(e) {{}}
      }});
    }})();
    </script>
    """
    return _layout("Рассылка на почту", body, request=request)


@router.post("/broadcast/email/preview")
async def admin_email_broadcast_preview(
    request: Request,
    kind: str = Form("info"),
    subject: str = Form(""),
    title: str = Form(""),
    text: str = Form(""),
    button_text: str = Form(""),
    button_url: str = Form(""),
) -> HTMLResponse:
    from api.routers.web_admin import _require_login

    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    c = _campaign_from_form(kind, subject, title, text, button_text, button_url)
    html = render_email(
        title=c.title or c.subject or "Заголовок письма",
        intro=c.text or "Текст письма",
        button_text=c.button_text or None,
        button_url=c.button_url or None,
        badge="Акция" if c.kind == "promo" else "Новости",
        unsubscribe_url=site_url(settings, "/email/unsubscribe?t=preview") or "#",
        settings=settings,
    )
    return HTMLResponse(html)


@router.post("/broadcast/email/test")
async def admin_email_broadcast_test(
    request: Request,
    kind: str = Form("info"),
    subject: str = Form(""),
    title: str = Form(""),
    text: str = Form(""),
    button_text: str = Form(""),
    button_url: str = Form(""),
    test_email: str = Form(""),
) -> RedirectResponse:
    from api.routers.web_admin import _require_login

    denied = _require_login(request)
    if denied is not None:
        return denied
    c = _campaign_from_form(kind, subject, title, text, button_text, button_url)
    err = _validate(c)
    to = normalize_email(test_email)
    if err is None and to is None:
        err = "Укажите корректный тестовый адрес"
    if err:
        return RedirectResponse(f"{_BASE}?err={quote_plus(err)}", status_code=303)
    c.subject = "[ТЕСТ] " + c.subject
    ok, send_err = await email_marketing.send_campaign_email(to, None, c, get_settings())
    if not ok:
        return RedirectResponse(f"{_BASE}?err={quote_plus(send_err[:300])}", status_code=303)
    return RedirectResponse(f"{_BASE}?ok={quote_plus(f'Тестовое письмо отправлено на {to}')}", status_code=303)


@router.post("/broadcast/email/send")
async def admin_email_broadcast_send(
    request: Request,
    kind: str = Form("info"),
    subject: str = Form(""),
    title: str = Form(""),
    text: str = Form(""),
    button_text: str = Form(""),
    button_url: str = Form(""),
) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _web_admin_actor_label

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not email_sending_configured(get_settings()):
        return RedirectResponse(f"{_BASE}?err={quote_plus('Отправка писем не настроена')}", status_code=303)
    c = _campaign_from_form(kind, subject, title, text, button_text, button_url)
    err = _validate(c)
    if err:
        return RedirectResponse(f"{_BASE}?err={quote_plus(err)}", status_code=303)
    if not email_marketing.start_campaign(c, started_by=_web_admin_actor_label(request)):
        return RedirectResponse(f"{_BASE}?err={quote_plus('Уже идёт другая рассылка — дождитесь окончания')}", status_code=303)
    return RedirectResponse(f"{_BASE}?ok={quote_plus('Рассылка запущена')}", status_code=303)


@router.post("/broadcast/email/stop")
async def admin_email_broadcast_stop(request: Request) -> RedirectResponse:
    from api.routers.web_admin import _require_login

    denied = _require_login(request)
    if denied is not None:
        return denied
    email_marketing.cancel_campaign()
    return RedirectResponse(f"{_BASE}?ok={quote_plus('Рассылка останавливается…')}", status_code=303)
