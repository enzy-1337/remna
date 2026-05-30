"""Проверка RBAC для /admin/*."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from shared.services.web_admin_rbac import (
    ADMIN_PUBLIC_PREFIXES,
    required_permission_for_request,
    session_has_permission,
)


class WebAdminRbacMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not path.startswith("/admin"):
            return await call_next(request)
        for prefix in ADMIN_PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        if not request.session.get("wauth"):
            return await call_next(request)

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
