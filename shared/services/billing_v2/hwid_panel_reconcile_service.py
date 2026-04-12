"""Сверка HWID из панели Remnawave с DeviceHistory — если вебхуки не доходят, биллинг всё равно видит устройства."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.user import User
from shared.services.billing_v2.billing_calendar import billing_today
from shared.services.billing_v2.charging_policy import applies_pay_per_use_charges
from shared.services.billing_v2.device_service import add_device_history_event, list_active_device_hwids
from shared.services.billing_v2.rating_service import charge_daily_device_once

logger = logging.getLogger(__name__)


def _hwid_from_panel_row(d: dict) -> str:
    for key in ("hwid", "hwId", "deviceHwid", "hw_id"):
        raw = d.get(key)
        if raw is not None:
            s = str(raw).strip()
            if s:
                return s
    return ""


async def reconcile_hwid_devices_from_panel(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
) -> int:
    """
    Сравнивает активные HWID в панели с последним состоянием DeviceHistory.
    Для новых в панели — пишет user_hwid_devices.added и пытается суточное списание за сегодня (идемпотентно).
    Для исчезнувших из панели — user_hwid_devices.deleted.
    Возвращает число записанных событий (attach+detach).
    """
    if not settings.billing_v2_enabled or not applies_pay_per_use_charges(user, settings):
        return 0
    if user.remnawave_uuid is None:
        return 0

    rw = RemnaWaveClient(settings)
    try:
        devices = await rw.get_user_hwid_devices(str(user.remnawave_uuid))
    except RemnaWaveError as e:
        logger.debug("hwid_reconcile: get_user_hwid_devices failed user_id=%s: %s", user.id, e)
        return 0

    panel_hwids: set[str] = set()
    for d in devices:
        if isinstance(d, dict):
            h = _hwid_from_panel_row(d)
            if h:
                panel_hwids.add(h)

    current_active = set(await list_active_device_hwids(session, user_id=user.id))
    now = datetime.now(timezone.utc)
    today = billing_today(settings)
    n = 0

    for h in sorted(panel_hwids - current_active):
        await add_device_history_event(
            session,
            user_id=user.id,
            subscription_id=None,
            device_hwid=h,
            event_type="user_hwid_devices.added",
            event_ts=now,
            is_active=True,
            meta={"source": "panel_hwid_reconcile"},
        )
        n += 1
        await charge_daily_device_once(
            session,
            user=user,
            device_hwid=h,
            day=today,
            settings=settings,
        )

    for h in sorted(current_active - panel_hwids):
        await add_device_history_event(
            session,
            user_id=user.id,
            subscription_id=None,
            device_hwid=h,
            event_type="user_hwid_devices.deleted",
            event_ts=now,
            is_active=False,
            meta={"source": "panel_hwid_reconcile"},
        )
        n += 1

    if n:
        await session.flush()
    return n


async def reconcile_hwid_devices_for_hybrid_users_sharded(
    session: AsyncSession,
    settings: Settings,
    *,
    phase: int,
    phases: int,
) -> tuple[int, int]:
    """
    Обходит hybrid-пользователей с панелью по шарду user.id % phases == phase.
    Возвращает (число пользователей с попыткой reconcile, число записанных событий attach/detach).
    """
    if not settings.billing_v2_enabled or phases < 1:
        return 0, 0
    ph = phase % phases
    users = list(
        (
            await session.execute(
                select(User).where(
                    User.is_blocked.is_(False),
                    User.billing_mode == "hybrid",
                    User.remnawave_uuid.isnot(None),
                )
            )
        ).scalars()
    )
    touched = 0
    events = 0
    for u in users:
        if int(u.id) % phases != ph:
            continue
        touched += 1
        try:
            events += await reconcile_hwid_devices_from_panel(session, user=u, settings=settings)
        except Exception:
            logger.exception("hwid_reconcile: user_id=%s", u.id)
    return touched, events
