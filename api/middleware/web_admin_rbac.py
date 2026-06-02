"""Проверка RBAC для /admin/*."""

from __future__ import annotations

import time
from urllib.parse import quote as url_quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from shared.services.web_admin_rbac import (
    ADMIN_OPEN_PATHS,
    ADMIN_PUBLIC_PREFIXES,
    required_permission_for_request,
    session_has_permission,
)

# Как часто (в секундах) обновляем права из БД для залогиненного админа.
# 60 сек — баланс между скоростью применения и нагрузкой на БД.
_PERM_REFRESH_TTL = 60


def _login_redirect(request: Request) -> RedirectResponse:
    """Редирект на страницу входа с сохранением целевого пути в ?next=."""
    path = request.url.path or ""
    query = request.url.query or ""
    target = path + (("?" + query) if query else "")
    # Редиректим только внутренние /admin пути (защита от open-redirect)
    if target.startswith("/admin") and not target.startswith("/admin/login"):
        login_url = "/admin/login?next=" + url_quote(target, safe="")
    else:
        login_url = "/admin/login"
    return RedirectResponse(login_url, status_code=303)


async def _refresh_permissions_if_needed(request: Request) -> None:
    """Обновляет wauth_permissions и wauth_is_superadmin из БД раз в TTL секунд.

    Это гарантирует, что права применяются без перелогина —
    максимум через _PERM_REFRESH_TTL секунд после изменения в админке.
    """
    last_refresh = request.session.get("wauth_perms_refreshed_at") or 0
    now = time.time()
    if now - last_refresh < _PERM_REFRESH_TTL:
        return

    wauth = request.session.get("wauth") or {}
    raw_tg = wauth.get("telegram_id") or wauth.get("id")
    try:
        tg_id = int(raw_tg) if raw_tg is not None else 0
    except (TypeError, ValueError):
        tg_id = 0

    if tg_id <= 0:
        return

    try:
        from shared.config import get_settings
        from shared.database import get_session_factory
        from shared.models.user import User
        from shared.services.admin_rbac_service import (
            ensure_env_admin_records,
            sync_session_permissions,
        )
        from sqlalchemy import select

        settings = get_settings()
        async with get_session_factory()() as session:
            user = (
                await session.execute(
                    select(User).where(User.telegram_id == tg_id).limit(1)
                )
            ).scalar_one_or_none()
            if user is not None:
                await ensure_env_admin_records(session, settings, user)
                await sync_session_permissions(request, session, user, settings)
                await session.commit()

        request.session["wauth_perms_refreshed_at"] = int(now)
    except Exception:
        # Не роняем запрос из-за ошибки рефреша
        pass


class WebAdminRbacMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not path.startswith("/admin"):
            return await call_next(request)

        # Публичные маршруты (логин, OAuth-колбэки) — пропускаем без проверок
        for prefix in ADMIN_PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Не авторизован → редирект на страницу входа
        if not request.session.get("wauth"):
            if request.method == "GET":
                return _login_redirect(request)
            return RedirectResponse("/admin/login", status_code=303)

        # Обновляем права из БД (раз в TTL секунд)
        await _refresh_permissions_if_needed(request)

        perm = required_permission_for_request(request.method, path)
        perms_raw = request.session.get("wauth_permissions") or []
        permissions = {str(x) for x in perms_raw}
        is_superadmin = bool(request.session.get("wauth_is_superadmin"))

        if not session_has_permission(
            request.session,
            perm,
            is_superadmin=is_superadmin,
            permissions=permissions,
        ):
            if request.method == "GET":
                body = (
                    "<!DOCTYPE html><html><body style='font-family:sans-serif;padding:2rem'>"
                    "<h1>403</h1><p>Недостаточно прав для этого раздела.</p>"
                    "<p><a href='/admin/dashboard'>На дашборд</a></p></body></html>"
                )
                return HTMLResponse(body, status_code=403)
            return HTMLResponse("Forbidden", status_code=403)

        return await call_next(request)
