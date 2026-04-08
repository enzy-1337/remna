"""Реферальная программа: награда пригласившему за первую платную подписку друга."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import bold, code, join_lines, plain
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.plan import Plan
from shared.models.referral_reward import ReferralReward
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.subscription_service import (
    get_active_subscription,
    update_rw_user_respecting_hwid_limit,
)
from shared.services.telegram_notify import send_telegram_message

logger = logging.getLogger(__name__)

SOURCE_FIRST_PAID_PLAN = "first_paid_plan"


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


def first_paid_plan_referrer_rewards(
    plan: Plan,
    settings: Settings,
) -> tuple[Decimal, int]:
    """
    За первую покупку приглашённого: ₽ и дни пригласившему, пропорционально duration_days (за каждые 30 дн. периода).
    """
    d = max(1, int(plan.duration_days))
    rub_per_30 = settings.referral_inviter_reward_rub_per_30_days
    days_per_30 = settings.referral_inviter_reward_days_per_30_days
    rub = ((rub_per_30 * Decimal(d)) / Decimal(30)).quantize(Decimal("0.01"))
    bonus_days = (days_per_30 * d) // 30
    return rub, bonus_days


async def grant_referrer_reward_first_paid_plan(
    session: AsyncSession,
    *,
    buyer: User,
    plan: Plan,
    settings: Settings,
) -> None:
    """
    Однократно при первой успешной покупке платного тарифа (не триал):
    начисление referrer'у RUB на баланс и продление активной подписки (пропорционально сроку купленного тарифа).
    """
    rub, days = first_paid_plan_referrer_rewards(plan, settings)
    if rub <= 0 and days <= 0:
        return
    if buyer.referred_by is None:
        return
    referrer_id = buyer.referred_by
    if referrer_id == buyer.id:
        return

    referrer = await session.get(User, referrer_id)
    if referrer is None or referrer.is_blocked:
        return

    reward = ReferralReward(
        referrer_id=referrer.id,
        referred_id=buyer.id,
        plan_id=plan.id,
        bonus_days=days,
        bonus_rub=rub,
        source=SOURCE_FIRST_PAID_PLAN,
        status="applied",
        applied_at=datetime.now(timezone.utc),
    )
    try:
        async with session.begin_nested():
            session.add(reward)
            await session.flush()
    except IntegrityError:
        logger.info("referral reward duplicate skipped referred_id=%s", buyer.id)
        return

    parts: list[str] = []
    if rub > 0:
        referrer.balance += rub
        session.add(
            Transaction(
                user_id=referrer.id,
                type="referral_reward",
                amount=rub,
                currency="RUB",
                payment_provider="referral",
                payment_id=None,
                status="completed",
                description=f"Реферал: первый платный тариф (user #{buyer.id})",
                meta={
                    "referred_id": buyer.id,
                    "plan_id": plan.id,
                    "plan_duration_days": int(plan.duration_days),
                },
            )
        )
        parts.append(plain("+") + bold(str(rub)) + plain(" ₽ на баланс"))

    if days > 0:
        sub = await get_active_subscription(session, referrer.id)
        if sub is not None:
            new_exp = sub.expires_at + timedelta(days=days)
            sub.expires_at = new_exp
            if referrer.remnawave_uuid is not None:
                rw = RemnaWaveClient(settings)
                try:
                    await update_rw_user_respecting_hwid_limit(
                        rw,
                        str(referrer.remnawave_uuid),
                        devices_limit_for_panel=sub.devices_count,
                        expire_at=new_exp,
                        status="ACTIVE",
                    )
                except RemnaWaveError as e:
                    logger.warning("Referrer RW extend failed user=%s: %s", referrer.id, e)
            parts.append(plain("+") + bold(str(days)) + plain(" дн. к подписке"))
        else:
            logger.info(
                "Referral bonus days skipped: no active sub for referrer_id=%s", referrer.id
            )

    await session.flush()

    if parts:
        msg = join_lines(
            "🎁 " + bold("Реферальный бонус"),
            plain("Ваш приглашённый оформил первый платный тариф ")
            + bold(plan.name)
            + plain("."),
            *parts,
        )
        await send_telegram_message(referrer.telegram_id, msg, settings=settings)
        from shared.services.admin_notify import notify_admin

        from shared.services.admin_log_topics import AdminLogTopic

        await notify_admin(
            settings,
            title="👥 " + bold("Реферальный бонус начислен"),
            lines=[
                "Реферер: "
                + bold(f"#{referrer.id}")
                + " tg "
                + code(str(referrer.telegram_id)),
                "Приглашённый: "
                + bold(f"#{buyer.id}")
                + " tg "
                + code(str(buyer.telegram_id)),
                plain("Тариф: ") + bold(plan.name),
                plain("Начисление: ") + plain(" · ").join(parts),
            ],
            event_type="referral_reward",
            topic=AdminLogTopic.BONUSES,
            subject_user=buyer,
            session=session,
        )


async def grant_referrer_reward_from_topup(
    session: AsyncSession,
    *,
    referred_user: User,
    topup_amount_rub: Decimal,
    settings: Settings,
) -> Decimal:
    if topup_amount_rub <= 0 or referred_user.referred_by is None:
        return Decimal("0")
    if settings.referral_topup_percent <= 0:
        return Decimal("0")
    referrer = await session.get(User, referred_user.referred_by)
    if referrer is None or referrer.is_blocked or referrer.id == referred_user.id:
        return Decimal("0")

    bonus = (topup_amount_rub * settings.referral_topup_percent / Decimal("100")).quantize(Decimal("0.01"))
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
            source="topup_percent",
            status="applied",
            applied_at=datetime.now(timezone.utc),
        )
    )
    session.add(
        Transaction(
            user_id=referrer.id,
            type="referral_topup_percent",
            amount=bonus,
            currency="RUB",
            payment_provider="referral",
            payment_id=None,
            status="completed",
            description=f"Реферал: {settings.referral_topup_percent}% от пополнения user #{referred_user.id}",
            meta={"referred_id": referred_user.id, "topup_amount_rub": str(topup_amount_rub)},
        )
    )
    await send_telegram_message(
        referrer.telegram_id,
        join_lines(
            "🎁 " + bold("Реферальное начисление"),
            plain("Ваш реферал пополнил баланс: +")
            + bold(str(bonus))
            + plain(" ₽ (")
            + bold(str(settings.referral_topup_percent))
            + plain("%)."),
        ),
        settings=settings,
    )
    return bonus
