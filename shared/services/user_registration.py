"""Регистрация пользователя в БД."""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.md2 import bold, code, plain
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin
from shared.services.referral_parse import parse_referral_code_from_start_args


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
) -> tuple[User, bool]:
    """
    Возвращает (user, created).
    Если пользователь уже есть — только возврат, без изменения referred_by.
    """
    existing = await get_user_by_telegram_id(session, tg_user.id)
    if existing:
        return existing, False

    ref_code = parse_referral_code_from_start_args(start_args)
    referrer_id: int | None = None
    if ref_code:
        r = await session.execute(select(User).where(User.referral_code == ref_code))
        ref_user = r.scalar_one_or_none()
        if ref_user and ref_user.telegram_id != tg_user.id:
            referrer_id = ref_user.id

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
    )
    session.add(user)
    await session.flush()

    settings = get_settings()
    bonus = settings.referral_signup_bonus_rub
    if referrer_id is not None and bonus > 0:
        referrer = await session.get(User, referrer_id)
        if referrer is not None and not referrer.is_blocked and referrer.id != user.id:
            referrer.balance += bonus
            session.add(
                Transaction(
                    user_id=referrer.id,
                    type="referral_signup",
                    amount=bonus,
                    currency="RUB",
                    payment_provider="referral",
                    payment_id=f"signup:{user.id}",
                    status="completed",
                    description=f"Реферал: регистрация друга (user #{user.id})",
                    meta={"referred_id": user.id},
                )
            )
            await session.flush()
            await notify_admin(
                settings,
                title="🎁 " + bold("Реферальный бонус за регистрацию друга"),
                lines=[
                    plain("Новый пользователь: ")
                    + bold(f"#{user.id}")
                    + plain(" tg ")
                    + code(str(user.telegram_id)),
                    plain("Реферер: ") + bold(f"#{referrer.id}") + plain(": +") + bold(str(bonus)) + plain(" ₽"),
                ],
                event_type="referral_signup_bonus",
                topic=AdminLogTopic.BONUSES,
                subject_user=user,
                session=session,
            )

    return user, True


async def touch_activity(session: AsyncSession, user: User) -> None:
    user.last_activity_at = datetime.now(timezone.utc)
