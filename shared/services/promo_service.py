"""Промокоды: валидация и применение для пользователя."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.promo import PromoCode, PromoUsage
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.md2 import bold, plain

SUPPORTED_PROMO_TYPES = {"balance_rub", "bonus_rub"}


async def apply_promo_code_for_user(
    session: AsyncSession,
    *,
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

    value = Decimal(str(promo.value))
    if value <= 0:
        return False, plain("Некорректное значение промокода."), None

    if promo.type == "balance_rub":
        user.balance += value
        txn_type = "promo_balance"
        label = "на основной баланс"
    else:
        user.bonus_balance += value
        txn_type = "promo_bonus"
        label = "на бонусный баланс"

    promo.used_count += 1
    session.add(PromoUsage(promo_id=promo.id, user_id=user.id))
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
        return False, plain("Вы уже использовали этот промокод."), None
    return (
        True,
        plain("✅ Промокод применён: +")
        + bold(str(value))
        + plain(f" ₽ {label}."),
        {"code": promo.code, "type": promo.type, "value": str(value)},
    )
