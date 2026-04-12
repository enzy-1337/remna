"""Покупка/продление подписки с баланса, синхронизация Remnawave, устройства."""

from __future__ import annotations

import logging
import uuid as uuid_lib
from typing import Any
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.config import Settings
from shared.md2 import bold, esc, join_lines, link, plain
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError, subscription_url_for_telegram
from shared.integrations.rw_traffic import should_apply_hwid_device_limit_to_panel
from shared.models.device import Device
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.remnawave_description import build_remnawave_panel_description
from shared.services.remnawave_username import build_remnawave_username_from_db_user
from shared.services.smart_cart import set_cart_plan
from shared.services.billing_v2.device_service import add_device_history_event
from shared.services.promo_service import get_pending_purchase_discount_percent
from shared.services.optimized_route_service import remnawave_squads_for_db_user
from shared.services.referral_service import grant_referrer_percent_of_referred_payment

logger = logging.getLogger(__name__)

MIN_DEVICES = 2
MAX_DEVICES = 10


async def update_rw_user_respecting_hwid_limit(
    rw: RemnaWaveClient,
    user_uuid: str,
    *,
    devices_limit_for_panel: int | None = None,
    **kwargs: Any,
) -> None:
    """
    PATCH пользователя в панели. ``hwidDeviceLimit`` добавляется только если в панели
    для этого пользователя не отключён лимит HWID (см. ``should_apply_hwid_device_limit_to_panel``).
    """
    uinf: dict[str, Any] | None = None
    try:
        uinf = await rw.get_user(user_uuid)
    except RemnaWaveError:
        pass
    if devices_limit_for_panel is not None and should_apply_hwid_device_limit_to_panel(uinf):
        kwargs["hwid_device_limit"] = devices_limit_for_panel
    if not kwargs:
        return
    await rw.update_user(user_uuid, **kwargs)


def plan_tariff_button_label(plan: Plan) -> str:
    """Текст кнопки тарифа со скидкой, напр.: «3 месяца — 370 ₽ (-5%)»."""
    name = (plan.name or "")[:28]
    price = plan.price_rub
    price_s = str(int(price)) if price == price.to_integral_value() else str(price)
    disc = plan.discount_percent
    suffix = ""
    if plan.is_package_monthly:
        dev = f"{plan.device_limit}" if plan.device_limit is not None else "∞"
        gb = f"{plan.monthly_gb_limit}" if plan.monthly_gb_limit is not None else "∞"
        suffix = f" · {dev} устр / {gb} ГБ"
    if disc and disc > 0:
        d = int(disc) if disc == disc.to_integral_value() else float(disc)
        return f"{name} — {price_s} ₽ (-{d:g}%){suffix}"
    return f"{name} — {price_s} ₽{suffix}"


def plan_tariff_button_label_with_discount(plan: Plan, discount_percent: Decimal) -> str:
    base = plan_tariff_button_label(plan)
    if discount_percent <= 0:
        return base
    original = plan.price_rub
    discount_amount = (original * discount_percent / Decimal("100")).quantize(Decimal("0.01"))
    final = (original - discount_amount).quantize(Decimal("0.01"))
    if final < 0:
        final = Decimal("0")
    return f"{base} → {final} ₽"


def calculate_discounted_plan_price(plan: Plan, discount_percent: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    original = plan.price_rub
    if discount_percent <= 0:
        return original, Decimal("0"), original
    discount_amount = (original * discount_percent / Decimal("100")).quantize(Decimal("0.01"))
    final = (original - discount_amount).quantize(Decimal("0.01"))
    if final < 0:
        final = Decimal("0")
    return original, discount_amount, final


BASE_SUBSCRIPTION_PLAN_NAME = "Базовый"
TRIAL_PLAN_NAME = "Триал"


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
    idempotency_key: str | None = None,
) -> tuple[bool, str, str]:
    """
    Покупка тарифа с баланса.
    Возвращает (ok, message, kind) где kind: success | insufficient | error
    """
    if idempotency_key:
        existing_txn = (
            await session.execute(
                select(Transaction)
                .where(
                    Transaction.user_id == user.id,
                    Transaction.type == "subscription",
                    Transaction.payment_id == idempotency_key,
                    Transaction.status == "completed",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing_txn is not None:
            return True, plain("Покупка уже была подтверждена ранее."), "success"

    plan = await session.get(Plan, plan_id)
    if not plan or plan.price_rub <= 0 or not plan.is_active:
        return False, plain("Тариф не найден или недоступен."), "error"
    if plan.name == BASE_SUBSCRIPTION_PLAN_NAME:
        return False, plain("Этот тариф недоступен для покупки в магазине."), "error"
    if plan.name == TRIAL_PLAN_NAME:
        return False, plain("Тариф «Триал» недоступен для покупки в магазине."), "error"

    purchased_plan = plan
    base_plan = await get_base_subscription_plan(session)
    if base_plan is None:
        return False, plain("В БД не настроен тариф «Базовый» (seed планов)."), "error"

    original_price = purchased_plan.price_rub
    price = original_price
    discount_usage, discount_percent = await get_pending_purchase_discount_percent(session, user_id=user.id)
    discount_amount = Decimal("0")
    if discount_percent > 0:
        discount_amount = (price * discount_percent / Decimal("100")).quantize(Decimal("0.01"))
        price = (price - discount_amount).quantize(Decimal("0.01"))
    if user.balance - price < settings.billing_balance_floor_rub:
        if save_to_cart_if_insufficient:
            await set_cart_plan(telegram_id, plan_id=plan.id, amount_rub=price, settings=settings)
        need = (price - user.balance).quantize(Decimal("0.01"))
        return (
            False,
            join_lines(
                plain("Недостаточно доступного лимита: нужно ")
                + bold(str(price))
                + plain(" ₽, не хватает ")
                + bold(str(need))
                + plain(" ₽.")
            ),
            "insufficient",
        )

    rw = RemnaWaveClient(settings)
    squads = remnawave_squads_for_db_user(settings, user)

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
        await update_rw_user_respecting_hwid_limit(
            rw,
            str(user.remnawave_uuid),
            devices_limit_for_panel=dev_limit,
            expire_at=new_expires,
            traffic_limit_bytes=traffic_bytes,
            status="ACTIVE",
            description=desc,
            active_internal_squads=squads,
        )
    except RemnaWaveError as e:
        logger.exception("Remnawave purchase/extend failed")
        return False, join_lines(plain("Не удалось обновить доступ VPN:"), esc(str(e))), "error"

    user.balance -= price
    purchase_txn = Transaction(
        user_id=user.id,
        type="subscription",
        amount=price,
        currency="RUB",
        payment_provider="balance",
        payment_id=idempotency_key,
        status="completed",
        description=f"Тариф «{purchased_plan.name}»",
        meta={
            "plan_id": purchased_plan.id,
            "purchased_plan_id": purchased_plan.id,
            "storage_plan_id": base_plan.id,
            "original_price_rub": str(original_price),
            "final_price_rub": str(price),
            "discount_percent": str(discount_percent),
            "discount_amount_rub": str(discount_amount),
        },
    )
    session.add(purchase_txn)
    await session.flush()

    if user.billing_mode == "hybrid" and settings.billing_v2_enabled:
        from shared.services.billing_v2.balance_floor_panel_service import sync_hybrid_balance_floor_panel_state

        await sync_hybrid_balance_floor_panel_state(session, user, settings)

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
    if discount_usage is not None:
        discount_usage.topup_bonus_applied_at = datetime.now(timezone.utc)
    await session.flush()

    sub_url = ""
    try:
        uinf = await rw.get_user(str(rw_uuid))
        sub_url = subscription_url_for_telegram(uinf.get("subscriptionUrl"), settings) or ""
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
    if discount_percent > 0 and discount_amount > 0:
        msg = join_lines(
            msg,
            plain("Промокод скидки: ")
            + bold(str(discount_percent))
            + plain("% (−")
            + bold(str(discount_amount))
            + plain(" ₽)."),
            plain("Цена без скидки: ")
            + bold(str(original_price))
            + plain(" ₽."),
        )
    if sub_url:
        msg += "\n\n" + link("Ссылка подписки", sub_url)

    from shared.services.admin_notify import notify_admin

    await grant_referrer_percent_of_referred_payment(
        session,
        referred_user=user,
        payment_amount_rub=price,
        settings=settings,
        idempotency_key=f"referral_pct:subscription:{purchase_txn.id}",
        reward_source="payment_pct_plan",
    )
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
    idempotency_key: str | None = None,
) -> tuple[bool, str]:
    if idempotency_key:
        existing_txn = (
            await session.execute(
                select(Transaction)
                .where(
                    Transaction.user_id == user.id,
                    Transaction.type == "manual_add",
                    Transaction.payment_id == idempotency_key,
                    Transaction.status == "completed",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing_txn is not None:
            return True, plain("Слот уже был добавлен ранее по этому подтверждению.")

    sub = await get_active_subscription(session, user.id)
    if not sub:
        return False, plain("Сначала оформите подписку.")
    if sub.devices_count >= MAX_DEVICES:
        return False, plain("Уже максимум слотов: ") + bold(str(MAX_DEVICES)) + plain(".")

    price = settings.extra_device_price_rub
    if user.balance - price < settings.billing_balance_floor_rub:
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
        await update_rw_user_respecting_hwid_limit(
            rw,
            str(user.remnawave_uuid),
            devices_limit_for_panel=new_limit,
        )
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
    slot_txn = Transaction(
        user_id=user.id,
        type="manual_add",
        amount=price,
        currency="RUB",
        payment_provider="balance",
        payment_id=idempotency_key,
        status="completed",
        description="Дополнительное устройство",
        meta={"subscription_id": sub.id},
    )
    session.add(slot_txn)
    await session.flush()
    if user.billing_mode == "hybrid" and settings.billing_v2_enabled:
        from shared.services.billing_v2.balance_floor_panel_service import sync_hybrid_balance_floor_panel_state

        await sync_hybrid_balance_floor_panel_state(session, user, settings)
    await grant_referrer_percent_of_referred_payment(
        session,
        referred_user=user,
        payment_amount_rub=price,
        settings=settings,
        idempotency_key=f"referral_pct:device_slot:{slot_txn.id}",
        reward_source="payment_pct_device",
    )
    return True, join_lines(
        plain("Добавлен слот устройства (−")
        + bold(str(price))
        + plain(" ₽)."),
        plain("Всего слотов: ") + bold(str(sub.devices_count)) + plain("."),
    )


async def unlink_hwid_device_keep_slots(
    session: AsyncSession,
    *,
    user: User,
    hwid: str,
    settings: Settings,
) -> tuple[bool, str]:
    """Снять HWID только с панели: слоты подписки (devices_count) и лимит в панели не уменьшаем."""
    sub = await get_active_subscription(session, user.id)
    if not sub:
        return False, plain("Нет активной подписки.")
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

    r = await session.execute(
        select(Device).where(
            Device.user_id == user.id,
            Device.subscription_id == sub.id,
            Device.remnawave_client_id == hwid,
        )
    )
    for row in r.scalars().all():
        await session.delete(row)
    await add_device_history_event(
        session,
        user_id=user.id,
        subscription_id=sub.id,
        device_hwid=hwid,
        event_type="device.detached",
        event_ts=datetime.now(timezone.utc),
        is_active=False,
        meta={"source": "unlink_hwid_device_keep_slots"},
    )
    await session.flush()
    return True, join_lines(
        plain("Устройство отвязано от панели."),
        plain("Оплаченные слоты не изменялись."),
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
    if sub.devices_count < 1:
        return False, plain("Нет оплаченных слотов для уменьшения лимита.")

    if user.remnawave_uuid is None:
        return False, plain("Ошибка профиля VPN.")

    hwid = (hwid or "").strip()
    if not hwid:
        return False, plain("Некорректный HWID.")

    rw = RemnaWaveClient(settings)
    uinf_pol: dict[str, Any] | None = None
    try:
        uinf_pol = await rw.get_user(str(user.remnawave_uuid))
    except RemnaWaveError:
        pass
    try:
        await rw.delete_user_hwid_device(str(user.remnawave_uuid), hwid)
    except RemnaWaveError as e:
        return False, join_lines(plain("Панель VPN:"), esc(str(e)))

    new_limit = max(0, sub.devices_count - 1)
    if should_apply_hwid_device_limit_to_panel(uinf_pol):
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
    await add_device_history_event(
        session,
        user_id=user.id,
        subscription_id=sub.id,
        device_hwid=hwid,
        event_type="device.detached",
        event_ts=datetime.now(timezone.utc),
        is_active=False,
        meta={"source": "remove_hwid_device_from_panel"},
    )
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
    if sub.devices_count < 1:
        return False, plain("Нет слотов для уменьшения лимита.")

    dev = await session.get(Device, device_id)
    if dev is None or dev.user_id != user.id or dev.subscription_id != sub.id:
        return False, plain("Устройство не найдено.")

    if user.remnawave_uuid is None:
        return False, plain("Ошибка профиля VPN.")

    rw = RemnaWaveClient(settings)
    uinf_pol: dict[str, Any] | None = None
    try:
        uinf_pol = await rw.get_user(str(user.remnawave_uuid))
    except RemnaWaveError:
        pass
    new_limit = max(0, sub.devices_count - 1)
    if should_apply_hwid_device_limit_to_panel(uinf_pol):
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


async def admin_disable_subscription_record(
    session: AsyncSession,
    *,
    user_id: int,
    subscription_id: int,
    settings: Settings,
) -> tuple[bool, str]:
    """Отключить подписку как в TG-админке: статус cancelled + DISABLED в панели."""
    sub = await session.get(Subscription, subscription_id)
    if sub is None or sub.user_id != user_id:
        return False, plain("Подписка не найдена.")
    now = datetime.now(timezone.utc)
    if sub.status not in ("active", "trial") or sub.expires_at <= now:
        return False, plain("Нет активной подписки для отключения.")
    sub.status = "cancelled"
    u = await session.get(User, user_id)
    if u is not None and u.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            await rw.update_user(str(u.remnawave_uuid), status="DISABLED")
        except RemnaWaveError as e:
            logger.warning("admin_disable_subscription_record RW: %s", e)
    return True, plain("Подписка отключена.")


async def admin_enable_subscription_record(
    session: AsyncSession,
    *,
    user_id: int,
    subscription_id: int,
    settings: Settings,
) -> tuple[bool, str]:
    """Включить отменённую подписку (как admin:se в боте)."""
    sub = (
        await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.id == subscription_id)
        )
    ).scalar_one_or_none()
    if sub is None or sub.user_id != user_id:
        return False, plain("Подписка не найдена.")
    if sub.status != "cancelled":
        return False, plain("Запись не в статусе «отключена админом».")
    plan = sub.plan
    is_trial = plan is not None and plan.name == "Триал"
    sub.status = "trial" if is_trial else "active"
    if not is_trial:
        bp = await get_base_subscription_plan(session)
        if bp is not None:
            sub.plan_id = bp.id
    u = await session.get(User, user_id)
    if u is not None and u.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            await update_rw_user_respecting_hwid_limit(
                rw,
                str(u.remnawave_uuid),
                devices_limit_for_panel=sub.devices_count,
                expire_at=sub.expires_at,
                status="ACTIVE",
            )
        except RemnaWaveError as e:
            logger.warning("admin_enable_subscription_record RW: %s", e)
    return True, plain("Подписка снова активна.")
