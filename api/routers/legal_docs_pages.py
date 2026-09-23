"""Веб-админка: юридические документы сайта (/legal/privacy, /legal/terms) — просмотр, правка, импорт из Telegra.ph.

Доступ — как у «Настроек» (супер-админ, см. web_admin_rbac: префикс /admin/settings).
"""

from __future__ import annotations

import html as html_lib
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import get_settings
from shared.datetime_msk import fmt_dt_msk
from shared.services.legal_docs import (
    DOCS,
    get_doc,
    import_doc,
    plain_text_to_html,
    public_doc_url,
    sanitize_html,
    save_doc,
    telegraph_path,
)

router = APIRouter(tags=["web-admin-legal-docs"])

_BASE = "/admin/settings/documents"


def _esc(s: object) -> str:
    return html_lib.escape(str(s or ""), quote=True)


@router.get("/settings/documents")
async def admin_legal_docs_page(request: Request) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    ok = (request.query_params.get("ok") or "").strip()
    err = (request.query_params.get("err") or "").strip()
    alerts = ""
    if ok:
        alerts += f"<div class='alert alert-success'><span>{_esc(ok)}</span></div>"
    if err:
        alerts += f"<div class='alert alert-error'><span>{_esc(err)}</span></div>"

    cards = []
    async with await _session() as session:
        for slug, (default_title, src_attr) in DOCS.items():
            doc = await get_doc(session, slug)
            src = (doc or {}).get("source_url") or getattr(settings, src_attr) or ""
            url = public_doc_url(settings, slug) or f"/legal/{slug}"
            status = (
                f"<span class='badge badge-success'>на сайте · обновлён {_esc(fmt_dt_msk(doc['updated_at']))}</span>"
                if doc
                else "<span class='badge badge-warning'>ещё не импортирован — подтянется при первом открытии страницы</span>"
            )
            cards.append(
                f"""
      <div class="card bg-base-100 border border-base-content/10 shadow">
        <div class="card-body gap-4">
          <div class="flex flex-wrap items-center gap-3">
            <h2 class="text-lg font-semibold">{_esc((doc or {}).get('title') or default_title)}</h2>
            {status}
            <a class="link link-primary text-sm" href="{_esc(url)}" target="_blank" rel="noopener">{_esc(url)}</a>
          </div>
          <form method="post" action="{_BASE}/{slug}/import" class="flex flex-wrap items-end gap-2"
            data-remna-confirm-msg="Заменить текст на сайте содержимым страницы Telegra.ph?">
            <label class="form-control flex-1 min-w-64">
              <span class="label-text text-xs opacity-70">Импорт из Telegra.ph (заменит текущий текст)</span>
              <input name="source_url" value="{_esc(src)}" class="input input-bordered input-sm h-9 min-h-9 font-mono text-xs" placeholder="https://telegra.ph/..." />
            </label>
            <button class="btn btn-outline btn-sm h-9 min-h-9 gap-1.5"><i class="fa-solid fa-cloud-arrow-down"></i>Импортировать</button>
          </form>
          <form method="post" action="{_BASE}/{slug}/save" class="flex flex-col gap-2">
            <label class="form-control">
              <span class="label-text text-xs opacity-70">Заголовок</span>
              <input name="title" value="{_esc((doc or {}).get('title') or default_title)}" class="input input-bordered input-sm h-9 min-h-9" />
            </label>
            <label class="form-control">
              <span class="label-text text-xs opacity-70">Текст (HTML: &lt;p&gt;, &lt;strong&gt;, &lt;em&gt;, &lt;u&gt;, &lt;a&gt;, &lt;ul&gt;/&lt;li&gt;, &lt;h3&gt;, &lt;blockquote&gt;; можно вставить и обычный текст — абзацы по пустой строке)</span>
              <textarea name="content" rows="14" class="textarea textarea-bordered font-mono text-xs leading-relaxed">{_esc((doc or {}).get('content_html') or '')}</textarea>
            </label>
            <button class="btn btn-primary btn-sm h-9 min-h-9 w-fit gap-1.5"><i class="fa-solid fa-floppy-disk"></i>Сохранить на сайт</button>
          </form>
        </div>
      </div>"""
            )
        await session.commit()

    body = f"""
    <div class="flex flex-col gap-4">
      <div class="flex flex-wrap items-center gap-3">
        <h1 class="text-xl font-bold"><i class="fa-solid fa-file-contract text-primary mr-2"></i>Документы сайта</h1>
        <a href="/admin/settings" class="btn btn-ghost btn-sm h-8 min-h-8">← Настройки</a>
      </div>
      <div class="alert alert-info text-sm"><span>Эти страницы открываются по ссылкам из бота (экран «Информация»), с сайта и из окна входа.
        Правьте текст здесь — изменения видны сразу, Telegra.ph больше не нужен.</span></div>
      {alerts}
      {''.join(cards)}
    </div>"""
    return _layout("Документы сайта", body, request=request)


@router.post("/settings/documents/{slug}/save")
async def admin_legal_doc_save(request: Request, slug: str, title: str = Form(""), content: str = Form("")) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if slug not in DOCS:
        return RedirectResponse(_BASE, status_code=303)
    raw = (content or "").strip()
    if not raw:
        return RedirectResponse(f"{_BASE}?err={quote_plus('Текст пустой')}", status_code=303)
    body = sanitize_html(raw) if "<" in raw else plain_text_to_html(raw)
    async with await _session() as session:
        prev = await get_doc(session, slug)
        await save_doc(
            session, slug,
            title=(title or "").strip() or DOCS[slug][0],
            content_html=body,
            source_url=(prev or {}).get("source_url"),
        )
        await session.commit()
    return RedirectResponse(f"{_BASE}?ok={quote_plus('Сохранено — документ обновлён на сайте')}", status_code=303)


@router.post("/settings/documents/{slug}/import")
async def admin_legal_doc_import(request: Request, slug: str, source_url: str = Form("")) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if slug not in DOCS:
        return RedirectResponse(_BASE, status_code=303)
    if not telegraph_path(source_url):
        return RedirectResponse(f"{_BASE}?err={quote_plus('Нужна ссылка вида https://telegra.ph/...')}", status_code=303)
    try:
        async with await _session() as session:
            doc = await import_doc(session, slug, source_url.strip())
            await session.commit()
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"{_BASE}?err={quote_plus(f'Не удалось импортировать: {e}'[:300])}", status_code=303)
    return RedirectResponse(f"{_BASE}?ok={quote_plus('Импортировано: ' + doc['title'])}", status_code=303)
