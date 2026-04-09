from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot
from aiogram.enums import ParseMode
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from shared.database import get_session_factory
from shared.tickets_db_compat import ticket_messages_has_photo_file_id_column
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.user import User
from shared.services.billing_v2.detail_service import get_month_summaries, get_today_summary, summarize_month_total
from tickets.config import config as tickets_config

router = APIRouter(tags=["tickets-api"])
_MSK_TZ = ZoneInfo("Europe/Moscow")
_TICKETS_LIST_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_TICKETS_LIST_CACHE_TTL_SEC = 8.0


def _is_api_logged(request: Request) -> bool:
    return bool(request.session.get("wauth"))


def _require_api_login(request: Request) -> None:
    if not _is_api_logged(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _session() -> AsyncSession:
    return get_session_factory()()


def _invalidate_tickets_list_cache() -> None:
    _TICKETS_LIST_CACHE.clear()


def _to_iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")
    return str(dt)


def _ticket_message_json(m: Any, has_photo: bool) -> dict[str, Any]:
    d = {k: (_to_iso(v) if isinstance(v, datetime) else v) for k, v in dict(m).items()}
    if not has_photo:
        d["photo_file_id"] = None
    return d


class TicketReplyIn(BaseModel):
    text: str


class TicketNoteIn(BaseModel):
    text: str


class TicketStatusIn(BaseModel):
    status: str


class TicketAssignIn(BaseModel):
    assigned_admin_id: int | None = None
    telegram_assigned_admin_id: int | None = None


@router.get("/billing/{user_id}/detail/today")
async def api_billing_detail_today(request: Request, user_id: int) -> dict:
    _require_api_login(request)
    async with await _session() as session:
        row = await get_today_summary(session, user_id=user_id)
    if row is None:
        return {"day": None, "items": None, "total_rub": "0.00"}
    return {
        "day": row.day.isoformat(),
        "items": {
            "gb_units": row.gb_units,
            "device_units": row.device_units,
            "mobile_gb_units": row.mobile_gb_units,
            "gb_amount_rub": str(row.gb_amount_rub),
            "device_amount_rub": str(row.device_amount_rub),
            "mobile_amount_rub": str(row.mobile_amount_rub),
        },
        "total_rub": str(row.total_amount_rub),
    }


@router.get("/billing/{user_id}/detail/month")
async def api_billing_detail_month(request: Request, user_id: int) -> dict:
    _require_api_login(request)
    async with await _session() as session:
        rows = await get_month_summaries(session, user_id=user_id)
    return {
        "days": [{"day": r.day.isoformat(), "total_rub": str(r.total_amount_rub)} for r in rows],
        "total_rub": str(summarize_month_total(rows)),
    }


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
    wauth = request.session.get("wauth") or {}
    admin_key = int(wauth.get("telegram_id") or 0)
    cache_key = (
        admin_key,
        (status or "").strip(),
        (date_from or "").strip(),
        (date_to or "").strip(),
        (q or "").strip(),
        (sort or "desc").strip().lower(),
        int(limit),
    )
    now_m = time.monotonic()
    cached = _TICKETS_LIST_CACHE.get(cache_key)
    if cached is not None and now_m - cached[0] < _TICKETS_LIST_CACHE_TTL_SEC:
        return cached[1]
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
    payload = {"items": items, "count": len(items)}
    _TICKETS_LIST_CACHE[cache_key] = (time.monotonic(), payload)
    if len(_TICKETS_LIST_CACHE) > 512:
        stale_keys = [k for k, v in _TICKETS_LIST_CACHE.items() if now_m - v[0] >= _TICKETS_LIST_CACHE_TTL_SEC]
        for k in stale_keys:
            _TICKETS_LIST_CACHE.pop(k, None)
    return payload


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
        has_photo = await ticket_messages_has_photo_file_id_column(session)
        msg_cols = (
            "id,ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal,photo_file_id"
            if has_photo
            else "id,ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal"
        )
        msgs = (
            await session.execute(
                text(
                    f"""
                    SELECT {msg_cols}
                    FROM ticket_messages
                    WHERE ticket_id = :tid
                    ORDER BY id ASC
                    """
                ),
                {"tid": ticket_id},
            )
        ).mappings().all()
        uid = int(t["user_id"])
        now = datetime.now(timezone.utc)
        urow = (await session.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        user_info: dict[str, object] | None = None
        if urow is not None:
            user_info = {
                "id": urow.id,
                "telegram_id": urow.telegram_id,
                "username": urow.username,
                "first_name": urow.first_name,
                "last_name": urow.last_name,
                "balance": str(urow.balance),
                "is_blocked": bool(urow.is_blocked),
            }
        sub_row = (
            await session.execute(
                select(Subscription, Plan)
                .join(Plan, Plan.id == Subscription.plan_id)
                .where(
                    Subscription.user_id == uid,
                    Subscription.status.in_(("active", "trial")),
                    Subscription.expires_at > now,
                )
                .order_by(Subscription.expires_at.desc())
                .limit(1)
            )
        ).first()
        user_subscription: dict[str, object] | None = None
        if sub_row is not None:
            sub, plan = sub_row
            user_subscription = {
                "id": sub.id,
                "plan_name": plan.name,
                "status": sub.status,
                "expires_at": _to_iso(sub.expires_at),
                "devices_count": sub.devices_count,
                "auto_renew": bool(sub.auto_renew),
            }
        last_cancelled_sub_id: int | None = None
        lc = (
            await session.execute(
                select(Subscription.id)
                .where(Subscription.user_id == uid, Subscription.status == "cancelled")
                .order_by(Subscription.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if lc is not None:
            last_cancelled_sub_id = int(lc)
    return {
        "ticket": {k: (_to_iso(v) if isinstance(v, datetime) else v) for k, v in dict(t).items()},
        "messages": [
            _ticket_message_json(m, has_photo)
            for m in msgs
        ],
        "user": user_info,
        "user_subscription": user_subscription,
        "last_cancelled_subscription_id": last_cancelled_sub_id,
    }


def _tg_photo_mime(path: str) -> str:
    p = (path or "").lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".webp"):
        return "image/webp"
    if p.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


@router.get("/tickets/{ticket_id}/messages/{msg_id}/photo")
async def api_ticket_message_photo(request: Request, ticket_id: int, msg_id: int) -> Response:
    """Прокси фото из Telegram (file_id бота тикетов), только для авторизованного веб-админа."""
    _require_api_login(request)
    tok = (tickets_config.bot_token or "").strip()
    if not tok:
        raise HTTPException(status_code=503, detail="Tickets bot not configured")
    async with await _session() as session:
        if not await ticket_messages_has_photo_file_id_column(session):
            raise HTTPException(status_code=404, detail="Photo not available")
        row = (
            await session.execute(
                text("SELECT photo_file_id FROM ticket_messages WHERE id=:mid AND ticket_id=:tid"),
                {"mid": msg_id, "tid": ticket_id},
            )
        ).mappings().first()
    if not row or not row.get("photo_file_id"):
        raise HTTPException(status_code=404, detail="Photo not found")
    fid = str(row["photo_file_id"])
    async with Bot(token=tok) as bot:
        f = await bot.get_file(fid)
        fp = f.file_path
        if not fp:
            raise HTTPException(status_code=404, detail="File path unavailable")
    url = f"https://api.telegram.org/file/bot{tok}/{fp}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return Response(content=r.content, media_type=_tg_photo_mime(fp))


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
        has_photo = await ticket_messages_has_photo_file_id_column(session)
        if has_photo:
            await session.execute(
                text(
                    """
                    INSERT INTO ticket_messages (ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal,photo_file_id)
                    VALUES (:tid,:sid,'admin',:stg,:txt,:now,false,NULL)
                    """
                ),
                {"tid": ticket_id, "sid": admin_uid, "stg": admin_tg or None, "txt": txt, "now": now},
            )
        else:
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
    _invalidate_tickets_list_cache()

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
    txt_html = html.escape(txt)
    if not txt:
        raise HTTPException(status_code=400, detail="text is required")
    wauth = request.session.get("wauth") or {}
    admin_label = html.escape(str(wauth.get("label") or "Администратор"))
    async with await _session() as session:
        t = (
            await session.execute(text("SELECT id, topic_id FROM tickets WHERE id=:tid"), {"tid": ticket_id})
        ).mappings().first()
        if t is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        admin_tg = int(wauth.get("telegram_id") or 0)
        admin_uid = None
        if admin_tg:
            u = (await session.execute(text("SELECT id FROM users WHERE telegram_id=:tg LIMIT 1"), {"tg": admin_tg})).first()
            admin_uid = int(u[0]) if u else None
        now = datetime.now(timezone.utc)
        has_photo = await ticket_messages_has_photo_file_id_column(session)
        if has_photo:
            await session.execute(
                text(
                    """
                    INSERT INTO ticket_messages (ticket_id,sender_id,sender_role,sender_telegram_id,text,created_at,is_internal,photo_file_id)
                    VALUES (:tid,:sid,'admin',:stg,:txt,:now,true,NULL)
                    """
                ),
                {"tid": ticket_id, "sid": admin_uid, "stg": admin_tg or None, "txt": txt, "now": now},
            )
        else:
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
    _invalidate_tickets_list_cache()
    if tickets_config.bot_token:
        try:
            topic_id = int(t["topic_id"] or 0)
        except Exception:
            topic_id = 0
        if topic_id:
            bot = Bot(token=tickets_config.bot_token)
            try:
                await bot.send_message(
                    chat_id=tickets_config.support_group_id,
                    message_thread_id=topic_id,
                    text=f"<b>📝 Внутренняя заметка</b> — {admin_label}\n\n<blockquote>{txt_html}</blockquote>",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            finally:
                await bot.session.close()
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
    _invalidate_tickets_list_cache()
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
    _invalidate_tickets_list_cache()
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

