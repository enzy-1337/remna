"""
Уведомление пользователям, которые купили подписку, но ни разу не подключились.

Логика:
  - Ищем пользователей с активной подпиской, у которых:
      * subscription.started_at < now - 24h  (прошло больше суток)
      * в Remnawave firstConnectedAt = null  (ни разу не подключались)
      * connection_notify_sent_at IS NULL  (уведомление ещё не отправлялось)
  - Отправляем сообщение в личку, сохраняем message_id и ставим connection_notify_sent_at = now

Очистка (cleanup):
  - Находим пользователей, которым отправили уведомление (connection_notify_message_id NOT NULL),
    но в Remnawave у них firstConnectedAt уже есть → удаляем сообщение, сбрасываем поля.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient
from shared.integrations.rw_user_meta import rw_user_first_connected_at
from shared.md2 import bold, join_lines, link, plain
from shared.models.subscription import Subscription
from shared.models.user import User

logger = logging.getLogger(__name__)

_NOTIFY_DELAY_HOURS = 24
_BATCH_SIZE = 50


def _build_message(support_username: str | None) -> tuple[str, object]:
    """Возвращает (текст MarkdownV2, клавиатура)."""
    if support_username:
        sup_link = link("поддержку", f"https://t.me/{support_username.lstrip('@')}")
    else:
        sup_link = bold("поддержку")

    text = join_lines(
        "👋 " + bold("Вы ещё не подключили устройство!"),
        "",
        plain("Вы приобрели подписку, но ни одно устройство пока не подключено к VPN."),
        "",
        plain("Попробуйте подключиться к серверам — это займёт всего пару минут."),
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


async def _rw_first_connected_at(
    rw: RemnaWaveClient, user: User
) -> datetime | None:
    """Запрашивает firstConnectedAt из Remnawave. None если uuid нет или ошибка."""
    if not user.remnawave_uuid:
        return None
    try:
        info = await rw.get_user(str(user.remnawave_uuid))
        return rw_user_first_connected_at(info)
    except Exception as exc:
        logger.warning(
            "connection_notify: не удалось получить данные из Remnawave user_id=%s: %s",
            user.id, exc,
        )
        return None


async def run_connection_notify_cleanup(
    bot: Bot, settings: Settings, session: AsyncSession
) -> None:
    """
    Удаляет ошибочно отправленные уведомления:
    пользователи, которым уже отправили сообщение (message_id сохранён),
    но в Remnawave они оказались подключёнными — удаляем сообщение и сбрасываем поля.
    """
    if settings.remnawave_stub:
        return

    # Пользователи с сохранённым message_id уведомления
    q = (
        select(User)
        .where(
            User.connection_notify_message_id.is_not(None),
        )
        .limit(100)
    )
    users = (await session.execute(q)).scalars().all()
    if not users:
        return

    rw = RemnaWaveClient(settings)
    cleaned = 0
    for user in users:
        first_connected = await _rw_first_connected_at(rw, user)
        if first_connected is None:
            # Ещё не подключались — уведомление правильное, оставляем
            continue
        # Уже подключены — удаляем сообщение
        msg_id = user.connection_notify_message_id
        if msg_id:
            try:
                await bot.delete_message(chat_id=user.telegram_id, message_id=msg_id)
            except Exception as exc:
                logger.debug(
                    "connection_notify cleanup: не удалось удалить msg user_id=%s msg_id=%s: %s",
                    user.id, msg_id, exc,
                )
        # Сбрасываем — чтобы уведомление не отправлялось повторно (при следующей подписке)
        user.connection_notify_message_id = None
        # connection_notify_sent_at оставляем — уже отправляли, повтор не нужен
        cleaned += 1

    if cleaned:
        await session.flush()
        logger.info("connection_notify cleanup: удалено %d ошибочных уведомлений", cleaned)


async def run_connection_notify_loop(
    bot: Bot, settings: Settings, session: AsyncSession
) -> None:
    """
    Один прогон: находит до BATCH_SIZE пользователей и отправляет уведомления.
    Вызывается из фонового loop каждые N минут.
    """
    # Сначала чистим ошибочно отправленные сообщения
    await run_connection_notify_cleanup(bot, settings, session)

    if settings.remnawave_stub:
        return

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
            User.remnawave_uuid.is_not(None),
        )
        .distinct()
        .limit(_BATCH_SIZE)
    )
    users = (await session.execute(users_q)).scalars().all()

    if not users:
        return

    logger.info("connection_notify: нашли %d кандидатов", len(users))
    rw = RemnaWaveClient(settings)
    sent = 0
    skipped = 0

    for user in users:
        # Проверяем через Remnawave API — подключался ли пользователь
        first_connected = await _rw_first_connected_at(rw, user)
        if first_connected is not None:
            # Уже подключался — помечаем чтобы больше не проверять
            user.connection_notify_sent_at = datetime.now(UTC)
            skipped += 1
            continue

        text, markup = _build_message(settings.support_username)
        try:
            sent_msg = await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            user.connection_notify_sent_at = datetime.now(UTC)
            user.connection_notify_message_id = sent_msg.message_id
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
