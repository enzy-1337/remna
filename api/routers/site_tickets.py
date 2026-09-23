"""Тикеты поддержки в личном кабинете сайта.

/app/tickets        — список обращений (без предпросмотра);
/app/tickets/{id}   — отдельная страница-чат: фото, видео, кружочки, голосовые, аудио, документы;
                      отправка текста, файлов и записанных в браузере голосовых.

Медиа хранятся как file_id бота тикетов (как и сообщения из Telegram): файл с сайта отправляется
в тему тикета в группе поддержки, а file_id записывается в ticket_messages. Просмотр — через прокси
/app/tickets/{id}/media/{msg_id} (только владельцу тикета), с поддержкой Range для видео на iPhone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, Message
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.database import get_session_factory
from shared.services.site_session_service import load_site_user, touch_session
from tickets.config import config as tickets_config
from tickets.services import add_ticket_message, bump_ticket_activity, create_ticket, open_ticket_forum_topic

from api.routers.site_theme import app_topbar, esc, fmt_money, icon, page, site_footer

router = APIRouter()
logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")
# Bot API отдаёт на скачивание файлы только до 20 МБ — больше загружать нет смысла (не откроется на сайте).
_TG_DOWNLOAD_LIMIT_MB = 20

_STATUS_BADGE = {
    "open": ('<span class="badge badge-warn">Открыт</span>', "Ожидает ответа поддержки"),
    "in_progress": ('<span class="badge badge-success">В работе</span>', "Поддержка ответила"),
    "closed": ('<span class="badge badge-neutral">Закрыт</span>', "Диалог завершён"),
}

_MEDIA_COLS = (
    "photo_file_id, video_file_id, document_file_id, document_file_name, voice_file_id, "
    "video_note_file_id, audio_file_id, audio_file_name"
)


# --------------------------------------------------------------------------------------------
# Данные
# --------------------------------------------------------------------------------------------


async def _user_tickets(session: AsyncSession, user_id: int) -> list[dict]:
    rows = (
        await session.execute(
            text(
                """
                SELECT t.id, t.status, t.created_at, t.updated_at, t.closed_at,
                       (SELECT COALESCE(NULLIF(TRIM(m.text), ''),
                               CASE WHEN m.photo_file_id IS NOT NULL THEN '📷 Фото'
                                    WHEN m.video_file_id IS NOT NULL THEN '🎥 Видео'
                                    WHEN m.voice_file_id IS NOT NULL THEN '🎤 Голосовое сообщение'
                                    WHEN m.video_note_file_id IS NOT NULL THEN '⭕ Видеосообщение'
                                    WHEN m.audio_file_id IS NOT NULL THEN '🎵 Аудио'
                                    WHEN m.document_file_id IS NOT NULL THEN '📎 Файл'
                                    ELSE '' END)
                          FROM ticket_messages m
                         WHERE m.ticket_id = t.id AND COALESCE(m.is_internal,false)=false
                         ORDER BY m.id DESC LIMIT 1) AS last_text,
                       (SELECT m.sender_role FROM ticket_messages m
                         WHERE m.ticket_id = t.id AND COALESCE(m.is_internal,false)=false
                         ORDER BY m.id DESC LIMIT 1) AS last_role,
                       (SELECT COUNT(*) FROM ticket_messages m WHERE m.ticket_id = t.id AND COALESCE(m.is_internal,false)=false) AS msg_count
                FROM tickets t
                WHERE t.user_id = :uid
                ORDER BY t.updated_at DESC
                LIMIT 100
                """
            ),
            {"uid": user_id},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def _ticket_row(session: AsyncSession, ticket_id: int, user_id: int) -> dict | None:
    row = (
        await session.execute(
            text("SELECT id, user_id, topic_id, status, created_at FROM tickets WHERE id=:tid"),
            {"tid": ticket_id},
        )
    ).mappings().first()
    if row is None or int(row["user_id"]) != int(user_id):
        return None
    return dict(row)


async def _ticket_messages(session: AsyncSession, ticket_id: int, user_id: int) -> list[dict] | None:
    if await _ticket_row(session, ticket_id, user_id) is None:
        return None
    rows = (
        await session.execute(
            text(
                f"SELECT id, sender_role, text, created_at, {_MEDIA_COLS} FROM ticket_messages "
                "WHERE ticket_id=:tid AND COALESCE(is_internal,false)=false ORDER BY id ASC"
            ),
            {"tid": ticket_id},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def _media_info(m: dict, ticket_id: int) -> dict | None:
    url = f"/app/tickets/{ticket_id}/media/{m['id']}"
    if m.get("photo_file_id"):
        return {"kind": "photo", "url": url}
    if m.get("video_file_id"):
        return {"kind": "video", "url": url}
    if m.get("video_note_file_id"):
        return {"kind": "video_note", "url": url}
    if m.get("voice_file_id"):
        return {"kind": "voice", "url": url}
    if m.get("audio_file_id"):
        return {"kind": "audio", "url": url, "name": (m.get("audio_file_name") or "").strip() or "Аудио"}
    if m.get("document_file_id"):
        return {"kind": "document", "url": url, "name": (m.get("document_file_name") or "").strip() or "Файл"}
    return None


def _msk(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK)


def _message_json(m: dict, ticket_id: int) -> dict:
    ts = _msk(m.get("created_at"))
    return {
        "id": int(m["id"]),
        "sender_role": m.get("sender_role"),
        "text": m.get("text") or "",
        "time": ts.strftime("%H:%M") if ts else "",
        "date": ts.strftime("%d.%m.%Y") if ts else "",
        "media": _media_info(m, ticket_id),
    }


def _relative_time(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "только что"
    if secs < 3600:
        return f"{secs // 60} мин. назад"
    if secs < 86400:
        return f"{secs // 3600} ч. назад"
    if secs < 86400 * 30:
        return f"{secs // 86400} дн. назад"
    return dt.strftime("%d.%m.%Y")


# --------------------------------------------------------------------------------------------
# Список тикетов
# --------------------------------------------------------------------------------------------


@router.get("/app/tickets")
async def tickets_page(request: Request, ticket: int = 0) -> HTMLResponse:
    if ticket:
        # старые ссылки вида /app/tickets?ticket=5
        return RedirectResponse(f"/app/tickets/{ticket}", status_code=303)
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        tickets = await _user_tickets(session, user.id)

    initial = (user.first_name or user.username or "U")[:1].upper()
    n_open = sum(1 for t in tickets if t["status"] == "open")
    n_prog = sum(1 for t in tickets if t["status"] == "in_progress")
    n_closed = sum(1 for t in tickets if t["status"] == "closed")

    rows_html = ""
    for t in tickets:
        badge, _hint = _STATUS_BADGE.get(t["status"], _STATUS_BADGE["open"])
        preview = (t.get("last_text") or "").strip().replace("\n", " ")[:90] or "Новый тикет"
        who = "Вы: " if t.get("last_role") == "user" else ("Поддержка: " if t.get("last_role") else "")
        rows_html += f"""
        <a href="/app/tickets/{t['id']}" class="card tk-list-item">
          <div style="display:flex;align-items:center;gap:10px;justify-content:space-between;">
            <span style="font:800 14px Manrope;color:var(--text-1);">Тикет #{t['id']}</span>
            {badge}
          </div>
          <div style="font:500 13px Manrope;color:var(--text-3);margin-top:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
            <span style="color:var(--text-4);">{esc(who)}</span>{esc(preview)}</div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-top:8px;">
            <span style="font:500 11px Manrope;color:var(--text-4);">Обновлён {_relative_time(t['updated_at'])} · {t['msg_count']} сообщ.</span>
            {icon('chevron-right', size=15, color='var(--text-4)')}
          </div>
        </a>"""
    if not tickets:
        rows_html = _empty_list_html()

    body = f"""
<style>
.tk-list-item{{display:block;margin-bottom:10px;padding:16px 18px;transition:border-color .15s ease,background .15s ease;}}
.tk-list-item:hover{{border-color:rgba(123,92,255,.35);background:rgba(123,92,255,.05);}}
</style>
<div class="cabinet-bg" style="min-height:100vh;">
  <div class="shell-wide">
    {app_topbar(active="tickets", balance_rub=fmt_money(user.balance), unread_tickets=n_open, initial=initial)}

    <div style="max-width:860px;margin:0 auto;">
      <div class="fade-up" style="display:flex;align-items:center;justify-content:space-between;margin-top:12px;flex-wrap:wrap;gap:12px;">
        <div style="display:flex;align-items:center;gap:14px;">
          <div style="width:34px;height:34px;border-radius:11px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;">{icon('tickets', size=17, color='#7B5CFF')}</div>
          <div>
            <div style="font:800 22px Manrope;color:var(--text-1);">Тикеты</div>
            <div style="font:500 13px Manrope;color:var(--text-3);">Обращения в службу поддержки</div>
          </div>
        </div>
        <button class="btn btn-primary" data-new-ticket>{icon('plus', size=15, color='#fff')}<span>Новый тикет</span></button>
      </div>

      <div class="grid-auto fade-up d1" style="grid-template-columns:repeat(3,1fr);margin-top:20px;">
        <div class="card" style="text-align:center;"><div style="font:800 22px Manrope;color:var(--warn);">{n_open}</div><div style="font:600 11px Manrope;color:var(--text-4);margin-top:4px;">ОТКРЫТЫХ</div></div>
        <div class="card" style="text-align:center;"><div style="font:800 22px Manrope;color:var(--success);">{n_prog}</div><div style="font:600 11px Manrope;color:var(--text-4);margin-top:4px;">В РАБОТЕ</div></div>
        <div class="card" style="text-align:center;"><div style="font:800 22px Manrope;color:var(--text-2);">{n_closed}</div><div style="font:600 11px Manrope;color:var(--text-4);margin-top:4px;">ЗАКРЫТО</div></div>
      </div>

      <div class="fade-up d2" style="margin-top:20px;">{rows_html}</div>
    </div>
    {site_footer()}
  </div>
</div>
<div id="new-ticket-modal" class="modal-overlay">
  <div class="fade-in" style="width:100%;max-width:420px;background:var(--card-2);border:1px solid var(--line-2);border-radius:18px;padding:24px;">
    <div style="font:800 17px Manrope;color:var(--text-1);">Новый тикет</div>
    <div style="font:500 12px Manrope;color:var(--text-4);margin-top:4px;">Фото, видео, файлы и голосовые можно отправить в чате тикета после создания.</div>
    <textarea id="new-ticket-text" class="input" rows="4" placeholder="Опишите проблему…" style="margin-top:14px;resize:vertical;"></textarea>
    <div style="display:flex;gap:10px;margin-top:14px;">
      <button type="button" class="btn btn-outline btn-block" data-close-new-ticket>Отмена</button>
      <button type="button" class="btn btn-primary btn-block" data-submit-new-ticket>Создать</button>
    </div>
  </div>
</div>
<script>
document.querySelectorAll('[data-new-ticket]').forEach(function(b){{b.addEventListener('click',function(){{document.getElementById('new-ticket-modal').classList.add('open');document.getElementById('new-ticket-text').focus();}});}});
document.querySelectorAll('[data-close-new-ticket]').forEach(function(b){{b.addEventListener('click',function(){{document.getElementById('new-ticket-modal').classList.remove('open');}});}});
document.getElementById('new-ticket-modal').addEventListener('click',function(e){{ if (e.target === this) this.classList.remove('open'); }});
document.querySelectorAll('[data-submit-new-ticket]').forEach(function(b){{b.addEventListener('click',function(){{
  var val=document.getElementById('new-ticket-text').value.trim();
  if(!val) return;
  b.disabled=true;
  fetch('/app/tickets/0/send',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'text='+encodeURIComponent(val)}})
    .then(function(r){{return r.json();}}).then(function(d){{ if(d.ticket_id) window.location.href='/app/tickets/'+d.ticket_id; else b.disabled=false; }})
    .catch(function(){{ b.disabled=false; }});
}});}});
</script>
"""
    return HTMLResponse(page(title="Тикеты — Flux Network", body=body))


def _empty_list_html() -> str:
    tips = [
        "Опишите проблему как можно точнее — что вы делали и что пошло не так",
        "Укажите устройство и приложение, если вопрос про подключение",
        "В чате тикета можно отправить скриншот, видео, файл или голосовое сообщение",
    ]
    tips_html = "".join(
        f'<div style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;">'
        f'<span style="width:20px;height:20px;border-radius:7px;background:rgba(123,92,255,.14);color:var(--accent-soft);'
        f'font:800 11px Manrope;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px;">{i + 1}</span>'
        f'<span style="font:500 12.5px Manrope;color:var(--text-3);line-height:1.5;">{esc(tip)}</span></div>'
        for i, tip in enumerate(tips)
    )
    return f"""
    <div class="card" style="text-align:center;padding:40px 20px 28px;">
      {icon('tickets', size=32, color='var(--text-4)')}
      <div style="font:700 15px Manrope;color:var(--text-3);margin-top:12px;">Тикетов пока нет</div>
      <div style="font:500 12px Manrope;color:var(--text-4);margin-top:4px;">Создайте обращение — отвечаем в этом же чате и в Telegram</div>
      <div style="text-align:left;max-width:360px;margin:18px auto 0;border-top:1px solid var(--line);padding-top:6px;">{tips_html}</div>
    </div>"""


# --------------------------------------------------------------------------------------------
# Страница чата тикета
# --------------------------------------------------------------------------------------------


@router.get("/app/tickets/{ticket_id}")
async def ticket_chat_page(request: Request, ticket_id: int) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        t = await _ticket_row(session, ticket_id, user.id)
        if t is None:
            return RedirectResponse("/app/tickets", status_code=303)
        msgs = await _ticket_messages(session, ticket_id, user.id) or []

    initial = (user.first_name or user.username or "U")[:1].upper()
    badge, hint = _STATUS_BADGE.get(t["status"], _STATUS_BADGE["open"])
    created = _msk(t.get("created_at"))
    created_label = created.strftime("%d.%m.%Y, %H:%M") if created else ""
    closed = t["status"] == "closed"
    max_mb = min(int(tickets_config.media_max_mb), _TG_DOWNLOAD_LIMIT_MB)
    media_ready = bool(tickets_config.bot_token and tickets_config.support_group_id)
    cfg = {
        "ticketId": ticket_id,
        "initial": initial,
        "closed": closed,
        "maxBytes": max_mb * 1024 * 1024,
        "maxMb": max_mb,
        "mediaReady": media_ready,
        "messages": [_message_json(m, ticket_id) for m in msgs],
    }
    composer = (
        '<div class="tk-closed">Тикет закрыт. Если вопрос остался — создайте новый.</div>'
        if closed
        else f"""
      <div class="tk-compose">
        <div class="tk-attach" id="tk-attach" hidden>
          <div class="tk-attach-thumb" id="tk-attach-thumb">{icon('file', size=18, color='var(--accent-soft)')}</div>
          <div style="flex:1;min-width:0;">
            <div class="tk-attach-name" id="tk-attach-name"></div>
            <div class="tk-attach-size" id="tk-attach-size"></div>
          </div>
          <button type="button" class="tk-icon-btn" id="tk-attach-remove" title="Убрать">{icon('x', size=15)}</button>
        </div>
        <div class="tk-rec" id="tk-rec" hidden>
          <button type="button" class="tk-icon-btn" id="tk-rec-cancel" title="Отменить">{icon('trash', size=17, color='var(--danger-soft)')}</button>
          <span class="tk-rec-dot"></span><span class="tk-rec-time" id="tk-rec-time">0:00</span>
          <span class="tk-rec-hint">Идёт запись…</span>
          <button type="button" class="tk-send-btn" id="tk-rec-send" title="Отправить">{icon('send-tg', size=17, color='#fff')}</button>
        </div>
        <div class="tk-row" id="tk-row">
          <label class="tk-icon-btn" title="Прикрепить файл" {'' if media_ready else 'hidden'}>
            {icon('paperclip', size=19)}
            <input type="file" id="tk-file" hidden />
          </label>
          <textarea id="tk-text" rows="1" placeholder="Сообщение…" maxlength="4000"></textarea>
          <button type="button" class="tk-send-btn" id="tk-mic" title="Записать голосовое" {'' if media_ready else 'hidden'}>{icon('mic', size=18, color='#fff')}</button>
          <button type="button" class="tk-send-btn" id="tk-send" title="Отправить" hidden>{icon('send-tg', size=17, color='#fff')}</button>
        </div>
      </div>"""
    )
    body = f"""
<style>{_CHAT_CSS}</style>
<div class="cabinet-bg tk-wrap">
  <div class="tk-page">
    <div class="tk-head">
      <a href="/app/tickets" class="tk-icon-btn" title="К списку тикетов">{icon('arrow-left', size=19)}</a>
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span style="font:800 16px Manrope;color:var(--text-1);">Тикет #{ticket_id}</span>{badge}
        </div>
        <div style="font:500 11.5px Manrope;color:var(--text-4);margin-top:2px;">{esc(hint)} · создан {esc(created_label)}</div>
      </div>
      <a href="/app" class="tk-icon-btn" title="В личный кабинет">{icon('home', size=17)}</a>
    </div>
    <div class="tk-scroll" id="tk-scroll"></div>
    {composer}
  </div>
</div>
<div class="tk-lightbox" id="tk-lightbox" hidden><img id="tk-lightbox-img" alt=""/></div>
<script id="tk-config" type="application/json">{json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")}</script>
<script>{_CHAT_JS}</script>
"""
    return HTMLResponse(page(title=f"Тикет #{ticket_id} — Flux Network", body=body))


_CHAT_CSS = """
.tk-page [hidden],.tk-lightbox[hidden]{display:none !important;}
.tk-wrap{min-height:100vh;min-height:100dvh;}
.tk-page{max-width:860px;margin:0 auto;height:100vh;height:100dvh;display:flex;flex-direction:column;
  padding:16px 16px calc(12px + env(safe-area-inset-bottom,0px));}
.tk-head{display:flex;align-items:center;gap:12px;padding:12px 14px;background:rgba(17,24,32,.92);border:1px solid var(--line-2);
  border-radius:16px;flex-shrink:0;}
.tk-icon-btn{width:38px;height:38px;border-radius:12px;background:var(--card-3);border:0;color:var(--text-2);display:flex;
  align-items:center;justify-content:center;flex-shrink:0;cursor:pointer;}
.tk-icon-btn:hover{background:var(--card-4);color:var(--text-1);}
.tk-scroll{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:16px 4px;display:flex;flex-direction:column;gap:8px;}
.tk-day{align-self:center;font:700 11px Manrope;color:var(--text-4);background:var(--card-3);padding:4px 10px;border-radius:999px;margin:8px 0;}
.tk-msg{display:flex;gap:8px;max-width:86%;align-items:flex-end;}
.tk-msg.me{margin-left:auto;flex-direction:row-reverse;}
.tk-ava{width:28px;height:28px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;
  font:800 11px Manrope;color:#fff;background:linear-gradient(140deg,var(--accent),var(--accent-2));}
.tk-msg:not(.me) .tk-ava{background:var(--card-4);color:var(--accent-soft);}
.tk-bubble{padding:9px 12px;border-radius:16px 16px 16px 5px;background:var(--card-4);font:500 14px/1.5 Manrope;color:var(--text-2);
  word-break:break-word;min-width:0;max-width:100%;}
.tk-msg.me .tk-bubble{background:rgba(123,92,255,.16);border:1px solid rgba(123,92,255,.25);border-radius:16px 16px 5px 16px;color:var(--text-1);}
.tk-time{font:600 10px Manrope;color:var(--text-4);margin-top:4px;text-align:right;}
.tk-media{display:block;border-radius:12px;max-width:100%;margin:-2px -4px 6px;background:#000;}
.tk-photo{max-height:320px;width:auto;cursor:zoom-in;object-fit:cover;}
.tk-video{max-height:360px;width:100%;}
.tk-vnote{width:220px;height:220px;border-radius:50%;object-fit:cover;}
.tk-audio{width:260px;max-width:100%;height:40px;margin:2px 0 4px;}
.tk-doc{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:12px;background:rgba(255,255,255,.04);
  border:1px solid var(--line-2);color:var(--text-1);text-decoration:none;margin-bottom:6px;max-width:280px;}
.tk-doc-ic{width:34px;height:34px;border-radius:10px;background:rgba(123,92,255,.18);display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.tk-doc-name{font:700 13px Manrope;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.tk-doc-sub{font:500 11px Manrope;color:var(--text-4);}
.tk-pending{opacity:.75;}
.tk-progress{height:4px;border-radius:4px;background:var(--card-3);overflow:hidden;margin-top:6px;min-width:160px;}
.tk-progress>div{height:100%;background:var(--accent);width:0;transition:width .2s ease;}
.tk-compose{flex-shrink:0;background:rgba(17,24,32,.95);border:1px solid var(--line-2);border-radius:18px;padding:8px;}
.tk-row{display:flex;align-items:flex-end;gap:8px;}
.tk-row textarea{flex:1;resize:none;background:transparent;border:0;outline:none;color:var(--text-1);font:500 15px/1.45 Manrope;
  padding:9px 4px;max-height:140px;min-height:38px;}
.tk-row textarea::placeholder{color:var(--text-5);}
.tk-send-btn{width:40px;height:40px;border-radius:50%;border:0;background:var(--accent);display:flex;align-items:center;
  justify-content:center;flex-shrink:0;cursor:pointer;box-shadow:0 10px 24px -12px rgba(123,92,255,.9);}
.tk-send-btn:disabled{opacity:.5;cursor:default;}
.tk-attach{display:flex;align-items:center;gap:10px;padding:6px 6px 10px;border-bottom:1px solid var(--line);margin-bottom:6px;}
.tk-attach-thumb{width:44px;height:44px;border-radius:10px;background:var(--card-3);display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;}
.tk-attach-thumb img,.tk-attach-thumb video{width:100%;height:100%;object-fit:cover;}
.tk-attach-name{font:700 13px Manrope;color:var(--text-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.tk-attach-size{font:500 11px Manrope;color:var(--text-4);}
.tk-rec{display:flex;align-items:center;gap:10px;padding:2px;}
.tk-rec-dot{width:10px;height:10px;border-radius:50%;background:var(--danger);animation:tkPulse 1s ease-in-out infinite;}
.tk-rec-time{font:800 14px 'JetBrains Mono',monospace;color:var(--text-1);}
.tk-rec-hint{flex:1;font:500 12px Manrope;color:var(--text-4);}
@keyframes tkPulse{50%{opacity:.3;}}
.tk-closed{flex-shrink:0;text-align:center;font:600 12.5px Manrope;color:var(--text-4);padding:14px;border:1px dashed var(--line-2);border-radius:14px;}
.tk-lightbox{position:fixed;inset:0;z-index:1200;background:rgba(0,0,0,.92);display:flex;align-items:center;justify-content:center;padding:12px;cursor:zoom-out;}
.tk-lightbox[hidden]{display:none;}
.tk-lightbox img{max-width:100%;max-height:100%;border-radius:10px;}
@media (max-width:640px){
  .tk-page{padding:8px 8px calc(8px + env(safe-area-inset-bottom,0px));}
  .tk-msg{max-width:92%;}
  .tk-vnote{width:180px;height:180px;}
  .tk-row textarea{font-size:16px;} /* iOS не зумит поле ввода при 16px */
}
"""


_CHAT_JS = r"""
(function(){
  var cfg = JSON.parse(document.getElementById('tk-config').textContent);
  var scroll = document.getElementById('tk-scroll');
  var rendered = {};
  var lastDate = '';
  var toast = function(kind, msg){ if (window.remnaToast) window.remnaToast(kind, msg); else alert(msg); };

  function esc(s){ var d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
  function fmtText(s){ return esc(s).replace(/\n/g,'<br>').replace(/(https?:\/\/[^\s<]+)/g,'<a href="$1" target="_blank" rel="noopener" style="color:var(--accent-softer);text-decoration:underline;">$1</a>'); }
  function fmtSize(b){ if(b<1024) return b+' Б'; if(b<1048576) return (b/1024).toFixed(0)+' КБ'; return (b/1048576).toFixed(1)+' МБ'; }
  function nearBottom(){ return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140; }
  function toBottom(){ scroll.scrollTop = scroll.scrollHeight; }

  var FILE_IC = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#C8B6FF" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';

  function mediaHtml(md){
    if(!md) return '';
    var u = md.url;
    switch(md.kind){
      case 'photo': return '<img class="tk-media tk-photo" src="'+u+'" loading="lazy" alt="Фото" data-zoom="'+u+'">';
      case 'video': return '<video class="tk-media tk-video" src="'+u+'" controls playsinline preload="metadata"></video>';
      case 'video_note': return '<video class="tk-media tk-vnote" src="'+u+'" controls playsinline preload="metadata"></video>';
      case 'voice': return '<audio class="tk-audio" src="'+u+'" controls preload="none"></audio>';
      case 'audio': return '<div class="tk-doc-sub" style="margin-bottom:4px;">🎵 '+esc(md.name)+'</div><audio class="tk-audio" src="'+u+'" controls preload="none"></audio>';
      default: return '<a class="tk-doc" href="'+u+'?dl=1" download><span class="tk-doc-ic">'+FILE_IC+'</span>'
        + '<span style="min-width:0;"><div class="tk-doc-name">'+esc(md.name)+'</div><div class="tk-doc-sub">Нажмите, чтобы скачать</div></span></a>';
    }
  }

  function msgHtml(m){
    var me = m.sender_role === 'user';
    var body = mediaHtml(m.media) + (m.text ? '<div>'+fmtText(m.text)+'</div>' : '');
    if(!body) body = '<span style="opacity:.5;">Пустое сообщение</span>';
    return '<div class="tk-msg'+(me?' me':'')+'" data-mid="'+m.id+'">'
      + '<div class="tk-ava">'+esc(me ? cfg.initial : 'S')+'</div>'
      + '<div class="tk-bubble">'+body+'<div class="tk-time">'+esc(m.time)+'</div></div></div>';
  }

  function append(msgs){
    var stick = nearBottom();
    var added = false;
    msgs.forEach(function(m){
      if(rendered[m.id]) return;
      rendered[m.id] = true;
      if(m.date && m.date !== lastDate){
        lastDate = m.date;
        scroll.insertAdjacentHTML('beforeend', '<div class="tk-day">'+esc(m.date)+'</div>');
      }
      scroll.insertAdjacentHTML('beforeend', msgHtml(m));
      added = true;
    });
    if(!Object.keys(rendered).length && !scroll.querySelector('.tk-empty')){
      scroll.innerHTML = '<div class="tk-empty" style="margin:auto;text-align:center;font:500 13px Manrope;color:var(--text-4);">Сообщений пока нет — напишите нам</div>';
    }
    if(added){
      var empty = scroll.querySelector('.tk-empty'); if(empty) empty.remove();
      if(stick) toBottom();
    }
  }
  append(cfg.messages || []);
  toBottom();
  // картинки догружаются — держим низ
  scroll.addEventListener('load', function(e){ if(e.target.tagName==='IMG' && nearBottom()) toBottom(); }, true);

  function poll(){
    fetch('/app/tickets/'+cfg.ticketId+'/messages', {credentials:'same-origin'})
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(d){ if(d && d.messages) append(d.messages); })
      .catch(function(){});
  }
  setInterval(poll, 3000);
  document.addEventListener('visibilitychange', function(){ if(!document.hidden) poll(); });

  // просмотр фото
  var lb = document.getElementById('tk-lightbox'), lbImg = document.getElementById('tk-lightbox-img');
  scroll.addEventListener('click', function(e){
    var z = e.target.closest('[data-zoom]'); if(!z) return;
    lbImg.src = z.getAttribute('data-zoom'); lb.hidden = false;
  });
  lb.addEventListener('click', function(){ lb.hidden = true; lbImg.src=''; });

  if(cfg.closed) return;

  var textEl = document.getElementById('tk-text');
  var fileEl = document.getElementById('tk-file');
  var sendBtn = document.getElementById('tk-send');
  var micBtn = document.getElementById('tk-mic');
  var attachBox = document.getElementById('tk-attach');
  var row = document.getElementById('tk-row');
  var recBox = document.getElementById('tk-rec');
  var file = null;
  var busy = false;
  var canRecord = cfg.mediaReady && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  var isTouch = window.matchMedia && window.matchMedia('(pointer:coarse)').matches;

  function syncButtons(){
    var hasContent = textEl.value.trim().length > 0 || !!file;
    sendBtn.hidden = !hasContent && canRecord;
    micBtn.hidden = hasContent || !canRecord;
  }
  function autosize(){ textEl.style.height='auto'; textEl.style.height=Math.min(textEl.scrollHeight,140)+'px'; }
  textEl.addEventListener('input', function(){ autosize(); syncButtons(); });
  textEl.addEventListener('keydown', function(e){
    if(e.key==='Enter' && !e.shiftKey && !isTouch){ e.preventDefault(); send(); }
  });
  syncButtons();

  function setFile(f){
    if(f && f.size > cfg.maxBytes){ toast('error', 'Файл больше '+cfg.maxMb+' МБ — отправьте поменьше'); fileEl.value=''; return; }
    file = f || null;
    attachBox.hidden = !file;
    if(file){
      document.getElementById('tk-attach-name').textContent = file.name || 'Файл';
      document.getElementById('tk-attach-size').textContent = fmtSize(file.size);
      var th = document.getElementById('tk-attach-thumb');
      if(/^image\//.test(file.type)){ th.innerHTML = '<img src="'+URL.createObjectURL(file)+'" alt="">'; }
      else if(/^video\//.test(file.type)){ th.innerHTML = '<video src="'+URL.createObjectURL(file)+'" muted playsinline></video>'; }
      else { th.innerHTML = FILE_IC; }
    }
    syncButtons();
  }
  if(fileEl) fileEl.addEventListener('change', function(){ setFile(fileEl.files && fileEl.files[0]); });
  document.getElementById('tk-attach-remove').addEventListener('click', function(){ fileEl.value=''; setFile(null); });

  function upload(fd, label){
    busy = true;
    var pid = 'p'+Date.now();
    var empty = scroll.querySelector('.tk-empty'); if(empty) empty.remove();
    scroll.insertAdjacentHTML('beforeend', '<div class="tk-msg me tk-pending" id="'+pid+'"><div class="tk-ava">'+esc(cfg.initial)+'</div>'
      + '<div class="tk-bubble"><div>'+esc(label)+'</div><div class="tk-progress"><div></div></div><div class="tk-time">отправка…</div></div></div>');
    toBottom();
    var el = document.getElementById(pid), bar = el.querySelector('.tk-progress>div');
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/app/tickets/'+cfg.ticketId+'/send');
    xhr.upload.onprogress = function(e){ if(e.lengthComputable) bar.style.width = Math.round(e.loaded*100/e.total)+'%'; };
    xhr.onload = function(){
      busy = false;
      var d = null; try { d = JSON.parse(xhr.responseText); } catch(_){}
      if(xhr.status >= 200 && xhr.status < 300){ el.remove(); poll(); }
      else { el.remove(); toast('error', (d && d.detail) || 'Не удалось отправить, попробуйте ещё раз'); }
    };
    xhr.onerror = function(){ busy = false; el.remove(); toast('error', 'Нет соединения — сообщение не отправлено'); };
    xhr.send(fd);
  }

  function send(){
    if(busy) return;
    var txt = textEl.value.trim();
    if(!txt && !file) return;
    var fd = new FormData();
    fd.append('text', txt);
    var label = txt || '';
    if(file){ fd.append('file', file, file.name || 'file'); label = '📎 ' + (file.name || 'Файл') + (txt ? ' — ' + txt : ''); }
    textEl.value=''; autosize(); fileEl.value=''; setFile(null);
    upload(fd, label);
  }
  sendBtn.addEventListener('click', send);

  // ---------------- голосовые ----------------
  var rec = null, chunks = [], stream = null, t0 = 0, timer = null, cancelled = false;
  function pickMime(){
    var c = ['audio/ogg;codecs=opus','audio/webm;codecs=opus','audio/mp4','audio/webm'];
    for(var i=0;i<c.length;i++){ if(MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(c[i])) return c[i]; }
    return '';
  }
  function stopTracks(){ if(stream){ stream.getTracks().forEach(function(t){ t.stop(); }); stream = null; } }
  function setRecUI(on){
    recBox.hidden = !on; row.hidden = on;
    if(on){
      t0 = Date.now();
      var te = document.getElementById('tk-rec-time');
      te.textContent = '0:00';
      timer = setInterval(function(){
        var s = Math.floor((Date.now()-t0)/1000);
        te.textContent = Math.floor(s/60)+':'+('0'+(s%60)).slice(-2);
        if(s >= 300) finishRec(false); // не больше 5 минут
      }, 250);
    } else { clearInterval(timer); }
  }
  function finishRec(cancel){
    if(!rec) return;
    cancelled = cancel;
    try { rec.stop(); } catch(_){ }
  }
  if(canRecord){
    micBtn.addEventListener('click', function(){
      if(busy || rec) return;
      navigator.mediaDevices.getUserMedia({audio:true}).then(function(s){
        stream = s; chunks = []; cancelled = false;
        var mime = pickMime();
        try { rec = mime ? new MediaRecorder(s, {mimeType: mime}) : new MediaRecorder(s); }
        catch(_){ rec = new MediaRecorder(s); }
        rec.ondataavailable = function(e){ if(e.data && e.data.size) chunks.push(e.data); };
        rec.onstop = function(){
          stopTracks(); setRecUI(false);
          var type = (rec && rec.mimeType) || mime || 'audio/webm';
          rec = null;
          var dur = (Date.now()-t0)/1000;
          if(cancelled || !chunks.length) return;
          if(dur < 0.7){ toast('error', 'Слишком короткое голосовое'); return; }
          var ext = /ogg/.test(type) ? 'ogg' : (/mp4|aac|m4a/.test(type) ? 'm4a' : 'webm');
          var blob = new Blob(chunks, {type: type.split(';')[0]});
          var fd = new FormData();
          fd.append('text', '');
          fd.append('kind', 'voice');
          fd.append('file', blob, 'voice.'+ext);
          upload(fd, '🎤 Голосовое сообщение');
        };
        rec.start(250);
        setRecUI(true);
      }).catch(function(){
        toast('error', 'Нет доступа к микрофону — разрешите его в настройках браузера');
      });
    });
    document.getElementById('tk-rec-cancel').addEventListener('click', function(){ finishRec(true); });
    document.getElementById('tk-rec-send').addEventListener('click', function(){ finishRec(false); });
  }
})();
"""


# --------------------------------------------------------------------------------------------
# JSON сообщений
# --------------------------------------------------------------------------------------------


@router.get("/app/tickets/{ticket_id}/messages")
async def ticket_messages_poll(ticket_id: int, request: Request) -> JSONResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user, _row = auth
        msgs = await _ticket_messages(session, ticket_id, user.id)
    if msgs is None:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse({"messages": [_message_json(m, ticket_id) for m in msgs]})


# --------------------------------------------------------------------------------------------
# Прокси медиа из Telegram (только владельцу тикета)
# --------------------------------------------------------------------------------------------

_MEDIA_CACHE: "OrderedDict[tuple[int, str], tuple[bytes, str]]" = OrderedDict()
_MEDIA_CACHE_MAX_BYTES = 80 * 1024 * 1024
_MEDIA_CACHE_BYTES = 0


def _cache_get(key: tuple[int, str]) -> tuple[bytes, str] | None:
    item = _MEDIA_CACHE.get(key)
    if item is not None:
        _MEDIA_CACHE.move_to_end(key)
    return item


def _cache_put(key: tuple[int, str], data: bytes, mime: str) -> None:
    global _MEDIA_CACHE_BYTES
    if len(data) > _MEDIA_CACHE_MAX_BYTES // 2:
        return
    _MEDIA_CACHE[key] = (data, mime)
    _MEDIA_CACHE_BYTES += len(data)
    while _MEDIA_CACHE_BYTES > _MEDIA_CACHE_MAX_BYTES and _MEDIA_CACHE:
        _k, (d, _m) = _MEDIA_CACHE.popitem(last=False)
        _MEDIA_CACHE_BYTES -= len(d)


_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".m4v": "video/mp4",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".wav": "audio/wav", ".flac": "audio/flac", ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
}


def _mime_for(path: str, fallback: str) -> str:
    return _EXT_MIME.get(Path(path or "").suffix.lower(), fallback)


async def _tg_download(file_id: str) -> tuple[bytes, str]:
    tok = (tickets_config.bot_token or "").strip()
    if not tok:
        raise HTTPException(status_code=503, detail="Бот тикетов не настроен")
    async with Bot(token=tok) as bot:
        try:
            f = await bot.get_file(file_id)
        except TelegramBadRequest as e:
            if "too big" in str(e).lower():
                raise HTTPException(status_code=413, detail="Файл больше 20 МБ — откройте его в Telegram") from e
            raise HTTPException(status_code=404, detail="Файл недоступен") from e
    if not f.file_path:
        raise HTTPException(status_code=404, detail="Файл недоступен")
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(f"https://api.telegram.org/file/bot{tok}/{f.file_path}")
        r.raise_for_status()
    return r.content, f.file_path


async def _ffmpeg(data: bytes, in_ext: str, out_ext: str, args: list[str]) -> bytes | None:
    """Перекодирование через ffmpeg (есть в Docker-образе). None — если не получилось."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"in{in_ext}"
        dst = Path(tmp) / f"out{out_ext}"
        src.write_bytes(data)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(src), *args, str(dst),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        except (FileNotFoundError, asyncio.TimeoutError):
            logger.warning("ffmpeg недоступен или не успел перекодировать %s → %s", in_ext, out_ext)
            return None
        if proc.returncode != 0 or not dst.exists():
            logger.warning("ffmpeg %s → %s failed: %s", in_ext, out_ext, (err or b"")[:300])
            return None
        return dst.read_bytes()


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _ranged(request: Request, data: bytes, mime: str, *, filename: str | None, attachment: bool) -> Response:
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if filename:
        from urllib.parse import quote

        disp = "attachment" if attachment else "inline"
        headers["Content-Disposition"] = f"{disp}; filename*=UTF-8''{quote(filename)}"
    total = len(data)
    m = _RANGE_RE.fullmatch((request.headers.get("range") or "").strip())
    if m and total:
        start_s, end_s = m.groups()
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        else:  # bytes=-N — последние N байт
            start = max(0, total - int(end_s or 0))
            end = total - 1
        end = min(end, total - 1)
        if start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        return Response(content=data[start : end + 1], status_code=206, media_type=mime, headers=headers)
    return Response(content=data, media_type=mime, headers=headers)


@router.get("/app/tickets/{ticket_id}/media/{msg_id}")
async def ticket_media(request: Request, ticket_id: int, msg_id: int, dl: int = 0) -> Response:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user, _row = auth
        if await _ticket_row(session, ticket_id, user.id) is None:
            raise HTTPException(status_code=404, detail="Not found")
        m = (
            await session.execute(
                text(
                    f"SELECT id, {_MEDIA_COLS} FROM ticket_messages "
                    "WHERE id=:mid AND ticket_id=:tid AND COALESCE(is_internal,false)=false"
                ),
                {"mid": msg_id, "tid": ticket_id},
            )
        ).mappings().first()
    if m is None:
        raise HTTPException(status_code=404, detail="Not found")
    m = dict(m)
    info = _media_info(m, ticket_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Not found")
    kind = info["kind"]
    fid = {
        "photo": m.get("photo_file_id"),
        "video": m.get("video_file_id"),
        "video_note": m.get("video_note_file_id"),
        "voice": m.get("voice_file_id"),
        "audio": m.get("audio_file_id"),
        "document": m.get("document_file_id"),
    }[kind]
    filename = info.get("name")

    key = (msg_id, kind)
    cached = _cache_get(key)
    if cached is None:
        data, path = await _tg_download(str(fid))
        fallback = {
            "photo": "image/jpeg",
            "video": "video/mp4",
            "video_note": "video/mp4",
            "voice": "audio/ogg",
            "audio": "audio/mpeg",
        }.get(kind, "application/octet-stream")
        mime = _mime_for(path, fallback)
        if kind == "voice":
            # Голосовые Telegram — OGG/Opus: Safari на iPhone их не играет. Отдаём mp3 (играет везде).
            mp3 = await _ffmpeg(data, Path(path).suffix or ".ogg", ".mp3", ["-vn", "-c:a", "libmp3lame", "-b:a", "64k"])
            if mp3:
                data, mime = mp3, "audio/mpeg"
        cached = (data, mime)
        _cache_put(key, data, mime)
    data, mime = cached
    return _ranged(request, data, mime, filename=filename, attachment=bool(dl))


# --------------------------------------------------------------------------------------------
# Отправка сообщения (текст и/или файл, голосовое)
# --------------------------------------------------------------------------------------------


def _classify_upload(filename: str, ctype: str, kind: str) -> str:
    ctype = (ctype or "").lower()
    ext = Path(filename or "").suffix.lower()
    if kind == "voice":
        return "voice"
    if ctype in ("image/jpeg", "image/png", "image/webp") or ext in (".jpg", ".jpeg", ".png", ".webp"):
        return "photo"
    if ctype.startswith("video/") or ext in (".mp4", ".mov", ".m4v", ".webm"):
        return "video"
    if ctype.startswith("audio/") or ext in (".mp3", ".m4a", ".ogg", ".oga", ".wav", ".flac", ".aac"):
        return "audio"
    return "document"


async def _send_media_to_support(
    *,
    kind: str,
    data: bytes,
    filename: str,
    caption: str,
    topic_id: int,
) -> dict:
    """Отправляет файл в тему тикета в группе поддержки. Возвращает file_id для ticket_messages."""
    chat_id = tickets_config.support_group_id
    thread = {"message_thread_id": topic_id} if topic_id else {}
    cap = caption[:1024] or None
    out: dict = {}
    async with Bot(token=tickets_config.bot_token) as bot:

        async def as_document() -> None:
            sent: Message = await bot.send_document(
                chat_id=chat_id, document=BufferedInputFile(data, filename=filename), caption=cap, **thread
            )
            if sent.document:
                out["document_file_id"] = sent.document.file_id
                out["document_file_name"] = filename

        try:
            if kind == "photo":
                sent = await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(data, filename=filename), caption=cap, **thread)
                if sent.photo:
                    out["photo_file_id"] = sent.photo[-1].file_id
            elif kind == "video":
                sent = await bot.send_video(
                    chat_id=chat_id, video=BufferedInputFile(data, filename=filename), caption=cap, supports_streaming=True, **thread
                )
                if sent.video:
                    out["video_file_id"] = sent.video.file_id
                elif sent.document:  # Telegram иногда сохраняет неподдерживаемое видео как файл
                    out["document_file_id"] = sent.document.file_id
                    out["document_file_name"] = filename
            elif kind == "voice":
                ogg = data
                if not filename.lower().endswith((".ogg", ".oga", ".opus")):
                    ogg = await _ffmpeg(data, Path(filename).suffix or ".webm", ".ogg",
                                        ["-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "48k"]) or b""
                if not ogg:
                    raise HTTPException(status_code=500, detail="Не удалось обработать голосовое — попробуйте ещё раз")
                sent = await bot.send_voice(chat_id=chat_id, voice=BufferedInputFile(ogg, filename="voice.ogg"), caption=cap, **thread)
                if sent.voice:
                    out["voice_file_id"] = sent.voice.file_id
            elif kind == "audio":
                sent = await bot.send_audio(chat_id=chat_id, audio=BufferedInputFile(data, filename=filename), caption=cap, **thread)
                if sent.audio:
                    out["audio_file_id"] = sent.audio.file_id
                    out["audio_file_name"] = filename
                elif sent.voice:
                    out["voice_file_id"] = sent.voice.file_id
            else:
                await as_document()
        except TelegramBadRequest as e:
            # фото с необычными размерами, HEIC, битое видео и т.п. — отправляем как файл
            logger.info("ticket upload %s → document fallback: %s", kind, e)
            if kind == "voice":
                raise HTTPException(status_code=400, detail="Telegram не принял голосовое") from e
            await as_document()
    if not out:
        raise HTTPException(status_code=502, detail="Telegram не вернул файл")
    return out


@router.post("/app/tickets/{ticket_id}/send")
async def ticket_send(
    ticket_id: int,
    request: Request,
    text_value: str = Form(default="", alias="text"),
    kind: str = Form(default=""),
    file: UploadFile | None = File(default=None),
) -> JSONResponse:
    settings = get_settings()
    msg = (text_value or "").strip()[:4000]
    data = b""
    filename = ""
    if file is not None and file.filename:
        max_mb = min(int(tickets_config.media_max_mb), _TG_DOWNLOAD_LIMIT_MB)
        data = await file.read(max_mb * 1024 * 1024 + 1)
        if len(data) > max_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Файл больше {max_mb} МБ")
        filename = (Path(file.filename or "").name or "file")[:120]
        if not data:
            raise HTTPException(status_code=400, detail="Пустой файл")
    if not msg and not data:
        raise HTTPException(status_code=400, detail="Пустое сообщение")
    if data and not (tickets_config.bot_token and tickets_config.support_group_id):
        raise HTTPException(status_code=503, detail="Отправка файлов временно недоступна")

    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user, sess_row = auth
        await touch_session(session, sess_row)
        who = user.first_name or user.username or f"#{user.id}"

        if ticket_id <= 0:
            if not msg:
                raise HTTPException(status_code=400, detail="Опишите проблему текстом")
            new_id = await create_ticket(session, user=user, telegram_user_id=int(user.telegram_id), text_body=msg)
            await session.commit()
            if tickets_config.bot_token and tickets_config.support_group_id:
                try:
                    async with Bot(token=tickets_config.bot_token) as bot:
                        await open_ticket_forum_topic(
                            bot,
                            session,
                            ticket_id=new_id,
                            db_user=user,
                            message_text=msg,
                            display_name=who,
                            telegram_user_id=int(user.telegram_id),
                            username=user.username,
                            settings=settings,
                        )
                        await session.commit()
                except Exception:
                    logger.exception("site ticket: open forum topic failed ticket=%s", new_id)
            return JSONResponse({"ok": True, "ticket_id": new_id})

        t = await _ticket_row(session, ticket_id, user.id)
        if t is None:
            raise HTTPException(status_code=404, detail="Not found")
        if t["status"] == "closed":
            raise HTTPException(status_code=400, detail="Тикет закрыт")
        topic_id = int(t["topic_id"] or 0)

        media: dict = {}
        if data:
            media_kind = _classify_upload(filename, file.content_type if file else "", kind)
            caption = f"💬 {who} (сайт)" + (f": {msg}" if msg else "")
            media = await _send_media_to_support(
                kind=media_kind, data=data, filename=filename, caption=caption, topic_id=topic_id
            )

        await add_ticket_message(
            session,
            ticket_id=ticket_id,
            sender_id=user.id,
            sender_role="user",
            sender_telegram_id=int(user.telegram_id),
            text_body=msg,
            is_internal=False,
            **media,
        )
        await bump_ticket_activity(session, ticket_id=ticket_id)
        await session.commit()

    if not data and topic_id and tickets_config.bot_token:
        try:
            async with Bot(token=tickets_config.bot_token) as bot:
                await bot.send_message(
                    chat_id=tickets_config.support_group_id,
                    message_thread_id=topic_id,
                    text=f"💬 {who}: {msg}"[:4000],
                )
        except Exception:
            logger.exception("site ticket: relay text to topic failed ticket=%s", ticket_id)
    return JSONResponse({"ok": True, "ticket_id": ticket_id})
