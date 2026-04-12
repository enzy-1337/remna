"""Списание ГБ по опросу панели: число шагов = ceil(used_gb), синхронно с Remnawave get_user."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.integrations.rw_traffic import extract_traffic_gb_from_rw_user
from shared.models.billing_traffic_meter import BillingTrafficMeter
from shared.models.billing_usage_event import BillingUsageEvent
from shared.models.user import User
from shared.services.billing_v2.charging_policy import applies_pay_per_use_charges
from shared.services.billing_v2.rating_service import charge_gb_step

logger = logging.getLogger(__name__)

_MAX_CHARGES_PER_USER_PER_TICK = 100
_EPS = 1e-9


def gb_steps_due_from_used_gb(used_gb: float | None) -> int:
    """Полные «потолки» гигабайта: 0 → 0, 0.01 → 1, 1.0 → 1, 1.001 → 2."""
    if used_gb is None:
        return 0
    try:
        u = float(used_gb)
    except (TypeError, ValueError):
        return 0
    if u <= _EPS:
        return 0
    return min(_MAX_CHARGES_PER_USER_PER_TICK * 10, int(math.ceil(u - _EPS)))


async def _count_traffic_gb_step_events(session: AsyncSession, user_id: int) -> int:
    r = await session.execute(
        select(func.count())
        .select_from(BillingUsageEvent)
        .where(
            BillingUsageEvent.user_id == user_id,
            BillingUsageEvent.event_type == "traffic_gb_step",
        )
    )
    return int(r.scalar_one() or 0)


async def sync_user_traffic_meter_from_panel(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
) -> int:
    """
    Сверяет used_gb из панели с BillingTrafficMeter.charged_gb_steps; при росте — вызывает charge_gb_step
    с event_id ``traffic_meter:{user_id}:{step}``. При падении usage в панели — уменьшает счётчик без возврата денег.

    Для новой строки счётчика: charged_gb_steps = min(steps_due, число уже записанных traffic_gb_step),
    чтобы не дублировать списания после миграции с вебхуков.

    Возвращает число успешных новых списаний за этот вызов.
    """
    if not settings.billing_v2_enabled or not settings.billing_traffic_rw_meter_enabled:
        return 0
    if not applies_pay_per_use_charges(user, settings):
        return 0
    if user.remnawave_uuid is None:
        return 0

    rw = RemnaWaveClient(settings)
    try:
        uinf = await rw.get_user(str(user.remnawave_uuid))
    except RemnaWaveError as e:
        logger.debug("traffic_meter: get_user failed user_id=%s: %s", user.id, e)
        return 0

    used_gb, _lim = extract_traffic_gb_from_rw_user(uinf)
    steps_due = gb_steps_due_from_used_gb(used_gb)

    meter = (
        await session.execute(select(BillingTrafficMeter).where(BillingTrafficMeter.user_id == user.id).limit(1))
    ).scalar_one_or_none()
    if meter is None:
        legacy = await _count_traffic_gb_step_events(session, user.id)
        initial = min(steps_due, legacy)
        meter = BillingTrafficMeter(user_id=user.id, charged_gb_steps=initial)
        session.add(meter)
        await session.flush()

    if steps_due < meter.charged_gb_steps:
        meter.charged_gb_steps = steps_due
        await session.flush()
        return 0

    now = datetime.now(timezone.utc)
    charges = 0
    while meter.charged_gb_steps < steps_due and charges < _MAX_CHARGES_PER_USER_PER_TICK:
        next_step = meter.charged_gb_steps + 1
        event_id = f"traffic_meter:{user.id}:{next_step}"
        ok = await charge_gb_step(
            session,
            user=user,
            event_id=event_id,
            event_ts=now,
            is_mobile_internet=False,
            settings=settings,
        )
        if not ok:
            break
        meter.charged_gb_steps = next_step
        charges += 1
        await session.flush()

    return charges


async def sync_traffic_meter_for_hybrid_users_sharded(
    session: AsyncSession,
    settings: Settings,
    *,
    phase: int,
    phases: int,
) -> tuple[int, int]:
    """
    Hybrid-пользователи с панелью: шард user.id % phases == phase.
    Возвращает (число обработанных пользователей, суммарное число успешных списаний шага).
    """
    if not settings.billing_v2_enabled or not settings.billing_traffic_rw_meter_enabled or phases < 1:
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
    total_charges = 0
    for u in users:
        if int(u.id) % phases != ph:
            continue
        touched += 1
        try:
            total_charges += await sync_user_traffic_meter_from_panel(session, user=u, settings=settings)
        except Exception:
            logger.exception("traffic_meter: user_id=%s", u.id)
    return touched, total_charges
