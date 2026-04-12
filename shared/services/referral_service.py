"""Реферальная программа: процент с платежей приглашённого на баланс реферера."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import bold, code, join_lines, plain
from shared.models.referral_reward import ReferralReward
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.telegram_notify import delete_telegram_message, send_telegram_message

logger = logging.getLogger(__name__)

_REF_LABELS: dict[str, tuple[str, str]] = {
    "payment_pct_topup": ("пополнил баланс", "пополнения"),
    "payment_pct_plan": ("оформил тариф с баланса", "покупки тарифа"),
    "payment_pct_device": ("оплатил слот устройства", "слота устройства"),
}


async def replace_referrer_bonus_telegram_message(
    session: AsyncSession,
    referrer: User,
    text: str,
    settings: Settings,
) -> None:
    """Удаляет предыдущее реферальное уведомление бота и отправляет новое (одно «окно» в чате)."""
    old = referrer.referral_bonus_message_id
    if old is not None:
        deleted = await delete_telegram_message(referrer.telegram_id, int(old), settings=settings)
        if not deleted:
            logger.debug(
                "replace_referrer_bonus_telegram_message: old message not deleted tg=%s mid=%s",
                referrer.telegram_id,
                old,
            )
    mid = await send_telegram_message(referrer.telegram_id, text, settings=settings)
    referrer.referral_bonus_message_id = int(mid) if mid is not None else None
    await session.flush()


async def count_invited_users(session: AsyncSession, referrer_user_id: int) -> int:
    r = await session.execute(
        select(func.count()).select_from(User).where(User.referred_by == referrer_user_id)
    )
    return int(r.scalar_one() or 0)


async def sum_referrer_bonus_rub(session: AsyncSession, referrer_user_id: int) -> Decimal:
    r = await session.execute(
        select(func.coalesce(func.sum(ReferralReward.bonus_rub), 0)).where(
            ReferralReward.referrer_id == referrer_user_id,
            ReferralReward.status == "applied",
        )
    )
    val = r.scalar_one()
    return Decimal(str(val)) if val is not None else Decimal("0")


async def sum_referrer_bonus_days(session: AsyncSession, referrer_user_id: int) -> int:
    r = await session.execute(
        select(func.coalesce(func.sum(ReferralReward.bonus_days), 0)).where(
            ReferralReward.referrer_id == referrer_user_id,
            ReferralReward.status == "applied",
        )
    )
    return int(r.scalar_one() or 0)


async def list_invited_users(
    session: AsyncSession,
    referrer_user_id: int,
    *,
    limit: int = 40,
) -> list[User]:
    r = await session.execute(
        select(User)
        .where(User.referred_by == referrer_user_id)
        .order_by(User.id.desc())
        .limit(limit)
    )
    return list(r.scalars().all())


async def list_referrer_rewards(
    session: AsyncSession,
    referrer_user_id: int,
    *,
    limit: int = 30,
) -> list[ReferralReward]:
    r = await session.execute(
        select(ReferralReward)
        .where(ReferralReward.referrer_id == referrer_user_id, ReferralReward.status == "applied")
        .order_by(ReferralReward.id.desc())
        .limit(limit)
    )
    return list(r.scalars().all())


async def list_referrer_rewards_with_referred(
    session: AsyncSession,
    referrer_user_id: int,
    *,
    limit: int = 35,
) -> list[tuple[ReferralReward, User]]:
    r = await session.execute(
        select(ReferralReward, User)
        .join(User, User.id == ReferralReward.referred_id)
        .where(ReferralReward.referrer_id == referrer_user_id, ReferralReward.status == "applied")
        .order_by(ReferralReward.id.desc())
        .limit(limit)
    )
    return list(r.all())


async def grant_referrer_percent_of_referred_payment(
    session: AsyncSession,
    *,
    referred_user: User,
    payment_amount_rub: Decimal,
    settings: Settings,
    idempotency_key: str,
    reward_source: str,
) -> Decimal:
    """
    Процент REFERRAL_PAYMENT_PERCENT на баланс реферера.
    Идемпотентность: Transaction у реферера с type=referral_payment_percent и payment_id=idempotency_key.
    """
    if payment_amount_rub <= 0 or referred_user.referred_by is None:
        return Decimal("0")
    pct = settings.referral_payment_percent
    if pct <= 0:
        return Decimal("0")
    referrer = await session.get(User, referred_user.referred_by)
    if referrer is None or referrer.is_blocked or referrer.id == referred_user.id:
        return Decimal("0")

    dup = (
        await session.execute(
            select(Transaction.id).where(
                Transaction.type == "referral_payment_percent",
                Transaction.payment_id == idempotency_key,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if dup is not None:
        return Decimal("0")

    bonus = (payment_amount_rub * pct / Decimal("100")).quantize(Decimal("0.01"))
    if bonus <= 0:
        return Decimal("0")

    referrer.balance += bonus
    session.add(
        ReferralReward(
            referrer_id=referrer.id,
            referred_id=referred_user.id,
            plan_id=None,
            bonus_days=0,
            bonus_rub=bonus,
            source=reward_source,
            status="applied",
            applied_at=datetime.now(timezone.utc),
        )
    )
    session.add(
        Transaction(
            user_id=referrer.id,
            type="referral_payment_percent",
            amount=bonus,
            currency="RUB",
            payment_provider="referral",
            payment_id=idempotency_key,
            status="completed",
            description=f"Реферал: {pct}% от платежа user #{referred_user.id} ({reward_source})",
            meta={
                "referred_id": referred_user.id,
                "payment_amount_rub": str(payment_amount_rub),
                "reward_source": reward_source,
            },
        )
    )
    labels = _REF_LABELS.get(reward_source, ("совершил платёж", "платежа"))
    msg = join_lines(
        "🎁 " + bold("Реферальное начисление"),
        plain("Ваш приглашённый ")
        + bold(labels[0])
        + plain(": +")
        + bold(str(bonus))
        + plain(" ₽ (")
        + bold(str(pct))
        + plain("% от ")
        + plain(labels[1])
        + plain(")."),
    )
    await replace_referrer_bonus_telegram_message(session, referrer, msg, settings)
    return bonus


async def grant_referrer_reward_from_topup(
    session: AsyncSession,
    *,
    referred_user: User,
    topup_amount_rub: Decimal,
    settings: Settings,
    internal_topup_txn_id: int,
) -> Decimal:
    """
    Процент рефереру от успешного пополнения (идемпотентно по id внутренней транзакции topup).

    Процент задаётся ``Settings.referral_payment_percent`` (в .env: ``REFERRAL_PAYMENT_PERCENT`` или
    устаревшее имя ``REFERRAL_TOPUP_PERCENT``). Зачисление на ``referrer.balance`` + ``Transaction`` + ``ReferralReward``.
    """
    return await grant_referrer_percent_of_referred_payment(
        session,
        referred_user=referred_user,
        payment_amount_rub=topup_amount_rub,
        settings=settings,
        idempotency_key=f"referral_pct:topup:{internal_topup_txn_id}",
        reward_source="payment_pct_topup",
    )
