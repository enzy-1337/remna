"""Веб-админка антифрода: дашборд, стадии детекторов, инциденты, блок-лист.

Разделяет общую сессию/лэйаут с api/routers/web_admin.py (см. web_admin_rbac_pages.py
для того же паттерна ленивого импорта _layout/_require_login/_session).
"""

from __future__ import annotations

import html as html_lib
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import desc, func, select

from shared.config import get_settings
from shared.models.fraud_detector_state import FraudDetectorState
from shared.models.fraud_incident import FraudIncident
from shared.models.telegram_blacklist_entry import TelegramBlacklistEntry
from shared.models.user import User
from shared.models.user_fraud_state import UserFraudState
from shared.services.fraud.actions import unblock_user_for_fraud
from shared.services.fraud.blacklist_action import apply_blacklist_block
from shared.services.fraud.blacklist_sync import parse_telegram_id_list

router = APIRouter(tags=["web-admin-fraud"])

_DETECTOR_LABELS = {
    "ip_hop": "Смена IP",
    "hwid_collision": "Мультиаккаунт (HWID)",
    "traffic_spike": "Шеринг трафика",
    "blacklist": "Чёрный список",
}
_STAGED_DETECTORS = ("ip_hop", "hwid_collision", "traffic_spike")
_STAGES = ("learning", "advisory", "autonomous")


def _esc(s: object) -> str:
    return html_lib.escape(str(s or ""), quote=True)


def _esc_attr(v: object) -> str:
    return html_lib.escape(str(v or ""), quote=True)


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y %H:%M")


def _current_admin_label(request: Request) -> str:
    uid = request.session.get("wauth_user_id")
    return f"web:{uid}" if uid is not None else "web:unknown"


@router.get("/fraud")
async def admin_fraud_dashboard(request: Request) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied

    async with await _session() as session:
        detector_rows = list(
            (await session.execute(select(FraudDetectorState))).scalars()
        )
        counts_rows = (
            await session.execute(
                select(FraudIncident.detector, func.count())
                .where(FraudIncident.status == "open")
                .group_by(FraudIncident.detector)
            )
        ).all()
        open_by_detector = {d: c for d, c in counts_rows}
        blocked_count = (
            await session.execute(
                select(func.count()).select_from(User).where(User.is_blocked.is_(True))
            )
        ).scalar_one()

    cards = ""
    for det in (*_STAGED_DETECTORS, "blacklist"):
        stage_row = next((r for r in detector_rows if r.detector == det), None)
        stage = stage_row.stage if stage_row else ("—" if det == "blacklist" else "learning")
        open_n = int(open_by_detector.get(det, 0))
        cards += f"""
        <div class="card bg-base-100 border border-base-content/10 shadow">
          <div class="card-body gap-1">
            <h3 class="text-sm font-semibold">{_esc(_DETECTOR_LABELS.get(det, det))}</h3>
            <p class="text-xs opacity-70">Стадия: <b>{_esc(stage)}</b></p>
            <p class="text-xs opacity-70">Открытых инцидентов: <b>{open_n}</b></p>
          </div>
        </div>"""

    body = f"""
    <div class="flex flex-col gap-4">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <h1 class="text-xl font-bold">Антифрод</h1>
        <div class="flex gap-2">
          <a href="/admin/fraud/detectors" class="btn btn-outline btn-sm">Стадии детекторов</a>
          <a href="/admin/fraud/incidents" class="btn btn-outline btn-sm">Инциденты</a>
          <a href="/admin/fraud/blocklist" class="btn btn-outline btn-sm">Блок-лист ({blocked_count})</a>
        </div>
      </div>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{cards}</div>
    </div>
    """
    return _layout("Антифрод", body, request=request)


@router.get("/fraud/detectors")
async def admin_fraud_detectors(request: Request) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied

    async with await _session() as session:
        rows = {
            r.detector: r
            for r in (await session.execute(select(FraudDetectorState))).scalars()
        }

    forms = ""
    for det in _STAGED_DETECTORS:
        row = rows.get(det)
        stage = row.stage if row else "learning"
        threshold = row.auto_block_confidence_threshold if row else 0.9
        approved_line = ""
        if row and row.approved_by:
            approved_line = (
                f'<p class="text-xs opacity-60">Утверждено: {_esc(row.approved_by)} '
                f"в {_esc(_fmt_dt(row.approved_at))}</p>"
            )
        stage_options = "".join(
            f'<option value="{s}"{" selected" if s == stage else ""}>{_esc(s)}</option>' for s in _STAGES
        )
        forms += f"""
        <div class="card bg-base-100 border border-base-content/10 shadow">
          <div class="card-body gap-3">
            <h3 class="text-sm font-semibold">{_esc(_DETECTOR_LABELS.get(det, det))}</h3>
            <p class="text-xs opacity-70">Стадия начата: {_esc(_fmt_dt(row.stage_started_at if row else None))}</p>
            {approved_line}
            <form method="post" action="/admin/fraud/detectors/{det}/approve" class="flex flex-wrap items-end gap-3">
              <label class="form-control">
                <span class="label-text text-xs opacity-70">Стадия</span>
                <select name="stage" class="select select-bordered select-sm">{stage_options}</select>
              </label>
              <label class="form-control">
                <span class="label-text text-xs opacity-70">Порог автобана (0-1)</span>
                <input type="text" name="auto_block_confidence_threshold" value="{_esc(threshold)}"
                  inputmode="decimal" class="input input-bordered input-sm w-28" />
              </label>
              <button type="submit" class="btn btn-primary btn-sm">Сохранить</button>
            </form>
          </div>
        </div>"""

    body = f"""
    <div class="flex flex-col gap-4">
      <h1 class="text-xl font-bold">Стадии детекторов</h1>
      <div class="alert alert-info text-sm">
        <span>learning — только наблюдение, без алертов. advisory — предупреждения на ручное решение.
        autonomous — автобан при уверенности выше порога, иначе тоже предупреждение.</span>
      </div>
      <div class="grid gap-3 lg:grid-cols-2">{forms}</div>
    </div>
    """
    return _layout("Стадии детекторов", body, request=request)


@router.post("/fraud/detectors/{detector}/approve")
async def admin_fraud_detector_approve(
    request: Request,
    detector: str,
    stage: str = Form(...),
    auto_block_confidence_threshold: str = Form("0.9"),
) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if detector not in _STAGED_DETECTORS or stage not in _STAGES:
        return RedirectResponse("/admin/fraud/detectors", status_code=303)
    try:
        threshold = max(0.0, min(1.0, float(auto_block_confidence_threshold.replace(",", "."))))
    except ValueError:
        threshold = 0.9

    async with await _session() as session:
        row = await session.get(FraudDetectorState, detector)
        if row is None:
            row = FraudDetectorState(detector=detector)
            session.add(row)
        now = datetime.now(timezone.utc)
        if row.stage != stage:
            row.stage_started_at = now
        row.stage = stage
        row.auto_block_confidence_threshold = threshold
        row.approved_by = _current_admin_label(request)
        row.approved_at = now
        await session.commit()
    return RedirectResponse("/admin/fraud/detectors?n=saved", status_code=303)


@router.get("/fraud/incidents")
async def admin_fraud_incidents(
    request: Request,
    user_id: int | None = None,
    detector: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied

    q = select(FraudIncident).order_by(desc(FraudIncident.event_ts)).limit(200)
    if user_id is not None:
        q = q.where(FraudIncident.user_id == user_id)
    if detector:
        q = q.where(FraudIncident.detector == detector)
    if status:
        q = q.where(FraudIncident.status == status)

    async with await _session() as session:
        rows = list((await session.execute(q)).scalars())

    trs = "".join(
        f"<tr><td>{_fmt_dt(fi.event_ts)}</td>"
        f'<td><a href="/admin/users/{fi.user_id}" class="link link-primary">#{fi.user_id}</a></td>'
        f"<td>{_esc(_DETECTOR_LABELS.get(fi.detector, fi.detector))}</td>"
        f"<td>{_esc(fi.severity)}</td>"
        f"<td>{_esc(format(fi.confidence, '.0%'))}</td>"
        f"<td>{_esc(fi.action_taken)}</td>"
        f"<td>{_esc(fi.status)}</td>"
        f"<td>{_esc(fi.resolved_by or '—')}</td></tr>"
        for fi in rows
    )
    body = f"""
    <div class="flex flex-col gap-4">
      <h1 class="text-xl font-bold">Инциденты антифрода{f' — пользователь #{user_id}' if user_id else ''}</h1>
      <div class="overflow-x-auto rounded-lg border border-base-content/10">
        <table class="table table-zebra table-sm">
          <thead><tr><th>Когда</th><th>Юзер</th><th>Детектор</th><th>Тяжесть</th>
          <th>Увер.</th><th>Действие</th><th>Статус</th><th>Решил</th></tr></thead>
          <tbody>{trs or '<tr><td colspan="8" class="opacity-50">Инцидентов нет</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    """
    return _layout("Инциденты антифрода", body, request=request)


@router.get("/fraud/blocklist")
async def admin_fraud_blocklist(request: Request) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied

    async with await _session() as session:
        rows = list(
            (
                await session.execute(
                    select(User).where(User.is_blocked.is_(True)).order_by(desc(User.id)).limit(500)
                )
            ).scalars()
        )

    def _source(reason: str | None) -> str:
        r = reason or ""
        if r.startswith("fraud:blacklist:"):
            return "чёрный список"
        if r.startswith("fraud:hwid_collision:"):
            return "мультиаккаунт"
        if r.startswith("fraud:ip_hop:"):
            return "смена IP"
        if r.startswith("fraud:traffic_spike:"):
            return "шеринг трафика"
        if r.startswith("fraud:"):
            return "антифрод"
        return "вручную (бот/админка)"

    trs = "".join(
        f"<tr><td><a href=\"/admin/users/{u.id}\" class=\"link link-primary\">#{u.id}</a></td>"
        f"<td>{_esc(u.telegram_id)}</td>"
        f"<td>{_esc('@' + u.username) if u.username else '—'}</td>"
        f"<td>{_esc(_source(u.block_reason))}</td>"
        f"<td class=\"text-xs opacity-70\">{_esc(u.block_reason or '—')}</td>"
        f"<td><form method=\"post\" action=\"/admin/fraud/blocklist/{u.id}/unblock\">"
        f"<button type=\"submit\" class=\"btn btn-outline btn-success btn-xs\">Разблокировать</button></form></td></tr>"
        for u in rows
    )

    body = f"""
    <div class="flex flex-col gap-4">
      <h1 class="text-xl font-bold">Блок-лист</h1>
      <div class="card bg-base-100 border border-base-content/10 shadow">
        <div class="card-body gap-3">
          <h3 class="text-sm font-semibold">Добавить в чёрный список вручную</h3>
          <p class="text-xs opacity-70">По одному Telegram ID на строку — можно с комментарием через
            <code>#</code> (например: <code>123456789 # причина</code>), он будет проигнорирован.
            Повторы схлопываются. Уже зарегистрированные — блокируются сразу.</p>
          <form method="post" action="/admin/fraud/blocklist/bulk" enctype="multipart/form-data" class="flex flex-col gap-3">
            <textarea name="telegram_ids" rows="4" class="textarea textarea-bordered font-mono text-sm"
              placeholder="123456789&#10;987654321 # причина"></textarea>
            <input type="file" name="file" accept=".txt" class="file-input file-input-bordered file-input-sm" />
            <button type="submit" class="btn btn-primary btn-sm w-fit">Добавить и заблокировать</button>
          </form>
        </div>
      </div>
      <div class="flex justify-end">
        <a href="/admin/fraud/blocklist/export" class="btn btn-outline btn-sm">Экспорт в .txt</a>
      </div>
      <div class="overflow-x-auto rounded-lg border border-base-content/10">
        <table class="table table-zebra table-sm">
          <thead><tr><th>ID</th><th>Telegram</th><th>Username</th><th>Источник</th><th>Причина</th><th></th></tr></thead>
          <tbody>{trs or '<tr><td colspan="6" class="opacity-50">Заблокированных нет</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    """
    return _layout("Блок-лист", body, request=request)


@router.get("/fraud/blocklist/export")
async def admin_fraud_blocklist_export(request: Request) -> PlainTextResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied

    async with await _session() as session:
        rows = (
            await session.execute(
                select(User.telegram_id, User.block_reason)
                .where(User.is_blocked.is_(True))
                .order_by(User.telegram_id.asc())
            )
        ).all()

    lines = [
        f"{tg_id} # {reason}" if reason else str(tg_id)
        for tg_id, reason in rows
        if tg_id is not None
    ]
    text = "\n".join(lines) + ("\n" if lines else "")
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": "attachment; filename=blocklist_export.txt"},
    )


@router.post("/fraud/blocklist/bulk")
async def admin_fraud_blocklist_bulk(
    request: Request,
    telegram_ids: str = Form(""),
    file: UploadFile | None = File(None),
) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied

    ids = parse_telegram_id_list(telegram_ids)
    if file is not None and file.filename:
        raw = (await file.read()).decode("utf-8", errors="ignore")
        ids |= parse_telegram_id_list(raw)

    settings = get_settings()
    async with await _session() as session:
        if ids:
            existing = set(
                (
                    await session.execute(
                        select(TelegramBlacklistEntry.telegram_id).where(
                            TelegramBlacklistEntry.telegram_id.in_(ids)
                        )
                    )
                ).scalars()
            )
            for tg_id in ids - existing:
                session.add(TelegramBlacklistEntry(telegram_id=tg_id, source="manual"))
            await session.flush()

            users = list(
                (
                    await session.execute(
                        select(User).where(User.telegram_id.in_(ids), User.is_blocked.is_(False))
                    )
                ).scalars()
            )
            for u in users:
                await apply_blacklist_block(session, settings, u, source="manual")
        await session.commit()
    return RedirectResponse(f"/admin/fraud/blocklist?n=added+{len(ids)}", status_code=303)


@router.post("/fraud/blocklist/{user_id}/unblock")
async def admin_fraud_blocklist_unblock(request: Request, user_id: int) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    settings = get_settings()
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is not None:
            await unblock_user_for_fraud(session, settings, user)
            await session.commit()
    return RedirectResponse("/admin/fraud/blocklist?n=unblocked", status_code=303)


@router.post("/fraud/users/{user_id}/watch")
async def admin_fraud_user_watch_toggle(
    request: Request, user_id: int, enabled: str = Form("1")
) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    async with await _session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        state = await session.get(UserFraudState, user_id)
        now = datetime.now(timezone.utc)
        want_watched = enabled == "1"
        if state is None:
            state = UserFraudState(user_id=user_id)
            session.add(state)
        state.is_watched = want_watched
        if want_watched:
            state.watched_reason = f"{_current_admin_label(request)}:manual"
            state.watched_at = now
        else:
            state.watched_reason = None
        await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)
