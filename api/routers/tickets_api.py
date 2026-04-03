from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import get_session_factory
from tickets.config import config as tickets_config

router = APIRouter(tags=["tickets-api"])
_MSK_TZ = ZoneInfo("Europe/Moscow")


def _is_api_logged(request: Request) -> bool:
    return bool(request.session.get("wauth"))


def _require_api_login(request: Request) -> None:
    if not _is_api_logged(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _session() -> AsyncSession:
    return get_session_factory()()


def _to_iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")
    return str(dt)


class TicketReplyIn(BaseModel):
    text: str


class TicketNoteIn(BaseModel):
    text: str


class TicketStatusIn(BaseModel):
    status: str


class TicketAssignIn(BaseModel):
    assigned_admin_id: int | None = None
    telegram_assigned_admin_id: int | None = None


@router.get("/tickets")
async def api_tickets_list(
    request: Request,
    status: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    q: str | None = Query(default=None),
    sort: str = Query(default="desc"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    _require_api_login(request)
    where: list[str] = ["1=1"]
    params: dict[str, Any] = {"lim": int(limit)}
    if status:
        where.append("t.status = :st")
        params["st"] = status.strip()
    if date_from:
        where.append("t.created_at >= :df")
        params["df"] = date_from.strip()
    if date_to:
        where.append("t.created_at <= :dt")
        params["dt"] = date_to.strip()
    if q:
        where.append(
            "(CAST(t.id AS TEXT) ILIKE :q OR COALESCE(m0.text,'') ILIKE :q OR COALESCE(u.first_name,'') ILIKE :q OR COALESCE(u.username,'') ILIKE :q)"
        )
        params["q"] = f"%{q.strip()}%"
    ord_dir = "ASC" if sort.strip().lower() == "asc" else "DESC"
    sql = f"""
        SELECT
            t.id, t.status, t.topic_id, t.assigned_admin_id, t.telegram_assigned_admin_id,
            t.created_at, t.updated_at, t.closed_at, t.last_activity,
            u.id AS user_id, u.telegram_id, u.username, u.first_name, u.last_name,
            m0.text AS first_text
        FROM tickets t
        JOIN users u ON u.id = t.user_id
        LEFT JOIN LATERAL (
            SELECT tm.text
            FROM ticket_messages tm
            WHERE tm.ticket_id = t.id
            ORDER BY tm.id ASC
            LIMIT 1
        ) m0 ON true
        WHERE {" AND ".join(where)}
        ORDER BY COALESCE(t.last_activity, t.created_at) {ord_dir}, t.id DESC
        LIMIT :lim
    """
    async with await _session() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    items = []
    for r in rows:
        items.append(
            {
                "id": int(r["id"]),
                "status": r["status"],
                "topic_id": r["topic_id"],
                "assigned_admin_id": r["assigned_admin_id"],
                "telegram_assigned_admin_id": r["telegram_assigned_admin_id"],
                "created_at": _to_iso(r["created_at"]),
                "updated_at": _to_iso(r["updated_at"]),
                "closed_at": _to_iso(r["closed_at"]),
                "last_activity": _to_iso(r["last_activity"]),
                "user": {
                    "id": r["user_id"],
                    "telegram_id": r["telegram_id"],
                    "username": r["username"],
                    "first_name": r["first_name"],
                    "last_name": r["last_name"],
                },
                "preview": r["first_text"] or "",
            }
        )
    return {"items": items, "count": len(items)}


@router.get("/tickets/{ticket_id}")
async def api_ticket_detail(request: Request, ticket_id: int) -> dict:
    _require_api_login(request)
    async with await _session() as session:
        t = (
            await session.execute(
                text(
                    """
                    SELECT id,status,topic_id,user_id,telegram_user_id,assigned_admin_id,telegram_assigned_admin_id,
                           created_at,updated_at,closed_at,last_activity
                    FROM tickets WHERE id = :tid
                    """
                ),
                {"tid": ticket_id},
            )
        ).mappings().first()
        if t is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        msgs = (
            await session.execute(
                text(
                    """
                    SELECT id,ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal
                    FROM ticket_messages
                    WHERE ticket_id = :tid
                    ORDER BY id ASC
                    """
                ),
                {"tid": ticket_id},
            )
        ).mappings().all()
    return {
        "ticket": {k: (_to_iso(v) if isinstance(v, datetime) else v) for k, v in dict(t).items()},
        "messages": [
            {k: (_to_iso(v) if isinstance(v, datetime) else v) for k, v in dict(m).items()}
            for m in msgs
        ],
    }


@router.post("/tickets/{ticket_id}/reply")
async def api_ticket_reply(request: Request, ticket_id: int, body: TicketReplyIn) -> dict:
    _require_api_login(request)
    txt = (body.text or "").strip()
    txt_html = html.escape(txt)
    if not txt:
        raise HTTPException(status_code=400, detail="text is required")
    async with await _session() as session:
        t = (
            await session.execute(
                text("SELECT id,status,topic_id,telegram_user_id FROM tickets WHERE id=:tid"),
                {"tid": ticket_id},
            )
        ).mappings().first()
        if t is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if str(t["status"]) == "closed":
            raise HTTPException(status_code=400, detail="Ticket is closed")
        wauth = request.session.get("wauth") or {}
        admin_tg = int(wauth.get("telegram_id") or 0)
        admin_uid = None
        if admin_tg:
            u = (
                await session.execute(
                    text("SELECT id FROM users WHERE telegram_id=:tg LIMIT 1"),
                    {"tg": admin_tg},
                )
            ).first()
            admin_uid = int(u[0]) if u else None
        now = datetime.now(timezone.utc)
        await session.execute(
            text(
                """
                INSERT INTO ticket_messages (ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal)
                VALUES (:tid,:sid,'admin',:stg,:txt,:now,false)
                """
            ),
            {"tid": ticket_id, "sid": admin_uid, "stg": admin_tg or None, "txt": txt, "now": now},
        )
        await session.execute(
            text(
                """
                UPDATE tickets
                SET status = CASE WHEN status='open' THEN 'in_progress' ELSE status END,
                    assigned_admin_id = COALESCE(:aid, assigned_admin_id),
                    telegram_assigned_admin_id = COALESCE(:atg, telegram_assigned_admin_id),
                    updated_at=:now, last_activity=:now
                WHERE id=:tid
                """
            ),
            {"aid": admin_uid, "atg": admin_tg or None, "now": now, "tid": ticket_id},
        )
        await session.commit()

    if tickets_config.bot_token:
        bot = Bot(token=tickets_config.bot_token)
        try:
            uid = int(t["telegram_user_id"] or 0)
            if uid:
                await bot.send_message(
                    chat_id=uid,
                    text=f"📨 Ответ от администратора | Тикет #{ticket_id}\n\n{txt_html}\n\nС уважением, Flux Network",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            topic_id = int(t["topic_id"] or 0)
            if topic_id:
                label = html.escape(str((request.session.get("wauth") or {}).get("label") or "Администратор"))
                await bot.send_message(
                    chat_id=tickets_config.support_group_id,
                    message_thread_id=topic_id,
                    text=f"<b>💬 Ответ администратора</b> — {label}\n\n<blockquote>{txt_html}</blockquote>",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        finally:
            await bot.session.close()
    return {"ok": True}


@router.post("/tickets/{ticket_id}/note")
async def api_ticket_note(request: Request, ticket_id: int, body: TicketNoteIn) -> dict:
    _require_api_login(request)
    txt = (body.text or "").strip()
    if not txt:
        raise HTTPException(status_code=400, detail="text is required")
    async with await _session() as session:
        t = (await session.execute(text("SELECT id FROM tickets WHERE id=:tid"), {"tid": ticket_id})).first()
        if t is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        wauth = request.session.get("wauth") or {}
        admin_tg = int(wauth.get("telegram_id") or 0)
        admin_uid = None
        if admin_tg:
            u = (await session.execute(text("SELECT id FROM users WHERE telegram_id=:tg LIMIT 1"), {"tg": admin_tg})).first()
            admin_uid = int(u[0]) if u else None
        now = datetime.now(timezone.utc)
        await session.execute(
            text(
                """
                INSERT INTO ticket_messages (ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal)
                VALUES (:tid,:sid,'admin',:stg,:txt,:now,true)
                """
            ),
            {"tid": ticket_id, "sid": admin_uid, "stg": admin_tg or None, "txt": txt, "now": now},
        )
        await session.execute(
            text("UPDATE tickets SET updated_at=:now,last_activity=:now WHERE id=:tid"),
            {"now": now, "tid": ticket_id},
        )
        await session.commit()
    return {"ok": True}


@router.patch("/tickets/{ticket_id}/status")
async def api_ticket_status(request: Request, ticket_id: int, body: TicketStatusIn) -> dict:
    _require_api_login(request)
    st = (body.status or "").strip()
    if st not in {"open", "in_progress", "closed"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    now = datetime.now(timezone.utc)
    async with await _session() as session:
        t = (
            await session.execute(
                text("SELECT id,topic_id,telegram_user_id,status FROM tickets WHERE id=:tid"),
                {"tid": ticket_id},
            )
        ).mappings().first()
        if t is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        await session.execute(
            text(
                """
                UPDATE tickets
                SET status=CAST(:st AS VARCHAR), updated_at=:now, last_activity=:now,
                    closed_at=CASE WHEN CAST(:st AS VARCHAR)='closed' THEN :now ELSE closed_at END
                WHERE id=:tid
                """
            ),
            {"st": st, "now": now, "tid": ticket_id},
        )
        await session.commit()
    if tickets_config.bot_token:
        bot = Bot(token=tickets_config.bot_token)
        try:
            try:
                topic_id = int(t["topic_id"] or 0)
            except Exception:
                topic_id = 0
            # Всегда пишем в топик о смене статуса из веба.
            if topic_id:
                status_ru = {"open": "Открыт", "in_progress": "В работе", "closed": "Закрыт"}.get(st, st)
                try:
                    await bot.send_message(
                        chat_id=tickets_config.support_group_id,
                        message_thread_id=topic_id,
                        text=f"<b>ℹ️ Статус тикета #{ticket_id} обновлен через сайт:</b> {status_ru}",
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
                if st in {"open", "in_progress"}:
                    try:
                        await bot.reopen_forum_topic(chat_id=tickets_config.support_group_id, message_thread_id=topic_id)
                    except Exception:
                        pass
                if st == "closed":
                    try:
                        await bot.close_forum_topic(chat_id=tickets_config.support_group_id, message_thread_id=topic_id)
                    except Exception:
                        pass
            if st == "closed":
                uid = int(t["telegram_user_id"] or 0)
                if uid:
                    try:
                        await bot.send_message(chat_id=uid, text=f"Ваш тикет #{ticket_id} был закрыт администратором")
                    except Exception:
                        pass
        finally:
            await bot.session.close()
    return {"ok": True}


@router.patch("/tickets/{ticket_id}/assign")
async def api_ticket_assign(request: Request, ticket_id: int, body: TicketAssignIn) -> dict:
    _require_api_login(request)
    now = datetime.now(timezone.utc)
    async with await _session() as session:
        t = (await session.execute(text("SELECT id FROM tickets WHERE id=:tid"), {"tid": ticket_id})).first()
        if t is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        await session.execute(
            text(
                """
                UPDATE tickets
                SET assigned_admin_id=:aid,
                    telegram_assigned_admin_id=:atg,
                    updated_at=:now
                WHERE id=:tid
                """
            ),
            {"aid": body.assigned_admin_id, "atg": body.telegram_assigned_admin_id, "now": now, "tid": ticket_id},
        )
        await session.commit()
    return {"ok": True}


@router.get("/users/{user_id}")
async def api_user_profile_with_tickets(request: Request, user_id: int) -> dict:
    _require_api_login(request)
    async with await _session() as session:
        u = (
            await session.execute(
                text(
                    """
                    SELECT id,telegram_id,username,first_name,last_name,created_at
                    FROM users
                    WHERE id=:uid
                    """
                ),
                {"uid": user_id},
            )
        ).mappings().first()
        if u is None:
            raise HTTPException(status_code=404, detail="User not found")
        tickets = (
            await session.execute(
                text(
                    """
                    SELECT t.id,t.status,t.created_at,t.closed_at,t.last_activity,
                           (SELECT tr.rating FROM ticket_ratings tr WHERE tr.ticket_id=t.id ORDER BY tr.id DESC LIMIT 1) AS rating
                    FROM tickets t
                    WHERE t.user_id=:uid
                    ORDER BY t.id DESC
                    """
                ),
                {"uid": user_id},
            )
        ).mappings().all()
    stats_total = len(tickets)
    stats_open = sum(1 for t in tickets if t["status"] in ("open", "in_progress"))
    stats_closed = sum(1 for t in tickets if t["status"] == "closed")
    rates = [1 if t["rating"] is True else 0 for t in tickets if t["rating"] is not None]
    avg_rating = (sum(rates) / len(rates)) if rates else None
    return {
        "user": {k: (_to_iso(v) if isinstance(v, datetime) else v) for k, v in dict(u).items()},
        "stats": {
            "total": stats_total,
            "open": stats_open,
            "closed": stats_closed,
            "avg_rating": avg_rating,
        },
        "tickets": [{k: (_to_iso(v) if isinstance(v, datetime) else v) for k, v in dict(t).items()} for t in tickets],
    }

