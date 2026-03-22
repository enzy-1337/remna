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
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError, is_remnawave_not_found
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.user import User
from shared.services.plan_seed import ensure_default_plans_if_needed
from shared.services.schema_patches import ensure_subscription_expiry_notify_columns
from shared.services.remnawave_description import (
    build_remnawave_panel_description,
    normalize_remnawave_description,
)

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


def _pick_str(d: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s[:255]
    return None


def _apply_rw_list_row_to_user(user: User, ru: dict) -> None:
    """Подмешиваем в User поля из ответа списка панели (если есть)."""
    fn = _pick_str(
        ru,
        ("telegramFirstName", "telegram_first_name", "firstName", "first_name", "name"),
    )
    if fn:
        user.first_name = fn[:255]

    ln = _pick_str(ru, ("telegramLastName", "telegram_last_name", "lastName", "last_name"))
    if ln:
        user.last_name = ln[:255]

    ph = _pick_str(ru, ("phone", "phoneNumber", "phone_number", "telegramPhone"))
    if ph:
        user.phone = ph[:32]

    tg_un = _pick_str(
        ru,
        ("telegramUsername", "telegram_username", "tgUsername", "tg_username"),
    )
    if tg_un:
        user.username = tg_un.lstrip("@")[:255]
    elif not (user.username or "").strip():
        generic = _pick_str(ru, ("username",))
        if generic:
            user.username = generic.lstrip("@")[:255]


async def _generate_unique_referral_code(session: AsyncSession) -> str:
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        q = await session.execute(select(User.id).where(User.referral_code == code))
        if q.scalar_one_or_none() is None:
            return code
    raise RuntimeError("Не удалось сгенерировать referral_code")


async def _pick_plan_for_import(session: AsyncSession) -> Plan | None:
    base = await session.execute(select(Plan).where(Plan.name == "Базовый", Plan.is_active.is_(True)).limit(1))
    p = base.scalar_one_or_none()
    if p is not None:
        return p
    paid = await session.execute(
        select(Plan).where(Plan.is_active.is_(True), Plan.price_rub > 0).order_by(Plan.sort_order, Plan.id).limit(1)
    )
    return paid.scalar_one_or_none()


async def _upsert_subscription_from_rw_payload(
    session: AsyncSession,
    *,
    user: User,
    info: dict,
    now: datetime,
) -> None:
    exp = _parse_rw_dt(info.get("expireAt"))
    dlim = _int_or_none(info.get("hwidDeviceLimit"))
    if exp is None:
        return
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
            return
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
        logger.info("RW sync: created missing local subscription user=%s", user.id)
    else:
        if abs((sub.expires_at - exp).total_seconds()) > 60:
            logger.info(
                "RW sync: expires adjust user=%s local=%s rw=%s",
                user.id,
                sub.expires_at.isoformat(),
                exp.isoformat(),
            )
            sub.expires_at = exp
        if dlim is not None and dlim >= 2 and sub.devices_count != dlim:
            sub.devices_count = dlim
        sub.status = "active" if exp > now else "expired"


async def _maybe_push_rw_description(
    rw: RemnaWaveClient,
    settings: Settings,
    user: User,
    info: dict,
) -> None:
    if not settings.remnawave_sync_push_description or user.remnawave_uuid is None:
        return
    new_desc = build_remnawave_panel_description(user)
    cur = str(info.get("description") or "")
    if normalize_remnawave_description(cur) == normalize_remnawave_description(new_desc):
        return
    try:
        await rw.update_user(str(user.remnawave_uuid), description=new_desc)
    except RemnaWaveError as e:
        logger.warning("RW sync: push description failed user=%s: %s", user.id, e)


async def _sync_one_linked_user(
    session: AsyncSession,
    rw: RemnaWaveClient,
    settings: Settings,
    user: User,
    now: datetime,
) -> None:
    try:
        info = await rw.get_user(str(user.remnawave_uuid))
    except RemnaWaveError as e:
        if is_remnawave_not_found(e):
            gone = user.remnawave_uuid
            user.remnawave_uuid = None
            logger.info(
                "RW sync: учётная запись в панели не найдена (404), сброшен remnawave_uuid "
                "user=%s uuid=%s",
                user.id,
                gone,
            )
            return
        logger.warning("RW sync get_user failed user=%s: %s", user.id, e)
        return
    await _upsert_subscription_from_rw_payload(session, user=user, info=info, now=now)
    await _maybe_push_rw_description(rw, settings, user, info)


async def sync_once(settings: Settings) -> None:
    if settings.remnawave_stub:
        return
    rw = RemnaWaveClient(settings)
    factory = get_session_factory()
    max_items = max(50, int(settings.remnawave_sync_import_limit))
    async with factory() as session:
        await ensure_default_plans_if_needed(session)
        await ensure_subscription_expiry_notify_columns(session)
        now = datetime.now(timezone.utc)
        try:
            rw_users = await rw.list_all_users(page_size=min(200, max_items), max_items=max_items)
        except RemnaWaveError as e:
            logger.warning("RW sync list_all_users failed: %s", e)
            rw_users = []

        for ru in rw_users:
            uid_raw = ru.get("uuid")
            if not uid_raw:
                continue
            try:
                uid = uuid_lib.UUID(str(uid_raw))
            except ValueError:
                continue

            tg = _int_or_none(ru.get("telegramId") or ru.get("telegram_id") or ru.get("tgId"))

            user: User | None = None
            if tg is not None:
                q = await session.execute(select(User).where(User.telegram_id == tg))
                user = q.scalar_one_or_none()
            if user is None:
                q2 = await session.execute(select(User).where(User.remnawave_uuid == uid))
                user = q2.scalar_one_or_none()

            if user is None:
                if tg is None:
                    continue
                user = User(
                    telegram_id=tg,
                    username=None,
                    first_name=None,
                    last_name=None,
                    language_code=None,
                    referral_code=await _generate_unique_referral_code(session),
                    is_subscribed_channel=True,
                )
                session.add(user)
                await session.flush()
                logger.info("RW sync: imported new user tg=%s db_user=%s", tg, user.id)

            _apply_rw_list_row_to_user(user, ru)
            user.remnawave_uuid = uid

        r = await session.execute(select(User).where(User.remnawave_uuid.is_not(None)))
        linked = list(r.scalars().all())
        for user in linked:
            await _sync_one_linked_user(session, rw, settings, user, now)

        await session.commit()
        logger.info(
            "RW sync: цикл завершён (импорт списка + синхр. %s пользователей с remnawave_uuid)",
            len(linked),
        )


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
