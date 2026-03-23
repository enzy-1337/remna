"""Промокоды: валидация и применение для пользователя."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.promo import PromoCode, PromoUsage
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.models.subscription import Subscription
from shared.md2 import bold, plain
from shared.services.subscription_service import get_active_subscription, update_rw_user_respecting_hwid_limit
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError

# bonus_rub — устаревший тип: начисление на основной баланс (как balance_rub)
SUPPORTED_PROMO_TYPES = {
    "balance_rub",
    "bonus_rub",
    "subscription_days",
    "topup_bonus_percent",
}


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

    if promo.type == "subscription_days":
        days_d = value
        days_i = int(days_d)
        if days_d != Decimal(days_i):
            return False, plain("Дни должны быть целым числом."), None
        if days_i <= 0:
            return False, plain("Некорректное значение дней промокода."), None

        sub: Subscription | None = await get_active_subscription(session, user.id)
        if sub is not None:
            sub.expires_at = sub.expires_at + timedelta(days=days_i)

            if user.remnawave_uuid is not None and not settings.remnawave_stub:
                rw = RemnaWaveClient(settings)
                try:
                    await update_rw_user_respecting_hwid_limit(
                        rw,
                        str(user.remnawave_uuid),
                        devices_limit_for_panel=sub.devices_count,
                        expire_at=sub.expires_at,
                        status="ACTIVE",
                    )
                except RemnaWaveError:
                    # Дни в БД всё равно начисляем; панель можно синхронизировать следующими циклами.
                    pass

            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return False, plain("Вы уже использовали этот промокод."), None
            return (
                True,
                join_lines(
                    plain("✅ Промокод применён: "),
                    bold(f"+{days_i} дн."),
                    plain("к вашей активной подписке."),
                ),
                {"code": promo.code, "type": promo.type, "value": str(days_i)},
            )

        fallback = Decimal(str(promo.fallback_value_rub or "0"))
        if fallback <= 0:
            return False, plain("Нет активной подписки и не задан фолбэк-деньги."), None

        user.balance += fallback
        session.add(
            Transaction(
                user_id=user.id,
                type="promo_balance",
                amount=fallback,
                currency="RUB",
                payment_provider="promo",
                payment_id=promo.code,
                status="completed",
                description=f"Промокод {promo.code} (фолбэк при отсутствии подписки)",
                meta={"promo_id": promo.id, "promo_type": promo.type, "fallback": True},
            )
        )

        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return False, plain("Вы уже использовали этот промокод."), None
        return (
            True,
            join_lines(
                plain("✅ Промокод применён: "),
                bold(f"+{fallback} ₽"),
                plain("на баланс (нет активной подписки)."),
            ),
            {"code": promo.code, "type": promo.type, "value": str(days_i), "fallback": str(fallback)},
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

    # В теории не должно доходить сюда из-за SUPPORTED_PROMO_TYPES
    return False, plain("Этот тип промокода пока не поддерживается."), None
