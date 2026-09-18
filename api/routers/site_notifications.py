"""Колокольчик уведомлений в личном кабинете — реальная история рассылок (BroadcastHistory),
те же сообщения, что уходят пользователям через бота (см. shared/services/broadcast_service.py)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from shared.database import get_session_factory
from shared.models.broadcast_mailing import BroadcastHistory
from shared.services.broadcast_service import broadcast_html_preview_fragment
from shared.services.site_session_service import load_site_user
from sqlalchemy import select

router = APIRouter()

_LIMIT = 12


def _short_title(body_text: str) -> str:
    first_line = (body_text or "").strip().splitlines()[0] if body_text.strip() else ""
    # Черновик рассылки часто начинается с markdown-разметки — грубо снимем самые частые символы.
    first_line = first_line.strip("*_# ").strip()
    if len(first_line) > 72:
        first_line = first_line[:72].rstrip() + "…"
    return first_line or "Уведомление"


def _relative(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 3600:
        return "только что" if secs < 60 else f"{secs // 60} мин. назад"
    if secs < 86400:
        return f"{secs // 3600} ч. назад"
    if secs < 86400 * 30:
        return f"{secs // 86400} дн. назад"
    return dt.strftime("%d.%m.%Y")


@router.get("/app/api/notifications")
async def site_notifications(request: Request) -> JSONResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return JSONResponse({"unread": 0, "items": []}, status_code=401)
        user, _sess_row = auth

        rows = list(
            (
                await session.execute(
                    select(BroadcastHistory).order_by(BroadcastHistory.sent_at.desc()).limit(_LIMIT)
                )
            ).scalars()
        )

        seen_at = user.notifications_seen_at
        seen_cmp = seen_at.replace(tzinfo=timezone.utc) if seen_at and seen_at.tzinfo is None else seen_at
        unread = 0
        for r in rows:
            sent = r.sent_at.replace(tzinfo=timezone.utc) if r.sent_at.tzinfo is None else r.sent_at
            if seen_cmp is None or sent > seen_cmp:
                unread += 1

        user.notifications_seen_at = datetime.now(timezone.utc)
        await session.commit()

    items = [
        {
            "id": r.id,
            "title": _short_title(r.body_text),
            "body_html": broadcast_html_preview_fragment(r.body_text)[:1500],
            "sent_at": _relative(r.sent_at),
        }
        for r in rows
    ]
    return JSONResponse({"unread": unread, "items": items})
