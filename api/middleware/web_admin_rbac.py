"""Проверка RBAC для /admin/*."""

from __future__ import annotations

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
            # GET-запросы редиректим с ?next=, POST/прочие — просто на логин
            if request.method == "GET":
                return _login_redirect(request)
            return RedirectResponse("/admin/login", status_code=303)

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
