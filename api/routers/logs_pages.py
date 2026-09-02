"""Веб-админка: вкладка "Логи" — вся активность (notifications_log: регистрации, оплаты,
промокоды, устройства, антифрод, массовые выдачи, рассылки, бэкапы, запуск сервисов и т.д.,
всё что проходит через notify_admin*() в shared/services/admin_notify.py) и отдельно
сырые ERROR+ трейсбеки (app_error_logs, см. shared/services/admin_error_log_handler.py).

Разделяет общую сессию/лэйаут с api/routers/web_admin.py (см. fraud_admin_pages.py/
mass_grant_pages.py для того же паттерна ленивого импорта _layout/_require_login/_session).
"""

from __future__ import annotations

import html as html_lib
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, or_, select

from shared.models.app_error_log import AppErrorLog
from shared.models.notification_log import NotificationLog

router = APIRouter(tags=["web-admin-logs"])

_DEFAULT_DAYS = 14
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000


def _esc(s: object) -> str:
    return html_lib.escape(str(s or ""), quote=True)


def _tab_link(tab: str, *, active_tab: str, q: str, days: int) -> str:
    cls = "tab tab-active" if tab == active_tab else "tab"
    label = "📋 Активность" if tab == "activity" else "🛑 Ошибки"
    from urllib.parse import urlencode

    qs = urlencode({"tab": tab, "q": q, "days": days})
    return f'<a class="{cls}" href="/admin/logs?{qs}">{label}</a>'


@router.get("/logs")
async def admin_logs_page(
    request: Request, tab: str = "activity", q: str = "", days: int = _DEFAULT_DAYS, limit: int = _DEFAULT_LIMIT
) -> HTMLResponse:
    from api.routers.web_admin import _fmt_dt_msk, _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied

    tab = tab if tab in ("activity", "errors") else "activity"
    try:
        days_n = max(1, min(365, int(days)))
    except (TypeError, ValueError):
        days_n = _DEFAULT_DAYS
    try:
        lim = max(10, min(_MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        lim = _DEFAULT_LIMIT
    query = (q or "").strip()
    since = datetime.now(UTC) - timedelta(days=days_n)

    rows_html: list[str] = []
    async with await _session() as session:
        if tab == "activity":
            stmt = select(NotificationLog).where(NotificationLog.sent_at >= since)
            if query:
                like = f"%{query}%"
                stmt = stmt.where(
                    or_(NotificationLog.message_text.ilike(like), NotificationLog.type.ilike(like))
                )
            stmt = stmt.order_by(desc(NotificationLog.sent_at)).limit(lim)
            for row in (await session.execute(stmt)).scalars():
                status_badge = {
                    "sent": "badge-success",
                    "failed": "badge-error",
                    "skipped_no_admin_chat": "badge-ghost",
                }.get(row.status, "badge-ghost")
                who = f'<a class="link link-primary" href="/admin/users/{row.user_id}">#{row.user_id}</a>' if row.user_id else "<span class='opacity-50'>система</span>"
                text = row.message_text or ""
                preview = text.splitlines()[0][:160] if text else ""
                rows_html.append(
                    "<tr class='border-b border-base-content/10 hover:bg-base-200/40 align-top'>"
                    f"<td class='whitespace-nowrap text-xs opacity-70'>{_esc(_fmt_dt_msk(row.sent_at))}</td>"
                    f"<td class='text-xs'><span class='badge badge-ghost badge-sm font-mono'>{_esc(row.type)}</span></td>"
                    f"<td class='text-sm'>{who}</td>"
                    f"<td class='text-xs'><span class='badge {status_badge} badge-sm'>{_esc(row.status)}</span></td>"
                    "<td class='text-sm max-w-[520px]'>"
                    f"<details><summary class='cursor-pointer'>{_esc(preview) or '(пусто)'}</summary>"
                    f"<pre class='whitespace-pre-wrap break-words text-xs mt-1 opacity-80'>{_esc(text)}</pre></details>"
                    "</td></tr>"
                )
            headers = "<th>Когда</th><th>Тип</th><th>Пользователь</th><th>Статус</th><th>Событие</th>"
        else:
            stmt = select(AppErrorLog).where(AppErrorLog.created_at >= since)
            if query:
                like = f"%{query}%"
                stmt = stmt.where(
                    or_(
                        AppErrorLog.message.ilike(like),
                        AppErrorLog.traceback.ilike(like),
                        AppErrorLog.logger_name.ilike(like),
                        AppErrorLog.service.ilike(like),
                    )
                )
            stmt = stmt.order_by(desc(AppErrorLog.created_at)).limit(lim)
            for row in (await session.execute(stmt)).scalars():
                msg = (row.message or "").splitlines()[0][:160] if row.message else ""
                full = row.message or ""
                if row.traceback:
                    full += "\n\n" + row.traceback
                rows_html.append(
                    "<tr class='border-b border-base-content/10 hover:bg-base-200/40 align-top'>"
                    f"<td class='whitespace-nowrap text-xs opacity-70'>{_esc(_fmt_dt_msk(row.created_at))}</td>"
                    f"<td class='text-xs'><span class='badge badge-ghost badge-sm'>{_esc(row.service)}</span></td>"
                    f"<td class='text-xs font-mono opacity-80'>{_esc(row.logger_name)}</td>"
                    "<td class='text-sm max-w-[560px]'>"
                    f"<details><summary class='cursor-pointer text-error'>{_esc(msg) or '(пусто)'}</summary>"
                    f"<pre class='whitespace-pre-wrap break-words text-xs mt-1 opacity-80'>{_esc(full)}</pre></details>"
                    "</td></tr>"
                )
            headers = "<th>Когда</th><th>Сервис</th><th>Логгер</th><th>Ошибка</th>"

    tabs_html = "".join(
        _tab_link(t, active_tab=tab, q=query, days=days_n) for t in ("activity", "errors")
    )

    body = f"""
    <div class="flex flex-col gap-4">
      <h1 class="text-xl font-bold">Логи</h1>
      <div class="tabs tabs-boxed w-fit">{tabs_html}</div>
      <form method="get" action="/admin/logs" class="flex flex-wrap items-end gap-2">
        <input type="hidden" name="tab" value="{_esc(tab)}" />
        <label class="form-control">
          <span class="label-text text-xs opacity-70">Поиск</span>
          <input type="text" name="q" value="{_esc(query)}" placeholder="текст события / тип / логгер"
            class="input input-bordered input-sm w-64" />
        </label>
        <label class="form-control">
          <span class="label-text text-xs opacity-70">За сколько дней</span>
          <input type="number" name="days" value="{days_n}" min="1" max="365" class="input input-bordered input-sm w-28" />
        </label>
        <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5">
          <i class="fa-solid fa-filter" aria-hidden="true"></i>Применить
        </button>
        <a class="btn btn-outline btn-sm h-9 min-h-9" href="/admin/logs?tab={_esc(tab)}">Сброс</a>
      </form>
      <div class="card bg-base-100 border border-base-content/10 shadow-lg">
        <div class="card-body gap-4">
          <div class="overflow-x-auto rounded-xl border border-base-content/10">
            <table class="table table-zebra table-sm">
              <thead><tr>{headers}</tr></thead>
              <tbody>{''.join(rows_html) or f'<tr><td colspan="5" class="opacity-50">Записей за последние {days_n} дн. не найдено</td></tr>'}</tbody>
            </table>
          </div>
          <span class="text-xs opacity-60">Показано до {lim} записей за последние {days_n} дн.</span>
        </div>
      </div>
    </div>
    """
    return _layout("Логи", body, request=request)
