"""События подписки/отписки на обязательный канал."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    ChatMemberUpdated,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.md2 import bold, code, esc
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin

router = Router(name="channel_events")


def _member_is_subscribed(member) -> bool:
    if isinstance(member, (ChatMemberOwner, ChatMemberAdministrator, ChatMemberMember)):
        return True
    if isinstance(member, ChatMemberRestricted):
        return bool(member.is_member)
    if isinstance(member, (ChatMemberLeft, ChatMemberBanned)):
        return False
    return False


@router.chat_member()
async def on_required_channel_membership_changed(
    event: ChatMemberUpdated,
    session: AsyncSession,
) -> None:
    settings = get_settings()
    try:
        required_chat_id = int(settings.required_channel_id)
    except (TypeError, ValueError):
        return
    if event.chat.id != required_chat_id:
        return
    target_user = event.new_chat_member.user
    if target_user is None or target_user.is_bot:
        return

    old_subscribed = _member_is_subscribed(event.old_chat_member)
    new_subscribed = _member_is_subscribed(event.new_chat_member)
    if old_subscribed == new_subscribed:
        return

    db_user = (
        await session.execute(select(User).where(User.telegram_id == int(target_user.id)).limit(1))
    ).scalar_one_or_none()
    if db_user is not None:
        db_user.is_subscribed_channel = new_subscribed
        await session.commit()

    full_name = (f"{target_user.first_name or ''} {target_user.last_name or ''}").strip() or "—"
    username = f"@{target_user.username}" if target_user.username else "—"
    action_text = "подписался на канал" if new_subscribed else "отписался от канала"
    channel_ref = (
        f"@{settings.required_channel_username.lstrip('@')}"
        if (settings.required_channel_username or "").strip()
        else str(required_chat_id)
    )
    await notify_admin(
        settings,
        title="📢 " + bold("Новостной канал: ") + esc(action_text),
        lines=[
            "Канал: " + esc(channel_ref),
            "Telegram ID: " + code(str(target_user.id)),
            "Username: " + esc(username),
            "Имя/фамилия: " + esc(full_name),
        ],
        event_type="channel_membership_changed",
        topic=AdminLogTopic.CHANNEL,
        subject_user=db_user,
        subject_user_id=(db_user.id if db_user is not None else None),
        session=session,
    )
