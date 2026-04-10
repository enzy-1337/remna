"""Регистрация пользователя в БД."""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from decimal import Decimal

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.md2 import bold, code, join_lines, plain
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin
from shared.services.referral_parse import parse_referral_code_from_start_args
from shared.services.referral_service import replace_referrer_bonus_telegram_message


async def _generate_unique_referral_code(session: AsyncSession) -> str:
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        q = await session.execute(select(User.id).where(User.referral_code == code))
        if q.scalar_one_or_none() is None:
            return code
    raise RuntimeError("Не удалось сгенерировать уникальный referral_code")


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    r = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return r.scalar_one_or_none()


async def register_user(
    session: AsyncSession,
    tg_user: TgUser,
    start_args: str | None,
) -> tuple[User, bool, Decimal | None]:
    """
    Возвращает (user, created, invited_signup_bonus_rub).
    invited_signup_bonus_rub — начисление приглашённому за вход по реф-ссылке (или None).
    """
    existing = await get_user_by_telegram_id(session, tg_user.id)
    if existing:
        return existing, False, None

    ref_code = parse_referral_code_from_start_args(start_args)
    referrer_id: int | None = None
    if ref_code:
        r = await session.execute(select(User).where(User.referral_code == ref_code))
        ref_user = r.scalar_one_or_none()
        if ref_user and ref_user.telegram_id != tg_user.id:
            referrer_id = ref_user.id

    settings = get_settings()
    referral_code = await _generate_unique_referral_code(session)
    now = datetime.now(timezone.utc)
    user = User(
        telegram_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        language_code=tg_user.language_code,
        referred_by=referrer_id,
        referral_code=referral_code,
        is_subscribed_channel=True,
        last_activity_at=now,
        billing_mode="hybrid" if settings.billing_v2_enabled else "legacy",
    )
    session.add(user)
    await session.flush()

    bonus = settings.referral_signup_bonus_rub
    invited_bonus: Decimal | None = None
    if referrer_id is not None and bonus > 0:
        referrer = await session.get(User, referrer_id)
        if referrer is not None and not referrer.is_blocked and referrer.id != user.id:
            user.balance += bonus
            referrer.balance += bonus
            invited_bonus = bonus
            session.add(
                Transaction(
                    user_id=user.id,
                    type="referral_signup_invited",
                    amount=bonus,
                    currency="RUB",
                    payment_provider="referral",
                    payment_id=f"signup:invited:{user.id}",
                    status="completed",
                    description=f"Бонус за регистрацию по приглашению (реферер #{referrer.id})",
                    meta={"referrer_id": referrer.id},
                )
            )
            session.add(
                Transaction(
                    user_id=referrer.id,
                    type="referral_signup",
                    amount=bonus,
                    currency="RUB",
                    payment_provider="referral",
                    payment_id=f"signup:referrer:{user.id}",
                    status="completed",
                    description=f"Реферал: регистрация друга (user #{user.id})",
                    meta={"referred_id": user.id},
                )
            )
            await session.flush()
            await replace_referrer_bonus_telegram_message(
                session,
                referrer,
                join_lines(
                    "🎁 " + bold("Реферальный бонус"),
                    plain("По вашей ссылке зарегистрировался друг: +")
                    + bold(str(bonus))
                    + plain(" ₽ на баланс."),
                ),
                settings,
            )
            await notify_admin(
                settings,
                title="🎁 " + bold("Реферальные бонусы за регистрацию"),
                lines=[
                    plain("Новый пользователь: ")
                    + bold(f"#{user.id}")
                    + plain(" tg ")
                    + code(str(user.telegram_id))
                    + plain(": +")
                    + bold(str(bonus))
                    + plain(" ₽"),
                    plain("Пригласивший: ")
                    + bold(f"#{referrer.id}")
                    + plain(": +")
                    + bold(str(bonus))
                    + plain(" ₽"),
                ],
                event_type="referral_signup_bonus",
                topic=AdminLogTopic.BONUSES,
                subject_user=user,
                session=session,
            )

    return user, True, invited_bonus


async def touch_activity(session: AsyncSession, user: User) -> None:
    user.last_activity_at = datetime.now(timezone.utc)
