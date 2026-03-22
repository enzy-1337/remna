"""Покупка/продление подписки с баланса, синхронизация Remnawave, устройства."""

from __future__ import annotations

import logging
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.config import Settings
from shared.md2 import bold, esc, join_lines, link, plain
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.device import Device
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.remnawave_description import build_remnawave_panel_description
from shared.services.remnawave_username import build_remnawave_username_from_db_user
from shared.services.smart_cart import set_cart_plan

logger = logging.getLogger(__name__)

MIN_DEVICES = 2
MAX_DEVICES = 10


def plan_tariff_button_label(plan: Plan) -> str:
    """Текст кнопки тарифа со скидкой, напр.: «3 месяца — 370 ₽ (-5%)»."""
    name = (plan.name or "")[:28]
    price = plan.price_rub
    price_s = str(int(price)) if price == price.to_integral_value() else str(price)
    disc = plan.discount_percent
    if disc and disc > 0:
        d = int(disc) if disc == disc.to_integral_value() else float(disc)
        return f"{name} — {price_s} ₽ (-{d:g}%)"
    return f"{name} — {price_s} ₽"


BASE_SUBSCRIPTION_PLAN_NAME = "Базовый"


async def get_base_subscription_plan(session: AsyncSession) -> Plan | None:
    """План учётной подписки и суммы автопродления (+1 мес.)."""
    r = await session.execute(
        select(Plan).where(Plan.name == BASE_SUBSCRIPTION_PLAN_NAME, Plan.is_active.is_(True)).limit(1)
    )
    return r.scalar_one_or_none()


async def list_paid_plans(session: AsyncSession) -> list[Plan]:
    r = await session.execute(
        select(Plan)
        .where(
            Plan.is_active.is_(True),
            Plan.price_rub > 0,
            Plan.name != BASE_SUBSCRIPTION_PLAN_NAME,
        )
        .order_by(Plan.sort_order, Plan.id)
    )
    return list(r.scalars().all())


async def get_active_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    now = datetime.now(timezone.utc)
    r = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(("active", "trial")),
            Subscription.expires_at > now,
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    return r.scalar_one_or_none()


async def count_devices(session: AsyncSession, subscription_id: int) -> int:
    r = await session.execute(
        select(func.count()).select_from(Device).where(Device.subscription_id == subscription_id)
    )
    return int(r.scalar_one() or 0)


async def ensure_placeholder_devices(session: AsyncSession, sub: Subscription) -> None:
    n = await count_devices(session, sub.id)
    need = max(0, sub.devices_count - n)
    for i in range(need):
        idx = n + i + 1
        session.add(
            Device(
                subscription_id=sub.id,
                user_id=sub.user_id,
                name=f"Устройство {idx}",
            )
        )
    if need > 0:
        await session.flush()


async def _create_rw_user_retries(
    rw: RemnaWaveClient,
    *,
    base_username: str,
    telegram_id: int,
    expire_at: datetime,
    traffic_limit_bytes: int,
    description: str,
    hwid_device_limit: int,
    active_internal_squads: list[str] | None,
) -> dict:
    base = base_username
    last: Exception | None = None
    for attempt in range(4):
        suffix = "" if attempt == 0 else f"_{attempt}"
        uname = (base[: 36 - len(suffix)] + suffix)[:36]
        if len(uname) < 3:
            uname = f"tg_{telegram_id}"[-36:]
        try:
            return await rw.create_user(
                username=uname,
                expire_at=expire_at,
                traffic_limit_bytes=traffic_limit_bytes,
                description=description,
                telegram_id=telegram_id,
                hwid_device_limit=hwid_device_limit,
                active_internal_squads=active_internal_squads,
            )
        except RemnaWaveError as e:
            last = e
            if attempt == 3:
                raise
    raise RemnaWaveError(str(last))


async def purchase_plan_with_balance(
    session: AsyncSession,
    *,
    user: User,
    plan_id: int,
    telegram_id: int,
    settings: Settings,
    save_to_cart_if_insufficient: bool = True,
) -> tuple[bool, str, str]:
    """
    Покупка тарифа с баланса.
    Возвращает (ok, message, kind) где kind: success | insufficient | error
    """
    plan = await session.get(Plan, plan_id)
    if not plan or plan.price_rub <= 0 or not plan.is_active:
        return False, plain("Тариф не найден или недоступен."), "error"
    if plan.name == BASE_SUBSCRIPTION_PLAN_NAME:
        return False, plain("Этот тариф недоступен для покупки в магазине."), "error"

    purchased_plan = plan
    base_plan = await get_base_subscription_plan(session)
    if base_plan is None:
        return False, plain("В БД не настроен тариф «Базовый» (seed планов)."), "error"

    price = purchased_plan.price_rub
    if user.balance < price:
        if save_to_cart_if_insufficient:
            await set_cart_plan(telegram_id, plan_id=plan.id, amount_rub=price, settings=settings)
        need = price - user.balance
        return (
            False,
            join_lines(
                plain("Недостаточно средств: нужно ")
                + bold(str(price))
                + plain(" ₽, не хватает ")
                + bold(str(need))
                + plain(" ₽.")
            ),
            "insufficient",
        )

    rw = RemnaWaveClient(settings)
    squads: list[str] | None = None
    if settings.remnawave_default_squad_uuid:
        squads = [settings.remnawave_default_squad_uuid.strip()]

    now = datetime.now(timezone.utc)
    active = await get_active_subscription(session, user.id)
    dev_limit = active.devices_count if active else MIN_DEVICES

    base = now
    if active and active.expires_at > now:
        base = active.expires_at
    new_expires = base + timedelta(days=purchased_plan.duration_days)

    traffic_bytes = 0
    if purchased_plan.traffic_limit_gb is not None and purchased_plan.traffic_limit_gb > 0:
        traffic_bytes = int(purchased_plan.traffic_limit_gb) * (1024**3)

    desc = build_remnawave_panel_description(user)

    try:
        if user.remnawave_uuid is None:
            existing = await rw.find_user_by_telegram_id(user.telegram_id)
            if existing is not None and existing.get("uuid"):
                user.remnawave_uuid = uuid_lib.UUID(str(existing["uuid"]))
            else:
                uname = build_remnawave_username_from_db_user(user)
                created = await _create_rw_user_retries(
                    rw,
                    base_username=uname,
                    telegram_id=user.telegram_id,
                    expire_at=new_expires,
                    traffic_limit_bytes=traffic_bytes,
                    description=desc,
                    hwid_device_limit=dev_limit,
                    active_internal_squads=squads,
                )
                uid = created.get("uuid")
                if not uid:
                    raise RemnaWaveError("Панель не вернула uuid пользователя")
                user.remnawave_uuid = uuid_lib.UUID(str(uid))
            await rw.update_user(
                str(user.remnawave_uuid),
                expire_at=new_expires,
                hwid_device_limit=dev_limit,
                traffic_limit_bytes=traffic_bytes,
                status="ACTIVE",
                description=desc,
                active_internal_squads=squads,
            )
        else:
            await rw.update_user(
                str(user.remnawave_uuid),
                expire_at=new_expires,
                hwid_device_limit=dev_limit,
                traffic_limit_bytes=traffic_bytes,
                status="ACTIVE",
                description=desc,
                active_internal_squads=squads,
            )
    except RemnaWaveError as e:
        logger.exception("Remnawave purchase/extend failed")
        return False, join_lines(plain("Не удалось обновить доступ VPN:"), esc(str(e))), "error"

    user.balance -= price
    session.add(
        Transaction(
            user_id=user.id,
            type="subscription",
            amount=price,
            currency="RUB",
            payment_provider="balance",
            payment_id=None,
            status="completed",
            description=f"Тариф «{purchased_plan.name}»",
            meta={
                "plan_id": purchased_plan.id,
                "purchased_plan_id": purchased_plan.id,
                "storage_plan_id": base_plan.id,
            },
        )
    )

    rw_uuid = user.remnawave_uuid
    assert rw_uuid is not None

    if active:
        active.plan_id = base_plan.id
        active.expires_at = new_expires
        active.status = "active"
        active.remnawave_sub_uuid = rw_uuid
        active.auto_renew = True
        sub = active
    else:
        sub = Subscription(
            user_id=user.id,
            plan_id=base_plan.id,
            remnawave_sub_uuid=rw_uuid,
            status="active",
            devices_count=MIN_DEVICES,
            started_at=now,
            expires_at=new_expires,
            auto_renew=True,
        )
        session.add(sub)
        await session.flush()
        for i in range(1, MIN_DEVICES + 1):
            session.add(
                Device(subscription_id=sub.id, user_id=user.id, name=f"Устройство {i}")
            )

    await ensure_placeholder_devices(session, sub)
    await session.flush()

    sub_url = ""
    try:
        uinf = await rw.get_user(str(rw_uuid))
        sub_url = uinf.get("subscriptionUrl") or ""
    except RemnaWaveError:
        pass

    msg = join_lines(
        plain("✅ Списано ")
        + bold(str(price))
        + plain(" ₽ с баланса."),
        plain("Оплачен пакет: ") + bold(purchased_plan.name),
        plain("Учётный тариф: ") + bold(base_plan.name),
        plain("Действует до: ")
        + bold(new_expires.strftime("%d.%m.%Y %H:%M") + " UTC"),
    )
    if sub_url:
        msg += "\n\n" + link("Ссылка подписки", sub_url)

    from shared.services.admin_notify import notify_admin
    from shared.services.referral_service import grant_referrer_reward_first_paid_plan

    await grant_referrer_reward_first_paid_plan(session, buyer=user, plan=purchased_plan, settings=settings)
    from shared.services.admin_log_topics import AdminLogTopic

    await notify_admin(
        settings,
        title="🔑 " + bold("Покупка тарифа с баланса"),
        lines=[
            plain("Пакет: ") + bold(purchased_plan.name),
            plain("Списано: ") + bold(str(price)) + plain(" ₽"),
            plain("До: ") + bold(new_expires.strftime("%d.%m.%Y %H:%M") + " UTC"),
        ],
        event_type="purchase_plan",
        topic=AdminLogTopic.SUBSCRIPTIONS,
        subject_user=user,
        session=session,
    )
    return True, msg, "success"


async def set_subscription_auto_renew(
    session: AsyncSession,
    user_id: int,
    enabled: bool,
) -> tuple[bool, str]:
    sub = await get_active_subscription(session, user_id)
    if not sub:
        return False, plain("Нет активной подписки.")
    sub.auto_renew = enabled
    return True, (
        plain("Авто-продление включено.") if enabled else plain("Авто-продление выключено.")
    )


async def add_paid_device_slot(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
) -> tuple[bool, str]:
    sub = await get_active_subscription(session, user.id)
    if not sub:
        return False, plain("Сначала оформите подписку.")
    if sub.devices_count >= MAX_DEVICES:
        return False, plain("Уже максимум слотов: ") + bold(str(MAX_DEVICES)) + plain(".")

    price = settings.extra_device_price_rub
    if user.balance < price:
        return (
            False,
            plain("Нужно ")
            + bold(str(price))
            + plain(" ₽ на балансе для дополнительного устройства."),
        )

    if user.remnawave_uuid is None:
        return False, plain("Нет учётной записи VPN. Активируйте триал или купите подписку.")

    rw = RemnaWaveClient(settings)
    new_limit = sub.devices_count + 1
    try:
        await rw.update_user(str(user.remnawave_uuid), hwid_device_limit=new_limit)
    except RemnaWaveError as e:
        return False, join_lines(plain("Панель VPN:"), esc(str(e)))

    new_idx = await count_devices(session, sub.id) + 1
    sub.devices_count = new_limit
    user.balance -= price
    session.add(
        Device(
            subscription_id=sub.id,
            user_id=user.id,
            name=f"Устройство {new_idx}",
        )
    )
    session.add(
        Transaction(
            user_id=user.id,
            type="manual_add",
            amount=price,
            currency="RUB",
            payment_provider="balance",
            payment_id=None,
            status="completed",
            description="Дополнительное устройство",
            meta={"subscription_id": sub.id},
        )
    )
    await session.flush()
    return True, join_lines(
        plain("Добавлен слот устройства (−")
        + bold(str(price))
        + plain(" ₽)."),
        plain("Всего слотов: ") + bold(str(sub.devices_count)) + plain("."),
    )


async def remove_hwid_device_from_panel(
    session: AsyncSession,
    *,
    user: User,
    hwid: str,
    settings: Settings,
) -> tuple[bool, str]:
    """Удалить устройство в Remnawave (HWID API) и синхронизировать лимит слотов в боте."""
    sub = await get_active_subscription(session, user.id)
    if not sub:
        return False, plain("Нет активной подписки.")
    if sub.devices_count <= MIN_DEVICES:
        return False, plain(f"Минимум {MIN_DEVICES} устройства — отвязка недоступна.")

    if user.remnawave_uuid is None:
        return False, plain("Ошибка профиля VPN.")

    hwid = (hwid or "").strip()
    if not hwid:
        return False, plain("Некорректный HWID.")

    rw = RemnaWaveClient(settings)
    try:
        await rw.delete_user_hwid_device(str(user.remnawave_uuid), hwid)
    except RemnaWaveError as e:
        return False, join_lines(plain("Панель VPN:"), esc(str(e)))

    new_limit = sub.devices_count - 1
    try:
        await rw.update_user(str(user.remnawave_uuid), hwid_device_limit=new_limit)
    except RemnaWaveError as e:
        return False, join_lines(plain("Устройство снято, но лимит слотов не обновлён:"), esc(str(e)))

    sub.devices_count = new_limit
    r = await session.execute(
        select(Device).where(
            Device.user_id == user.id,
            Device.subscription_id == sub.id,
            Device.remnawave_client_id == hwid,
        )
    )
    for row in r.scalars().all():
        await session.delete(row)
    await session.flush()
    return True, join_lines(
        plain("Устройство отвязано."),
        plain("Слотов: ") + bold(str(sub.devices_count)) + plain("."),
    )


async def remove_device_slot(
    session: AsyncSession,
    *,
    user: User,
    device_id: int,
    settings: Settings,
) -> tuple[bool, str]:
    sub = await get_active_subscription(session, user.id)
    if not sub:
        return False, plain("Нет активной подписки.")
    if sub.devices_count <= MIN_DEVICES:
        return False, plain(f"Минимум {MIN_DEVICES} устройства — удаление недоступно.")

    dev = await session.get(Device, device_id)
    if dev is None or dev.user_id != user.id or dev.subscription_id != sub.id:
        return False, plain("Устройство не найдено.")

    if user.remnawave_uuid is None:
        return False, plain("Ошибка профиля VPN.")

    rw = RemnaWaveClient(settings)
    new_limit = sub.devices_count - 1
    try:
        await rw.update_user(str(user.remnawave_uuid), hwid_device_limit=new_limit)
    except RemnaWaveError as e:
        return False, join_lines(plain("Панель VPN:"), esc(str(e)))

    sub.devices_count = new_limit
    await session.delete(dev)
    await session.flush()
    return True, join_lines(
        plain("Устройство удалено."),
        plain("Слотов: ") + bold(str(sub.devices_count)) + plain("."),
    )


async def list_user_devices(session: AsyncSession, subscription_id: int) -> list[Device]:
    r = await session.execute(
        select(Device).where(Device.subscription_id == subscription_id).order_by(Device.id)
    )
    return list(r.scalars().all())
