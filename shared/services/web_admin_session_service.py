"""Сессии web-admin: отпечаток браузера, 24 ч, подсказка последнего аккаунта."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import desc, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from shared.config import Settings, get_settings
from shared.models.user import User
from shared.models.web_admin_browser_session import WebAdminBrowserSession

logger = logging.getLogger(__name__)

SESSION_TTL = timedelta(hours=24)
LOGIN_HINT_MAX_AGE_SEC = 60 * 60 * 24 * 30
LOGIN_HINT_COOKIE = "remna_login_hint"


def browser_fingerprint(request: Request) -> str:
    client = request.client
    ip = (client.host if client else "") or ""
    ua = (request.headers.get("user-agent") or "")[:400]
    lang = (request.headers.get("accept-language") or "")[:80]
    raw = f"{ip}|{ua}|{lang}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def client_ip(request: Request) -> str | None:
    client = request.client
    if client is None:
        return None
    host = (client.host or "").strip()
    return host[:64] if host else None


def _hint_serializer(settings: Settings | None = None) -> URLSafeTimedSerializer:
    secret = (settings or get_settings()).web_admin_session_secret
    return URLSafeTimedSerializer(str(secret), salt="remna-login-hint")


def login_hint_payload(
    *,
    user_id: int,
    login_kind: str,
    label: str,
    avatar_url: str = "",
    telegram_id: int | None = None,
    github_login: str = "",
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "login_kind": login_kind,
        "label": label,
        "avatar_url": avatar_url,
        "telegram_id": telegram_id,
        "github_login": github_login,
    }


def set_login_hint_cookie(response, payload: dict[str, Any], *, settings: Settings | None = None) -> None:
    token = _hint_serializer(settings).dumps(payload)
    response.set_cookie(
        LOGIN_HINT_COOKIE,
        token,
        max_age=LOGIN_HINT_MAX_AGE_SEC,
        httponly=True,
        samesite="lax",
        secure=False,
    )


def read_login_hint(request: Request, *, settings: Settings | None = None) -> dict[str, Any] | None:
    raw = (request.cookies.get(LOGIN_HINT_COOKIE) or "").strip()
    if not raw:
        return None
    try:
        data = _hint_serializer(settings).loads(raw, max_age=LOGIN_HINT_MAX_AGE_SEC)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) else None


def clear_login_hint_cookie(response) -> None:
    response.delete_cookie(LOGIN_HINT_COOKIE)


def _missing_browser_sessions_table(exc: BaseException) -> bool:
    err = str(exc).lower()
    return "web_admin_browser_sessions" in err and (
        "does not exist" in err or "undefinedtable" in err or "no such table" in err
    )


async def get_browser_session(
    session: AsyncSession,
    *,
    token: str,
    fingerprint_hash: str | None = None,
) -> WebAdminBrowserSession | None:
    tok = (token or "").strip()
    if not tok:
        return None
    row = (
        await session.execute(
            select(WebAdminBrowserSession).where(WebAdminBrowserSession.session_token == tok).limit(1)
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    now = datetime.now(UTC)
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if exp <= now:
        return None
    if fingerprint_hash is not None and row.fingerprint_hash != fingerprint_hash:
        return None
    return row


async def try_get_browser_session(
    session: AsyncSession,
    *,
    token: str,
    fingerprint_hash: str | None = None,
) -> WebAdminBrowserSession | None:
    """Как get_browser_session, но без падения, если миграция 0029 ещё не применена."""
    try:
        return await get_browser_session(session, token=token, fingerprint_hash=fingerprint_hash)
    except ProgrammingError as exc:
        if _missing_browser_sessions_table(exc):
            logger.warning(
                "Таблица web_admin_browser_sessions отсутствует — выполните: docker compose run --rm migrate"
            )
            return None
        raise


async def list_user_browser_sessions_safe(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int = 30,
) -> list[WebAdminBrowserSession]:
    try:
        return await list_user_browser_sessions(session, user_id, limit=limit)
    except ProgrammingError as exc:
        if _missing_browser_sessions_table(exc):
            logger.warning("web_admin_browser_sessions missing (list)")
            return []
        raise


async def touch_browser_session(session: AsyncSession, row: WebAdminBrowserSession) -> datetime:
    now = datetime.now(UTC)
    new_exp = now + SESSION_TTL
    row.last_seen_at = now
    row.expires_at = new_exp
    user = await session.get(User, row.user_id)
    if user is not None:
        user.web_admin_session_token = row.session_token
        user.web_admin_session_expires_at = new_exp
    await session.commit()
    return new_exp


async def revoke_browser_session(session: AsyncSession, *, token: str) -> None:
    tok = (token or "").strip()
    if not tok:
        return
    now = datetime.now(UTC)
    await session.execute(
        update(WebAdminBrowserSession)
        .where(WebAdminBrowserSession.session_token == tok)
        .values(revoked_at=now)
    )


async def revoke_all_user_browser_sessions(session: AsyncSession, user_id: int) -> None:
    now = datetime.now(UTC)
    await session.execute(
        update(WebAdminBrowserSession)
        .where(
            WebAdminBrowserSession.user_id == user_id,
            WebAdminBrowserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


async def create_browser_session(
    session: AsyncSession,
    *,
    user: User,
    request: Request,
    session_token: str,
    login_kind: str,
    auth_snapshot: dict[str, Any],
) -> WebAdminBrowserSession:
    now = datetime.now(UTC)
    expires_at = now + SESSION_TTL
    fp = browser_fingerprint(request)
    ua = (request.headers.get("user-agent") or "")[:512]
    row = WebAdminBrowserSession(
        user_id=user.id,
        session_token=session_token,
        fingerprint_hash=fp,
        login_kind=login_kind,
        ip_address=client_ip(request),
        user_agent=ua or None,
        auth_snapshot=auth_snapshot,
        expires_at=expires_at,
        last_seen_at=now,
    )
    session.add(row)
    db_user = await session.get(User, user.id)
    if db_user is not None:
        db_user.web_admin_session_token = session_token
        db_user.web_admin_session_expires_at = expires_at
    await session.commit()
    return row


def restore_wauth_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    kind = str(snapshot.get("kind") or snapshot.get("login_kind") or "web")
    if kind == "github":
        login = str(snapshot.get("login") or snapshot.get("github_login") or snapshot.get("username") or "")
        return {
            "kind": "github",
            "login": login,
            "label": str(snapshot.get("label") or login),
            "avatar_url": str(snapshot.get("avatar_url") or ""),
            "username": login,
            **({"telegram_id": int(snapshot["telegram_id"])} if snapshot.get("telegram_id") is not None else {}),
        }
    tid = snapshot.get("telegram_id") or snapshot.get("id")
    try:
        tid_i = int(tid)
    except (TypeError, ValueError):
        tid_i = 0
    return {
        "kind": "telegram",
        "id": tid_i,
        "telegram_id": tid_i,
        "label": str(snapshot.get("label") or ""),
        "avatar_url": str(snapshot.get("avatar_url") or ""),
        "username": str(snapshot.get("username") or ""),
        "github_login": str(snapshot.get("github_login") or ""),
        "github_avatar_url": str(snapshot.get("github_avatar_url") or ""),
    }


def auth_snapshot_from_wauth(wauth: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(wauth, default=str))


async def list_user_browser_sessions(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int = 30,
) -> list[WebAdminBrowserSession]:
    rows = (
        await session.execute(
            select(WebAdminBrowserSession)
            .where(WebAdminBrowserSession.user_id == user_id)
            .order_by(desc(WebAdminBrowserSession.last_seen_at))
            .limit(max(1, min(limit, 100)))
        )
    ).scalars().all()
    return list(rows)


async def revoke_browser_session_by_id(
    session: AsyncSession,
    *,
    user_id: int,
    session_row_id: int,
) -> bool:
    row = await session.get(WebAdminBrowserSession, session_row_id)
    if row is None or int(row.user_id) != int(user_id):
        return False
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
    user = await session.get(User, user_id)
    if user is not None and (user.web_admin_session_token or "") == row.session_token:
        user.web_admin_session_token = None
        user.web_admin_session_expires_at = None
    await session.commit()
    return True


async def revoke_all_user_browser_sessions(
    session: AsyncSession,
    user_id: int,
    *,
    except_token: str | None = None,
) -> int:
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(WebAdminBrowserSession).where(
                WebAdminBrowserSession.user_id == user_id,
                WebAdminBrowserSession.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    n = 0
    exc = (except_token or "").strip()
    user = await session.get(User, user_id)
    for row in rows:
        if exc and row.session_token == exc:
            continue
        row.revoked_at = now
        n += 1
        if user is not None and (user.web_admin_session_token or "") == row.session_token:
            user.web_admin_session_token = None
            user.web_admin_session_expires_at = None
    if n:
        await session.commit()
    return n
