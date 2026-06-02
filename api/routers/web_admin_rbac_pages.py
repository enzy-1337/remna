"""Страницы CRUD: администраторы и роли (супер-админ)."""

from __future__ import annotations

import html as html_lib
from urllib.parse import quote as url_quote

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


def _normalize_extra(raw) -> set[str]:
    if not raw:
        return set()
    return {str(x) for x in raw}


def _perm_checkboxes(name: str, selected: set[str]) -> str:
    labels = permission_labels()
    parts = []
    for p in ALL_ADMIN_PERMISSIONS:
        chk = " checked" if p in selected else ""
        parts.append(
            f'<label class="flex items-center gap-2 text-sm cursor-pointer">'
            f'<input type="checkbox" name="{name}" value="{_esc(p)}" class="checkbox checkbox-sm checkbox-primary"{chk} />'
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


# ─── Виджет поиска пользователя ──────────────────────────────────────────────

def _user_search_widget(
    *,
    field_id: str = "admin-user-search",
    hidden_name: str = "user_id",
    hidden_id: str = "admin-user-id-hidden",
    chips_id: str = "admin-user-chips",
    dropdown_id: str = "admin-user-dropdown",
    initial_label: str = "",
    initial_user_id: str = "",
    placeholder: str = "Введите @username, имя или Telegram ID…",
    single: bool = True,
) -> str:
    """Виджет выбора одного пользователя (chip + autocomplete)."""
    initial_chip = ""
    if initial_user_id and initial_label:
        initial_chip = (
            f"<span class='badge badge-primary gap-1 py-3 pl-3 pr-1' "
            f"data-user-chip data-user-id='{_esc(initial_user_id)}'>"
            f"<span class='text-xs'>{_esc(initial_label)}</span>"
            f"<button type='button' class='btn btn-ghost btn-xs btn-circle' "
            f"data-user-chip-remove aria-label='Убрать'>"
            f"<i class='fa-solid fa-xmark text-[10px]' aria-hidden='true'></i></button></span>"
        )

    html = f"""
    <div class="flex flex-col gap-2">
      <div id="{_esc(chips_id)}" class="flex flex-wrap gap-1.5 min-h-[32px]">{initial_chip}</div>
      <div class="relative">
        <input id="{_esc(field_id)}" type="text" autocomplete="off"
               class="input input-bordered input-sm h-9 min-h-9 text-sm w-full"
               placeholder="{_esc(placeholder)}" />
        <div id="{_esc(dropdown_id)}"
             class="absolute left-0 right-0 top-full mt-1 z-[120] hidden max-h-72 overflow-auto
                    rounded-lg border border-base-content/10 bg-base-100 shadow-xl"></div>
      </div>
      <input type="hidden" name="{_esc(hidden_name)}" id="{_esc(hidden_id)}" value="{_esc(initial_user_id)}" />
    </div>
    <script>(function(){{
      var chipsBox  = document.getElementById('{_esc(chips_id)}');
      var searchInp = document.getElementById('{_esc(field_id)}');
      var dropdown  = document.getElementById('{_esc(dropdown_id)}');
      var hiddenId  = document.getElementById('{_esc(hidden_id)}');
      if (!chipsBox || !searchInp || !dropdown || !hiddenId) return;

      var selected = new Map();
      chipsBox.querySelectorAll('[data-user-chip]').forEach(function(el) {{
        var id = el.getAttribute('data-user-id');
        if (id) selected.set(String(id), el);
      }});

      function syncHidden() {{
        {'hiddenId.value = selected.size ? Array.from(selected.keys())[0] : "";' if single else 'hiddenId.value = Array.from(selected.keys()).join(",");'}
      }}

      function bindRemove() {{
        chipsBox.querySelectorAll('[data-user-chip-remove]').forEach(function(btn) {{
          if (btn._bound) return;
          btn._bound = true;
          btn.addEventListener('click', function() {{
            var chip = btn.closest('[data-user-chip]');
            if (!chip) return;
            var id = chip.getAttribute('data-user-id');
            chip.remove();
            if (id) selected.delete(String(id));
            syncHidden();
            searchInp.disabled = false;
          }});
        }});
      }}
      bindRemove();

      function addChip(u) {{
        var id = String(u.id);
        if (selected.has(id)) return;
        {'if (selected.size > 0) return;' if single else ''}
        var label = u.label || ('#' + id);
        var span = document.createElement('span');
        span.className = 'badge badge-primary gap-1 py-3 pl-3 pr-1';
        span.setAttribute('data-user-chip', '');
        span.setAttribute('data-user-id', id);
        span.innerHTML = "<span class='text-xs'></span>"
          + "<button type='button' class='btn btn-ghost btn-xs btn-circle' "
          + "data-user-chip-remove aria-label='Убрать'>"
          + "<i class='fa-solid fa-xmark text-[10px]' aria-hidden='true'></i></button>";
        span.firstElementChild.textContent = label;
        chipsBox.appendChild(span);
        selected.set(id, span);
        syncHidden();
        bindRemove();
        {'searchInp.disabled = true; searchInp.value = "";' if single else ''}
      }}

      function closeDropdown() {{
        dropdown.classList.add('hidden');
        dropdown.innerHTML = '';
      }}

      function renderResults(items) {{
        if (!items || !items.length) {{
          dropdown.innerHTML = "<div class='px-3 py-2 text-xs opacity-60'>Ничего не найдено</div>";
          dropdown.classList.remove('hidden');
          return;
        }}
        var html = '';
        for (var i = 0; i < items.length; i++) {{
          var u = items[i];
          var dis = selected.has(String(u.id));
          html += "<button type='button' class='block w-full text-left px-3 py-2 hover:bg-base-200 text-sm"
            + (dis ? " opacity-40 cursor-not-allowed" : "") + "' "
            + "data-pick-id='" + u.id + "' data-pick-label='" + (u.label || '').replace(/'/g, "&#39;") + "' "
            + (dis ? "disabled" : "") + ">"
            + (u.label_html || u.label || '#' + u.id) + "</button>";
        }}
        dropdown.innerHTML = html;
        dropdown.classList.remove('hidden');
        dropdown.querySelectorAll('button[data-pick-id]').forEach(function(b) {{
          b.addEventListener('click', function() {{
            addChip({{id: b.getAttribute('data-pick-id'), label: b.getAttribute('data-pick-label')}});
            searchInp.value = '';
            closeDropdown();
            if (!searchInp.disabled) searchInp.focus();
          }});
        }});
      }}

      var _timer = null, _reqN = 0;
      function runSearch(q) {{
        var n = ++_reqN;
        fetch('/admin/api/users-search?q=' + encodeURIComponent(q || '') + '&limit=20',
              {{credentials: 'same-origin'}})
          .then(function(r) {{ return r.json(); }})
          .then(function(d) {{ if (n === _reqN) renderResults(d.users || []); }})
          .catch(function() {{
            if (n === _reqN) {{
              dropdown.innerHTML = "<div class='px-3 py-2 text-xs text-error'>Ошибка поиска</div>";
              dropdown.classList.remove('hidden');
            }}
          }});
      }}

      searchInp.addEventListener('input', function() {{
        var q = searchInp.value.trim();
        if (_timer) clearTimeout(_timer);
        _timer = setTimeout(function() {{ runSearch(q); }}, q.length ? 180 : 120);
      }});
      searchInp.addEventListener('focus', function() {{
        if (!searchInp.value.trim()) runSearch('');
      }});
      searchInp.addEventListener('keydown', function(ev) {{
        if (ev.key === 'Enter') ev.preventDefault();
      }});
      document.addEventListener('click', function(ev) {{
        if (ev.target !== searchInp && !dropdown.contains(ev.target)) closeDropdown();
      }});
    }})();</script>
    """
    return html


# ─── Администраторы ───────────────────────────────────────────────────────────

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
            name = (u.first_name or u.username or str(u.telegram_id)).strip()
            badge = " <span class='badge badge-warning badge-sm'>super</span>" if a.is_superadmin else ""
            perms_count = len(a.extra_permissions or [])
            perms_hint = f"{perms_count} доп. прав" if perms_count else "—"
            rows.append(
                f"<tr>"
                f"<td class='font-mono text-sm'>{a.id}</td>"
                f"<td><span class='font-medium'>{_esc(name)}</span>{badge}"
                f"<br><span class='text-xs opacity-60'>tg&nbsp;{_esc(u.telegram_id)}</span></td>"
                f"<td>{_esc(role_name)}</td>"
                f"<td class='text-sm opacity-70'>{_esc(perms_hint)}</td>"
                f"<td class='text-right'>"
                f"<a class='btn btn-ghost btn-xs' href='/admin/admins/{a.id}/edit'>"
                f"<i class='fa-solid fa-pen-to-square' aria-hidden='true'></i>Изменить</a>"
                + (
                    ""
                    if a.is_superadmin
                    else f" <form method='post' action='/admin/admins/{a.id}/delete' class='inline' "
                    f"onsubmit=\"return confirm('Удалить администратора?')\">"
                    f"<button class='btn btn-ghost btn-xs text-error'>"
                    f"<i class='fa-solid fa-trash' aria-hidden='true'></i>Удалить</button></form>"
                )
                + "</td></tr>"
            )
    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="card-title text-2xl">
            <i class="fa-solid fa-user-shield text-primary mr-2" aria-hidden="true"></i>Администраторы
          </h2>
          <a href="/admin/admins/new" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-plus" aria-hidden="true"></i>Добавить
          </a>
        </div>
        <div class="overflow-x-auto rounded-xl border border-base-content/10">
          <table class="table table-sm table-zebra">
            <thead>
              <tr><th>ID</th><th>Пользователь</th><th>Роль</th><th>Доп. права</th><th></th></tr>
            </thead>
            <tbody>
              {''.join(rows) or '<tr><td colspan="5" class="opacity-50 text-center py-4">Нет записей</td></tr>'}
            </tbody>
          </table>
        </div>
        <p class="text-xs opacity-60">
          <i class="fa-solid fa-circle-info mr-1" aria-hidden="true"></i>
          Пользователь должен хотя бы раз зайти в бота (/start), чтобы его можно было добавить.
          Изменения прав применяются автоматически (до&nbsp;60&nbsp;сек).
        </p>
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

    # Ошибки передаются через ?err=текст и показываются через JS-тост (remnaConsumeUrlNotify)
    err_hint = ""

    user_widget = _user_search_widget(
        field_id="new-admin-search",
        hidden_name="user_id",
        hidden_id="new-admin-user-id",
        chips_id="new-admin-chips",
        dropdown_id="new-admin-dropdown",
        placeholder="Введите @username, имя или Telegram ID…",
        single=True,
    )

    body = f"""
    <div class="flex w-full flex-col items-center justify-center py-6">
    <form method="post" action="/admin/admins/new"
          class="card bg-base-100 border border-base-content/10 shadow-lg w-full max-w-xl">
      <div class="card-body gap-4">
        <h2 class="card-title text-xl">
          <i class="fa-solid fa-user-plus text-primary mr-2" aria-hidden="true"></i>Новый администратор
        </h2>
        {err_hint}

        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Пользователь</span></label>
          {user_widget}
          <p class="text-xs opacity-60 mt-1">Начните вводить @username, имя или Telegram ID.</p>
        </div>

        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Роль</span></label>
          <select name="role_id" class="select select-bordered select-sm h-9 min-h-9">
            <option value="">— без роли —</option>{opts}
          </select>
        </div>

        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Дополнительные права</span></label>
          <div class="grid grid-cols-1 gap-2 rounded-lg border border-base-content/10 bg-base-200/30 p-4">
            {_perm_checkboxes('extra_permissions', set())}
          </div>
        </div>

        <div class="flex gap-2 justify-end">
          <a href="/admin/admins" class="btn btn-ghost btn-sm h-9 min-h-9">Отмена</a>
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Сохранить
          </button>
        </div>
      </div>
    </form>
    </div>"""
    return _layout("Новый администратор", body, request=request, back_href="/admin/admins")


@router.post("/admins/new")
async def admin_admins_create(
    request: Request,
    user_id: str = Form(default=""),
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

    # user_id может быть db-id пользователя (из виджета) или telegram_id (fallback)
    uid_raw = (user_id or "").strip()
    if not uid_raw or not uid_raw.isdigit():
        return RedirectResponse("/admin/admins/new?err=user", status_code=303)

    async with await _session() as session:
        # Пробуем сначала по users.id (из виджета), потом по telegram_id
        db_user = (
            await session.execute(
                select(User).where(User.id == int(uid_raw)).limit(1)
            )
        ).scalar_one_or_none()
        if db_user is None:
            db_user = (
                await session.execute(
                    select(User).where(User.telegram_id == int(uid_raw)).limit(1)
                )
            ).scalar_one_or_none()
        if db_user is None:
            return RedirectResponse(
                "/admin/admins/new?err=" + url_quote("Пользователь не найден. Убедитесь что он запускал бота (/start)."),
                status_code=303,
            )

        # Проверяем что уже не является администратором
        existing = (
            await session.execute(
                select(AdminUser).where(AdminUser.user_id == db_user.id).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return RedirectResponse(
                "/admin/admins/new?err=" + url_quote("Этот пользователь уже является администратором."),
                status_code=303,
            )

        await create_admin_user(
            session,
            user_id=db_user.id,
            role_id=rid,
            extra_permissions=list(extra_permissions or []),
        )
        await session.commit()
    return RedirectResponse("/admin/admins?n=saved", status_code=303)


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

    # Собираем метку пользователя для чипа
    if user:
        fn = (user.first_name or "").strip()
        un = (user.username or "").strip()
        label_main = f"@{un}" if un else (fn or f"tg:{user.telegram_id}")
        user_label = f"{label_main} · #{user.id}"
        user_id_str = str(user.id)
    else:
        user_label = f"user#{admin.user_id}"
        user_id_str = str(admin.user_id)

    user_widget = _user_search_widget(
        field_id="edit-admin-search",
        hidden_name="user_id",
        hidden_id="edit-admin-user-id",
        chips_id="edit-admin-chips",
        dropdown_id="edit-admin-dropdown",
        initial_label=user_label,
        initial_user_id=user_id_str,
        placeholder="Найти другого пользователя…",
        single=True,
    )

    super_badge = (
        "<div class='alert alert-warning text-sm'>"
        "<i class='fa-solid fa-crown' aria-hidden='true'></i>"
        "Это супер-администратор. Роль и права доступны только для просмотра.</div>"
        if admin.is_superadmin else ""
    )

    disabled = "disabled" if admin.is_superadmin else ""

    body = f"""
    <div class="flex w-full flex-col items-center justify-center py-6">
    <form method="post" action="/admin/admins/{admin_id}/edit"
          class="card bg-base-100 border border-base-content/10 shadow-lg w-full max-w-xl">
      <div class="card-body gap-4">
        <h2 class="card-title text-xl">
          <i class="fa-solid fa-user-gear text-primary mr-2" aria-hidden="true"></i>
          Администратор #{admin_id}
        </h2>
        {super_badge}

        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Пользователь</span></label>
          {user_widget}
        </div>

        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Роль</span></label>
          <select name="role_id" class="select select-bordered select-sm h-9 min-h-9" {disabled}>
            <option value="">— без роли —</option>{opts}
          </select>
        </div>

        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Дополнительные права</span></label>
          <div class="grid grid-cols-1 gap-2 rounded-lg border border-base-content/10 bg-base-200/30 p-4
                      {'opacity-60' if admin.is_superadmin else ''}">
            {_perm_checkboxes('extra_permissions', extra)}
          </div>
        </div>

        <div class="flex gap-2 justify-end">
          <a href="/admin/admins" class="btn btn-ghost btn-sm h-9 min-h-9">Отмена</a>
          {'<button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5"><i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Сохранить</button>' if not admin.is_superadmin else ''}
        </div>
      </div>
    </form>
    </div>"""
    return _layout("Редактирование администратора", body, request=request, back_href="/admin/admins")


@router.post("/admins/{admin_id}/edit")
async def admin_admins_update(
    request: Request,
    admin_id: int,
    user_id: str = Form(default=""),
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
    return RedirectResponse("/admin/admins?n=saved", status_code=303)


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


# ─── Роли ─────────────────────────────────────────────────────────────────────

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
            f"<tr>"
            f"<td class='font-mono text-sm'>{r.id}</td>"
            f"<td class='font-medium'>{_esc(r.name)}</td>"
            f"<td class='text-sm opacity-80'>{perms or '—'}</td>"
            f"<td class='text-right'>"
            f"<a class='btn btn-ghost btn-xs' href='/admin/roles/{r.id}/edit'>"
            f"<i class='fa-solid fa-pen-to-square' aria-hidden='true'></i>Изменить</a> "
            f"<form method='post' action='/admin/roles/{r.id}/delete' class='inline' "
            f"onsubmit=\"return confirm('Удалить роль {_esc(r.name)}?')\">"
            f"<button class='btn btn-ghost btn-xs text-error'>"
            f"<i class='fa-solid fa-trash' aria-hidden='true'></i>Удалить</button></form>"
            f"</td></tr>"
        )
    body = f"""
    <div class="card bg-base-100 border border-base-content/10 shadow-lg">
      <div class="card-body gap-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="card-title text-2xl">
            <i class="fa-solid fa-key text-primary mr-2" aria-hidden="true"></i>Роли
          </h2>
          <a href="/admin/roles/new" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-plus" aria-hidden="true"></i>Новая роль
          </a>
        </div>
        <div class="overflow-x-auto rounded-xl border border-base-content/10">
          <table class="table table-sm table-zebra">
            <thead><tr><th>ID</th><th>Название</th><th>Права</th><th></th></tr></thead>
            <tbody>
              {''.join(rows) or '<tr><td colspan="4" class="opacity-50 text-center py-4">Нет ролей</td></tr>'}
            </tbody>
          </table>
        </div>
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
    <div class="flex w-full flex-col items-center justify-center py-6">
    <form method="post" action="/admin/roles/new"
          class="card bg-base-100 border border-base-content/10 shadow-lg w-full max-w-xl">
      <div class="card-body gap-4">
        <h2 class="card-title text-xl">
          <i class="fa-solid fa-key text-primary mr-2" aria-hidden="true"></i>Новая роль
        </h2>
        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Название</span></label>
          <input name="name" required class="input input-bordered input-sm h-9 min-h-9" placeholder="Например: Оператор тикетов" />
        </div>
        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Права</span></label>
          <div class="grid grid-cols-1 gap-2 rounded-lg border border-base-content/10 bg-base-200/30 p-4">
            {_perm_checkboxes('permissions', set())}
          </div>
        </div>
        <div class="flex gap-2 justify-end">
          <a href="/admin/roles" class="btn btn-ghost btn-sm h-9 min-h-9">Отмена</a>
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Создать
          </button>
        </div>
      </div>
    </form>
    </div>"""
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
    return RedirectResponse("/admin/roles?n=saved", status_code=303)


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
    <div class="flex w-full flex-col items-center justify-center py-6">
    <form method="post" action="/admin/roles/{role_id}/edit"
          class="card bg-base-100 border border-base-content/10 shadow-lg w-full max-w-xl">
      <div class="card-body gap-4">
        <h2 class="card-title text-xl">
          <i class="fa-solid fa-key text-primary mr-2" aria-hidden="true"></i>Роль #{role_id}
        </h2>
        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Название</span></label>
          <input name="name" value="{_esc(role.name)}" required class="input input-bordered input-sm h-9 min-h-9" />
        </div>
        <div class="form-control w-full">
          <label class="label pb-1"><span class="label-text font-medium">Права</span></label>
          <div class="grid grid-cols-1 gap-2 rounded-lg border border-base-content/10 bg-base-200/30 p-4">
            {_perm_checkboxes('permissions', perms)}
          </div>
        </div>
        <div class="flex gap-2 justify-end">
          <a href="/admin/roles" class="btn btn-ghost btn-sm h-9 min-h-9">Отмена</a>
          <button type="submit" class="btn btn-primary btn-sm h-9 min-h-9 gap-1.5">
            <i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>Сохранить
          </button>
        </div>
      </div>
    </form>
    </div>"""
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
    return RedirectResponse("/admin/roles?n=saved", status_code=303)


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
