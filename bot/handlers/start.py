"""Команда /start: канал → регистрация → профиль."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, support_telegram_url
from bot.keyboards.inline import channel_required_keyboard
from bot.keyboards.profile_kb import profile_main_keyboard
from bot.ui.profile_text import profile_caption
from bot.utils.screen_photo import delete_message_safe, send_profile_screen
from shared.config import get_settings
from shared.services.subscription_service import get_active_subscription
from shared.services.trial_service import trial_eligible
from shared.md2 import bold, esc, join_lines
from shared.services.user_registration import register_user

router = Router(name="start")


def extract_start_payload(message: Message) -> str | None:
    if not message.text:
        return None
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    is_channel_member: bool,
    is_bot_admin: bool = False,
) -> None:
    settings = get_settings()
    if not is_channel_member:
        await message.answer(
            esc(
                "👋 Добро пожаловать!\n\n"
                "Чтобы пользоваться ботом, подпишитесь на наш канал "
                "и нажмите «✅ Я подписался»."
            ),
            reply_markup=channel_required_keyboard(settings.required_channel_username),
        )
        return

    payload = extract_start_payload(message)
    user, created = await register_user(session, message.from_user, payload)

    if await reject_if_blocked(message, user):
        return

    tg = message.from_user
    assert tg is not None

    intro_lines: list[str] = []
    if created:
        intro_lines.append("✅ " + bold("Регистрация прошла успешно!"))
        if user.referred_by is not None:
            intro_lines.append(esc("Вы присоединились по приглашению друга."))
    else:
        intro_lines.append(esc("С возвращением!"))

    has_act = await get_active_subscription(session, user.id) is not None
    show_trial = trial_eligible(user, has_act)
    kb = profile_main_keyboard(
        has_active_sub=has_act,
        show_trial=show_trial,
        support_url=support_telegram_url(settings.support_username),
        is_admin=is_bot_admin,
    )
    profile_block = profile_caption(user, tg)
    body = join_lines(*intro_lines, "", profile_block)
    await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=body,
        reply_markup=kb,
        settings=settings,
        delete_message=None,
    )
    await delete_message_safe(message)
