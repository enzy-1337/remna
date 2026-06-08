"""
Уведомление пользователям, которые купили подписку, но ни разу не подключились.

Логика:
  - Ищем пользователей с активной подпиской, у которых:
      * subscription.started_at < now - 24h  (прошло больше суток)
      * нет ни одной записи в device_history (тип attached)
      * connection_notify_sent_at IS NULL  (уведомление ещё не отправлялось)
  - Отправляем сообщение в личку и ставим connection_notify_sent_at = now
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import bold, join_lines, link, plain
from shared.models.device_history import DeviceHistory
from shared.models.subscription import Subscription
from shared.models.user import User

logger = logging.getLogger(__name__)

_NOTIFY_DELAY_HOURS = 24
_BATCH_SIZE = 50


def _build_message(support_username: str | None) -> tuple[str, object]:
    """Возвращает (текст, клавиатура)."""
    if support_username:
        sup_link = link("поддержку", f"https://t.me/{support_username.lstrip('@')}")
    else:
        sup_link = bold("поддержку")

    text = join_lines(
        "👋 " + bold("Вы ещё не подключили устройство!"),
        "",
        plain("Вы приобрели подписку, но ни одно устройство пока не подключено к VPN."),
        "",
        plain("Если возникли вопросы или нужна помощь с настройкой — напишите в ") + sup_link + plain("."),
    )

    kb = InlineKeyboardBuilder()
    if support_username:
        kb.row(
            InlineKeyboardButton(
                text="✍️ Написать в поддержку",
                url=f"https://t.me/{support_username.lstrip('@')}",
            )
        )

    return text, kb.as_markup() if support_username else None


async def _user_has_ever_connected(session: AsyncSession, user_id: int) -> bool:
    """True если есть хотя бы одно событие привязки устройства."""
    count = (
        await session.execute(
            select(func.count())
            .select_from(DeviceHistory)
            .where(
                DeviceHistory.user_id == user_id,
                DeviceHistory.event_type.in_(
                    ("device.attached", "user_hwid_devices.added")
                ),
            )
        )
    ).scalar_one()
    return int(count or 0) > 0


async def _first_connected_at(session: AsyncSession, user_id: int) -> datetime | None:
    """Дата первого подключения (первая запись device.attached)."""
    ts = (
        await session.execute(
            select(func.min(DeviceHistory.event_ts))
            .where(
                DeviceHistory.user_id == user_id,
                DeviceHistory.event_type.in_(
                    ("device.attached", "user_hwid_devices.added")
                ),
            )
        )
    ).scalar_one_or_none()
    return ts


async def run_connection_notify_loop(bot: Bot, settings: Settings, session: AsyncSession) -> None:
    """
    Один прогон: находит до BATCH_SIZE пользователей и отправляет уведомления.
    Вызывается из фонового loop каждые N минут.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=_NOTIFY_DELAY_HOURS)

    # Пользователи с активной подпиской, стартовавшей > 24ч назад, без уведомления
    users_q = (
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            Subscription.status == "active",
            Subscription.started_at < cutoff,
            User.connection_notify_sent_at.is_(None),
            User.is_blocked.is_(False),
        )
        .distinct()
        .limit(_BATCH_SIZE)
    )
    users = (await session.execute(users_q)).scalars().all()

    if not users:
        return

    logger.info("connection_notify: нашли %d кандидатов", len(users))
    sent = 0
    skipped = 0

    for user in users:
        # Проверяем — вдруг устройство уже привязано
        if await _user_has_ever_connected(session, user.id):
            # Помечаем чтобы больше не проверять этого пользователя
            user.connection_notify_sent_at = datetime.now(UTC)
            skipped += 1
            continue

        text, markup = _build_message(settings.support_username)
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=markup,
            )
            user.connection_notify_sent_at = datetime.now(UTC)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            # Бот заблокирован у пользователя или чат не найден — всё равно помечаем
            logger.warning(
                "connection_notify: не удалось отправить user_id=%s tg_id=%s: %s",
                user.id,
                user.telegram_id,
                exc,
            )
            user.connection_notify_sent_at = datetime.now(UTC)
        except Exception as exc:
            logger.exception(
                "connection_notify: ошибка для user_id=%s: %s", user.id, exc
            )

    await session.flush()
    logger.info(
        "connection_notify: отправлено=%d, пропущено(уже подкл.)=%d", sent, skipped
    )
