"""Промокоды: валидация и применение для пользователя."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.promo import PromoCode, PromoCodeAllowedUser, PromoUsage
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.md2 import bold, plain

# bonus_rub — устаревший тип: начисление на основной баланс (как balance_rub)
SUPPORTED_PROMO_TYPES = {
    "balance_rub",
    "bonus_rub",
    "topup_bonus_percent",
    "discount_percent",
    "extra_gb",
    "extra_devices",
    "extra_days",
}

_PAID_SUBSCRIPTION_TXN_TYPES = ("subscription", "subscription_autorenew")


async def user_has_recent_paid_subscription(
    session: AsyncSession,
    user_id: int,
    *,
    months: int,
    now: datetime | None = None,
) -> bool:
    """Была ли успешная покупка/продление тарифа за последние N календарных месяцев (~30 дн.)."""
    if months < 1:
        return False
    at = now or datetime.now(timezone.utc)
    cutoff = at - timedelta(days=int(months) * 30)
    row = (
        await session.execute(
            select(Transaction.id)
            .where(
                Transaction.user_id == user_id,
                Transaction.type.in_(_PAID_SUBSCRIPTION_TXN_TYPES),
                Transaction.status == "completed",
                Transaction.created_at >= cutoff,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def validate_promo_activation_eligibility(
    session: AsyncSession,
    *,
    promo: PromoCode,
    user: User,
) -> str | None:
    """
    Дополнительные условия из web-admin. None — можно активировать.
    Возвращает готовый текст ошибки (MarkdownV2).
    """
    from shared.services.subscription_service import get_active_subscription

    if bool(getattr(promo, "require_no_active_subscription", False)):
        if await get_active_subscription(session, user.id) is not None:
            return plain("Промокод доступен только без активной подписки.")

    months = getattr(promo, "require_no_paid_subscription_months", None)
    if months is not None and int(months) > 0:
        if await user_has_recent_paid_subscription(session, user.id, months=int(months)):
            m = int(months)
            if m == 1:
                tail = "1 месяц"
            elif 2 <= m <= 4:
                tail = f"{m} месяца"
            else:
                tail = f"{m} месяцев"
            return plain(
                "Промокод для тех, кто не покупал подписку последние "
            ) + plain(tail) + plain(".")
    return None


async def get_pending_purchase_discount_percent(
    session: AsyncSession,
    *,
    user_id: int,
) -> tuple[PromoUsage | None, Decimal]:
    row = (
        await session.execute(
            select(PromoUsage, PromoCode)
            .join(PromoCode, PromoUsage.promo_id == PromoCode.id)
            .where(
                PromoUsage.user_id == user_id,
                PromoCode.type == "discount_percent",
                PromoUsage.topup_bonus_applied_at.is_(None),
            )
            .order_by(PromoUsage.id.asc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None, Decimal("0")
    usage, promo = row
    return usage, Decimal(str(promo.value))


async def get_pending_purchase_discount_info(
    session: AsyncSession,
    *,
    user_id: int,
) -> tuple[str | None, Decimal]:
    row = (
        await session.execute(
            select(PromoUsage, PromoCode)
            .join(PromoCode, PromoUsage.promo_id == PromoCode.id)
            .where(
                PromoUsage.user_id == user_id,
                PromoCode.type == "discount_percent",
                PromoUsage.topup_bonus_applied_at.is_(None),
            )
            .order_by(PromoUsage.id.asc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None, Decimal("0")
    _usage, promo = row
    return promo.code, Decimal(str(promo.value))


async def apply_promo_code_for_user(
    session: AsyncSession,
    *,
    settings: Settings,
    user: User,
    raw_code: str,
) -> tuple[bool, str, dict | None]:
    return await apply_promo_code_for_user_v2(
        session,
        settings=settings,
        user=user,
        raw_code=raw_code,
    )


async def apply_promo_code_for_user_v2(
    session: AsyncSession,
    *,
    settings: Settings,
    user: User,
    raw_code: str,
) -> tuple[bool, str, dict | None]:
    code = (raw_code or "").strip().upper()
    if not code:
        return False, plain("Введите промокод."), None
    if len(code) > 64:
        return False, plain("Слишком длинный промокод."), None

    now = datetime.now(timezone.utc)
    r = await session.execute(
        select(PromoCode).where(PromoCode.code == code).with_for_update()
    )
    promo = r.scalar_one_or_none()
    if promo is None:
        return False, plain("Промокод не найден."), None
    if not promo.is_active:
        return False, plain("Промокод неактивен."), None
    if promo.expires_at is not None and promo.expires_at <= now:
        return False, plain("Срок действия промокода истёк."), None
    if promo.type not in SUPPORTED_PROMO_TYPES:
        return False, plain("Этот тип промокода пока не поддерживается."), None

    # Если у промокода есть allow-list — пользователь должен быть в нём.
    # Когда allow-list задан, глобальный max_uses игнорируется (каждый из списка может 1 раз).
    has_allowlist = (
        await session.execute(
            select(PromoCodeAllowedUser.id)
            .where(PromoCodeAllowedUser.promo_id == promo.id)
            .limit(1)
        )
    ).scalar_one_or_none() is not None
    if has_allowlist:
        in_list = (
            await session.execute(
                select(PromoCodeAllowedUser.id)
                .where(
                    PromoCodeAllowedUser.promo_id == promo.id,
                    PromoCodeAllowedUser.user_id == user.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if in_list is None:
            return False, plain("Этот промокод доступен только избранным пользователям."), None
    else:
        if promo.max_uses is not None and promo.used_count >= promo.max_uses:
            return False, plain("Лимит активаций промокода исчерпан."), None

    used = await session.execute(
        select(PromoUsage.id).where(
            PromoUsage.promo_id == promo.id,
            PromoUsage.user_id == user.id,
        )
    )
    if used.scalar_one_or_none() is not None:
        return False, plain("Вы уже использовали этот промокод."), None

    elig_err = await validate_promo_activation_eligibility(session, promo=promo, user=user)
    if elig_err is not None:
        return False, elig_err, None

    now = datetime.now(timezone.utc)
    value = Decimal(str(promo.value))
    if value <= 0:
        return False, plain("Некорректное значение промокода."), None

    # Общие изменения на этапе "активации промокода"
    promo.used_count += 1
    session.add(
        PromoUsage(
            promo_id=promo.id,
            user_id=user.id,
            topup_bonus_applied_at=None,
            used_at=now,
        )
    )

    if promo.type in ("balance_rub", "bonus_rub"):
        user.balance += value
        txn_type = "promo_balance" if promo.type == "balance_rub" else "promo_bonus"
        label = "на баланс"
        session.add(
            Transaction(
                user_id=user.id,
                type=txn_type,
                amount=value,
                currency="RUB",
                payment_provider="promo",
                payment_id=promo.code,
                status="completed",
                description=f"Промокод {promo.code}",
                meta={"promo_id": promo.id, "promo_type": promo.type},
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return False, plain("Вы уже использовали этот промокод."), None
        return (
            True,
            plain("✅ Промокод применён: +")
            + bold(str(value))
            + plain(" ₽ ")
            + plain(label)
            + plain("."),
            {"code": promo.code, "type": promo.type, "value": str(value)},
        )

    if promo.type == "topup_bonus_percent":
        # Бонус начисляется при первом успешном пополнении после активации.
        percent = value
        if percent <= 0:
            return False, plain("Некорректный % промокода."), None
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return False, plain("Вы уже использовали этот промокод."), None
        return (
            True,
            plain("✅ Промокод применён! Бонус +")
            + bold(str(percent))
            + plain("% начислится на ")
            + bold("первое пополнение")
            + plain(" после активации. Бонус сработает один раз."),
            {"code": promo.code, "type": promo.type, "value": str(percent)},
        )

    if promo.type == "discount_percent":
        percent = value
        if percent <= 0 or percent >= 100:
            return False, plain("Скидка должна быть в диапазоне (0, 100)."), None
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return False, plain("Вы уже использовали этот промокод."), None
        return (
            True,
            plain("✅ Промокод применён! Скидка ")
            + bold(str(percent))
            + plain("% будет применена к следующей покупке тарифа."),
            {"code": promo.code, "type": promo.type, "value": str(percent)},
        )

    if promo.type == "extra_gb":
        gb = int(value)
        if gb <= 0:
            return False, plain("Некорректное количество ГБ."), None
        if user.remnawave_uuid is None:
            return False, plain("Нет аккаунта в VPN-панели для начисления ГБ."), None
        rw = RemnaWaveClient(settings)
        try:
            uinfo = await rw.get_user(str(user.remnawave_uuid))
            current = int(uinfo.get("trafficLimitBytes") or 0)
            new_limit = current + gb * (1024**3)
            await rw.update_user(str(user.remnawave_uuid), traffic_limit_bytes=new_limit)
        except RemnaWaveError:
            return False, plain("Не удалось начислить ГБ в Remnawave."), None
        session.add(
            Transaction(
                user_id=user.id,
                type="promo_extra_gb",
                amount=Decimal("0"),
                currency="RUB",
                payment_provider="promo",
                payment_id=promo.code,
                status="completed",
                description=f"Промокод {promo.code}: +{gb} ГБ",
                meta={"promo_id": promo.id, "promo_type": promo.type, "extra_gb": gb},
            )
        )
        await session.flush()
        return True, plain("✅ Начислено ") + bold(str(gb)) + plain(" ГБ."), {"code": promo.code, "type": promo.type, "value": str(gb)}

    if promo.type == "extra_days":
        from shared.services.subscription_service import grant_subscription_extra_days

        add_days = int(value)
        if add_days <= 0:
            return False, plain("Некорректное количество дней."), None
        try:
            had_active, new_expires = await grant_subscription_extra_days(
                session,
                user=user,
                days=add_days,
                settings=settings,
                promo_code=promo.code,
            )
        except ValueError as e:
            return False, plain(str(e)), None
        except RemnaWaveError:
            return False, plain("Не удалось обновить срок подписки в VPN-панели."), None
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return False, plain("Вы уже использовали этот промокод."), None
        from shared.datetime_msk import fmt_dt_msk

        exp_s = fmt_dt_msk(new_expires)
        if had_active:
            msg = (
                plain("✅ К подписке добавлено ")
                + bold(str(add_days))
                + plain(" дн. Действует до ")
                + bold(exp_s)
                + plain(".")
            )
        else:
            msg = (
                plain("✅ Подписка активирована на ")
                + bold(str(add_days))
                + plain(" дн. (до ")
                + bold(exp_s)
                + plain(").")
            )
        return True, msg, {"code": promo.code, "type": promo.type, "value": str(add_days)}

    if promo.type == "extra_devices":
        from shared.services.remnawave_user_panel_sync import update_rw_user_respecting_hwid_limit
        from shared.services.subscription_service import get_active_subscription

        add_slots = int(value)
        if add_slots <= 0:
            return False, plain("Некорректное количество устройств."), None
        sub = await get_active_subscription(session, user.id)
        if sub is None:
            return False, plain("Нет активной подписки для добавления устройств."), None
        sub.devices_count += add_slots
        if user.remnawave_uuid is not None and not settings.remnawave_stub:
            rw = RemnaWaveClient(settings)
            try:
                await update_rw_user_respecting_hwid_limit(
                    rw,
                    str(user.remnawave_uuid),
                    devices_limit_for_panel=sub.devices_count,
                )
            except RemnaWaveError:
                pass
        session.add(
            Transaction(
                user_id=user.id,
                type="promo_extra_devices",
                amount=Decimal("0"),
                currency="RUB",
                payment_provider="promo",
                payment_id=promo.code,
                status="completed",
                description=f"Промокод {promo.code}: +{add_slots} устройств",
                meta={"promo_id": promo.id, "promo_type": promo.type, "extra_devices": add_slots},
            )
        )
        await session.flush()
        return (
            True,
            plain("✅ Добавлено слотов устройств: ") + bold(str(add_slots)),
            {"code": promo.code, "type": promo.type, "value": str(add_slots)},
        )

    # В теории не должно доходить сюда из-за SUPPORTED_PROMO_TYPES
    return False, plain("Этот тип промокода пока не поддерживается."), None
