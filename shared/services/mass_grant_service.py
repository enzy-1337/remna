"""Массовая выдача баланса или дней подписки списку пользователей.

Аудитория — явный список Telegram ID или все пользователи; опционально фильтруется
условием "была подписка (любого статуса), действовавшая в последние N дней" — не
подошедшие под условие пользователи пропускаются, а не считаются ошибкой.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.subscription_service import grant_subscription_extra_days
from shared.services.topup_service import apply_balance_credit_followups

GrantType = Literal["balance", "days"]

_MSK_TZ = ZoneInfo("Europe/Moscow")


def _midnight_cutoff_utc(days: int) -> datetime:
    """Полночь (00:00 МСК) N дней назад, а не ровно N*24ч от текущего момента —
    иначе пользователь, чья подписка истекла сегодня утром, но фильтр запускают
    вечером, несправедливо вылетает из окна "N дней назад". Например, для N=14
    и сегодняшней даты это будет 00:00 МСК числа (сегодня - 14)."""
    cutoff_date = (datetime.now(_MSK_TZ) - timedelta(days=days)).date()
    return datetime.combine(cutoff_date, time.min, tzinfo=_MSK_TZ).astimezone(timezone.utc)


@dataclass
class MassGrantResult:
    total_candidates: int = 0
    granted: int = 0
    skipped_no_subscription: int = 0
    skipped_invalid_user: int = 0
    errors: list[str] = field(default_factory=list)


async def resolve_candidate_users(
    session: AsyncSession, *, telegram_ids: list[int] | None
) -> list[User]:
    """telegram_ids=None — все пользователи; иначе только перечисленные ID (не найденные молча опускаются)."""
    if telegram_ids is not None:
        if not telegram_ids:
            return []
        rows = await session.execute(select(User).where(User.telegram_id.in_(telegram_ids)))
    else:
        rows = await session.execute(select(User))
    return list(rows.scalars().all())


async def _had_subscription_within_days(
    session: AsyncSession, *, user_id: int, days: int
) -> bool:
    """Была ли у пользователя подписка (любой статус), действовавшая после cutoff =
    полночь МСК N дней назад (см. _midnight_cutoff_utc)."""
    cutoff = _midnight_cutoff_utc(days)
    row = (
        await session.execute(
            select(Subscription.id)
            .where(Subscription.user_id == user_id, Subscription.expires_at >= cutoff)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def apply_mass_grant(
    session: AsyncSession,
    settings: Settings,
    *,
    users: list[User],
    grant_type: GrantType,
    amount_rub: Decimal | None = None,
    days: int | None = None,
    subscription_within_days: int | None = None,
    actor_label: str,
) -> MassGrantResult:
    """Применяет выдачу к каждому пользователю по очереди; коммит — забота вызывающего кода."""
    result = MassGrantResult(total_candidates=len(users))
    for u in users:
        if subscription_within_days is not None:
            had = await _had_subscription_within_days(
                session, user_id=u.id, days=subscription_within_days
            )
            if not had:
                result.skipped_no_subscription += 1
                continue
        try:
            if grant_type == "balance":
                if amount_rub is None or amount_rub <= 0:
                    raise ValueError("Сумма должна быть > 0.")
                u.balance += amount_rub
                txn = Transaction(
                    user_id=u.id,
                    type="admin_mass_balance_add",
                    amount=amount_rub,
                    currency="RUB",
                    payment_provider="admin",
                    payment_id=None,
                    status="completed",
                    description=f"Массовая выдача баланса: +{amount_rub} ₽ ({actor_label})",
                    meta={"mass_grant": True, "actor": actor_label},
                )
                session.add(txn)
                await session.flush()
                await apply_balance_credit_followups(
                    session,
                    user=u,
                    credited=amount_rub,
                    settings=settings,
                    triggering_txn=txn,
                    grant_referrer_reward=True,
                    try_smart_cart=True,
                )
            else:
                if days is None or days <= 0:
                    raise ValueError("Количество дней должно быть > 0.")
                await grant_subscription_extra_days(
                    session,
                    user=u,
                    days=days,
                    settings=settings,
                    promo_code=f"mass_grant_{actor_label}",
                )
            result.granted += 1
        except ValueError:
            result.skipped_invalid_user += 1
        except Exception as exc:
            result.errors.append(f"user#{u.id} (tg {u.telegram_id}): {exc}")
    return result
