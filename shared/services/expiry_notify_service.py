"""Напоминания пользователю в Telegram: за ~24 ч и ~3 ч до окончания подписки."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.config import Settings
from shared.database import get_session_factory
from shared.models.subscription import Subscription
from shared.services.telegram_notify import send_telegram_message

logger = logging.getLogger(__name__)

# Окна «осталось до конца» (с запасом под интервал опроса)
_WINDOW_24H = (timedelta(hours=22), timedelta(hours=25))
_WINDOW_3H = (timedelta(hours=2, minutes=20), timedelta(hours=3, minutes=40))

_ANCHOR_DRIFT_SEC = 90


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sync_expiry_anchor(sub: Subscription) -> None:
    """При продлении/смене срока сбрасываем флаги напоминаний."""
    exp = _utc(sub.expires_at)
    anchor = sub.expiry_notify_anchor_at
    if anchor is None:
        sub.expiry_notify_anchor_at = exp
        return
    anchor = _utc(anchor)
    if abs((exp - anchor).total_seconds()) > _ANCHOR_DRIFT_SEC:
        sub.expiry_notified_24h = False
        sub.expiry_notified_3h = False
        sub.expiry_notify_anchor_at = exp


async def process_subscription_expiry_notifications(session: AsyncSession, settings: Settings) -> tuple[int, int]:
    """
    Активные и trial подписки с будущим expires_at.
    Возвращает (отправлено_24ч, отправлено_3ч).
    """
    if not settings.subscription_expiry_notify_enabled:
        return 0, 0

    now = datetime.now(timezone.utc)
    stmt = (
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            Subscription.status.in_(("active", "trial")),
            Subscription.expires_at > now,
        )
    )
    r = await session.execute(stmt)
    rows = list(r.scalars().all())

    n24 = 0
    n3 = 0
    for sub in rows:
        user = sub.user
        if user is None or user.is_blocked:
            continue

        _sync_expiry_anchor(sub)
        exp = _utc(sub.expires_at)
        remaining = exp - now
        if remaining.total_seconds() <= 0:
            continue

        exp_s = exp.strftime("%d.%m.%Y %H:%M UTC")

        lo, hi = _WINDOW_24H
        if lo <= remaining <= hi and not sub.expiry_notified_24h:
            text = (
                "⏰ Напоминание: ваша подписка заканчивается примерно через сутки.\n"
                f"Окончание: {exp_s}.\n"
                "Продлите доступ в разделе «Моя подписка» в боте."
            )
            if await send_telegram_message(
                user.telegram_id, text, parse_mode=None, settings=settings
            ):
                sub.expiry_notified_24h = True
                n24 += 1
            else:
                logger.warning("expiry_notify: не удалось отправить 24ч user=%s sub=%s", user.id, sub.id)

        lo3, hi3 = _WINDOW_3H
        if lo3 <= remaining <= hi3 and not sub.expiry_notified_3h:
            text = (
                "⏰ Скоро конец подписки: осталось около 3 часов.\n"
                f"Окончание: {exp_s}.\n"
                "Продлите в боте, чтобы не потерять доступ."
            )
            if await send_telegram_message(
                user.telegram_id, text, parse_mode=None, settings=settings
            ):
                sub.expiry_notified_3h = True
                n3 += 1
            else:
                logger.warning("expiry_notify: не удалось отправить 3ч user=%s sub=%s", user.id, sub.id)

    return n24, n3


async def subscription_expiry_notify_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    interval = max(120, int(settings.subscription_expiry_notify_interval_sec))
    while not stop_event.is_set():
        if not settings.subscription_expiry_notify_enabled:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            factory = get_session_factory()
            async with factory() as session:
                async with session.begin():
                    a, b = await process_subscription_expiry_notifications(session, settings)
                if a or b:
                    logger.info("expiry_notify: отправлено напоминаний 24ч=%s, 3ч=%s", a, b)
        except Exception:
            logger.exception("expiry_notify: итерация не удалась")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
