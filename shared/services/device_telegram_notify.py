"""Уведомления в Telegram о привязке HWID (вебхук → пользователь)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import bold, join_lines, plain
from shared.models.user import User
from shared.services.telegram_notify import delete_telegram_message, send_telegram_message

logger = logging.getLogger(__name__)


async def notify_device_attached_replace_message(
    session: AsyncSession,
    user: User,
    settings: Settings,
    *,
    first_ever: bool,
) -> None:
    """Удаляет прошлое сервисное сообщение об устройствах и шлёт новое."""
    old = user.device_notify_message_id
    if old is not None:
        deleted = await delete_telegram_message(user.telegram_id, int(old), settings=settings)
        if not deleted:
            logger.debug(
                "notify_device_attached_replace_message: old message not deleted tg=%s mid=%s",
                user.telegram_id,
                old,
            )
    if first_ever:
        body = join_lines(
            "✅ " + bold("Вы успешно привязали первое устройство"),
            "",
            plain("Дальше — подключение в приложении по ссылке из «Моя подписка»."),
        )
    else:
        body = join_lines(
            "📱 " + bold("Новое устройство подключено"),
            "",
            plain("В панели зарегистрирован ещё один HWID. Список и отвязка — в разделе «Устройства»."),
        )
    mid = await send_telegram_message(
        user.telegram_id,
        body,
        settings=settings,
        reply_markup={
            "inline_keyboard": [[{"text": "⬅️ Главное меню", "callback_data": "menu:main"}]],
        },
    )
    user.device_notify_message_id = int(mid) if mid is not None else None
    await session.flush()
