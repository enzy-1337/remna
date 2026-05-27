"""Бонус на баланс при повторной покупке тарифа с баланса."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.transaction import Transaction
from shared.models.user import User


async def count_completed_subscription_purchases(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Transaction)
                .where(
                    Transaction.user_id == user_id,
                    Transaction.type == "subscription",
                    Transaction.status == "completed",
                )
            )
        ).scalar_one()
        or 0
    )


async def grant_repeat_subscription_purchase_bonus(
    session: AsyncSession,
    *,
    user: User,
    price_rub: Decimal,
    settings: Settings,
    purchase_txn_id: int,
    is_repeat_purchase: bool,
) -> Decimal:
    """Бонус на баланс при 2-й и последующих покупках тарифа."""
    if not is_repeat_purchase:
        return Decimal("0")
    pct = Decimal(str(getattr(settings, "subscription_repeat_purchase_bonus_percent", Decimal("5"))))
    if pct <= 0 or price_rub <= 0:
        return Decimal("0")
    bonus = (price_rub * pct / Decimal("100")).quantize(Decimal("0.01"))
    if bonus <= 0:
        return Decimal("0")
    bonus_pid = f"subscription_repeat_bonus:{purchase_txn_id}"
    existing = (
        await session.execute(
            select(Transaction.id).where(Transaction.payment_id == bonus_pid).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return Decimal("0")
    user.balance += bonus
    session.add(
        Transaction(
            user_id=user.id,
            type="subscription_repeat_bonus",
            amount=bonus,
            currency="RUB",
            payment_provider="balance",
            payment_id=bonus_pid,
            status="completed",
            description=f"Бонус за повторную покупку тарифа ({pct:g}%)",
            meta={
                "percent": str(pct),
                "base_price_rub": str(price_rub),
                "purchase_txn_id": purchase_txn_id,
            },
        )
    )
    return bonus
