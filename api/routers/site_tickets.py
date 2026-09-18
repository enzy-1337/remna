"""Тикеты поддержки в личном кабинете сайта: список + чат (текст), реюз tickets/services.py."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.database import get_session_factory
from shared.models.user import User
from shared.services.site_session_service import load_site_user, touch_session
from tickets.config import config as tickets_config
from tickets.services import add_ticket_message, bump_ticket_activity, create_ticket, open_ticket_forum_topic

from api.routers.site_theme import app_topbar, esc, fmt_money, icon, page, site_footer

router = APIRouter()

_STATUS_BADGE = {
    "open": ('<span class="badge badge-warn">Открыт</span>', "Ожидает ответа поддержки"),
    "in_progress": ('<span class="badge badge-success">В работе</span>', "Поддержка ответила"),
    "closed": ('<span class="badge badge-neutral">Закрыт</span>', "Диалог завершён"),
}


async def _user_tickets(session: AsyncSession, user_id: int) -> list[dict]:
    rows = (
        await session.execute(
            text(
                """
                SELECT t.id, t.status, t.created_at, t.updated_at, t.closed_at,
                       (SELECT text FROM ticket_messages m WHERE m.ticket_id = t.id ORDER BY m.id DESC LIMIT 1) AS last_text,
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


async def _ticket_messages(session: AsyncSession, ticket_id: int, user_id: int) -> list[dict] | None:
    owner = (
        await session.execute(text("SELECT user_id FROM tickets WHERE id=:tid"), {"tid": ticket_id})
    ).first()
    if owner is None or int(owner[0]) != int(user_id):
        return None
    rows = (
        await session.execute(
            text(
                "SELECT id, sender_role, text, created_at, photo_file_id, video_file_id, "
                "document_file_id, document_file_name, voice_file_id, video_note_file_id, "
                "audio_file_id, audio_file_name FROM ticket_messages "
                "WHERE ticket_id=:tid AND COALESCE(is_internal,false)=false ORDER BY id ASC"
            ),
            {"tid": ticket_id},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def _media_label(m: dict) -> str | None:
    if m.get("photo_file_id"):
        return "📷 Фото"
    if m.get("video_file_id"):
        return "🎥 Видео"
    if m.get("voice_file_id"):
        return "🎤 Голосовое сообщение"
    if m.get("video_note_file_id"):
        return "⭕ Видеосообщение"
    if m.get("audio_file_id"):
        name = (m.get("audio_file_name") or "").strip()
        return f"🎵 Аудио: {name}" if name else "🎵 Аудио"
    if m.get("document_file_id"):
        name = (m.get("document_file_name") or "").strip()
        return f"📎 Документ: {name}" if name else "📎 Документ"
    return None


def _relative_time(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "только что"
    if secs < 3600:
        return f"{secs // 60} мин. назад"
    if secs < 86400:
        return f"{secs // 3600} ч. назад"
    if secs < 86400 * 30:
        return f"{secs // 86400} дн. назад"
    return dt.strftime("%d.%m.%Y")


@router.get("/app/tickets")
async def tickets_page(request: Request, ticket: int = 0) -> HTMLResponse:
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

    selected_id = ticket or (tickets[0]["id"] if tickets else 0)

    rows_html = ""
    for t in tickets:
        badge, _hint = _STATUS_BADGE.get(t["status"], _STATUS_BADGE["open"])
        active_style = "background:rgba(123,92,255,.1);border-color:rgba(123,92,255,.3);" if t["id"] == selected_id else ""
        preview = (t.get("last_text") or "").strip()[:64] or "Новый тикет"
        rows_html += f"""
        <a href="/app/tickets?ticket={t['id']}" class="card" style="display:block;margin-bottom:10px;padding:14px 16px;{active_style}">
          <div style="display:flex;align-items:center;gap:10px;justify-content:space-between;">
            <span class="mono" style="font:700 12px 'JetBrains Mono';color:var(--text-4);">#{t['id']}</span>
            {badge}
          </div>
          <div style="font:700 13.5px Manrope;color:var(--text-1);margin-top:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{esc(preview)}</div>
          <div style="font:500 11px Manrope;color:var(--text-4);margin-top:4px;">Обновлён {_relative_time(t['updated_at'])} · {t['msg_count']} сообщ.</div>
        </a>"""
    if not tickets:
        rows_html = '<div style="opacity:.5;font:500 13px Manrope;padding:20px 0;text-align:center;">Тикетов пока нет</div>'

    detail_html = _empty_detail_html()
    if selected_id:
        factory2 = get_session_factory()
        async with factory2() as session2:
            msgs = await _ticket_messages(session2, selected_id, user.id)
        if msgs is not None:
            sel = next((t for t in tickets if t["id"] == selected_id), None)
            detail_html = _detail_html(selected_id, sel, msgs, initial)

    body = f"""
<div class="cabinet-bg" style="min-height:100vh;">
  <div class="shell-wide">
    {app_topbar(active="tickets", balance_rub=fmt_money(user.balance), unread_tickets=n_open, initial=initial)}

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

    <div class="grid-auto fade-up d1" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-top:20px;">
      <div class="card" style="text-align:center;"><div style="font:800 22px Manrope;color:var(--warn);">{n_open}</div><div style="font:600 11px Manrope;color:var(--text-4);margin-top:4px;">ОТКРЫТЫХ</div></div>
      <div class="card" style="text-align:center;"><div style="font:800 22px Manrope;color:var(--success);">{n_prog}</div><div style="font:600 11px Manrope;color:var(--text-4);margin-top:4px;">В РАБОТЕ</div></div>
      <div class="card" style="text-align:center;"><div style="font:800 22px Manrope;color:var(--text-2);">{n_closed}</div><div style="font:600 11px Manrope;color:var(--text-4);margin-top:4px;">ЗАКРЫТО</div></div>
    </div>

    <div class="grid-auto fade-up d2 cols-2" style="grid-template-columns:360px 1fr;margin-top:20px;align-items:start;">
      <div>{rows_html}</div>
      <div id="ticket-detail">{detail_html}</div>
    </div>
    {site_footer()}
  </div>
</div>
<div id="new-ticket-modal" class="modal-overlay">
  <div class="fade-in" style="width:100%;max-width:420px;background:var(--card-2);border:1px solid var(--line-2);border-radius:18px;padding:24px;">
    <div style="font:800 17px Manrope;color:var(--text-1);">Новый тикет</div>
    <textarea id="new-ticket-text" class="input" rows="4" placeholder="Опишите проблему…" style="margin-top:14px;resize:vertical;"></textarea>
    <div style="display:flex;gap:10px;margin-top:14px;">
      <button type="button" class="btn btn-outline btn-block" data-close-new-ticket>Отмена</button>
      <button type="button" class="btn btn-primary btn-block" data-submit-new-ticket>Создать</button>
    </div>
  </div>
</div>
<script>
document.querySelectorAll('[data-new-ticket]').forEach(function(b){{b.addEventListener('click',function(){{document.getElementById('new-ticket-modal').classList.add('open');}});}});
document.querySelectorAll('[data-close-new-ticket]').forEach(function(b){{b.addEventListener('click',function(){{document.getElementById('new-ticket-modal').classList.remove('open');}});}});
document.getElementById('new-ticket-modal').addEventListener('click',function(e){{ if (e.target === this) this.classList.remove('open'); }});
document.querySelectorAll('[data-submit-new-ticket]').forEach(function(b){{b.addEventListener('click',function(){{
  var val=document.getElementById('new-ticket-text').value.trim();
  if(!val) return;
  fetch('/app/tickets/0/send',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'text='+encodeURIComponent(val)}})
    .then(function(r){{return r.json();}}).then(function(d){{ if(d.ticket_id) window.location.href='/app/tickets?ticket='+d.ticket_id; }});
}});}});
{_chat_js(initial)}
</script>
"""
    return HTMLResponse(page(title="Тикеты — Flux Network", body=body))


def _empty_detail_html() -> str:
    return f"""
    <div class="card" style="text-align:center;padding:60px 20px;">
      {icon('tickets', size=32, color='var(--text-4)')}
      <div style="font:700 15px Manrope;color:var(--text-3);margin-top:12px;">Выберите тикет или создайте новый</div>
    </div>"""


def _detail_html(ticket_id: int, meta: dict | None, msgs: list[dict], initial: str) -> str:
    badge, _ = _STATUS_BADGE.get((meta or {}).get("status", "open"), _STATUS_BADGE["open"])
    created = (meta or {}).get("created_at")
    created_label = created.strftime("%d.%m.%Y, %H:%M") if created else ""
    msgs_html = "".join(_msg_bubble(m, initial) for m in msgs) or '<div style="opacity:.5;font:500 13px Manrope;padding:20px;text-align:center;">Нет сообщений</div>'
    closed = (meta or {}).get("status") == "closed"
    composer = (
        f"""
        <div class="composer" style="margin-top:14px;" data-ticket-id="{ticket_id}">
          <input type="text" placeholder="Напишите сообщение…" data-chat-input/>
          <button class="icon-btn" style="width:32px;height:32px;background:var(--accent);color:#fff;" data-chat-send>{icon('send-tg', size=15, color='#fff')}</button>
        </div>"""
        if not closed
        else '<div style="text-align:center;font:600 12px Manrope;color:var(--text-4);margin-top:14px;">Тикет закрыт</div>'
    )
    return f"""
    <div class="card" style="display:flex;flex-direction:column;min-height:520px;">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
        <div>
          <div style="display:flex;align-items:center;gap:10px;"><span style="font:800 16px Manrope;color:var(--text-1);">Тикет #{ticket_id}</span>{badge}</div>
          <div style="font:500 12px Manrope;color:var(--text-4);margin-top:4px;">создан {esc(created_label)}</div>
        </div>
      </div>
      <div class="divider" style="margin:14px 0;"></div>
      <div class="chat-scroll" id="chat-scroll" style="flex:1;max-height:420px;">{msgs_html}</div>
      {composer}
    </div>"""


def _msg_bubble(m: dict, initial: str) -> str:
    is_me = m.get("sender_role") == "user"
    ts = m.get("created_at")
    ts_label = ts.strftime("%H:%M") if ts else ""
    who = f'<div class="avatar-circle" style="width:28px;height:28px;font-size:11px;flex-shrink:0;">{esc(initial if is_me else "S")}</div>'
    body = (m.get("text") or "").strip()
    media_label = _media_label(m)
    if media_label:
        bubble_content = f'<span style="opacity:.8;">{esc(media_label)}</span>' + (f'<br>{esc(body)}' if body else "")
    else:
        bubble_content = esc(body) if body else '<span style="opacity:.5;">Пустое сообщение</span>'
    return f"""
    <div class="msg-row{' me' if is_me else ''}">
      {who}
      <div>
        <div class="msg-bubble">{bubble_content}</div>
        <div class="msg-meta" style="text-align:{'right' if is_me else 'left'};">{esc(ts_label)}</div>
      </div>
    </div>"""


def _chat_js(initial: str) -> str:
    return """
(function(){
  var scroll = document.getElementById('chat-scroll');
  var composer = document.querySelector('[data-ticket-id]');
  if(scroll) scroll.scrollTop = scroll.scrollHeight;
  if(!composer) return;
  var tid = composer.getAttribute('data-ticket-id');
  var meInitial = """ + repr(initial) + """;
  var lastSig = '';
  function esc(s){ var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
  function render(msgs){
    var sig = JSON.stringify(msgs.map(function(m){ return [m.id, m.text, m.media_label]; }));
    if (sig === lastSig) return;
    lastSig = sig;
    if (!msgs.length) { scroll.innerHTML = '<div style="opacity:.5;font:500 13px Manrope;padding:20px;text-align:center;">Нет сообщений</div>'; return; }
    scroll.innerHTML = msgs.map(function(m){
      var isMe = m.sender_role === 'user';
      var who = '<div class="avatar-circle" style="width:28px;height:28px;font-size:11px;flex-shrink:0;">' + esc(isMe ? meInitial : 'S') + '</div>';
      var body = (m.text || '').trim();
      var content;
      if (m.media_label) {
        content = '<span style="opacity:.8;">' + esc(m.media_label) + '</span>' + (body ? '<br>' + esc(body) : '');
      } else if (body) {
        content = esc(body);
      } else {
        content = '<span style="opacity:.5;">Пустое сообщение</span>';
      }
      return '<div class="msg-row' + (isMe ? ' me' : '') + '">' + who
        + '<div><div class="msg-bubble">' + content + '</div>'
        + '<div class="msg-meta" style="text-align:' + (isMe ? 'right' : 'left') + ';">' + esc(m.created_at) + '</div></div></div>';
    }).join('');
    scroll.scrollTop = scroll.scrollHeight;
  }
  function load(){
    fetch('/app/tickets/' + tid + '/messages', {credentials:'same-origin'})
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(d){ if (d) render(d.messages || []); })
      .catch(function(){});
  }
  document.addEventListener('click', function(e){
    var btn = e.target.closest('[data-chat-send]');
    if(!btn) return;
    var input = composer.querySelector('[data-chat-input]');
    var val = input.value.trim();
    if(!val) return;
    input.value = '';
    btn.disabled = true;
    fetch('/app/tickets/'+tid+'/send', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'text='+encodeURIComponent(val)})
      .then(function(){ btn.disabled = false; load(); })
      .catch(function(){ btn.disabled = false; });
  });
  document.querySelectorAll('[data-chat-input]').forEach(function(inp){
    inp.addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); var btn=inp.closest('[data-ticket-id]').querySelector('[data-chat-send]'); btn.click(); } });
  });
  setInterval(load, 2500);
})();
"""


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
    return JSONResponse(
        {
            "messages": [
                {
                    "id": m["id"],
                    "sender_role": m["sender_role"],
                    "text": m.get("text"),
                    "created_at": m["created_at"].strftime("%H:%M") if m.get("created_at") else "",
                    "media_label": _media_label(m),
                }
                for m in msgs
            ]
        }
    )


@router.post("/app/tickets/{ticket_id}/send")
async def ticket_send(ticket_id: int, request: Request, text_value: str = Form(default="", alias="text")) -> JSONResponse:
    settings = get_settings()
    msg = (text_value or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Empty message")
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user, sess_row = auth
        await touch_session(session, sess_row)

        is_new = ticket_id <= 0
        if is_new:
            new_id = await create_ticket(session, user=user, telegram_user_id=int(user.telegram_id), text_body=msg)
            await session.commit()
            topic_id = 0
            if tickets_config.bot_token and tickets_config.support_group_id:
                try:
                    async with Bot(token=tickets_config.bot_token) as bot:
                        topic_id = await open_ticket_forum_topic(
                            bot,
                            session,
                            ticket_id=new_id,
                            db_user=user,
                            message_text=msg,
                            display_name=(user.first_name or user.username or f"#{user.id}"),
                            telegram_user_id=int(user.telegram_id),
                            username=user.username,
                            settings=settings,
                        )
                        await session.commit()
                except Exception:
                    pass
            return JSONResponse({"ok": True, "ticket_id": new_id})

        owner = (
            await session.execute(text("SELECT user_id, topic_id, status FROM tickets WHERE id=:tid"), {"tid": ticket_id})
        ).first()
        if owner is None or int(owner[0]) != user.id:
            raise HTTPException(status_code=404, detail="Not found")
        topic_id = int(owner[1] or 0)
        await add_ticket_message(
            session,
            ticket_id=ticket_id,
            sender_id=user.id,
            sender_role="user",
            sender_telegram_id=int(user.telegram_id),
            text_body=msg,
            is_internal=False,
        )
        await bump_ticket_activity(session, ticket_id=ticket_id)
        await session.commit()
        if topic_id and tickets_config.bot_token:
            try:
                async with Bot(token=tickets_config.bot_token) as bot:
                    who = user.first_name or user.username or f"#{user.id}"
                    await bot.send_message(
                        chat_id=tickets_config.support_group_id,
                        message_thread_id=topic_id,
                        text=f"💬 {who}: {msg}"[:4000],
                    )
            except Exception:
                pass
        return JSONResponse({"ok": True, "ticket_id": ticket_id})
