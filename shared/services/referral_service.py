"""Реферальная программа: награда пригласившему за первую платную подписку друга."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.plan import Plan
from shared.models.referral_reward import ReferralReward
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.subscription_service import get_active_subscription
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


async def grant_referrer_reward_first_paid_plan(
    session: AsyncSession,
    *,
    buyer: User,
    plan: Plan,
    settings: Settings,
) -> None:
    """
    Однократно при первой успешной покупке платного тарифа (не триал):
    начисление referrer'у RUB на баланс и/или продление его активной подписки на N дней.
    """
    rub = settings.referral_inviter_bonus_rub
    days = settings.referral_inviter_bonus_days
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
                meta={"referred_id": buyer.id, "plan_id": plan.id},
            )
        )
        parts.append(f"+<b>{rub}</b> ₽ на баланс")

    if days > 0:
        sub = await get_active_subscription(session, referrer.id)
        if sub is not None:
            new_exp = sub.expires_at + timedelta(days=days)
            sub.expires_at = new_exp
            if referrer.remnawave_uuid is not None:
                rw = RemnaWaveClient(settings)
                try:
                    await rw.update_user(
                        str(referrer.remnawave_uuid),
                        expire_at=new_exp,
                        status="ACTIVE",
                    )
                except RemnaWaveError as e:
                    logger.warning("Referrer RW extend failed user=%s: %s", referrer.id, e)
            parts.append(f"+<b>{days}</b> дн. к подписке")
        else:
            logger.info(
                "Referral bonus days skipped: no active sub for referrer_id=%s", referrer.id
            )

    await session.flush()

    if parts:
        msg = (
            "🎁 <b>Реферальный бонус</b>\n"
            f"Ваш приглашённый оформил первый платный тариф «{plan.name}».\n"
            + "\n".join(parts)
        )
        await send_telegram_message(referrer.telegram_id, msg, settings=settings)
        from shared.services.admin_notify import notify_admin

        await notify_admin(
            settings,
            title="👥 <b>Реферальный бонус начислен</b>",
            lines=[
                f"Реферер: <b>#{referrer.id}</b> tg <code>{referrer.telegram_id}</code>",
                f"Приглашённый: <b>#{buyer.id}</b> tg <code>{buyer.telegram_id}</code>",
                f"Тариф: <b>{html.escape(plan.name)}</b>",
                "Начисление: " + " · ".join(parts),
            ],
            event_type="referral_reward",
            subject_user=buyer,
            session=session,
        )
