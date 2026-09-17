"""Сессии личного кабинета публичного сайта (отдельно от web-admin): cookie + БД, 30 дней."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from shared.config import Settings, get_settings
from shared.models.user import User
from shared.models.user_web_session import UserWebSession
from shared.services.web_admin_session_service import browser_fingerprint, client_ip

logger = logging.getLogger(__name__)

SESSION_TTL = timedelta(days=30)
SESSION_COOKIE = "flux_session"

__all__ = [
    "SESSION_TTL",
    "SESSION_COOKIE",
    "browser_fingerprint",
    "client_ip",
    "set_session_cookie",
    "read_session_cookie",
    "clear_session_cookie",
    "create_session",
    "get_session_by_token",
    "touch_session",
    "revoke_session",
    "revoke_session_by_id",
    "revoke_all_user_sessions",
    "list_user_sessions",
    "load_site_user",
]


def set_session_cookie(response: Response, token: str, *, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=s.web_admin_session_https_only,
        path="/",
    )


def read_session_cookie(request: Request) -> str | None:
    tok = (request.cookies.get(SESSION_COOKIE) or "").strip()
    return tok or None


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


async def create_session(
    session: AsyncSession,
    *,
    user: User,
    request: Request,
    login_kind: str = "telegram",
) -> UserWebSession:
    now = datetime.now(UTC)
    row = UserWebSession(
        user_id=user.id,
        session_token=secrets.token_urlsafe(32),
        fingerprint_hash=browser_fingerprint(request),
        login_kind=login_kind,
        ip_address=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        expires_at=now + SESSION_TTL,
        last_seen_at=now,
    )
    session.add(row)
    await session.commit()
    return row


async def get_session_by_token(
    session: AsyncSession, *, token: str
) -> UserWebSession | None:
    tok = (token or "").strip()
    if not tok:
        return None
    row = (
        await session.execute(
            select(UserWebSession).where(UserWebSession.session_token == tok).limit(1)
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
    return row


async def touch_session(session: AsyncSession, row: UserWebSession) -> None:
    now = datetime.now(UTC)
    row.last_seen_at = now
    row.expires_at = now + SESSION_TTL
    await session.commit()


async def revoke_session(session: AsyncSession, *, token: str) -> None:
    tok = (token or "").strip()
    if not tok:
        return
    await session.execute(
        update(UserWebSession).where(UserWebSession.session_token == tok).values(revoked_at=datetime.now(UTC))
    )
    await session.commit()


async def revoke_session_by_id(session: AsyncSession, *, user_id: int, session_row_id: int) -> bool:
    row = await session.get(UserWebSession, session_row_id)
    if row is None or int(row.user_id) != int(user_id):
        return False
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.commit()
    return True


async def revoke_all_user_sessions(
    session: AsyncSession, user_id: int, *, except_token: str | None = None
) -> int:
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(UserWebSession).where(
                UserWebSession.user_id == user_id,
                UserWebSession.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    exc = (except_token or "").strip()
    n = 0
    for row in rows:
        if exc and row.session_token == exc:
            continue
        row.revoked_at = now
        n += 1
    if n:
        await session.commit()
    return n


async def load_site_user(
    session: AsyncSession, request: Request
) -> tuple[User, UserWebSession] | None:
    """Резолвит текущего пользователя личного кабинета сайта по cookie `flux_session`."""
    token = read_session_cookie(request)
    if not token:
        return None
    row = await get_session_by_token(session, token=token)
    if row is None:
        return None
    user = await session.get(User, row.user_id)
    if user is None:
        return None
    return user, row


async def list_user_sessions(
    session: AsyncSession, user_id: int, *, limit: int = 30
) -> list[UserWebSession]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=3)
    await session.execute(
        delete(UserWebSession).where(
            UserWebSession.user_id == user_id,
            UserWebSession.revoked_at.is_not(None),
            UserWebSession.revoked_at < cutoff,
        )
    )
    await session.commit()
    rows = (
        await session.execute(
            select(UserWebSession)
            .where(UserWebSession.user_id == user_id, UserWebSession.revoked_at.is_(None))
            .order_by(desc(UserWebSession.last_seen_at))
            .limit(max(1, min(limit, 100)))
        )
    ).scalars().all()
    return list(rows)
