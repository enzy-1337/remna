"""Вход в личный кабинет сайта: Telegram Login Widget + опциональная 2FA. Google/email — заглушки."""

from __future__ import annotations

import base64
import json
import logging
from secrets import token_urlsafe

import httpx
from aiogram.types import User as TgUser
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select

from shared.config import get_settings
from shared.database import get_session_factory
from shared.services.referral_service import set_referrer
from shared.services.site_session_service import (
    clear_session_cookie,
    create_session,
    load_site_user,
    read_session_cookie,
    revoke_session,
    set_session_cookie,
)
from shared.services.site_telegram_login_service import (
    new_login_code,
    pop_login_result,
    save_pending_ref,
    site_bot_deeplink,
)
from shared.services.site_totp_service import verify_totp_code, verify_and_consume_backup_code
from shared.services.telegram_login_verify import verify_telegram_login
from shared.services.user_registration import get_user_by_telegram_id, register_user
from shared.models.user import User

from api.routers.site_landing import REF_COOKIE
from api.routers.site_theme import esc, esc_attr, icon, page, public_topbar, tg_logo_svg, google_logo_svg

logger = logging.getLogger(__name__)
router = APIRouter()

_PENDING_2FA_COOKIE = "flux_pending_2fa"
_PENDING_2FA_MAX_AGE = 60 * 5


def _pending_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(str(get_settings().web_admin_session_secret), salt="flux-site-pending-2fa")


def _set_pending_2fa_cookie(response, user_id: int) -> None:
    token = _pending_serializer().dumps({"user_id": user_id})
    response.set_cookie(
        _PENDING_2FA_COOKIE, token, max_age=_PENDING_2FA_MAX_AGE, httponly=True, samesite="lax", path="/"
    )


def _read_pending_2fa_user_id(request: Request) -> int | None:
    raw = (request.cookies.get(_PENDING_2FA_COOKIE) or "").strip()
    if not raw:
        return None
    try:
        data = _pending_serializer().loads(raw, max_age=_PENDING_2FA_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("user_id") if isinstance(data, dict) else None
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


_TG_BOT_LOGIN_JS = """
(function(){
  var btn = document.getElementById('tg-login-btn');
  var waitBox = document.getElementById('tg-login-wait');
  if (!btn) return;
  var poll = null;
  function stopPoll(){ if (poll) { clearInterval(poll); poll = null; } }
  btn.addEventListener('click', function(){
    if (btn.disabled) return;
    btn.disabled = true;
    fetch('/login/telegram/bot-start', {method:'POST', credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d.ok) { btn.disabled = false; return; }
        window.open(d.bot_url, '_blank', 'noopener');
        if (waitBox) waitBox.hidden = false;
        poll = setInterval(function(){
          fetch('/login/telegram/bot-poll?code=' + encodeURIComponent(d.code), {credentials:'same-origin'})
            .then(function(r){ return r.json(); })
            .then(function(p){
              if (p.status === 'done') { stopPoll(); window.location.href = '/app'; }
              else if (p.status === '2fa') { stopPoll(); window.location.href = '/login/2fa'; }
              else if (p.status === 'error') { stopPoll(); btn.disabled = false; if (waitBox) waitBox.hidden = true; }
            })
            .catch(function(){});
        }, 2000);
      })
      .catch(function(){ btn.disabled = false; });
  });
})();
"""


def _login_page_html(*, error: str = "") -> str:
    settings = get_settings()
    bot_username = (settings.bot_username or "").strip().lstrip("@")
    oidc_ready = bool(
        (settings.web_admin_telegram_client_id or "").strip()
        and (settings.web_admin_telegram_client_secret or "").strip()
    )
    err_html = (
        f'<div class="card-danger" style="padding:12px 14px;margin-top:16px;font:600 13px Manrope;color:var(--danger-soft);border-radius:12px;">{esc(error)}</div>'
        if error
        else ""
    )
    # Основной способ — тот же Telegram OAuth/OIDC (oauth.tg.dev), что уже настроен и работает
    # для входа в web-admin (WEB_ADMIN_TELEGRAM_CLIENT_ID/SECRET, тот же бот). Обычный top-level
    # редирект, без JS и без сломанного у Telegram Login Widget (oauth.telegram.org/auth сейчас
    # отвечает "deprecated" на любой запрос — проверено).
    primary_btn = (
        f'<a href="/login/telegram/oauth-start" class="btn btn-tg btn-block">{tg_logo_svg(20, "#fff")}<span>Войти через Telegram</span></a>'
        if oidc_ready
        else '<div style="font:600 13px Manrope;color:var(--danger-soft);">Telegram OAuth не настроен (WEB_ADMIN_TELEGRAM_CLIENT_ID/SECRET)</div>'
    )
    # Альтернатива — вход через бота (диплинк + код), на случай проблем с OAuth-редиректом.
    alt_btn = (
        f"""<button type="button" id="tg-login-btn" class="btn btn-outline btn-block">{icon('send-tg', size=16)}<span>Не получилось? Войти через бота</span></button>
      <div id="tg-login-wait" hidden style="text-align:center;font:600 12px Manrope;color:var(--text-3);margin-top:-4px;">Открыли бота? Нажмите Start и подождите — страница обновится сама…</div>
      <script>{_TG_BOT_LOGIN_JS}</script>"""
        if bot_username
        else ""
    )
    widget = f'{primary_btn}\n{alt_btn}'
    body = f"""
<div class="hero-bg" style="min-height:100vh;">
  <div class="shell" style="padding-top:20px;">
    {public_topbar()}
  </div>
  <div style="display:flex;align-items:center;justify-content:center;padding:40px 16px 80px;">
    <div class="fade-up" style="width:100%;max-width:960px;background:var(--card-2);border:1px solid var(--line-2);border-radius:22px;overflow:hidden;display:flex;box-shadow:0 50px 120px -30px rgba(0,0,0,.9);flex-wrap:wrap;">
      <div style="flex:1;min-width:280px;padding:36px 34px;background:linear-gradient(160deg,rgba(123,92,255,.1),transparent 60%);border-right:1px solid var(--line-2);">
        <div style="display:flex;align-items:center;gap:10px;">
          <div style="width:30px;height:30px;border-radius:9px;background:linear-gradient(140deg,var(--accent),var(--accent-2));display:flex;align-items:center;justify-content:center;">{icon('shield', size=17, color='#fff', stroke=2.3)}</div>
          <span style="font:800 17px Manrope;color:var(--text-1);">Flux Network</span>
        </div>
        <div style="font:800 27px Manrope;color:var(--text-1);margin-top:30px;line-height:1.2;">Безопасный доступ<br>к интернету</div>
        <div style="font:500 13px Manrope;color:var(--text-3);margin-top:12px;line-height:1.55;">Одна подписка на все ваши устройства — без логов и слежки</div>
        <div style="display:flex;flex-direction:column;gap:9px;margin-top:26px;">
          {"".join(f'''<div style="display:flex;align-items:center;gap:11px;background:var(--card-3);border:1px solid var(--line);border-radius:12px;padding:13px 14px;">{icon(ic, size=17, color="#7B5CFF")}<span style="font:600 13px Manrope;color:var(--text-2);">{esc(t)}</span></div>''' for ic, t in [("bolt","Быстрое подключение"),("lock","Шифрование и отсутствие логов"),("devices","iOS, Android, Windows, macOS, Linux"),("wallet","Карта, СБП или криптовалюта")])}
        </div>
      </div>
      <div style="flex:1.1;min-width:300px;padding:36px 40px;">
        <div style="text-align:center;">
          <div style="font:800 22px Manrope;color:var(--text-1);">Вход в личный кабинет</div>
          <div style="font:500 13px Manrope;color:var(--text-3);margin-top:6px;">Выберите удобный способ — аккаунт один для сайта, бота и приложений</div>
        </div>
        {err_html}
        <div style="display:flex;flex-direction:column;gap:11px;margin-top:24px;align-items:center;">
          <div style="width:100%;display:flex;flex-direction:column;gap:8px;">{widget}</div>
          <button type="button" class="btn btn-google btn-block" disabled title="Скоро">{google_logo_svg(19)}<span>Продолжить через Google</span><span class="badge badge-neutral" style="margin-left:6px;">скоро</span></button>
          <button type="button" class="btn btn-outline btn-block" disabled title="Скоро">{icon('devices', size=18)}<span>Войти по почте — код на e-mail</span><span class="badge badge-neutral" style="margin-left:6px;">скоро</span></button>
        </div>
        <div style="text-align:center;font:500 11.5px Manrope;color:var(--text-5);margin-top:22px;line-height:1.6;">
          Продолжая, вы соглашаетесь с <a href="/legal/offer">офертой</a> и <a href="/legal/privacy">политикой конфиденциальности</a>
        </div>
      </div>
    </div>
  </div>
</div>
"""
    return page(title="Вход — Flux Network", body=body)


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        auth = await load_site_user(session, request)
    if auth is not None:
        return RedirectResponse("/app", status_code=303)
    err = request.query_params.get("err") or ""
    err_map = {
        "widget": "Не удалось подтвердить вход через Telegram. Попробуйте ещё раз.",
        "expired": "Ссылка входа устарела, попробуйте снова.",
        "oauth": "Не удалось подтвердить вход через Telegram. Попробуйте ещё раз или войдите через бота.",
        "oauth_config": "Вход через Telegram временно недоступен. Попробуйте через бота.",
    }
    return HTMLResponse(_login_page_html(error=err_map.get(err, "")))


async def _finish_login(request: Request, user: User, *, login_kind: str) -> RedirectResponse:
    factory = get_session_factory()
    async with factory() as session:
        row = await create_session(session, user=user, request=request, login_kind=login_kind)
    resp = RedirectResponse("/app", status_code=303)
    set_session_cookie(resp, row.session_token)
    resp.delete_cookie(_PENDING_2FA_COOKIE, path="/")
    resp.delete_cookie(REF_COOKIE, path="/")
    return resp


def _jwt_payload_unverified(token: str) -> dict:
    """Payload id_token без проверки подписи — это нормально: id_token получен напрямую от Telegram
    HTTPS-запросом с нашим client_secret, доверие уже установлено на уровне обмена кода на токен
    (тот же подход, что в api/routers/web_admin.py для входа в веб-админку)."""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    padding = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode((payload_b64 + padding).encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


async def _apply_pending_referrer(session, user: User, ref_code: str) -> None:
    if not ref_code or user.referred_by is not None:
        return
    r = await session.execute(select(User).where(User.referral_code == ref_code.upper()))
    ref_user = r.scalar_one_or_none()
    if ref_user is not None and ref_user.id != user.id:
        try:
            await set_referrer(session, user=user, new_referrer=ref_user)
            await session.commit()
        except ValueError:
            pass


async def _resolve_or_register_by_tid(
    session, *, tid: int, label: str, username: str, ref_code: str
) -> User:
    existing = await get_user_by_telegram_id(session, tid)
    if existing is not None:
        await _apply_pending_referrer(session, existing, ref_code)
        return existing
    tg_user = TgUser(
        id=tid,
        is_bot=False,
        first_name=label or username or "Пользователь",
        last_name=None,
        username=username or None,
    )
    start_args = f"ref_{ref_code}" if ref_code else None
    user, _created, _bonus = await register_user(session, tg_user, start_args)
    return user


@router.get("/login/telegram/oauth-start")
async def telegram_oauth_start(request: Request) -> RedirectResponse:
    """Тот же Telegram OAuth/OIDC (oauth.tg.dev), что уже работает для входа в web-admin —
    просто без ограничения по списку администраторов."""
    settings = get_settings()
    client_id = (settings.web_admin_telegram_client_id or "").strip()
    if not client_id:
        return RedirectResponse("/login?err=oauth_config", status_code=303)
    state = base64.urlsafe_b64encode(token_urlsafe(24).encode("utf-8")).decode("ascii")[:40]
    request.session["site_tg_oauth_state"] = state
    base = (settings.public_site_url or "").strip().rstrip("/")
    redirect_uri = (settings.site_telegram_redirect_uri or "").strip() or f"{base}/login/telegram/oauth-callback"
    from urllib.parse import quote_plus

    url = (
        "https://oauth.tg.dev/auth"
        f"?client_id={quote_plus(client_id)}"
        f"&redirect_uri={quote_plus(redirect_uri)}"
        "&response_type=code"
        "&scope=openid%20profile"
        f"&state={quote_plus(state)}"
    )
    return RedirectResponse(url, status_code=303)


@router.get("/login/telegram/oauth-callback")
async def telegram_oauth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    settings = get_settings()
    if not code.strip() or state != request.session.get("site_tg_oauth_state"):
        return RedirectResponse("/login?err=oauth", status_code=303)
    request.session.pop("site_tg_oauth_state", None)

    client_id = (settings.web_admin_telegram_client_id or "").strip()
    client_secret = (settings.web_admin_telegram_client_secret or "").strip()
    base = (settings.public_site_url or "").strip().rstrip("/")
    redirect_uri = (settings.site_telegram_redirect_uri or "").strip() or f"{base}/login/telegram/oauth-callback"
    if not client_id or not client_secret or not redirect_uri:
        return RedirectResponse("/login?err=oauth_config", status_code=303)

    t_data: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for token_url in ("https://oauth.tg.dev/token", "https://oauth.telegram.org/token"):
                try:
                    t_resp = await client.post(
                        token_url,
                        headers={"Accept": "application/json"},
                        data={
                            "grant_type": "authorization_code",
                            "code": code.strip(),
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "redirect_uri": redirect_uri,
                        },
                    )
                    t_resp.raise_for_status()
                    payload = t_resp.json()
                    if isinstance(payload, dict):
                        t_data = payload
                        break
                except Exception:
                    continue
    except Exception:
        t_data = None
    if t_data is None:
        return RedirectResponse("/login?err=oauth", status_code=303)

    tid = 0
    tg_username = ""
    tg_label = ""
    id_token = str(t_data.get("id_token") or "").strip()
    if id_token:
        claims = _jwt_payload_unverified(id_token)
        try:
            tid = int(claims.get("id") or claims.get("sub") or 0)
        except (TypeError, ValueError):
            tid = 0
        tg_username = str(claims.get("preferred_username") or "").strip()
        tg_label = str(claims.get("name") or "").strip()
    if not tid:
        uobj = t_data.get("user")
        src = uobj if isinstance(uobj, dict) else t_data
        try:
            tid = int(src.get("id") or 0)
        except (TypeError, ValueError):
            tid = 0
        tg_username = tg_username or str(src.get("username") or "").strip()
        tg_label = tg_label or str(src.get("first_name") or "").strip()
    if not tid:
        return RedirectResponse("/login?err=oauth", status_code=303)

    ref_code = (request.cookies.get(REF_COOKIE) or "").strip()
    factory = get_session_factory()
    async with factory() as session:
        user = await _resolve_or_register_by_tid(
            session, tid=tid, label=tg_label, username=tg_username, ref_code=ref_code
        )
        if bool(user.site_totp_enabled):
            resp = RedirectResponse("/login/2fa", status_code=303)
            _set_pending_2fa_cookie(resp, user.id)
            return resp
        user_for_session = user
    return await _finish_login(request, user_for_session, login_kind="telegram_oauth")


@router.get("/login/telegram/callback")
async def telegram_login_callback(request: Request) -> RedirectResponse:
    settings = get_settings()
    payload = {k: v for k, v in request.query_params.items()}
    if not payload.get("id") or not verify_telegram_login(payload, settings.bot_token):
        return RedirectResponse("/login?err=widget", status_code=303)
    try:
        tg_id = int(payload["id"])
    except (TypeError, ValueError):
        return RedirectResponse("/login?err=widget", status_code=303)

    ref_code = (request.cookies.get(REF_COOKIE) or "").strip()
    factory = get_session_factory()
    async with factory() as session:
        existing = await get_user_by_telegram_id(session, tg_id)
        if existing is not None:
            user = existing
            if ref_code and user.referred_by is None:
                r = await session.execute(
                    select(User).where(User.referral_code == ref_code.upper())
                )
                ref_user = r.scalar_one_or_none()
                if ref_user is not None and ref_user.id != user.id:
                    try:
                        await set_referrer(session, user=user, new_referrer=ref_user)
                        await session.commit()
                    except ValueError:
                        pass
        else:
            tg_user = TgUser(
                id=tg_id,
                is_bot=False,
                first_name=(payload.get("first_name") or "").strip() or "Пользователь",
                last_name=(payload.get("last_name") or "").strip() or None,
                username=(payload.get("username") or "").strip() or None,
            )
            start_args = f"ref_{ref_code}" if ref_code else None
            user, _created, _bonus = await register_user(session, tg_user, start_args)

        if bool(user.site_totp_enabled):
            resp = RedirectResponse("/login/2fa", status_code=303)
            _set_pending_2fa_cookie(resp, user.id)
            return resp

    return await _finish_login(request, user, login_kind="telegram")


@router.post("/login/telegram/bot-start")
async def telegram_bot_login_start(request: Request) -> JSONResponse:
    """Выдаёт одноразовый код + диплинк на бота — фронт открывает бот в новой вкладке и поллит
    /login/telegram/bot-poll, пока пользователь не нажмёт Start."""
    settings = get_settings()
    code = new_login_code()
    bot_url = site_bot_deeplink(code, settings)
    if not bot_url:
        return JSONResponse({"ok": False, "error": "bot_unconfigured"}, status_code=503)
    ref_code = (request.cookies.get(REF_COOKIE) or "").strip()
    if ref_code:
        await save_pending_ref(code, ref_code, settings=settings)
    return JSONResponse({"ok": True, "code": code, "bot_url": bot_url})


@router.get("/login/telegram/bot-poll")
async def telegram_bot_login_poll(request: Request, code: str = "") -> JSONResponse:
    settings = get_settings()
    tg_id = await pop_login_result(code, settings=settings)
    if tg_id is None:
        return JSONResponse({"status": "pending"})
    factory = get_session_factory()
    async with factory() as session:
        user = await get_user_by_telegram_id(session, tg_id)
        if user is None:
            return JSONResponse({"status": "error"})
        if bool(user.site_totp_enabled):
            resp = JSONResponse({"status": "2fa"})
            _set_pending_2fa_cookie(resp, user.id)
            return resp
        row = await create_session(session, user=user, request=request, login_kind="telegram_bot")
    resp = JSONResponse({"status": "done"})
    set_session_cookie(resp, row.session_token)
    resp.delete_cookie(REF_COOKIE, path="/")
    return resp


def _otp_boxes_html() -> str:
    boxes = "".join(f'<input class="otp-box" inputmode="numeric" maxlength="1" data-otp-idx="{i}"/>' for i in range(6))
    return f'<div class="otp-row" data-otp-group>{boxes}</div><input type="hidden" name="code" data-otp-hidden/>'


_OTP_JS = """
document.querySelectorAll('[data-otp-group]').forEach(function(group){
  var boxes = Array.prototype.slice.call(group.querySelectorAll('.otp-box'));
  var hidden = group.parentElement.querySelector('[data-otp-hidden]');
  function sync(){ hidden.value = boxes.map(function(b){ return b.value; }).join(''); }
  boxes.forEach(function(box, i){
    box.addEventListener('input', function(){
      box.value = box.value.replace(/[^0-9]/g,'').slice(0,1);
      box.classList.toggle('filled', !!box.value);
      if(box.value && boxes[i+1]) boxes[i+1].focus();
      sync();
    });
    box.addEventListener('keydown', function(e){
      if(e.key === 'Backspace' && !box.value && boxes[i-1]) boxes[i-1].focus();
    });
    box.addEventListener('paste', function(e){
      var text = (e.clipboardData || window.clipboardData).getData('text').replace(/[^0-9]/g,'');
      if(!text) return;
      e.preventDefault();
      for(var j=0;j<boxes.length;j++){ boxes[j].value = text[j] || ''; boxes[j].classList.toggle('filled', !!boxes[j].value); }
      sync();
      boxes[Math.min(text.length, boxes.length-1)].focus();
    });
  });
  if(boxes[0]) boxes[0].focus();
});
"""


@router.get("/login/2fa")
async def login_2fa_page(request: Request, backup: str = "") -> HTMLResponse:
    uid = _read_pending_2fa_user_id(request)
    if uid is None:
        return RedirectResponse("/login?err=expired", status_code=303)
    err = request.query_params.get("err") or ""
    use_backup = backup == "1"
    if use_backup:
        field_html = """
        <input class="input mono" name="code" placeholder="XXXX-XXXX" autocomplete="off" style="text-align:center;font-size:18px;letter-spacing:.1em;" autofocus />
        """
        toggle_link = '<a class="link-btn" href="/login/2fa">Ввести код из приложения</a>'
        title = "Резервный код"
        subtitle = "Введите один из сохранённых резервных кодов"
    else:
        field_html = _otp_boxes_html()
        toggle_link = '<a class="link-btn" href="/login/2fa?backup=1">Использовать резервный код</a>'
        title = "Двухфакторная аутентификация"
        subtitle = "Код из Google Authenticator — обновляется каждые 30 секунд"
    err_html = (
        f'<div class="card-danger" style="padding:10px 14px;margin-top:14px;font:600 13px Manrope;color:var(--danger-soft);border-radius:12px;">Неверный код, попробуйте ещё раз.</div>'
        if err
        else ""
    )
    body = f"""
<div class="hero-bg" style="min-height:100vh;display:flex;align-items:center;justify-content:center;">
  <div class="fade-up" style="width:440px;background:var(--card-2);border:1px solid var(--line-2);border-radius:20px;padding:32px;">
    <div style="width:48px;height:48px;border-radius:14px;background:rgba(79,210,160,.12);display:flex;align-items:center;justify-content:center;margin:0 auto;">
      {icon('lock', size=24, color='#4FD2A0')}
    </div>
    <div style="text-align:center;margin-top:16px;">
      <div style="font:800 20px Manrope;color:var(--text-1);">{esc(title)}</div>
      <div style="font:500 13px Manrope;color:var(--text-3);margin-top:7px;">{esc(subtitle)}</div>
    </div>
    <form method="post" action="/login/2fa" style="margin-top:26px;">
      <input type="hidden" name="backup" value="{"1" if use_backup else "0"}"/>
      {field_html}
      {err_html}
      <button type="submit" class="btn btn-primary btn-block" style="margin-top:22px;">{"Войти" if use_backup else "Подтвердить"}</button>
    </form>
    <div style="text-align:center;margin-top:16px;">{toggle_link}</div>
  </div>
</div>
<script>{_OTP_JS}</script>
"""
    return HTMLResponse(page(title="Двухфакторная аутентификация — Flux Network", body=body))


@router.post("/login/2fa")
async def login_2fa_submit(request: Request, code: str = Form(""), backup: str = Form("0")) -> RedirectResponse:
    uid = _read_pending_2fa_user_id(request)
    if uid is None:
        return RedirectResponse("/login?err=expired", status_code=303)
    factory = get_session_factory()
    async with factory() as session:
        user = await session.get(User, uid)
        if user is None or not user.site_totp_enabled or not user.site_totp_secret:
            return RedirectResponse("/login?err=expired", status_code=303)
        ok = False
        if backup == "1":
            ok, updated = verify_and_consume_backup_code(user.site_totp_backup_codes, code)
            if ok:
                user.site_totp_backup_codes = updated
                await session.commit()
        else:
            ok = verify_totp_code(user.site_totp_secret, code)
        if not ok:
            suffix = "&backup=1" if backup == "1" else ""
            return RedirectResponse(f"/login/2fa?err=1{suffix}", status_code=303)
        user_for_session = user
    return await _finish_login(request, user_for_session, login_kind="telegram+2fa")


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    token = read_session_cookie(request)
    if token:
        factory = get_session_factory()
        async with factory() as session:
            await revoke_session(session, token=token)
    resp = RedirectResponse("/", status_code=303)
    clear_session_cookie(resp)
    return resp
