"""Промокоды: валидация и применение для пользователя."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.promo import PromoCode, PromoUsage
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
}


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
    if promo.max_uses is not None and promo.used_count >= promo.max_uses:
        return False, plain("Лимит активаций промокода исчерпан."), None
    if promo.type not in SUPPORTED_PROMO_TYPES:
        return False, plain("Этот тип промокода пока не поддерживается."), None

    used = await session.execute(
        select(PromoUsage.id).where(
            PromoUsage.promo_id == promo.id,
            PromoUsage.user_id == user.id,
        )
    )
    if used.scalar_one_or_none() is not None:
        return False, plain("Вы уже использовали этот промокод."), None

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

    if promo.type in {"discount_percent", "extra_gb", "extra_devices"}:
        return False, plain("Этот тип промокода будет доступен в следующем релизе."), None

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
            join_lines(
                plain("✅ Промокод применён!"),
                plain("Бонус +"),
                bold(str(percent)),
                plain("% начислится на "),
                bold("первое пополнение"),
                plain(" после активации."),
                plain("Бонус сработает один раз."),
            ),
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
            join_lines(
                plain("✅ Промокод применён!"),
                plain("Скидка "),
                bold(str(percent)),
                plain("% будет применена к следующей покупке тарифа."),
            ),
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
        return True, join_lines(plain("✅ Начислено "), bold(str(gb)), plain(" ГБ.")), {"code": promo.code, "type": promo.type, "value": str(gb)}

    if promo.type == "extra_devices":
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
            join_lines(plain("✅ Добавлено слотов устройств: "), bold(str(add_slots))),
            {"code": promo.code, "type": promo.type, "value": str(add_slots)},
        )

    # В теории не должно доходить сюда из-за SUPPORTED_PROMO_TYPES
    return False, plain("Этот тип промокода пока не поддерживается."), None
