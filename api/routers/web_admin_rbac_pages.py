"""Страницы CRUD: администраторы и роли (супер-админ)."""

from __future__ import annotations

import html as html_lib

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from shared.config import get_settings
from shared.models.admin_role import AdminRole
from shared.models.admin_user import AdminUser
from shared.models.user import User
from shared.services.admin_rbac_service import (
    ALL_ADMIN_PERMISSIONS,
    create_admin_role,
    create_admin_user,
    delete_admin_role,
    delete_admin_user,
    list_admin_roles,
    list_admin_users,
    update_admin_role,
    update_admin_user,
)
from shared.services.web_admin_rbac import permission_labels

router = APIRouter(tags=["web-admin-rbac"])


def _esc(s: object) -> str:
    return html_lib.escape(str(s or ""), quote=True)


def _perm_checkboxes(name: str, selected: set[str]) -> str:
    labels = permission_labels()
    parts = []
    for p in ALL_ADMIN_PERMISSIONS:
        chk = " checked" if p in selected else ""
        parts.append(
            f'<label class="flex items-center gap-2 text-sm">'
            f'<input type="checkbox" name="{name}" value="{_esc(p)}" class="checkbox checkbox-sm"{chk} />'
            f"<span>{_esc(labels.get(p, p))}</span></label>"
        )
    return "\n".join(parts)


def _require_superadmin_page(request: Request):
    from api.routers.web_admin import _layout, _require_login

    denied = _require_login(request)
    if denied is not None:
        return denied, None
    if not request.session.get("wauth_is_superadmin"):
        body = "<div class='alert alert-error shadow-lg'><span>Только для супер-администратора.</span></div>"
        return _layout("403", body, request=request), None
    return None, _layout


@router.get("/admins")
async def admin_admins_list(request: Request) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return _layout(
            "403",
            "<div class='alert alert-error shadow-lg'><span>Только для супер-администратора.</span></div>",
            request=request,
        )
    async with await _session() as session:
        admins = await list_admin_users(session)
        roles = {r.id: r for r in await list_admin_roles(session)}
        rows = []
        for a in admins:
            u = await session.get(User, a.user_id)
            if u is None:
                continue
            role_name = roles[a.role_id].name if a.role_id and a.role_id in roles else "—"
            label = (u.first_name or u.username or str(u.telegram_id)).strip()
            badge = " <span class='badge badge-warning badge-sm'>super</span>" if a.is_superadmin else ""
            rows.append(
                f"<tr><td>{a.id}</td><td>{_esc(label)}{badge}<br><span class='text-xs opacity-60'>"
                f"tg {_esc(u.telegram_id)}</span></td><td>{_esc(role_name)}</td>"
                f"<td class='text-right'>"
                f"<a class='btn btn-ghost btn-xs' href='/admin/admins/{a.id}/edit'>Изменить</a> "
                + (
                    ""
                    if a.is_superadmin
                    else f"<form method='post' action='/admin/admins/{a.id}/delete' class='inline' "
                    f"onsubmit=\"return confirm('Удалить администратора?')\"><button class='btn btn-ghost btn-xs text-error'>Удалить</button></form>"
                )
                + "</td></tr>"
            )
    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="card-title text-2xl"><i class="fa-solid fa-user-shield text-primary mr-2"></i>Администраторы</h2>
          <a href="/admin/admins/new" class="btn btn-primary btn-sm">Добавить</a>
        </div>
        <div class="overflow-x-auto"><table class="table table-sm">
          <thead><tr><th>ID</th><th>Пользователь</th><th>Роль</th><th></th></tr></thead>
          <tbody>{''.join(rows) or '<tr><td colspan="4">Нет записей</td></tr>'}</tbody>
        </table></div>
        <p class="text-xs opacity-60">Пользователь должен хотя бы раз зайти в бота (/start), чтобы его можно было найти по Telegram ID.</p>
      </div>
    </div>"""
    return _layout("Администраторы", body, request=request)


@router.get("/admins/new")
async def admin_admins_new(request: Request) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return _layout("403", "<div class='alert alert-error'>Нет доступа</div>", request=request)
    async with await _session() as session:
        roles = await list_admin_roles(session)
    opts = "".join(f'<option value="{r.id}">{_esc(r.name)}</option>' for r in roles)
    body = f"""
    <form method="post" action="/admin/admins/new" class="card bg-base-100 border border-base-content/10 shadow-lg max-w-lg">
      <div class="card-body gap-3">
        <h2 class="card-title">Новый администратор</h2>
        <label class="form-control"><span class="label-text">Telegram user id</span>
          <input name="telegram_id" type="number" required class="input input-bordered input-sm" /></label>
        <label class="form-control"><span class="label-text">Роль</span>
          <select name="role_id" class="select select-bordered select-sm"><option value="">—</option>{opts}</select></label>
        <div class="grid gap-2">{_perm_checkboxes('extra_permissions', set())}</div>
        <button type="submit" class="btn btn-primary btn-sm">Сохранить</button>
      </div>
    </form>"""
    return _layout("Новый администратор", body, request=request, back_href="/admin/admins")


@router.post("/admins/new")
async def admin_admins_create(
    request: Request,
    telegram_id: int = Form(...),
    role_id: str = Form(""),
    extra_permissions: list[str] = Form(default=[]),
) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return RedirectResponse("/admin/dashboard", status_code=303)
    rid = int(role_id) if str(role_id).strip().isdigit() else None
    async with await _session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == int(telegram_id)).limit(1))
        ).scalar_one_or_none()
        if user is None:
            return RedirectResponse("/admin/admins/new?err=user", status_code=303)
        await create_admin_user(
            session,
            user_id=user.id,
            role_id=rid,
            extra_permissions=list(extra_permissions or []),
        )
        await session.commit()
    return RedirectResponse("/admin/admins", status_code=303)


@router.get("/admins/{admin_id}/edit")
async def admin_admins_edit(request: Request, admin_id: int) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return _layout("403", "<div class='alert alert-error'>Нет доступа</div>", request=request)
    async with await _session() as session:
        admin = await session.get(AdminUser, admin_id)
        if admin is None:
            return _layout("404", "<div class='alert alert-warning'>Не найден</div>", request=request)
        user = await session.get(User, admin.user_id)
        roles = await list_admin_roles(session)
    opts = "".join(
        f'<option value="{r.id}"{" selected" if admin.role_id == r.id else ""}>{_esc(r.name)}</option>'
        for r in roles
    )
    extra = _normalize_extra(admin.extra_permissions)
    body = f"""
    <form method="post" action="/admin/admins/{admin_id}/edit" class="card bg-base-100 border border-base-content/10 shadow-lg max-w-lg">
      <div class="card-body gap-3">
        <h2 class="card-title">Редактировать #{admin_id}</h2>
        <p class="text-sm opacity-80">Пользователь: {_esc(user.first_name if user else admin.user_id)} · tg {_esc(user.telegram_id if user else '')}</p>
        <label class="form-control"><span class="label-text">Роль</span>
          <select name="role_id" class="select select-bordered select-sm"><option value="">—</option>{opts}</select></label>
        <div class="grid gap-2">{_perm_checkboxes('extra_permissions', extra)}</div>
        <button type="submit" class="btn btn-primary btn-sm">Сохранить</button>
      </div>
    </form>"""
    return _layout("Редактирование", body, request=request, back_href="/admin/admins")


def _normalize_extra(raw) -> set[str]:
    if not raw:
        return set()
    return {str(x) for x in raw}


@router.post("/admins/{admin_id}/edit")
async def admin_admins_update(
    request: Request,
    admin_id: int,
    role_id: str = Form(""),
    extra_permissions: list[str] = Form(default=[]),
) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return RedirectResponse("/admin/dashboard", status_code=303)
    rid = int(role_id) if str(role_id).strip().isdigit() else None
    async with await _session() as session:
        admin = await session.get(AdminUser, admin_id)
        if admin is None or admin.is_superadmin:
            return RedirectResponse("/admin/admins", status_code=303)
        await update_admin_user(session, admin, role_id=rid, extra_permissions=list(extra_permissions or []))
        await session.commit()
    return RedirectResponse("/admin/admins", status_code=303)


@router.post("/admins/{admin_id}/delete")
async def admin_admins_delete(request: Request, admin_id: int) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return RedirectResponse("/admin/dashboard", status_code=303)
    async with await _session() as session:
        admin = await session.get(AdminUser, admin_id)
        if admin is not None:
            try:
                await delete_admin_user(session, admin)
                await session.commit()
            except ValueError:
                pass
    return RedirectResponse("/admin/admins", status_code=303)


@router.get("/roles")
async def admin_roles_list(request: Request) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return _layout("403", "<div class='alert alert-error'>Нет доступа</div>", request=request)
    labels = permission_labels()
    async with await _session() as session:
        roles = await list_admin_roles(session)
    rows = []
    for r in roles:
        perms = ", ".join(_esc(labels.get(str(p), str(p))) for p in (r.permissions or []))
        rows.append(
            f"<tr><td>{r.id}</td><td>{_esc(r.name)}</td><td class='text-sm'>{perms or '—'}</td>"
            f"<td class='text-right'><a class='btn btn-ghost btn-xs' href='/admin/roles/{r.id}/edit'>Изменить</a> "
            f"<form method='post' action='/admin/roles/{r.id}/delete' class='inline' "
            f"onsubmit=\"return confirm('Удалить роль?')\"><button class='btn btn-ghost btn-xs text-error'>Удалить</button></form></td></tr>"
        )
    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <div class="flex justify-between items-center"><h2 class="card-title"><i class="fa-solid fa-key text-primary mr-2"></i>Роли</h2>
          <a href="/admin/roles/new" class="btn btn-primary btn-sm">Новая роль</a></div>
        <div class="overflow-x-auto"><table class="table table-sm">
          <thead><tr><th>ID</th><th>Название</th><th>Права</th><th></th></tr></thead>
          <tbody>{''.join(rows) or '<tr><td colspan="4">Нет ролей</td></tr>'}</tbody>
        </table></div>
      </div>
    </div>"""
    return _layout("Роли", body, request=request)


@router.get("/roles/new")
async def admin_roles_new(request: Request) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return _layout("403", "<div class='alert alert-error'>Нет доступа</div>", request=request)
    body = f"""
    <form method="post" action="/admin/roles/new" class="card bg-base-100 border border-base-content/10 shadow-lg max-w-lg">
      <div class="card-body gap-3">
        <h2 class="card-title">Новая роль</h2>
        <label class="form-control"><span class="label-text">Название</span>
          <input name="name" required class="input input-bordered input-sm" /></label>
        <div class="grid gap-2">{_perm_checkboxes('permissions', set())}</div>
        <button type="submit" class="btn btn-primary btn-sm">Создать</button>
      </div>
    </form>"""
    return _layout("Новая роль", body, request=request, back_href="/admin/roles")


@router.post("/roles/new")
async def admin_roles_create(
    request: Request,
    name: str = Form(...),
    permissions: list[str] = Form(default=[]),
) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return RedirectResponse("/admin/dashboard", status_code=303)
    async with await _session() as session:
        await create_admin_role(session, name=name, permissions=list(permissions or []))
        await session.commit()
    return RedirectResponse("/admin/roles", status_code=303)


@router.get("/roles/{role_id}/edit")
async def admin_roles_edit(request: Request, role_id: int) -> HTMLResponse:
    from api.routers.web_admin import _layout, _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return _layout("403", "<div class='alert alert-error'>Нет доступа</div>", request=request)
    async with await _session() as session:
        role = await session.get(AdminRole, role_id)
        if role is None:
            return _layout("404", "<div class='alert alert-warning'>Не найдена</div>", request=request)
    perms = _normalize_extra(role.permissions)
    body = f"""
    <form method="post" action="/admin/roles/{role_id}/edit" class="card bg-base-100 border border-base-content/10 shadow-lg max-w-lg">
      <div class="card-body gap-3">
        <h2 class="card-title">Роль #{role_id}</h2>
        <label class="form-control"><span class="label-text">Название</span>
          <input name="name" value="{_esc(role.name)}" required class="input input-bordered input-sm" /></label>
        <div class="grid gap-2">{_perm_checkboxes('permissions', perms)}</div>
        <button type="submit" class="btn btn-primary btn-sm">Сохранить</button>
      </div>
    </form>"""
    return _layout("Редактирование роли", body, request=request, back_href="/admin/roles")


@router.post("/roles/{role_id}/edit")
async def admin_roles_update(
    request: Request,
    role_id: int,
    name: str = Form(...),
    permissions: list[str] = Form(default=[]),
) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return RedirectResponse("/admin/dashboard", status_code=303)
    async with await _session() as session:
        role = await session.get(AdminRole, role_id)
        if role is not None:
            await update_admin_role(session, role, name=name, permissions=list(permissions or []))
            await session.commit()
    return RedirectResponse("/admin/roles", status_code=303)


@router.post("/roles/{role_id}/delete")
async def admin_roles_delete(request: Request, role_id: int) -> RedirectResponse:
    from api.routers.web_admin import _require_login, _session

    denied = _require_login(request)
    if denied is not None:
        return denied
    if not request.session.get("wauth_is_superadmin"):
        return RedirectResponse("/admin/dashboard", status_code=303)
    async with await _session() as session:
        role = await session.get(AdminRole, role_id)
        if role is not None:
            await delete_admin_role(session, role)
            await session.commit()
    return RedirectResponse("/admin/roles", status_code=303)
