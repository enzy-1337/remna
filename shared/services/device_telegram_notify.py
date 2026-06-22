"""Уведомления в Telegram о привязке HWID (вебхук → пользователь)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import bold, code, join_lines, plain
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin
from shared.services.telegram_notify import delete_telegram_message, send_telegram_message

_DEVICE_DETACH_MODE_RU: dict[str, str] = {
    "keep_slots": "Отвязка от панели, слоты не менялись",
    "decrease_slot": "Отвязка и уменьшение лимита слотов",
    "db_slot": "Удаление слота в БД и обновление лимита",
}

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


async def notify_admin_device_detached(
    settings: Settings,
    *,
    user: User,
    hwid: str,
    mode: str,
    initiator: str = "user_bot",
    session: AsyncSession | None = None,
) -> None:
    """Сообщение в админ-чат (тема DEVICES) после успешной отвязки устройства."""
    from shared.services.web_admin_notify import web_admin_actor_notify_line

    hw = ((hwid or "").strip() or "—")[:128]
    mode_label = _DEVICE_DETACH_MODE_RU.get(mode, mode)
    lines: list[str] = [
        plain("Режим: ") + bold(mode_label),
        plain("HWID: ") + code(hw),
    ]
    if initiator == "web_admin":
        lines.append(web_admin_actor_notify_line())
    else:
        lines.append(plain("Инициатор: ") + bold("пользователь в боте"))

    await notify_admin(
        settings,
        title="📱 " + bold("Устройство отвязано"),
        lines=lines,
        event_type="device_detached",
        topic=AdminLogTopic.DEVICES,
        subject_user=user,
        session=session,
    )


async def notify_admin_device_slots_purchased(
    settings: Settings,
    *,
    user: User,
    quantity: int,
    total_price,
    unit_price,
    discount_pct,
    total_slots: int,
    session: AsyncSession | None = None,
) -> None:
    """Сообщение в админ-чат (тема DEVICES) после докупки слотов устройств."""
    lines: list[str] = [
        plain("Докуплено слотов: ") + bold(str(quantity)),
        plain("Списано: ") + bold(str(total_price)) + plain(" ₽"),
        plain("Всего слотов в подписке: ") + bold(str(total_slots)),
    ]
    if discount_pct:
        lines.append(
            plain("Скидка: ")
            + bold(str(discount_pct))
            + plain("% (база ")
            + bold(str(unit_price))
            + plain(" ₽/слот)")
        )

    await notify_admin(
        settings,
        title="➕ " + bold("Докупка слотов устройств"),
        lines=lines,
        event_type="device_slots_purchased",
        topic=AdminLogTopic.DEVICES,
        subject_user=user,
        session=session,
    )
