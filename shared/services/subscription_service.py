"""Покупка/продление подписки с баланса, синхронизация Remnawave, устройства."""

from __future__ import annotations

import logging
import uuid as uuid_lib
from typing import Any
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal

from shared.datetime_msk import fmt_dt_msk

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
from shared.services.remnawave_user_panel_sync import update_rw_user_respecting_hwid_limit
from shared.services.referral_service import grant_referrer_percent_of_referred_payment
from shared.services.billing_calculator import transition_credit_for_remaining_legacy_rub
from shared.services.feature_flags import tariff_purchases_enabled

logger = logging.getLogger(__name__)

MIN_DEVICES = 2
MAX_DEVICES = 10

ONE_MONTH_DURATION_MIN = 28
ONE_MONTH_DURATION_MAX = 35
ONE_MONTH_REFERENCE_NAME = "1 месяц"


def hybrid_subscription_hwid_cap(settings: Settings, user: User) -> int | None:
    """Hybrid: фиксированный лимит слотов/HWID в панели (оплата по факту использования)."""
    if settings.billing_v2_enabled and user.billing_mode == "hybrid":
        return int(settings.billing_hybrid_hwid_slots)
    return None


def tariff_duration_months(duration_days: int) -> Decimal:
    """Количество «месяцев» для расчёта цены: duration_days / 30."""
    return (Decimal(int(duration_days)) / Decimal(30)).quantize(Decimal("0.0001"))


def calculate_tariff_price_from_base_month(
    base_month_rub: Decimal,
    *,
    duration_days: int,
    discount_percent: Decimal,
) -> Decimal:
    """
    Цена тарифа: база за месяц × число месяцев × (1 − скидка%), округление вниз до целых ₽.
    Пример: 179 × 2 × 0,98 = 350,84 → 350 ₽.
    """
    months = tariff_duration_months(duration_days)
    disc = discount_percent if discount_percent > 0 else Decimal("0")
    factor = Decimal("1") - disc / Decimal("100")
    raw = base_month_rub * months * factor
    return raw.quantize(Decimal("1"), rounding=ROUND_FLOOR)


def is_one_month_duration(duration_days: int) -> bool:
    return ONE_MONTH_DURATION_MIN <= int(duration_days) <= ONE_MONTH_DURATION_MAX


async def get_one_month_reference_plan(session: AsyncSession) -> Plan | None:
    """Тариф «1 месяц» — единственный источник базовой цены для магазина."""
    r = await session.execute(
        select(Plan)
        .where(
            Plan.is_active.is_(True),
            Plan.name.notin_([BASE_SUBSCRIPTION_PLAN_NAME, TRIAL_PLAN_NAME]),
            Plan.duration_days >= ONE_MONTH_DURATION_MIN,
            Plan.duration_days <= ONE_MONTH_DURATION_MAX,
        )
        .order_by(Plan.sort_order, Plan.id)
    )
    plans = list(r.scalars().all())
    if not plans:
        return None
    for p in plans:
        if (p.name or "").strip().lower() == ONE_MONTH_REFERENCE_NAME.lower():
            return p
    return plans[0]


async def resolve_plan_price_rub(session: AsyncSession, plan: Plan) -> Decimal:
    """Актуальная цена: для «1 месяц» — из поля плана, для остальных — расчёт от базы и скидки."""
    ref = await get_one_month_reference_plan(session)
    if ref is None:
        return plan.price_rub
    if plan.id is not None and plan.id == ref.id:
        return ref.price_rub
    return calculate_tariff_price_from_base_month(
        ref.price_rub,
        duration_days=int(plan.duration_days),
        discount_percent=plan.discount_percent,
    )


async def assign_plan_catalog_price(
    session: AsyncSession,
    plan: Plan,
    *,
    submitted_price_rub: Decimal | None = None,
) -> Decimal:
    """
    Записать price_rub в план: базовый месяц — вручную, остальные — только от базы × срок × (1−скидка%).
    """
    ref = await get_one_month_reference_plan(session)
    if ref is None:
        if is_one_month_duration(plan.duration_days):
            if submitted_price_rub is not None:
                plan.price_rub = submitted_price_rub
        return plan.price_rub
    if plan.id is not None and plan.id == ref.id:
        if submitted_price_rub is not None:
            plan.price_rub = submitted_price_rub
        return plan.price_rub
    base = ref.price_rub
    plan.price_rub = calculate_tariff_price_from_base_month(
        base,
        duration_days=int(plan.duration_days),
        discount_percent=plan.discount_percent,
    )
    return plan.price_rub


async def refresh_all_derived_plan_prices(session: AsyncSession) -> None:
    """Пересчитать price_rub у всех тарифов, кроме базового «1 месяц»."""
    ref = await get_one_month_reference_plan(session)
    if ref is None:
        return
    r = await session.execute(
        select(Plan).where(
            Plan.is_active.is_(True),
            Plan.name.notin_([BASE_SUBSCRIPTION_PLAN_NAME, TRIAL_PLAN_NAME]),
        )
    )
    for p in r.scalars().all():
        if p.id == ref.id:
            continue
        p.price_rub = calculate_tariff_price_from_base_month(
            ref.price_rub,
            duration_days=int(p.duration_days),
            discount_percent=p.discount_percent,
        )


async def apply_plan_price_from_monthly_base(
    session: AsyncSession,
    plan: Plan,
    *,
    base_month_rub: Decimal | None = None,
) -> Decimal:
    """Совместимость: пересчёт через assign_plan_catalog_price."""
    _ = base_month_rub
    return await assign_plan_catalog_price(session, plan)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def subscription_days_left(expires_at: datetime | None, *, now: datetime | None = None) -> int:
    if expires_at is None:
        return 0
    at = _as_utc(now or datetime.now(timezone.utc))
    exp = _as_utc(expires_at)
    return max(0, int((exp - at).total_seconds() // 86400))


def subscription_extension_would_stack(
    active: Subscription | None,
    *,
    now: datetime | None = None,
) -> bool:
    """
    Покупка тарифа прибавит срок к текущей подписке (не первая покупка после PAYG-заглушки).
    """
    if active is None or active.expires_at is None:
        return False
    at = _as_utc(now or datetime.now(timezone.utc))
    exp = _as_utc(active.expires_at)
    if exp <= at:
        return False
    if (exp - at).total_seconds() >= 86400 * 400:
        return False
    return True


def can_renew_subscription_with_tariff(
    active: Subscription | None,
    *,
    window_days: int,
    now: datetime | None = None,
) -> bool:
    """
    Продление разрешено: нет стекающейся подписки или до конца ≤ window_days.
    """
    if int(window_days) <= 0:
        return True
    if active is None:
        return True
    if not subscription_extension_would_stack(active, now=now):
        return True
    exp = active.expires_at
    assert exp is not None
    return subscription_days_left(exp, now=now) <= int(window_days)


async def check_tariff_extension_window(
    session: AsyncSession,
    user_id: int,
    settings: Settings,
) -> tuple[bool, int, int]:
    """(разрешено_ли_продление, дней_до_конца, окно_дней)."""
    window = int(settings.subscription_renewal_window_days)
    if window <= 0:
        return True, 0, 0
    active = await get_active_subscription(session, user_id)
    if not subscription_extension_would_stack(active):
        return True, 0, window
    assert active is not None and active.expires_at is not None
    days_left = subscription_days_left(active.expires_at)
    return days_left <= window, days_left, window


def renewal_blocked_message(days_left: int, window_days: int) -> str:
    return join_lines(
        plain(
            "Вы не можете продлить подписку: продление доступно только за "
        )
        + bold(str(window_days))
        + plain(" дн. до окончания подписки."),
        "",
        plain("Сейчас до конца осталось: ")
        + bold(str(days_left))
        + plain(" дн."),
    )


def plan_tariff_button_label(plan: Plan, *, price_rub: Decimal | None = None) -> str:
    """Текст кнопки тарифа со скидкой, напр.: «3 месяца — 370 ₽ (-5%)»."""
    name = (plan.name or "")[:28]
    price = price_rub if price_rub is not None else plan.price_rub
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


def plan_tariff_button_label_with_discount(
    plan: Plan,
    discount_percent: Decimal,
    *,
    price_rub: Decimal | None = None,
) -> str:
    base = plan_tariff_button_label(plan, price_rub=price_rub)
    if discount_percent <= 0:
        return base
    original = price_rub if price_rub is not None else plan.price_rub
    discount_amount = (original * discount_percent / Decimal("100")).quantize(Decimal("0.01"))
    final = (original - discount_amount).quantize(Decimal("0.01"))
    if final < 0:
        final = Decimal("0")
    return f"{base} → {final} ₽"


def calculate_discounted_plan_price(
    plan: Plan,
    discount_percent: Decimal,
    *,
    price_rub: Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    original = price_rub if price_rub is not None else plan.price_rub
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


async def default_one_month_tariff_price_rub(session: AsyncSession) -> Decimal | None:
    """Цена тарифа «1 месяц» — база для расчёта остальных пакетов."""
    ref = await get_one_month_reference_plan(session)
    if ref is None:
        return None
    return ref.price_rub


async def resolve_legacy_transition_base_month_rub(session: AsyncSession, settings: Settings) -> Decimal:
    """База ₽/мес для кредита legacy: тариф ~1 мес из БД или BILLING_TRANSITION_BASE_MONTH_RUB."""
    d = await default_one_month_tariff_price_rub(session)
    if d is not None:
        return d
    return settings.billing_transition_base_month_rub


async def get_active_subscription_at(
    session: AsyncSession, user_id: int, at: datetime
) -> Subscription | None:
    """Подписка со статусом active/trial, действующая строго после момента `at`."""
    r = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(("active", "trial")),
            Subscription.expires_at > at,
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    return r.scalar_one_or_none()


async def get_active_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    return await get_active_subscription_at(session, user_id, datetime.now(timezone.utc))


async def get_admin_manageable_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    """Подписка active/trial для админ-UI: сначала неистёкшая, иначе последняя active/trial (можно продлить дни/слоты)."""
    sub = await get_active_subscription(session, user_id)
    if sub is not None:
        return sub
    r = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(Subscription.user_id == user_id, Subscription.status.in_(("active", "trial")))
        .order_by(Subscription.id.desc())
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


async def grant_subscription_extra_days(
    session: AsyncSession,
    *,
    user: User,
    days: int,
    settings: Settings,
    promo_code: str,
) -> tuple[bool, datetime]:
    """
    Добавить дни к активной подписке или выдать новую на N дней (промокод extra_days).
    Возвращает (была_ли_активная_подписка, новый_expires_at).
    """
    if days <= 0:
        raise ValueError("Количество дней должно быть больше нуля.")
    if user.telegram_id is None or int(user.telegram_id) <= 0:
        raise ValueError("У пользователя не привязан Telegram — продление недоступно.")

    base_plan = await get_base_subscription_plan(session)
    if base_plan is None:
        raise ValueError("В БД не настроен тариф «Базовый».")

    now = datetime.now(timezone.utc)
    sub = await get_admin_manageable_subscription(session, user.id)
    had_active = (
        sub is not None
        and sub.expires_at is not None
        and _as_utc(sub.expires_at) > now
    )

    from shared.services.optimized_route_service import remnawave_squads_for_db_user

    rw = RemnaWaveClient(settings)
    squads = remnawave_squads_for_db_user(settings, user)
    hybrid_cap = hybrid_subscription_hwid_cap(settings, user)
    dev_limit = hybrid_cap if hybrid_cap is not None else MIN_DEVICES
    desc = build_remnawave_panel_description(user)
    tg_id = int(user.telegram_id)

    if sub is None:
        new_expires = now + timedelta(days=days)
        traffic_bytes = 0
        if base_plan.traffic_limit_gb and base_plan.traffic_limit_gb > 0:
            traffic_bytes = int(base_plan.traffic_limit_gb) * (1024**3)
        if user.remnawave_uuid is None:
            existing = await rw.find_user_by_telegram_id(tg_id)
            if existing is not None and existing.get("uuid"):
                user.remnawave_uuid = uuid_lib.UUID(str(existing["uuid"]))
            else:
                uname = build_remnawave_username_from_db_user(user)
                created = await _create_rw_user_retries(
                    rw,
                    base_username=uname,
                    telegram_id=tg_id,
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
        sub = Subscription(
            user_id=user.id,
            plan_id=base_plan.id,
            remnawave_sub_uuid=user.remnawave_uuid,
            status="active",
            devices_count=dev_limit,
            started_at=now,
            expires_at=new_expires,
            auto_renew=False,
        )
        session.add(sub)
        await session.flush()
        await ensure_placeholder_devices(session, sub)
        had_active = False
    else:
        exp = _as_utc(sub.expires_at) if sub.expires_at else now
        base_exp = exp if exp > now else now
        new_expires = base_exp + timedelta(days=days)
        sub.expires_at = new_expires
        if sub.status not in ("active", "trial"):
            sub.status = "active"
        if user.remnawave_uuid is not None:
            traffic_bytes = 0
            plan = await session.get(Plan, sub.plan_id) if sub.plan_id else None
            if plan and plan.traffic_limit_gb and plan.traffic_limit_gb > 0:
                traffic_bytes = int(plan.traffic_limit_gb) * (1024**3)
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
        had_active = exp > now

    session.add(
        Transaction(
            user_id=user.id,
            type="promo_extra_days",
            amount=Decimal("0"),
            currency="RUB",
            payment_provider="promo",
            payment_id=promo_code,
            status="completed",
            description=f"Промокод {promo_code}: +{days} дн. подписки",
            meta={"promo_type": "extra_days", "days": days, "expires_at": new_expires.isoformat()},
        )
    )
    await session.flush()
    return had_active, new_expires


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

    if not await tariff_purchases_enabled(settings):
        return False, plain("Покупка тарифов временно отключена."), "error"

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

    original_price = await resolve_plan_price_rub(session, purchased_plan)
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

    from shared.services.optimized_route_service import remnawave_squads_for_db_user

    rw = RemnaWaveClient(settings)
    squads = remnawave_squads_for_db_user(settings, user)

    now = datetime.now(timezone.utc)
    active = await get_active_subscription(session, user.id)
    allowed, days_left, renewal_window = await check_tariff_extension_window(
        session, user.id, settings
    )
    if not allowed:
        return False, renewal_blocked_message(days_left, renewal_window), "error"

    hybrid_cap = hybrid_subscription_hwid_cap(settings, user)
    if hybrid_cap is not None:
        dev_limit = hybrid_cap
    else:
        dev_limit = active.devices_count if active else MIN_DEVICES

    base = now
    if active and active.expires_at > now:
        # PAYG-заглушка после пополнения: длинный срок без пакета — первый платный тариф от «сейчас».
        long_horizon = (active.expires_at - now).total_seconds() >= 86400 * 400
        base = now if long_horizon else active.expires_at
    new_expires = base + timedelta(days=purchased_plan.duration_days)

    rb_was_active = active is not None
    rb_expires = active.expires_at if active else None
    rb_plan_id = active.plan_id if active else None
    rb_dc = active.devices_count if active else None
    rb_active_id = active.id if active else None

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
        if hybrid_cap is not None:
            active.devices_count = hybrid_cap
        sub = active
    else:
        dc = hybrid_cap if hybrid_cap is not None else MIN_DEVICES
        sub = Subscription(
            user_id=user.id,
            plan_id=base_plan.id,
            remnawave_sub_uuid=rw_uuid,
            status="active",
            devices_count=dc,
            started_at=now,
            expires_at=new_expires,
            auto_renew=True,
        )
        session.add(sub)
        await session.flush()
        for i in range(1, dc + 1):
            session.add(
                Device(subscription_id=sub.id, user_id=user.id, name=f"Устройство {i}")
            )

    await ensure_placeholder_devices(session, sub)
    if discount_usage is not None:
        discount_usage.topup_bonus_applied_at = datetime.now(timezone.utc)
    await session.flush()

    merge_meta: dict = {
        **(purchase_txn.meta or {}),
        "purchase_kind": "extend" if rb_was_active else "new",
        "duration_days": purchased_plan.duration_days,
        "subscription_db_id_after": sub.id,
    }
    if rb_was_active and rb_expires is not None:
        merge_meta["expires_at_before"] = rb_expires.isoformat()
        merge_meta["plan_id_before"] = rb_plan_id
        merge_meta["devices_count_before"] = rb_dc
        merge_meta["subscription_db_id"] = rb_active_id
    purchase_txn.meta = merge_meta
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
        + bold(fmt_dt_msk(new_expires)),
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
            plain("До: ") + bold(fmt_dt_msk(new_expires)),
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
    if settings.billing_v2_enabled and user.billing_mode == "hybrid":
        return False, plain("Для hybrid-пользователей покупка дополнительного устройства недоступна.")
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
    dev = Device(
        subscription_id=sub.id,
        user_id=user.id,
        name=f"Устройство {new_idx}",
    )
    session.add(dev)
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
    slot_txn.meta = {**(slot_txn.meta or {}), "device_id": dev.id}
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
    initiator: str = "user_bot",
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
    from shared.services.device_telegram_notify import notify_admin_device_detached

    await notify_admin_device_detached(
        settings,
        user=user,
        hwid=hwid,
        mode="keep_slots",
        initiator=initiator,
        session=session,
    )
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
    initiator: str = "user_bot",
) -> tuple[bool, str]:
    """Удалить устройство в Remnawave (HWID API) и синхронизировать лимит слотов в боте."""
    sub = await get_active_subscription(session, user.id)
    if not sub:
        return False, plain("Нет активной подписки.")
    if sub.devices_count <= MIN_DEVICES:
        return False, join_lines(
            plain("Нельзя удалить слот: в подписке минимум "),
            bold(str(MIN_DEVICES)),
            plain(" устройств."),
        )
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

    new_limit = max(MIN_DEVICES, sub.devices_count - 1)
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
    from shared.services.device_telegram_notify import notify_admin_device_detached

    await notify_admin_device_detached(
        settings,
        user=user,
        hwid=hwid,
        mode="decrease_slot",
        initiator=initiator,
        session=session,
    )
    return True, join_lines(
        plain("Слот снят с подписки, устройство отвязано."),
        plain("Слотов: ") + bold(str(sub.devices_count)) + plain("."),
    )


async def remove_device_slot(
    session: AsyncSession,
    *,
    user: User,
    device_id: int,
    settings: Settings,
    initiator: str = "user_bot",
) -> tuple[bool, str]:
    sub = await get_active_subscription(session, user.id)
    if not sub:
        return False, plain("Нет активной подписки.")
    if sub.devices_count <= MIN_DEVICES:
        return False, join_lines(
            plain("Нельзя удалить слот: в подписке минимум "),
            bold(str(MIN_DEVICES)),
            plain(" устройств."),
        )
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
    new_limit = max(MIN_DEVICES, sub.devices_count - 1)
    if should_apply_hwid_device_limit_to_panel(uinf_pol):
        try:
            await rw.update_user(str(user.remnawave_uuid), hwid_device_limit=new_limit)
        except RemnaWaveError as e:
            return False, join_lines(plain("Панель VPN:"), esc(str(e)))

    sub.devices_count = new_limit
    hwid_for_log = str(dev.remnawave_client_id or dev.name or device_id)
    await session.delete(dev)
    await session.flush()
    from shared.services.device_telegram_notify import notify_admin_device_detached

    await notify_admin_device_detached(
        settings,
        user=user,
        hwid=hwid_for_log,
        mode="db_slot",
        initiator=initiator,
        session=session,
    )
    return True, join_lines(
        plain("Устройство удалено."),
        plain("Слотов: ") + bold(str(sub.devices_count)) + plain("."),
    )


async def list_user_devices(session: AsyncSession, subscription_id: int) -> list[Device]:
    r = await session.execute(
        select(Device).where(Device.subscription_id == subscription_id).order_by(Device.id)
    )
    return list(r.scalars().all())


def _subscription_expiry_anchor(exp: datetime, now: datetime) -> datetime:
    """Опорная дата для сдвига срока: если подписка истекла — от «сейчас»."""
    return max(now, exp) if exp < now else exp


async def admin_adjust_subscription_days(
    session: AsyncSession,
    *,
    user_id: int,
    sub_id: int,
    days_delta: int,
    settings: Settings,
) -> tuple[bool, str]:
    """
    Сдвинуть expires_at на days_delta календарных дней (положительно — продлить, отрицательно — сократить).
    Синхронизирует Remnawave. Для триала (кроме смены плана) переводит на базовый тариф как при ручном продлении.
    """
    if days_delta == 0 or days_delta < -3650 or days_delta > 3650:
        return False, "Допустимо от -3650 до 3650 дней (не 0)."
    sub = (
        await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.id == sub_id, Subscription.user_id == user_id)
        )
    ).scalar_one_or_none()
    if sub is None:
        return False, "Подписка не найдена."
    if sub.expires_at is None:
        return False, "Нет даты окончания подписки."

    now = datetime.now(timezone.utc)
    exp = _as_utc(sub.expires_at)
    anchor = _subscription_expiry_anchor(exp, now)
    new_exp = anchor + timedelta(days=days_delta)
    if new_exp < now:
        new_exp = now
    sub.expires_at = new_exp

    pl = sub.plan
    if not (sub.status == "trial" and pl is not None and pl.name == "Триал"):
        bp = await get_base_subscription_plan(session)
        if bp is not None:
            sub.plan_id = bp.id

    u = await session.get(User, user_id)
    rw_status = "ACTIVE" if new_exp > now else "DISABLED"
    if u is not None and u.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            await update_rw_user_respecting_hwid_limit(
                rw,
                str(u.remnawave_uuid),
                devices_limit_for_panel=sub.devices_count,
                expire_at=sub.expires_at,
                status=rw_status,
            )
        except RemnaWaveError as e:
            logger.warning("admin_adjust_subscription_days RW failed: %s", e)
    return True, ""


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


PAYG_BOOTSTRAP_PAYMENT_ID = "payg_bootstrap"


async def provision_hybrid_payg_panel_if_needed(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
) -> bool:
    """
    Hybrid + v2: при пополнении без активной подписки — Remnawave + запись «Базовый» без списания с баланса,
    чтобы работали списания pay-as-you-go; покупка тарифа позже пересчитает срок (см. purchase_plan_with_balance).
    """
    if not settings.billing_v2_enabled or user.billing_mode != "hybrid":
        return False
    if await get_active_subscription(session, user.id) is not None:
        return False

    marker = f"{PAYG_BOOTSTRAP_PAYMENT_ID}:{user.id}"
    dup = (
        await session.execute(
            select(Transaction.id).where(Transaction.user_id == user.id, Transaction.payment_id == marker).limit(1)
        )
    ).scalar_one_or_none()
    if dup is not None:
        return False

    base_plan = await get_base_subscription_plan(session)
    if base_plan is None:
        logger.warning("provision_payg: нет плана «Базовый» user_id=%s", user.id)
        return False

    now = datetime.now(timezone.utc)
    payg_horizon = now + timedelta(days=int(settings.billing_payg_subscription_days))

    from shared.services.optimized_route_service import remnawave_squads_for_db_user

    rw = RemnaWaveClient(settings)
    squads = remnawave_squads_for_db_user(settings, user)
    desc = build_remnawave_panel_description(user)

    try:
        if user.remnawave_uuid is None:
            existing = await rw.find_user_by_telegram_id(user.telegram_id)
            if existing is not None and existing.get("uuid"):
                user.remnawave_uuid = uuid_lib.UUID(str(existing["uuid"]))
            else:
                uname = build_remnawave_username_from_db_user(user)
                hw_cap = int(settings.billing_hybrid_hwid_slots)
                created = await _create_rw_user_retries(
                    rw,
                    base_username=uname,
                    telegram_id=user.telegram_id,
                    expire_at=payg_horizon,
                    traffic_limit_bytes=0,
                    description=desc,
                    hwid_device_limit=hw_cap,
                    active_internal_squads=squads,
                )
                uid = created.get("uuid")
                if not uid:
                    raise RemnaWaveError("Панель не вернула uuid пользователя")
                user.remnawave_uuid = uuid_lib.UUID(str(uid))
        await update_rw_user_respecting_hwid_limit(
            rw,
            str(user.remnawave_uuid),
            devices_limit_for_panel=int(settings.billing_hybrid_hwid_slots),
            expire_at=payg_horizon,
            traffic_limit_bytes=0,
            status="ACTIVE",
            description=desc,
            active_internal_squads=squads,
        )
    except RemnaWaveError:
        logger.exception("provision_payg: Remnawave user_id=%s", user.id)
        return False

    uid = user.remnawave_uuid
    assert uid is not None

    hw_cap = int(settings.billing_hybrid_hwid_slots)
    sub = Subscription(
        user_id=user.id,
        plan_id=base_plan.id,
        remnawave_sub_uuid=uid,
        status="active",
        devices_count=hw_cap,
        started_at=now,
        expires_at=payg_horizon,
        auto_renew=True,
    )
    session.add(sub)
    await session.flush()
    for i in range(1, hw_cap + 1):
        session.add(Device(subscription_id=sub.id, user_id=user.id, name=f"Устройство {i}"))
    await ensure_placeholder_devices(session, sub)
    session.add(
        Transaction(
            user_id=user.id,
            type="payg_bootstrap",
            amount=Decimal("0"),
            currency="RUB",
            payment_provider="billing_v2",
            payment_id=marker,
            status="completed",
            description="Доступ PAYG после пополнения (без покупки тарифа)",
            meta={"subscription_id": sub.id},
        )
    )
    await session.flush()
    try:
        from shared.services.billing_v2.hwid_panel_reconcile_service import reconcile_hwid_devices_from_panel

        await reconcile_hwid_devices_from_panel(session, user=user, settings=settings)
    except Exception:
        logger.exception("provision_payg: hwid reconcile user_id=%s", user.id)
    return True


async def admin_convert_monthly_subscriptions_to_payg_balance(
    session: AsyncSession,
    *,
    settings: Settings,
) -> tuple[int, int, Decimal]:
    """
    Массовый переход: активные/триал подписки от 30 дней -> hybrid + годовой horizon + unlimited traffic.
    На баланс начисляется кредит по калькулятору transition_credit_for_remaining_legacy_rub от duration_days плана.
    Exempt: lifetime (>= cutoff year) и админы/флаги exempt (через billing_mode/lifetime_exempt обработку на месте).
    """
    from shared.services.optimized_route_service import remnawave_squads_for_db_user
    from shared.services.billing_v2.traffic_meter_poll_service import (
        baseline_meter_at_hybrid_transition,
    )

    now = datetime.now(timezone.utc)
    cutoff = datetime(settings.billing_legacy_lifetime_cutoff_year, 1, 1, tzinfo=timezone.utc)
    base_plan = await get_base_subscription_plan(session)
    if base_plan is None:
        return 0, 0, Decimal("0")

    rows = (
        await session.execute(
            select(Subscription, User, Plan)
            .join(User, User.id == Subscription.user_id)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.status.in_(("active", "trial")),
                Subscription.expires_at > now,
                Subscription.expires_at < cutoff,
                Plan.duration_days >= 30,
            )
            .order_by(Subscription.expires_at.desc())
        )
    ).all()
    if not rows:
        return 0, 0, Decimal("0")

    base_m = await resolve_legacy_transition_base_month_rub(session, settings)

    # Берём последнюю запись на пользователя.
    latest_by_user: dict[int, tuple[Subscription, User, Plan]] = {}
    for sub, user, plan in rows:
        if user.id not in latest_by_user:
            latest_by_user[user.id] = (sub, user, plan)

    changed = 0
    rw_changed = 0
    total_credit = Decimal("0")

    rw = RemnaWaveClient(settings)
    for sub, user, plan in latest_by_user.values():
        duration_days = int(plan.duration_days or 0)
        if duration_days < 30:
            continue
        credit = transition_credit_for_remaining_legacy_rub(
            settings, remaining_days=duration_days, base_month_rub=base_m
        )
        if credit > 0:
            user.balance += credit
            total_credit += credit
            session.add(
                Transaction(
                    user_id=user.id,
                    type="billing_transition",
                    amount=credit,
                    currency="RUB",
                    payment_provider="system",
                    payment_id=f"transition_credit:{user.id}:{sub.id}",
                    status="completed",
                    description="Массовая конвертация legacy подписки в баланс PAYG",
                    meta={
                        "source": "admin_mass_convert",
                        "subscription_id": sub.id,
                        "plan_id": plan.id,
                        "plan_duration_days": duration_days,
                        "payg_subscription_days": int(settings.billing_payg_subscription_days),
                        "base_month_rub": str(base_m),
                        "fee_percent": str(settings.billing_transition_fee_percent),
                    },
                )
            )

        user.billing_mode = "hybrid"
        sub.plan_id = base_plan.id
        sub.expires_at = now + timedelta(days=int(settings.billing_payg_subscription_days))
        sub.status = "active"
        sub.auto_renew = True
        sub.devices_count = int(settings.billing_hybrid_hwid_slots)
        await ensure_placeholder_devices(session, sub)
        try:
            await baseline_meter_at_hybrid_transition(session, user=user, settings=settings)
        except Exception:
            logger.exception("mass convert: baseline_meter failed user_id=%s", user.id)

        if user.remnawave_uuid is not None:
            try:
                squads = remnawave_squads_for_db_user(settings, user)
                desc = build_remnawave_panel_description(user)
                await update_rw_user_respecting_hwid_limit(
                    rw,
                    str(user.remnawave_uuid),
                    devices_limit_for_panel=sub.devices_count,
                    expire_at=sub.expires_at,
                    traffic_limit_bytes=0,
                    status="ACTIVE",
                    description=desc,
                    active_internal_squads=squads,
                )
                rw_changed += 1
            except RemnaWaveError:
                logger.exception("mass convert: remnawave sync failed user_id=%s", user.id)
        changed += 1

    await session.flush()
    return changed, rw_changed, total_credit


def subscription_included_device_slots(settings: Settings) -> int:
    """Слотов в подписке без отдельной доплаты (для будущего месячного биллинга устройств)."""
    return int(settings.subscription_included_device_slots)


def billable_device_slots_over_included(settings: Settings, subscription_devices_count: int) -> int:
    """Сколько слотов считаются «сверх включённых» (≥ 0)."""
    inc = subscription_included_device_slots(settings)
    return max(0, int(subscription_devices_count) - inc)


def monthly_extra_devices_rub_preview(settings: Settings, subscription_devices_count: int) -> Decimal:
    """Предпросмотр ₽/мес за платные слоты сверх включённых (бот пока не списывает)."""
    n = billable_device_slots_over_included(settings, subscription_devices_count)
    return (settings.extra_device_monthly_rub * Decimal(n)).quantize(Decimal("0.01"))
