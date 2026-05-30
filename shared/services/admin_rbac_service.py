"""RBAC веб-админки и Telegram-бота."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.admin_role import ADMIN_PERMISSIONS, AdminRole
from shared.models.admin_user import AdminUser
from shared.models.user import User

ALL_ADMIN_PERMISSIONS = sorted(ADMIN_PERMISSIONS)


def _normalize_permissions(raw: list | None) -> set[str]:
    if not raw:
        return set()
    return {str(x).strip() for x in raw if str(x).strip() in ADMIN_PERMISSIONS}


async def get_admin_user_by_user_id(session: AsyncSession, user_id: int) -> AdminUser | None:
    return (
        await session.execute(select(AdminUser).where(AdminUser.user_id == user_id).limit(1))
    ).scalar_one_or_none()


async def get_admin_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> AdminUser | None:
    user = (
        await session.execute(select(User).where(User.telegram_id == telegram_id).limit(1))
    ).scalar_one_or_none()
    if user is None:
        return None
    return await get_admin_user_by_user_id(session, user.id)


async def ensure_superadmin_record(session: AsyncSession, settings: Settings) -> AdminUser | None:
    tg_id = settings.effective_superadmin_telegram_id
    if tg_id is None:
        return None
    user = (
        await session.execute(select(User).where(User.telegram_id == int(tg_id)).limit(1))
    ).scalar_one_or_none()
    if user is None:
        return None
    row = await get_admin_user_by_user_id(session, user.id)
    if row is None:
        row = AdminUser(
            user_id=user.id,
            role_id=None,
            extra_permissions=list(ALL_ADMIN_PERMISSIONS),
            is_superadmin=True,
        )
        session.add(row)
        await session.flush()
        return row
    if not row.is_superadmin:
        row.is_superadmin = True
        row.extra_permissions = list(set(_normalize_permissions(row.extra_permissions)) | set(ALL_ADMIN_PERMISSIONS))
        await session.flush()
    return row


async def effective_permissions(session: AsyncSession, admin: AdminUser) -> set[str]:
    if admin.is_superadmin:
        return set(ALL_ADMIN_PERMISSIONS)
    perms = _normalize_permissions(admin.extra_permissions)
    if admin.role_id is not None:
        role = await session.get(AdminRole, admin.role_id)
        if role is not None:
            perms |= _normalize_permissions(role.permissions)
    return perms


async def user_has_permission(session: AsyncSession, *, user_id: int, permission: str) -> bool:
    admin = await get_admin_user_by_user_id(session, user_id)
    if admin is None:
        return False
    return permission in await effective_permissions(session, admin)


async def telegram_has_permission(
    session: AsyncSession, settings: Settings, *, telegram_id: int, permission: str
) -> bool:
    if settings.effective_superadmin_telegram_id is not None and int(telegram_id) == int(
        settings.effective_superadmin_telegram_id
    ):
        await ensure_superadmin_record(session, settings)
        return True
    admin = await get_admin_user_by_telegram_id(session, telegram_id)
    if admin is None:
        return False
    return permission in await effective_permissions(session, admin)


async def is_superadmin_user(session: AsyncSession, settings: Settings, *, user_id: int) -> bool:
    if settings.effective_superadmin_telegram_id is not None:
        user = await session.get(User, user_id)
        if user is not None and int(user.telegram_id or 0) == int(settings.effective_superadmin_telegram_id):
            return True
    admin = await get_admin_user_by_user_id(session, user_id)
    return bool(admin and admin.is_superadmin)


async def can_access_web_admin(session: AsyncSession, settings: Settings, *, user: User) -> bool:
    if settings.effective_superadmin_telegram_id is not None and int(user.telegram_id or 0) == int(
        settings.effective_superadmin_telegram_id
    ):
        return True
    admin = await get_admin_user_by_user_id(session, user.id)
    return admin is not None


async def resolve_operator_id(session: AsyncSession, *, user_id: int) -> int | None:
    admin = await get_admin_user_by_user_id(session, user_id)
    return int(admin.id) if admin is not None else None


async def list_admin_users(session: AsyncSession) -> list[AdminUser]:
    rows = (
        await session.execute(select(AdminUser).order_by(AdminUser.id.asc()))
    ).scalars()
    return list(rows.all())
