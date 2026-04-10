"""Суточное списание за активные устройства по календарным дням в billing_calendar_timezone."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.billing_cron_checkpoint import BillingCronCheckpoint
from shared.models.device_history import DeviceHistory
from shared.models.user import User
from shared.services.billing_v2.billing_calendar import (
    billing_local_day_end_utc_exclusive,
    billing_today,
)
from shared.services.billing_v2.charging_policy import applies_pay_per_use_charges
from shared.services.billing_v2.rating_service import charge_daily_device_once

logger = logging.getLogger(__name__)

JOB_DEVICE_DAILY = "device_daily_local"


async def active_hwids_at_or_before(
    session: AsyncSession,
    *,
    user_id: int,
    end_ts: datetime,
) -> list[str]:
    """Состояние HWID на момент end_ts: последнее событие по каждому hwid, активен ли."""
    rows = list(
        (
            await session.execute(
                select(DeviceHistory).where(
                    DeviceHistory.user_id == user_id,
                    DeviceHistory.event_ts <= end_ts,
                )
            )
        ).scalars()
    )
    best: dict[str, tuple[datetime, int, DeviceHistory]] = {}
    for row in rows:
        hw = row.device_hwid
        key = (row.event_ts, row.id)
        cur = best.get(hw)
        if cur is None or key > (cur[0], cur[1]):
            best[hw] = key + (row,)
    return sorted(hw for hw, (_, _, r) in best.items() if r.is_active)


async def process_device_daily_for_local_calendar_day(
    session: AsyncSession,
    *,
    local_day: date,
    settings: Settings,
) -> int:
    """
    Для каждого hybrid-пользователя — charge_daily_device_once по каждому HWID,
    активному на конец local_day (в billing_calendar_timezone).
    Идемпотентность — внутри charge_daily_device_once.
    """
    end_local_exclusive = billing_local_day_end_utc_exclusive(settings, local_day)
    end_ts = end_local_exclusive - timedelta(microseconds=1)

    users = list(
        (
            await session.execute(
                select(User).where(User.is_blocked.is_(False), User.billing_mode == "hybrid")
            )
        ).scalars()
    )
    n = 0
    for user in users:
        if not applies_pay_per_use_charges(user, settings):
            continue
        hwids = await active_hwids_at_or_before(session, user_id=user.id, end_ts=end_ts)
        if not hwids:
            continue
        for hwid in hwids:
            ok = await charge_daily_device_once(
                session,
                user=user,
                device_hwid=hwid,
                day=local_day,
                settings=settings,
                eval_at=end_ts,
                active_hwids_for_package=hwids,
            )
            if ok:
                n += 1
    return n


async def advance_device_daily_checkpoint(
    session: AsyncSession,
    *,
    local_day: date,
) -> None:
    row = await session.get(BillingCronCheckpoint, JOB_DEVICE_DAILY)
    if row is None:
        session.add(BillingCronCheckpoint(job_name=JOB_DEVICE_DAILY, last_completed_day=local_day))
    else:
        row.last_completed_day = local_day
    await session.flush()


async def get_last_device_daily_completed_day(session: AsyncSession) -> date | None:
    row = await session.get(BillingCronCheckpoint, JOB_DEVICE_DAILY)
    return row.last_completed_day if row is not None else None


async def catch_up_device_daily_charges(session: AsyncSession, settings: Settings) -> int:
    """
    После полуночи (локального календаря) обрабатывает все пропущенные дни до «вчера» включительно.
    Первый запуск без чекпойнта: начинает с «вчера» (без ретроактивного начисления за месяцы назад).
    """
    today_local = billing_today(settings)
    yesterday = today_local - timedelta(days=1)
    last = await get_last_device_daily_completed_day(session)
    if last is None:
        start = yesterday
    else:
        start = last + timedelta(days=1)
    if start > yesterday:
        return 0
    total = 0
    d = start
    while d <= yesterday:
        n = await process_device_daily_for_local_calendar_day(session, local_day=d, settings=settings)
        total += n
        await advance_device_daily_checkpoint(session, local_day=d)
        logger.info("device_daily_batch: completed local_day=%s tally=%s", d, n)
        d += timedelta(days=1)
    return total
