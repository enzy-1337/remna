from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.device_history import DeviceHistory


async def add_device_history_event(
    session: AsyncSession,
    *,
    user_id: int,
    subscription_id: int | None,
    device_hwid: str,
    event_type: str,
    event_ts: datetime,
    is_active: bool,
    meta: dict | None = None,
) -> DeviceHistory:
    row = DeviceHistory(
        user_id=user_id,
        subscription_id=subscription_id,
        device_hwid=device_hwid,
        event_type=event_type,
        is_active=is_active,
        event_ts=event_ts,
        meta=meta or {},
    )
    session.add(row)
    await session.flush()
    return row


async def list_active_device_hwids(session: AsyncSession, *, user_id: int) -> list[str]:
    rows = (
        await session.execute(
            select(DeviceHistory)
            .where(DeviceHistory.user_id == user_id)
            .order_by(DeviceHistory.device_hwid.asc(), DeviceHistory.event_ts.desc(), DeviceHistory.id.desc())
        )
    ).scalars()
    latest_by_hwid: dict[str, bool] = {}
    for row in rows:
        if row.device_hwid not in latest_by_hwid:
            latest_by_hwid[row.device_hwid] = bool(row.is_active)
    return sorted([hwid for hwid, active in latest_by_hwid.items() if active])


async def was_device_billed_today(
    session: AsyncSession,
    *,
    user_id: int,
    device_hwid: str,
    day_start: datetime,
    day_end: datetime,
) -> bool:
    existing = (
        await session.execute(
            select(DeviceHistory.id).where(
                and_(
                    DeviceHistory.user_id == user_id,
                    DeviceHistory.device_hwid == device_hwid,
                    DeviceHistory.event_type == "device_daily_charged",
                    DeviceHistory.event_ts >= day_start,
                    DeviceHistory.event_ts < day_end,
                )
            )
        )
    ).first()
    return existing is not None
