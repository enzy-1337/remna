"""Активация триала и проверки подписки."""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timedelta, timezone

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError, subscription_url_for_telegram
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.user import User
from shared.services.remnawave_description import build_remnawave_panel_description
from shared.services.remnawave_username import build_remnawave_username
from shared.services.subscription_service import update_rw_user_respecting_hwid_limit


async def get_trial_plan(session: AsyncSession) -> Plan | None:
    r = await session.execute(
        select(Plan).where(Plan.name == "Триал", Plan.is_active.is_(True)).limit(1)
    )
    return r.scalar_one_or_none()


async def has_active_subscription(session: AsyncSession, user_id: int) -> bool:
    now = datetime.now(timezone.utc)
    r = await session.execute(
        select(Subscription.id).where(
            Subscription.user_id == user_id,
            Subscription.status.in_(("active", "trial")),
            Subscription.expires_at > now,
        )
    )
    return r.scalar_one_or_none() is not None


def trial_eligible(user: User, session_result_has_active: bool) -> bool:
    if user.trial_used:
        return False
    if session_result_has_active:
        return False
    return True


async def activate_trial(
    session: AsyncSession,
    *,
    user: User,
    tg_user: TgUser,
    settings: Settings,
    rw: RemnaWaveClient | None = None,
) -> tuple[Subscription, str]:
    """
    Создаёт пользователя в Remnawave, подписку trial в БД, выставляет trial_used.
    Возвращает (subscription, subscription_url).
    """
    if user.trial_used:
        raise ValueError("Триал уже использован")
    if await has_active_subscription(session, user.id):
        raise ValueError("Уже есть активная подписка или триал")

    plan = await get_trial_plan(session)
    if not plan:
        raise RuntimeError("Тариф «Триал» не найден в БД (миграции / seed)")

    client = rw or RemnaWaveClient(settings)
    base_username = build_remnawave_username(tg_user)
    user.username = tg_user.username or user.username
    user.first_name = tg_user.first_name or user.first_name
    user.last_name = tg_user.last_name or user.last_name
    note = build_remnawave_panel_description(user)
    expire_at = datetime.now(timezone.utc) + timedelta(days=settings.trial_duration_days)
    traffic_bytes = settings.trial_traffic_gb * (1024**3)

    squads: list[str] | None = None
    if settings.remnawave_default_squad_uuid:
        squads = [settings.remnawave_default_squad_uuid.strip()]

    created: dict | None = None
    existing = await client.find_user_by_telegram_id(tg_user.id)
    if existing is not None and existing.get("uuid"):
        created = existing
        uid_ex = str(existing["uuid"])
        await update_rw_user_respecting_hwid_limit(
            client,
            uid_ex,
            devices_limit_for_panel=2,
            expire_at=expire_at,
            traffic_limit_bytes=traffic_bytes,
            status="ACTIVE",
            description=note,
            active_internal_squads=squads,
        )
    else:
        for attempt in range(4):
            suffix = "" if attempt == 0 else f"_{attempt}"
            username = (base_username[: 36 - len(suffix)] + suffix)[:36]
            if len(username) < 3:
                username = f"tg_{tg_user.id}"[-36:]
            try:
                created = await client.create_user(
                    username=username,
                    expire_at=expire_at,
                    traffic_limit_bytes=traffic_bytes,
                    description=note,
                    telegram_id=tg_user.id,
                    hwid_device_limit=2,
                    active_internal_squads=squads,
                )
                break
            except RemnaWaveError:
                if attempt == 3:
                    raise
                continue
            except Exception as e:
                raise RemnaWaveError(str(e)) from e

    assert created is not None
    uid_str = created.get("uuid")
    if not uid_str:
        raise RemnaWaveError("Remnawave не вернул uuid пользователя")

    sub_url = created.get("subscriptionUrl") or ""
    if not sub_url and not settings.remnawave_stub:
        try:
            full = await client.get_user(uid_str)
            sub_url = full.get("subscriptionUrl") or ""
        except RemnaWaveError:
            sub_url = ""
    sub_url = subscription_url_for_telegram(sub_url or None, settings) or ""

    rw_uuid = uuid_lib.UUID(str(uid_str))
    user.remnawave_uuid = rw_uuid
    user.trial_used = True

    sub = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        remnawave_sub_uuid=rw_uuid,
        status="trial",
        devices_count=2,
        started_at=datetime.now(timezone.utc),
        expires_at=expire_at,
        auto_renew=False,
    )
    session.add(sub)
    await session.flush()
    return sub, sub_url or "(ссылка недоступна — откройте панель Remnawave)"
