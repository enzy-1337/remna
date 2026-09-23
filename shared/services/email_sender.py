"""Отправка писем (SMTP в приоритете, Resend — запасной) и фирменный HTML-шаблон Flux Network.

Шаблон — табличная вёрстка с inline-стилями: так письмо одинаково выглядит в Gmail, Яндекс, Mail.ru,
Outlook и почтовых приложениях на телефоне. Цвета — те же, что у сайта (site_theme.py).
"""

from __future__ import annotations

import asyncio
import html
import logging
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from typing import Any, Callable, Coroutine

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

BRAND_NAME = "Flux Network"

# Палитра сайта (api/routers/site_theme.py)
_BG = "#0A0D11"
_CARD = "#111820"
_CARD_2 = "#0F141A"
_LINE = "#1E2630"
_ACCENT = "#7B5CFF"
_ACCENT_2 = "#5436C9"
_ACCENT_SOFTER = "#C8B6FF"
_TEXT_1 = "#EAF0F4"
_TEXT_2 = "#C8D2DA"
_TEXT_3 = "#8A96A3"
_SUCCESS = "#4FD2A0"
_WARN = "#F5B544"
_DANGER = "#FF6B6B"
_FONT = "'Manrope',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif"

TONE_COLORS = {"accent": _ACCENT, "success": _SUCCESS, "warn": _WARN, "danger": _DANGER}


def smtp_configured(s: Settings) -> bool:
    return bool((s.smtp_host or "").strip() and (s.smtp_user or "").strip() and (s.smtp_password or "").strip())


def resend_configured(s: Settings) -> bool:
    return bool((s.resend_api_key or "").strip() and (s.resend_from_email or "").strip())


def email_sending_configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return smtp_configured(s) or resend_configured(s)


def email_logo_url(settings: Settings) -> str:
    """Логотип для писем — тот же, что в шапке web-admin (ADMIN_PANEL_LOGO_URL).

    Почтовые клиенты грузят картинки только по абсолютному https-адресу, поэтому относительный путь
    (например /assets/logo/c_logo.png) дополняем доменом админки/сайта.
    """
    raw = (settings.admin_panel_logo_url or "").strip()
    if not raw:
        return ""
    if raw.startswith(("https://", "http://")):
        return raw
    if raw.startswith("//"):
        return "https:" + raw
    base = (settings.admin_site_url or settings.public_site_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/{raw.lstrip('/')}"


def site_url(settings: Settings, path: str = "") -> str:
    """Адрес сайта для писем: EMAIL_SITE_URL, иначе PUBLIC_SITE_URL."""
    base = (settings.email_site_url or settings.public_site_url or "").strip().rstrip("/")
    return f"{base}{path}" if base else ""


@dataclass
class EmailRow:
    label: str
    value: str


def render_email(
    *,
    title: str,
    intro: str,
    rows: list[EmailRow] | None = None,
    button_text: str | None = None,
    button_url: str | None = None,
    note: str | None = None,
    tone: str = "accent",
    badge: str | None = None,
    big_code: str | None = None,
    unsubscribe_url: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Письмо в стиле сайта. Все строки экранируются — передавайте обычный текст (переносы строк сохраняются)."""
    s = settings or get_settings()
    e = html.escape
    tone_color = TONE_COLORS.get(tone, _ACCENT)
    home = site_url(s) or "#"
    support = (s.email_support_bot_username or s.support_username or "").strip().lstrip("@")
    logo = email_logo_url(s)
    if logo:
        logo_cell = (
            f'<td width="30" height="30" style="width:30px;height:30px;">'
            f'<img src="{e(logo, quote=True)}" width="30" height="30" alt="{BRAND_NAME}" '
            f'style="display:block;width:30px;height:30px;border-radius:9px;border:0;outline:none;"></td>'
        )
    else:
        logo_cell = (
            f'<td width="30" height="30" bgcolor="{_ACCENT}" style="width:30px;height:30px;border-radius:9px;background:{_ACCENT};'
            f'background-image:linear-gradient(140deg,{_ACCENT},{_ACCENT_2});color:#fff;font:800 15px {_FONT};text-align:center;">F</td>'
        )

    badge_html = ""
    if badge:
        badge_html = (
            f'<tr><td style="padding:0 0 14px 0;">'
            f'<span style="display:inline-block;padding:5px 12px;border-radius:999px;background:{tone_color}22;'
            f'border:1px solid {tone_color}55;color:{tone_color};font:800 11px {_FONT};letter-spacing:.08em;'
            f'text-transform:uppercase;">{e(badge)}</span></td></tr>'
        )

    code_html = ""
    if big_code:
        code_html = (
            f'<tr><td style="padding:6px 0 18px 0;">'
            f'<div style="background:{_CARD_2};border:1px solid {_LINE};border-radius:14px;padding:18px;text-align:center;'
            f'font:800 34px \'JetBrains Mono\',Consolas,monospace;letter-spacing:.3em;color:{_ACCENT_SOFTER};">'
            f"{e(big_code)}</div></td></tr>"
        )

    rows_html = ""
    if rows:
        cells = "".join(
            f'<tr><td style="padding:11px 16px;border-bottom:1px solid {_LINE};color:{_TEXT_3};font:500 13px {_FONT};">'
            f"{e(r.label)}</td>"
            f'<td align="right" style="padding:11px 16px;border-bottom:1px solid {_LINE};color:{_TEXT_1};'
            f'font:700 13px {_FONT};">{e(r.value)}</td></tr>'
            for r in rows
        )
        rows_html = (
            f'<tr><td style="padding:4px 0 22px 0;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{_CARD_2};border:1px solid {_LINE};border-radius:14px;border-collapse:separate;overflow:hidden;">'
            f"{cells}</table></td></tr>"
        )

    button_html = ""
    if button_text and button_url:
        button_html = (
            f'<tr><td style="padding:2px 0 22px 0;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
            f'<td bgcolor="{_ACCENT}" style="border-radius:999px;background:{_ACCENT};'
            f'background-image:linear-gradient(140deg,{_ACCENT},{_ACCENT_2});">'
            f'<a href="{e(button_url, quote=True)}" target="_blank" '
            f'style="display:inline-block;padding:13px 28px;font:800 14px {_FONT};color:#ffffff;text-decoration:none;'
            f'border-radius:999px;">{e(button_text)}</a></td></tr></table></td></tr>'
        )

    note_html = ""
    if note:
        note_html = (
            f'<tr><td style="padding:0;color:{_TEXT_3};font:500 12px/1.6 {_FONT};">{e(note)}</td></tr>'
        )

    unsub_html = ""
    if unsubscribe_url:
        unsub_html = (
            f'<br>Не хотите получать новости и акции? <a href="{e(unsubscribe_url, quote=True)}" '
            f'style="color:{_TEXT_3};text-decoration:underline;">Отписаться от рассылки</a>. '
            f"Чеки и уведомления о подписке продолжат приходить."
        )

    support_html = ""
    if support:
        support_html = (
            f'<br>Поддержка — Telegram-бот <a href="https://t.me/{e(support, quote=True)}" '
            f'style="color:{_ACCENT_SOFTER};text-decoration:underline;">@{e(support)}</a>'
        )

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark">
<title>{e(title)}</title></head>
<body style="margin:0;padding:0;background:{_BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{e(" ".join(intro.split())[:140])}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{_BG}" style="background:{_BG};">
<tr><td align="center" style="padding:32px 14px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">
    <tr><td style="padding:0 4px 18px 4px;">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        {logo_cell}
        <td style="padding-left:10px;font:800 16px {_FONT};color:{_TEXT_1};">
          <a href="{e(home, quote=True)}" style="color:{_TEXT_1};text-decoration:none;">Flux <span style="color:{_ACCENT};">Network</span></a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td bgcolor="{_CARD}" style="background:{_CARD};border:1px solid {_LINE};border-radius:20px;overflow:hidden;">
      <div style="height:4px;background:{tone_color};background-image:linear-gradient(90deg,{tone_color},{_ACCENT_SOFTER});"></div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:28px 28px 26px 28px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {badge_html}
          <tr><td style="padding:0 0 10px 0;font:800 22px/1.3 {_FONT};color:{_TEXT_1};">{e(title)}</td></tr>
          <tr><td style="padding:0 0 20px 0;font:500 15px/1.6 {_FONT};color:{_TEXT_2};">{_multiline(intro)}</td></tr>
          {code_html}
          {rows_html}
          {button_html}
          {note_html}
        </table>
      </td></tr></table>
    </td></tr>
    <tr><td align="center" style="padding:18px 10px 0 10px;font:500 11px/1.6 {_FONT};color:{_TEXT_3};">
      Вы получили это письмо, потому что почта привязана к аккаунту {BRAND_NAME}.<br>
      <a href="{e(home, quote=True)}" style="color:{_TEXT_3};text-decoration:underline;">{e(home.replace('https://', '')) if home != '#' else BRAND_NAME}</a>{support_html}{unsub_html}
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def _multiline(text: str) -> str:
    return "<br>".join(html.escape(line) for line in (text or "").replace("\r\n", "\n").split("\n"))


def _plain_from_parts(title: str, intro: str, rows: list[EmailRow] | None, button_url: str | None) -> str:
    lines = [title, "", intro]
    if rows:
        lines.append("")
        lines.extend(f"{r.label}: {r.value}" for r in rows)
    if button_url:
        lines += ["", button_url]
    lines += ["", f"— {BRAND_NAME}"]
    return "\n".join(lines)


def _from_header(addr: str) -> str:
    return formataddr((BRAND_NAME, addr))


def _send_via_smtp_sync(
    to: str, subject: str, html_body: str, text_body: str, *, settings: Settings, headers: dict[str, str] | None = None
) -> None:
    from_addr = (settings.smtp_from_email or settings.smtp_user).strip()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _from_header(from_addr)
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] or None)
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    host = settings.smtp_host.strip()
    port = int(settings.smtp_port)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            smtp.login(settings.smtp_user.strip(), settings.smtp_password)
            smtp.sendmail(from_addr, [to], msg.as_string())
        return
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user.strip(), settings.smtp_password)
        smtp.sendmail(from_addr, [to], msg.as_string())


async def _send_via_resend(
    to: str, subject: str, html_body: str, text_body: str, *, settings: Settings, headers: dict[str, str] | None = None
) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": _from_header(settings.resend_from_email.strip()),
                "to": [to],
                "subject": subject,
                "html": html_body,
                "text": text_body,
                **({"headers": headers} if headers else {}),
            },
        )
        resp.raise_for_status()


async def send_email(
    to: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    *,
    settings: Settings | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Возвращает (ok, error_text). Исключения не пробрасывает."""
    s = settings or get_settings()
    text_body = text_body or subject
    try:
        if smtp_configured(s):
            await asyncio.to_thread(_send_via_smtp_sync, to, subject, html_body, text_body, settings=s, headers=headers)
        elif resend_configured(s):
            await _send_via_resend(to, subject, html_body, text_body, settings=s, headers=headers)
        else:
            return False, "Отправка писем не настроена (SMTP_* или RESEND_*)."
    except smtplib.SMTPAuthenticationError:
        logger.exception("email send: SMTP auth failed")
        return False, "SMTP отклонил логин/пароль. Для Gmail нужен пароль приложения (16 символов), а не пароль от почты."
    except Exception as e:  # noqa: BLE001
        logger.exception("email send failed to=%s subject=%s", to, subject)
        return False, f"{type(e).__name__}: {e}"
    return True, ""


async def send_branded_email(
    to: str,
    *,
    subject: str,
    title: str,
    intro: str,
    rows: list[EmailRow] | None = None,
    button_text: str | None = None,
    button_url: str | None = None,
    note: str | None = None,
    tone: str = "accent",
    badge: str | None = None,
    big_code: str | None = None,
    unsubscribe_url: str | None = None,
    settings: Settings | None = None,
) -> tuple[bool, str]:
    s = settings or get_settings()
    body = render_email(
        title=title,
        intro=intro,
        rows=rows,
        button_text=button_text,
        button_url=button_url,
        note=note,
        tone=tone,
        badge=badge,
        big_code=big_code,
        unsubscribe_url=unsubscribe_url,
        settings=s,
    )
    plain = _plain_from_parts(title, intro, rows, button_url)
    if big_code:
        plain = f"{title}\n\n{big_code}\n\n{intro}"
    headers = None
    if unsubscribe_url:
        plain += f"\n\nОтписаться от рассылки: {unsubscribe_url}"
        headers = {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    return await send_email(to, subject, body, plain, settings=s, headers=headers)


# --- Отправка после коммита транзакции ---------------------------------------------------------

_PENDING_KEY = "remna_pending_emails"
_LISTENERS_KEY = "remna_pending_emails_listeners"
_BG_TASKS: set[asyncio.Task[Any]] = set()


def _spawn(coro: Coroutine[Any, Any, Any]) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def send_after_commit(session: AsyncSession, factory: Callable[[], Coroutine[Any, Any, Any]]) -> None:
    """Письмо уйдёт только если транзакция закоммитится (иначе — отменяется). Не блокирует запрос."""
    sync = session.sync_session
    pending: list = sync.info.setdefault(_PENDING_KEY, [])
    pending.append(factory)
    if sync.info.get(_LISTENERS_KEY):
        return
    sync.info[_LISTENERS_KEY] = True

    def _after_commit(sess) -> None:  # noqa: ANN001
        items = sess.info.pop(_PENDING_KEY, [])
        for f in items:
            _spawn(f())

    def _after_rollback(sess, previous_transaction) -> None:  # noqa: ANN001
        # Откат SAVEPOINT (begin_nested) не отменяет внешнюю транзакцию — письма не трогаем.
        if getattr(previous_transaction, "parent", None) is None:
            sess.info.pop(_PENDING_KEY, None)

    event.listen(sync, "after_commit", _after_commit)
    event.listen(sync, "after_soft_rollback", _after_rollback)
