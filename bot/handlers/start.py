"""Команда /start: канал → регистрация → реферал → главное меню."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, support_telegram_url
from bot.handlers.menu import main_menu_welcome_text
from bot.keyboards.inline import channel_required_keyboard, main_menu_keyboard
from shared.config import get_settings
from shared.models.user import User
from shared.services.trial_service import has_active_subscription, trial_eligible
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
) -> None:
    settings = get_settings()
    if not is_channel_member:
        await message.answer(
            "👋 Добро пожаловать!\n\n"
            "Чтобы пользоваться ботом, подпишитесь на наш канал "
            "и нажмите «✅ Я подписался».",
            reply_markup=channel_required_keyboard(settings.required_channel_username),
        )
        return

    payload = extract_start_payload(message)
    user, created = await register_user(session, message.from_user, payload)

    if await reject_if_blocked(message, user):
        return

    intro_lines: list[str] = []
    if created:
        intro_lines.append("✅ <b>Регистрация прошла успешно!</b>")
        if user.referred_by is not None:
            intro_lines.append("Вы присоединились по приглашению друга.")
    else:
        intro_lines.append("С возвращением!")

    has_act = await has_active_subscription(session, user.id)
    show_trial = trial_eligible(user, has_act)
    kb = main_menu_keyboard(
        show_trial=show_trial,
        support_url=support_telegram_url(settings.support_username),
    )

    body = "\n".join(intro_lines) + "\n\n" + main_menu_welcome_text(user)
    await message.answer(body, reply_markup=kb)
