"""Семейная подписка: лимиты, привязка, отвязка."""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.family_member import FamilyMember
from shared.models.subscription import Subscription
from shared.models.user import User
from shared.services.subscription_service import get_active_subscription

MAX_FAMILY_TOTAL_USERS = 3
MAX_FAMILY_EXTRA_MEMBERS = 2


def max_family_extra_members(devices_count: int) -> int:
    """Сколько доп. пользователей можно привязать (без владельца)."""
    if devices_count <= 1:
        return 0
    return min(devices_count - 1, MAX_FAMILY_EXTRA_MEMBERS)


async def resolve_account_user_id(session: AsyncSession, user_id: int) -> int:
    row = (
        await session.execute(
            select(FamilyMember.owner_user_id).where(FamilyMember.member_user_id == user_id).limit(1)
        )
    ).scalar_one_or_none()
    return int(row) if row is not None else int(user_id)


async def resolve_account_user(session: AsyncSession, user: User) -> User:
    owner_id = await resolve_account_user_id(session, user.id)
    if owner_id == user.id:
        return user
    owner = await session.get(User, owner_id)
    return owner if owner is not None else user


async def get_family_membership(session: AsyncSession, *, user_id: int) -> FamilyMember | None:
    return (
        await session.execute(
            select(FamilyMember).where(FamilyMember.member_user_id == user_id).limit(1)
        )
    ).scalar_one_or_none()


async def is_family_owner(session: AsyncSession, *, user_id: int) -> bool:
    n = (
        await session.execute(
            select(func.count()).select_from(FamilyMember).where(FamilyMember.owner_user_id == user_id)
        )
    ).scalar_one()
    return int(n or 0) > 0


async def count_family_members(session: AsyncSession, *, owner_user_id: int) -> int:
    n = (
        await session.execute(
            select(func.count())
            .select_from(FamilyMember)
            .where(FamilyMember.owner_user_id == owner_user_id)
        )
    ).scalar_one()
    return int(n or 0)


async def list_family_members(session: AsyncSession, *, owner_user_id: int) -> list[FamilyMember]:
    rows = (
        await session.execute(
            select(FamilyMember)
            .where(FamilyMember.owner_user_id == owner_user_id)
            .order_by(FamilyMember.id.asc())
        )
    ).scalars()
    return list(rows.all())


async def family_bind_allowed(
    session: AsyncSession,
    *,
    owner: User,
    member: User,
) -> tuple[bool, str]:
    if owner.id == member.id:
        return False, "Нельзя привязать самого себя."
    if await get_family_membership(session, user_id=member.id) is not None:
        return False, "Этот пользователь уже состоит в другой семье."
    if await get_family_membership(session, user_id=owner.id) is not None:
        return False, "Участники семьи не могут привязывать других пользователей."
    sub = await get_active_subscription(session, owner.id, account_scope=False)
    if sub is None:
        return False, "Нет активной подписки для семейного доступа."
    limit = max_family_extra_members(int(sub.devices_count))
    if limit <= 0:
        return False, "Семейная подписка недоступна: в тарифе только 1 устройство."
    current = await count_family_members(session, owner_user_id=owner.id)
    if current >= limit:
        return False, "Достигнут лимит участников семейной подписки."
    return True, ""


async def bind_family_member(
    session: AsyncSession,
    *,
    owner: User,
    member: User,
) -> tuple[bool, str, FamilyMember | None]:
    ok, err = await family_bind_allowed(session, owner=owner, member=member)
    if not ok:
        return False, err, None
    sub = await get_active_subscription(session, owner.id, account_scope=False)
    assert sub is not None
    row = FamilyMember(
        owner_user_id=owner.id,
        member_user_id=member.id,
        subscription_id=sub.id,
    )
    session.add(row)
    await session.flush()
    return True, "", row


async def remove_family_member_hwids(
    session: AsyncSession,
    *,
    member: User,
    owner: User,
    settings: Settings,
) -> None:
    """Снять HWID участника с общего профиля владельца (если были)."""
    if owner.remnawave_uuid is None:
        return
    rw = RemnaWaveClient(settings)
    try:
        devs = await rw.get_user_hwid_devices(str(owner.remnawave_uuid))
    except RemnaWaveError:
        return
    member_tg = int(member.telegram_id or 0)
    for d in devs:
        meta = d.get("userAgent") or d.get("description") or ""
        tg_hint = str(d.get("telegramId") or d.get("telegram_id") or "")
        if member_tg and str(member_tg) in tg_hint:
            hwid = d.get("hwid")
            if hwid:
                try:
                    await rw.delete_user_hwid_device(str(owner.remnawave_uuid), str(hwid))
                except RemnaWaveError:
                    pass


async def unbind_family_member(
    session: AsyncSession,
    *,
    member_user_id: int,
    settings: Settings,
    by_owner: bool,
) -> tuple[bool, str, FamilyMember | None]:
    row = (
        await session.execute(
            select(FamilyMember).where(FamilyMember.member_user_id == member_user_id).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return False, "Участник семьи не найден.", None
    owner = await session.get(User, row.owner_user_id)
    member = await session.get(User, row.member_user_id)
    if owner is None or member is None:
        await session.execute(delete(FamilyMember).where(FamilyMember.id == row.id))
        return True, "", row
    await remove_family_member_hwids(session, member=member, owner=owner, settings=settings)
    await session.execute(delete(FamilyMember).where(FamilyMember.id == row.id))
    _ = by_owner
    return True, "", row


async def clear_family_for_owner(session: AsyncSession, *, owner_user_id: int) -> None:
    await session.execute(delete(FamilyMember).where(FamilyMember.owner_user_id == owner_user_id))


async def get_owner_subscription(session: AsyncSession, *, owner_user_id: int) -> Subscription | None:
    sub = await get_active_subscription(session, owner_user_id, account_scope=False)
    return sub
