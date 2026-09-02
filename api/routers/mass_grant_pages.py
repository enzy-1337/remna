"""Веб-админка: массовая выдача баланса или дней подписки списку пользователей
(или всем), с опциональным фильтром "была подписка за последние N дней".

Разделяет общую сессию/лэйаут с api/routers/web_admin.py (см. fraud_admin_pages.py
для того же паттерна ленивого импорта _layout/_require_login/_session).
"""

from __future__ import annotations

import html as html_lib
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import get_settings
from shared.services.mass_grant_service import apply_mass_grant, resolve_candidate_users

router = APIRouter(tags=["web-admin-mass-grant"])


def _esc(s: object) -> str:
    return html_lib.escape(str(s or ""), quote=True)


@router.get("/mass-grant")
async def admin_mass_grant_page(request: Request, n: str = "") -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login

    denied = _require_login(request)
    if denied is not None:
        return denied

    result_html = ""
    if n:
        result_html = f"""<div class="alert alert-success"><span>{_esc(n)}</span></div>"""

    body = f"""
    <div class="flex flex-col gap-4">
      <h1 class="text-xl font-bold">Массовая выдача</h1>
      {result_html}
      <div class="alert alert-warning">
        <span>Операция массовая и необратимая (баланс/дни выдаются сразу всем подходящим). Проверьте параметры перед запуском.</span>
      </div>
      <div class="card bg-base-100 border border-base-content/10 shadow">
        <div class="card-body gap-4">
          <form method="post" action="/admin/mass-grant/run" class="flex flex-col gap-4"
                data-remna-confirm-msg="Запустить массовую выдачу указанным пользователям?">

            <div class="flex flex-col gap-1">
              <span class="label-text font-semibold">Что выдать</span>
              <div class="flex flex-wrap gap-4">
                <label class="flex items-center gap-2 cursor-pointer">
                  <input type="radio" name="grant_type" value="balance" class="radio radio-sm" checked
                    onchange="document.getElementById('mg-amount-wrap').hidden=false;document.getElementById('mg-days-wrap').hidden=true;" />
                  <span>Баланс, ₽</span>
                </label>
                <label class="flex items-center gap-2 cursor-pointer">
                  <input type="radio" name="grant_type" value="days" class="radio radio-sm"
                    onchange="document.getElementById('mg-amount-wrap').hidden=true;document.getElementById('mg-days-wrap').hidden=false;" />
                  <span>Дни подписки</span>
                </label>
              </div>
              <div id="mg-amount-wrap">
                <input type="text" name="amount_rub" inputmode="decimal" placeholder="Например, 100"
                  class="input input-bordered input-sm w-48" />
              </div>
              <div id="mg-days-wrap" hidden>
                <input type="number" name="days" min="1" placeholder="Например, 7"
                  class="input input-bordered input-sm w-48" />
              </div>
            </div>

            <div class="divider my-0"></div>

            <div class="flex flex-col gap-1">
              <span class="label-text font-semibold">Кому</span>
              <div class="flex flex-wrap gap-4">
                <label class="flex items-center gap-2 cursor-pointer">
                  <input type="radio" name="audience" value="all" class="radio radio-sm" checked
                    onchange="document.getElementById('mg-ids-wrap').hidden=true;" />
                  <span>Всем пользователям</span>
                </label>
                <label class="flex items-center gap-2 cursor-pointer">
                  <input type="radio" name="audience" value="list" class="radio radio-sm"
                    onchange="document.getElementById('mg-ids-wrap').hidden=false;" />
                  <span>По списку Telegram ID</span>
                </label>
              </div>
              <div id="mg-ids-wrap" hidden>
                <textarea name="telegram_ids" rows="4" class="textarea textarea-bordered font-mono text-sm w-full max-w-md"
                  placeholder="123456789&#10;987654321"></textarea>
              </div>
            </div>

            <div class="divider my-0"></div>

            <div class="flex flex-col gap-1">
              <label class="flex items-center gap-2 cursor-pointer w-fit">
                <input type="checkbox" name="apply_subscription_filter" value="1" class="checkbox checkbox-sm"
                  onchange="document.getElementById('mg-filter-days-wrap').hidden=!this.checked;" />
                <span class="label-text font-semibold">Только у кого была подписка за последние N дней</span>
              </label>
              <p class="text-xs opacity-70">Остальные (без подписки в этом окне) будут пропущены, а не считаются ошибкой.</p>
              <div id="mg-filter-days-wrap" hidden>
                <input type="number" name="subscription_within_days" min="1" value="7"
                  class="input input-bordered input-sm w-32" />
              </div>
            </div>

            <button type="submit" class="btn btn-primary w-fit gap-2">
              <i class="fa-solid fa-gifts" aria-hidden="true"></i>Запустить массовую выдачу
            </button>
          </form>
        </div>
      </div>
    </div>
    """
    return _layout("Массовая выдача", body, request=request)


@router.post("/mass-grant/run")
async def admin_mass_grant_run(
    request: Request,
    grant_type: str = Form(...),
    audience: str = Form(...),
    amount_rub: str = Form(""),
    days: str = Form(""),
    telegram_ids: str = Form(""),
    apply_subscription_filter: str = Form(""),
    subscription_within_days: str = Form(""),
) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session, _web_admin_actor_label, _web_admin_actor_user
    from shared.services.admin_log_topics import AdminLogTopic
    from shared.services.admin_notify import notify_admin
    from shared.md2 import bold, plain
    from shared.services.web_admin_notify import web_admin_actor_notify_line

    denied = _require_login(request)
    if denied is not None:
        return denied

    grant_type = grant_type if grant_type in ("balance", "days") else "balance"

    amount: Decimal | None = None
    if grant_type == "balance":
        try:
            amount = Decimal((amount_rub or "0").strip().replace(",", "."))
        except InvalidOperation:
            return RedirectResponse("/admin/mass-grant?n=Некорректная+сумма", status_code=303)
        if amount <= 0:
            return RedirectResponse("/admin/mass-grant?n=Сумма+должна+быть+%3E+0", status_code=303)

    days_n: int | None = None
    if grant_type == "days":
        try:
            days_n = int((days or "0").strip())
        except ValueError:
            days_n = 0
        if days_n <= 0:
            return RedirectResponse("/admin/mass-grant?n=Количество+дней+должно+быть+%3E+0", status_code=303)

    ids: list[int] | None = None
    if audience == "list":
        seen: set[int] = set()
        for raw_line in telegram_ids.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                seen.add(int(line))
            except ValueError:
                continue
        ids = list(seen)

    within_days: int | None = None
    if apply_subscription_filter:
        try:
            within_days = int((subscription_within_days or "0").strip())
        except ValueError:
            within_days = 0
        if within_days <= 0:
            within_days = None

    settings = get_settings()
    async with await _session() as session:
        actor_user = await _web_admin_actor_user(session, request)
        actor_label = _web_admin_actor_label(request)

        users = await resolve_candidate_users(session, telegram_ids=ids)
        result = await apply_mass_grant(
            session,
            settings,
            users=users,
            grant_type=grant_type,  # type: ignore[arg-type]
            amount_rub=amount,
            days=days_n,
            subscription_within_days=within_days,
            actor_label=actor_label,
        )
        await session.commit()

        what = f"+{amount} ₽" if grant_type == "balance" else f"+{days_n} дн."
        await notify_admin(
            settings,
            title="🎁 " + bold("Массовая выдача"),
            lines=[
                plain(f"Что: {what}"),
                plain(
                    f"Кандидатов: {result.total_candidates} · выдано: {result.granted} · "
                    f"пропущено (нет подписки): {result.skipped_no_subscription} · "
                    f"пропущено (некорректный юзер): {result.skipped_invalid_user} · "
                    f"ошибок: {len(result.errors)}"
                ),
                web_admin_actor_notify_line(),
            ],
            event_type="mass_grant_web",
            topic=AdminLogTopic.BONUSES,
            subject_user=actor_user,
            session=session,
        )

    summary = (
        f"Выдано: {result.granted} из {result.total_candidates} "
        f"(пропущено без подписки: {result.skipped_no_subscription}, ошибок: {len(result.errors)})"
    )
    from urllib.parse import quote

    return RedirectResponse(f"/admin/mass-grant?n={quote(summary)}", status_code=303)
