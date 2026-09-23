"""Профиль: привязанные аккаунты, безопасность (2FA + сессии), выход."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select

from shared.config import get_settings
from shared.database import get_session_factory
from shared.models.user import User
from shared.services.site_session_service import (
    list_user_sessions,
    load_site_user,
    revoke_all_user_sessions,
    revoke_session_by_id,
    touch_session,
)
from shared.services.site_totp_service import (
    count_unused_backup_codes,
    generate_backup_codes,
    generate_totp_secret,
    totp_qr_data_uri,
    verify_totp_code,
)
from shared.services.email_code_service import email_sending_configured, normalize_email, start_email_code
from shared.services.email_marketing import parse_unsubscribe_token, set_marketing_consent
from api.routers.site_auth import _EMAIL_LINK_PURPOSE, _set_pending_email_cookie

from api.routers.site_theme import app_topbar, avatar_img, esc, fmt_money, icon, page, site_footer, google_logo_svg, tg_logo_svg, ua_label as _ua_label

router = APIRouter()


@router.get("/app/avatar")
async def my_avatar(request: Request) -> Response:
    """Фото профиля Telegram текущего пользователя — те же загрузчик и кэш, что у аватарок в web-admin."""
    import time

    from api.routers.web_admin import (
        _AVATAR_CACHE,
        _AVATAR_TTL_SEC,
        _avatar_fetch_lock,
        _fetch_telegram_public_userpic,
        _load_telegram_profile_photo,
    )

    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
    if auth is None:
        return Response(status_code=401)
    user = auth[0]
    headers = {"Cache-Control": "private, max-age=300", "Vary": "Cookie"}
    hit = _AVATAR_CACHE.get(user.id)
    if hit is not None and time.monotonic() - hit[0] < _AVATAR_TTL_SEC:
        return Response(content=hit[1], media_type=hit[2], headers=headers)
    async with _avatar_fetch_lock(user.id):
        hit = _AVATAR_CACHE.get(user.id)
        if hit is not None and time.monotonic() - hit[0] < _AVATAR_TTL_SEC:
            return Response(content=hit[1], media_type=hit[2], headers=headers)
        loaded = await _load_telegram_profile_photo(user)
        if loaded is None and user.username:
            loaded = await _fetch_telegram_public_userpic(user.username)
        if loaded is None:
            return Response(status_code=404, headers={"Cache-Control": "private, max-age=600"})
        body_b, mime = loaded
        _AVATAR_CACHE[user.id] = (time.monotonic(), body_b, mime)
    return Response(content=body_b, media_type=mime, headers=headers)


@router.get("/app/profile")
async def profile_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        sessions = await list_user_sessions(session, user.id)

    settings = get_settings()
    google_ready = bool((settings.site_google_client_id or "").strip() and (settings.site_google_client_secret or "").strip())
    email_ready = email_sending_configured(settings)

    initial = (user.first_name or user.username or "U")[:1].upper()
    name_display = esc(user.first_name or (f"@{user.username}" if user.username else f"#{user.id}"))
    join_date = user.created_at.strftime("%d %B %Y г.") if user.created_at else "—"
    days_with_us = 0
    if user.created_at is not None:
        created = user.created_at if user.created_at.tzinfo else user.created_at.replace(tzinfo=timezone.utc)
        days_with_us = max(0, (datetime.now(timezone.utc) - created).days)

    totp_on = bool(user.site_totp_enabled)
    backup_left = count_unused_backup_codes(user.site_totp_backup_codes) if totp_on else 0

    sessions_html = "".join(
        f"""
        <div style="display:flex;align-items:center;gap:12px;padding:12px 0;{'border-bottom:1px solid var(--line);' if i < len(sessions) - 1 else ''}">
          {icon('monitor', size=15, color='var(--text-3)')}
          <div style="flex:1;min-width:0;">
            <div style="font:700 13px Manrope;color:var(--text-1);">{esc(_ua_label(s.user_agent))}{' · <span style="color:var(--success);">эта сессия</span>' if s.session_token == sess_row.session_token else ''}</div>
            <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">Активна {esc(s.last_seen_at.strftime('%d.%m.%Y %H:%M') if s.last_seen_at else '')}</div>
          </div>
          {f'''<form method="post" action="/app/profile/sessions/revoke"><input type="hidden" name="session_id" value="{s.id}"/><button type="submit" class="link-btn" style="color:var(--danger-soft);">Завершить</button></form>''' if s.session_token != sess_row.session_token else ''}
        </div>"""
        for i, s in enumerate(sessions)
    ) or '<div style="opacity:.5;font:500 13px Manrope;padding:12px 0;">Нет активных сессий</div>'

    body = f"""
<div class="cabinet-bg" style="min-height:100vh;">
  <div class="shell-wide">
    {app_topbar(active="", balance_rub=fmt_money(user.balance), unread_tickets=0, initial=initial)}

    <div class="card card-accent fade-up" style="margin-top:16px;display:flex;align-items:center;gap:22px;flex-wrap:wrap;">
      <div class="avatar-circle" style="width:96px;height:96px;border-radius:22px;font-size:32px;position:relative;flex-shrink:0;">
        {esc(initial)}{avatar_img()}
        <div style="position:absolute;bottom:2px;right:2px;width:16px;height:16px;border-radius:50%;background:var(--success);border:3px solid var(--card-1);"></div>
      </div>
      <div style="flex:1;min-width:220px;">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;"><span style="font:800 24px Manrope;color:var(--text-1);">{name_display}</span>
          <span class="badge badge-purple">{icon('star', size=11)} Участник</span></div>
        <div style="font:500 13px Manrope;color:var(--text-3);margin-top:6px;">{f'@{esc(user.username)} · ' if user.username else ''}<span class="mono">ID: {user.id}</span> · <span class="mono">TG ID: {user.telegram_id}</span> · с {esc(join_date)}</div>
      </div>
      <div style="display:flex;gap:28px;">
        <div style="text-align:center;"><div class="mono" style="font:800 18px 'JetBrains Mono';color:var(--accent-soft);">{fmt_money(user.balance)} ₽</div><div style="font:600 10px Manrope;color:var(--text-4);margin-top:3px;">БАЛАНС</div></div>
        <div style="text-align:center;"><div class="mono" style="font:800 18px 'JetBrains Mono';color:var(--text-1);">{days_with_us}</div><div style="font:600 10px Manrope;color:var(--text-4);margin-top:3px;">ДНЕЙ С НАМИ</div></div>
      </div>
    </div>

    <div class="grid-auto fade-up d1 cols-2" style="grid-template-columns:1fr 440px;margin-top:20px;">
      <div style="display:flex;flex-direction:column;gap:16px;min-width:0;">
        <div class="card">
          <div class="section-label">{icon('link', size=14)}<span>Привязанные аккаунты</span></div>
          <div style="font:500 12px Manrope;color:var(--text-4);margin-top:4px;">Аккаунт, бот и приложения используют одну базу данных</div>
          <div style="margin-top:14px;display:flex;flex-direction:column;gap:2px;">
            <div style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);">
              <div style="width:34px;height:34px;border-radius:10px;background:linear-gradient(140deg,#2AABEE,#229ED9);display:flex;align-items:center;justify-content:center;">{tg_logo_svg(16,'#fff')}</div>
              <div style="flex:1;">
                <div style="font:700 13px Manrope;color:var(--text-1);">Telegram</div>
                <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">{f'@{esc(user.username)} · ' if user.username else ''}<span class="mono">ID: {user.telegram_id}</span></div>
              </div>
              <span class="badge badge-success">{icon('check-circle', size=12)} Привязан</span>
            </div>
            <div style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);">
              <div style="width:34px;height:34px;border-radius:10px;background:#fff;display:flex;align-items:center;justify-content:center;">{google_logo_svg(17)}</div>
              <div style="flex:1;"><div style="font:700 13px Manrope;color:var(--text-1);">Google</div></div>
              {(
                  '<span class="badge badge-success">' + icon('check-circle', size=12) + ' Привязан</span>'
                  if user.google_id else
                  ('<a href="/app/profile/google/link-start" class="btn btn-outline btn-sm">Привязать</a>' if google_ready else '<span class="badge badge-neutral">Скоро</span>')
              )}
            </div>
            <div style="display:flex;align-items:center;gap:12px;padding:12px 0;">
              <div style="width:34px;height:34px;border-radius:10px;background:var(--card-3);display:flex;align-items:center;justify-content:center;">{icon('devices', size=16, color='var(--text-3)')}</div>
              <div style="flex:1;min-width:0;">
                <div style="font:700 13px Manrope;color:var(--text-1);">Почта для входа по коду</div>
                {f'<div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">{esc(user.email)}</div>' if user.email_verified_at else ''}
              </div>
              {('<span class="badge badge-success">' + icon('check-circle', size=12) + ' Привязана</span>') if user.email_verified_at else ''}
            </div>
            {f'''<form method="post" action="/app/profile/email/link-start" style="display:flex;flex-direction:column;gap:10px;margin-top:10px;">
              <div style="display:flex;gap:8px;">
                <input class="input" type="email" name="email" placeholder="Ваша почта" required style="flex:1;"/>
                <button type="submit" class="btn btn-outline btn-sm" style="white-space:nowrap;">Получить код</button>
              </div>
              <div style="font:500 11px/1.5 Manrope;color:var(--text-5);">После привязки сменить или отвязать почту сможет только администратор.</div>
              <label style="display:flex;align-items:flex-start;gap:9px;cursor:pointer;font:500 12px/1.5 Manrope;color:var(--text-3);">
                <input type="checkbox" name="marketing" value="1" style="margin-top:2px;width:16px;height:16px;accent-color:var(--accent);flex-shrink:0;"/>
                <span>Хочу получать новости, акции и информационную рассылку на почту. Чеки об оплате и уведомления о подписке приходят в любом случае.</span>
              </label>
            </form>''' if (email_ready and not user.email_verified_at) else ''}
            {_marketing_toggle_row(user) if user.email_verified_at else ''}
            {'<div style="font:500 11px Manrope;color:var(--text-5);margin-top:8px;">Отправка писем ещё не настроена</div>' if (not email_ready and not user.email_verified_at) else ''}
          </div>
        </div>

        <div class="card">
          <div class="section-label">{icon('shield-check', size=14)}<span>Безопасность</span></div>
          <div style="margin-top:14px;display:flex;flex-direction:column;gap:2px;">
            <div style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);">
              <div style="width:34px;height:34px;border-radius:10px;background:{'rgba(79,210,160,.12)' if totp_on else 'var(--card-3)'};display:flex;align-items:center;justify-content:center;">{icon('lock', size=16, color='#4FD2A0' if totp_on else 'var(--text-3)')}</div>
              <div style="flex:1;">
                <div style="font:700 13px Manrope;color:var(--text-1);">Двухфакторная аутентификация</div>
                <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">{('Google Authenticator · осталось ' + str(backup_left) + ' резервных кодов') if totp_on else 'Не подключена'}</div>
              </div>
              <a href="/app/profile/2fa/{'disable' if totp_on else 'enable'}" class="toggle{' on' if totp_on else ''}" title="{'Отключить' if totp_on else 'Включить'}"><span class="knob"></span></a>
            </div>
            <div style="display:flex;align-items:center;gap:12px;padding:12px 0;{'border-bottom:1px solid var(--line);' if sessions else ''}">
              <div style="width:34px;height:34px;border-radius:10px;background:var(--card-3);display:flex;align-items:center;justify-content:center;">{icon('monitor', size=16, color='var(--text-3)')}</div>
              <div style="flex:1;"><div style="font:700 13px Manrope;color:var(--text-1);">Активные сессии</div><div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">{len(sessions)} устройств</div></div>
              <form method="post" action="/app/profile/sessions/revoke-all"><button type="submit" class="btn btn-outline btn-sm">Завершить все</button></form>
            </div>
            {sessions_html}
          </div>
        </div>
      </div>

      <div style="display:flex;flex-direction:column;gap:16px;min-width:0;">
        <div class="card card-danger" style="cursor:pointer;" onclick="document.getElementById('logout-form').requestSubmit();">
          <div style="display:flex;align-items:center;gap:14px;">
            <div style="width:38px;height:38px;border-radius:11px;background:rgba(255,107,107,.14);display:flex;align-items:center;justify-content:center;">{icon('logout', size=17, color='#FF8A8A')}</div>
            <div style="flex:1;"><div style="font:700 14px Manrope;color:var(--danger-soft);">Выйти из аккаунта</div><div style="font:500 11px Manrope;color:var(--danger-mut);margin-top:2px;">Завершить текущую сессию на этом устройстве</div></div>
            {icon('chevron-right', size=15, color='var(--danger-mut)')}
          </div>
        </div>
        <form id="logout-form" method="post" action="/logout" hidden></form>
      </div>
    </div>
    {site_footer()}
  </div>
</div>
"""
    return HTMLResponse(page(title="Профиль — Flux Network", body=body))


@router.get("/app/profile/2fa/enable")
async def totp_enable_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        if user.site_totp_enabled:
            return RedirectResponse("/app/profile", status_code=303)
        secret = generate_totp_secret()
        await touch_session(session, sess_row)
        # Временный секрет — фиксируем в подписанной cookie-подобной ссылке, а не в БД, до подтверждения кода.
        ser = URLSafeTimedSerializer(str(get_settings().web_admin_session_secret), salt="flux-site-2fa-setup")
        token = ser.dumps({"user_id": user.id, "secret": secret})
        account_name = f"@{user.username}" if user.username else f"user#{user.id}"
        qr = totp_qr_data_uri(secret=secret, account_name=account_name)

    err = request.query_params.get("err") or ""
    err_html = (
        '<div class="card-danger" style="padding:10px 14px;margin-top:14px;font:600 13px Manrope;color:var(--danger-soft);border-radius:12px;">Неверный код, попробуйте ещё раз.</div>'
        if err
        else ""
    )
    body = f"""
<div class="hero-bg" style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;">
  <div class="fade-up card" style="width:440px;padding:32px;">
    <div style="text-align:center;">
      <div style="font:800 19px Manrope;color:var(--text-1);">Подключить 2FA</div>
      <div style="font:500 13px Manrope;color:var(--text-3);margin-top:6px;">Отсканируйте QR в Google Authenticator (или любом TOTP-приложении)</div>
    </div>
    <div style="display:flex;justify-content:center;margin-top:18px;"><img src="{qr}" width="200" height="200" style="border-radius:12px;"/></div>
    <form method="post" action="/app/profile/2fa/enable" style="margin-top:18px;">
      <input type="hidden" name="setup_token" value="{token}"/>
      <input class="input mono" name="code" placeholder="000000" maxlength="6" autocomplete="off" style="text-align:center;font-size:20px;letter-spacing:.2em;" required/>
      {err_html}
      <button type="submit" class="btn btn-primary btn-block" style="margin-top:16px;">Подтвердить и включить</button>
    </form>
    <a href="/app/profile" class="btn btn-outline btn-block" style="margin-top:10px;">Отмена</a>
  </div>
</div>
"""
    return HTMLResponse(page(title="Включить 2FA — Flux Network", body=body))


@router.post("/app/profile/2fa/enable")
async def totp_enable_submit(request: Request, setup_token: str = Form(""), code: str = Form("")) -> HTMLResponse:
    ser = URLSafeTimedSerializer(str(get_settings().web_admin_session_secret), salt="flux-site-2fa-setup")
    try:
        data = ser.loads(setup_token, max_age=600)
    except (BadSignature, SignatureExpired):
        return RedirectResponse("/app/profile/2fa/enable", status_code=303)
    secret = str(data.get("secret") or "")
    uid = int(data.get("user_id") or 0)
    if not secret or not uid or not verify_totp_code(secret, code):
        return RedirectResponse("/app/profile/2fa/enable?err=1", status_code=303)

    plain_codes, stored = generate_backup_codes()
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None or auth[0].id != uid:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        user.site_totp_secret = secret
        user.site_totp_enabled = True
        user.site_totp_backup_codes = stored
        await touch_session(session, sess_row)
        await session.commit()

    codes_html = "".join(f'<div class="mono" style="padding:8px;background:var(--card-4);border-radius:8px;text-align:center;">{esc(c)}</div>' for c in plain_codes)
    body = f"""
<div class="hero-bg" style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;">
  <div class="fade-up card" style="width:460px;padding:32px;">
    <div style="text-align:center;">{icon('shield-check', size=30, color='var(--success)')}<div style="font:800 19px Manrope;color:var(--text-1);margin-top:10px;">2FA включена</div>
      <div style="font:500 13px Manrope;color:var(--text-3);margin-top:6px;">Сохраните резервные коды — каждый работает один раз, если потеряете доступ к приложению-аутентификатору</div></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:18px;font-size:13px;">{codes_html}</div>
    <a href="/app/profile" class="btn btn-primary btn-block" style="margin-top:20px;">Готово</a>
  </div>
</div>
"""
    return HTMLResponse(page(title="2FA включена — Flux Network", body=body))


@router.get("/app/profile/2fa/disable")
async def totp_disable_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        if not user.site_totp_enabled:
            return RedirectResponse("/app/profile", status_code=303)
        await touch_session(session, sess_row)
    err = request.query_params.get("err") or ""
    err_html = (
        '<div class="card-danger" style="padding:10px 14px;margin-top:14px;font:600 13px Manrope;color:var(--danger-soft);border-radius:12px;">Неверный код.</div>'
        if err
        else ""
    )
    body = f"""
<div class="hero-bg" style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;">
  <div class="fade-up card" style="width:400px;padding:32px;">
    <div style="text-align:center;">
      <div style="font:800 19px Manrope;color:var(--text-1);">Отключить 2FA</div>
      <div style="font:500 13px Manrope;color:var(--text-3);margin-top:6px;">Введите текущий код из приложения-аутентификатора, чтобы подтвердить отключение</div>
    </div>
    <form method="post" action="/app/profile/2fa/disable" style="margin-top:18px;">
      <input class="input mono" name="code" placeholder="000000" maxlength="6" autocomplete="off" style="text-align:center;font-size:20px;letter-spacing:.2em;" required/>
      {err_html}
      <button type="submit" class="btn btn-danger-outline btn-block" style="margin-top:16px;">Отключить</button>
    </form>
    <a href="/app/profile" class="btn btn-outline btn-block" style="margin-top:10px;">Отмена</a>
  </div>
</div>
"""
    return HTMLResponse(page(title="Отключить 2FA — Flux Network", body=body))


@router.post("/app/profile/2fa/disable")
async def totp_disable_submit(request: Request, code: str = Form("")) -> RedirectResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        if not user.site_totp_enabled or not user.site_totp_secret:
            return RedirectResponse("/app/profile", status_code=303)
        if not verify_totp_code(user.site_totp_secret, code):
            return RedirectResponse("/app/profile/2fa/disable?err=1", status_code=303)
        user.site_totp_enabled = False
        user.site_totp_secret = None
        user.site_totp_backup_codes = None
        await touch_session(session, sess_row)
        await session.commit()
    return RedirectResponse("/app/profile?n=2fa_off", status_code=303)


@router.post("/app/profile/sessions/revoke")
async def revoke_one_session(request: Request, session_id: int = Form(...)) -> RedirectResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await revoke_session_by_id(session, user_id=user.id, session_row_id=session_id)
        await touch_session(session, sess_row)
    return RedirectResponse("/app/profile?n=sessions_revoked", status_code=303)


@router.post("/app/profile/sessions/revoke-all")
async def revoke_all_sessions(request: Request) -> RedirectResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await revoke_all_user_sessions(session, user.id, except_token=sess_row.session_token)
        await touch_session(session, sess_row)
    return RedirectResponse("/app/profile?n=sessions_revoked", status_code=303)


def _marketing_toggle_row(user: User) -> str:
    on = bool(user.email_marketing_consent)
    return f'''
            <div style="display:flex;align-items:center;gap:12px;padding:12px 0 0 0;margin-top:10px;border-top:1px solid var(--line);">
              <div style="flex:1;min-width:0;">
                <div style="font:700 13px Manrope;color:var(--text-1);">Новости и акции на почту</div>
                <div style="font:500 11px Manrope;color:var(--text-4);margin-top:2px;">Чеки, продление и напоминания об окончании подписки приходят всегда. Сменить почту — через поддержку.</div>
              </div>
              <form method="post" action="/app/profile/email/marketing">
                <input type="hidden" name="on" value="{'0' if on else '1'}"/>
                <button type="submit" class="toggle{' on' if on else ''}" title="{'Отключить' if on else 'Включить'}" style="cursor:pointer;"><span class="knob"></span></button>
              </form>
            </div>'''


@router.post("/app/profile/email/marketing")
async def email_marketing_toggle(request: Request, on: str = Form("0")) -> RedirectResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        set_marketing_consent(user, on == "1")
        await session.commit()
    return RedirectResponse(f"/app/profile?n={'mkt_on' if on == '1' else 'mkt_off'}", status_code=303)


def _unsubscribe_page(title: str, text: str, *, ok: bool) -> HTMLResponse:
    color = "var(--success)" if ok else "var(--danger-soft)"
    body = f"""
<div class="shell" style="min-height:70vh;display:flex;align-items:center;justify-content:center;padding:40px 16px;">
  <div class="card" style="max-width:460px;width:100%;text-align:center;padding:32px 26px;">
    <div style="font:800 22px Manrope;color:var(--text-1);">{esc(title)}</div>
    <div style="font:500 14px/1.6 Manrope;color:var(--text-3);margin-top:12px;">{esc(text)}</div>
    <div style="height:3px;border-radius:3px;background:{color};width:60px;margin:22px auto 0 auto;"></div>
    <a href="/app/profile" class="btn btn-outline btn-sm" style="margin-top:22px;display:inline-flex;">Настройки профиля</a>
  </div>
</div>
{site_footer()}
"""
    return HTMLResponse(page(title="Отписка от рассылки — Flux Network", body=body))


async def _apply_unsubscribe(token: str) -> bool:
    parsed = parse_unsubscribe_token(token)
    if parsed is None:
        return False
    uid, email = parsed
    factory = get_session_factory()
    async with factory() as session:
        user = await session.get(User, uid)
        if user is None or (user.email or "").lower() != email.lower():
            return False
        set_marketing_consent(user, False)
        await session.commit()
    return True


@router.get("/email/unsubscribe")
async def email_unsubscribe(t: str = "") -> HTMLResponse:
    if not await _apply_unsubscribe(t):
        return _unsubscribe_page("Ссылка недействительна", "Отключить рассылку можно в профиле на сайте или в боте.", ok=False)
    return _unsubscribe_page(
        "Вы отписались от рассылки",
        "Новости и акции больше не будут приходить. Чеки об оплате и уведомления о подписке продолжат приходить на почту.",
        ok=True,
    )


@router.post("/email/unsubscribe")
async def email_unsubscribe_one_click(t: str = "") -> HTMLResponse:
    """RFC 8058 One-Click: почтовые клиенты (Gmail, Яндекс) отправляют POST по кнопке «Отписаться»."""
    await _apply_unsubscribe(t)
    return HTMLResponse("ok")


@router.post("/app/profile/email/link-start")
async def email_link_start(request: Request, email: str = Form(""), marketing: str = Form("")) -> RedirectResponse:
    settings = get_settings()
    norm = normalize_email(email)
    if norm is None:
        return RedirectResponse(f"/app/profile?err={quote_plus('Некорректный адрес почты')}", status_code=303)
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        user, sess_row = auth
        await touch_session(session, sess_row)
        if user.email_verified_at is not None:
            return RedirectResponse(
                f"/app/profile?err={quote_plus('Сменить почту может только администратор — напишите в поддержку')}",
                status_code=303,
            )
        taken = (
            await session.execute(select(User.id).where(User.id != user.id, User.email == norm).limit(1))
        ).scalar_one_or_none()
        if taken is not None:
            return RedirectResponse(f"/app/profile?err={quote_plus('Эта почта уже привязана к другому аккаунту')}", status_code=303)
        uid = user.id
    ok, err = await start_email_code(norm, purpose=_EMAIL_LINK_PURPOSE, settings=settings)
    if not ok:
        return RedirectResponse(f"/app/profile?err={quote_plus(err)}", status_code=303)
    resp = RedirectResponse("/login/email/verify", status_code=303)
    _set_pending_email_cookie(resp, email=norm, mode="link", link_user_id=uid, marketing=marketing == "1")
    return resp
