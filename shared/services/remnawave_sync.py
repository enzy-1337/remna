"""Фоновая синхронизация Remnawave <-> локальная БД."""

from __future__ import annotations

import asyncio
import logging
import secrets
import string
import uuid as uuid_lib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.user import User

logger = logging.getLogger(__name__)


def _parse_rw_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        val = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _int_or_none(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


async def _generate_unique_referral_code(session: AsyncSession) -> str:
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        q = await session.execute(select(User.id).where(User.referral_code == code))
        if q.scalar_one_or_none() is None:
            return code
    raise RuntimeError("Не удалось сгенерировать referral_code")


async def _pick_plan_for_import(session: AsyncSession) -> Plan | None:
    trial = await session.execute(select(Plan).where(Plan.name == "Триал", Plan.is_active.is_(True)).limit(1))
    p = trial.scalar_one_or_none()
    if p is not None:
        return p
    paid = await session.execute(
        select(Plan).where(Plan.is_active.is_(True), Plan.price_rub > 0).order_by(Plan.sort_order, Plan.id).limit(1)
    )
    return paid.scalar_one_or_none()


async def sync_once(settings: Settings) -> None:
    if settings.remnawave_stub:
        return
    rw = RemnaWaveClient(settings)
    factory = get_session_factory()
    async with factory() as session:
        now = datetime.now(timezone.utc)
        # 1) Синхронизируем уже привязанных пользователей.
        r = await session.execute(select(User).where(User.remnawave_uuid.is_not(None)))
        for user in list(r.scalars().all()):
            try:
                info = await rw.get_user(str(user.remnawave_uuid))
            except RemnaWaveError as e:
                logger.warning("RW sync get_user failed user=%s: %s", user.id, e)
                continue
            exp = _parse_rw_dt(info.get("expireAt"))
            dlim = _int_or_none(info.get("hwidDeviceLimit"))
            if exp is None:
                continue
            sub_q = await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
                .limit(1)
            )
            sub = sub_q.scalar_one_or_none()
            if sub is None:
                plan = await _pick_plan_for_import(session)
                if plan is None:
                    logger.warning("RW sync: no plan for creating sub user=%s", user.id)
                    continue
                sub = Subscription(
                    user_id=user.id,
                    plan_id=plan.id,
                    remnawave_sub_uuid=user.remnawave_uuid,
                    status="active" if exp > now else "expired",
                    devices_count=max(2, dlim or 2),
                    started_at=now,
                    expires_at=exp,
                    auto_renew=True,
                )
                session.add(sub)
                logger.warning("RW sync: created missing local subscription user=%s", user.id)
            else:
                if abs((sub.expires_at - exp).total_seconds()) > 60:
                    logger.warning(
                        "RW sync: expires mismatch user=%s local=%s rw=%s",
                        user.id,
                        sub.expires_at.isoformat(),
                        exp.isoformat(),
                    )
                    sub.expires_at = exp
                if dlim is not None and dlim >= 2 and sub.devices_count != dlim:
                    sub.devices_count = dlim
                sub.status = "active" if exp > now else "expired"

        # 2) Импортируем пользователей, которые есть в панели, но нет в БД.
        limit = max(50, int(settings.remnawave_sync_import_limit))
        try:
            rw_users = await rw.list_users(limit=limit)
        except RemnaWaveError as e:
            logger.warning("RW sync list_users failed: %s", e)
            rw_users = []

        for ru in rw_users[:limit]:
            tg = _int_or_none(ru.get("telegramId") or ru.get("telegram_id") or ru.get("tgId"))
            uid = ru.get("uuid")
            if tg is None or not uid:
                continue
            q = await session.execute(select(User).where(User.telegram_id == tg))
            user = q.scalar_one_or_none()
            if user is None:
                user = User(
                    telegram_id=tg,
                    username=ru.get("username"),
                    first_name=None,
                    last_name=None,
                    language_code=None,
                    referral_code=await _generate_unique_referral_code(session),
                    is_subscribed_channel=True,
                )
                session.add(user)
                await session.flush()
                logger.warning("RW sync: imported new user tg=%s db_user=%s", tg, user.id)
            user.remnawave_uuid = uuid_lib.UUID(str(uid))

        await session.commit()


async def sync_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    interval = max(60, int(settings.remnawave_sync_interval_sec))
    while not stop_event.is_set():
        try:
            await sync_once(settings)
        except Exception:
            logger.exception("RW sync loop iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
