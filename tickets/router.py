from __future__ import annotations

from aiogram import Router

from tickets.handlers_admin import router as admin_router
from tickets.handlers_user import router as user_router


def tickets_router() -> Router:
    r = Router(name="tickets")
    r.include_router(admin_router)
    r.include_router(user_router)
    return r

