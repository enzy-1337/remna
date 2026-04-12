"""Реферал: процент с пополнения на баланс реферера (SQLite)."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.models.base import Base
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.referral_service import grant_referrer_reward_from_topup


def _settings(**overrides: object) -> SimpleNamespace:
    base = dict(
        referral_payment_percent=Decimal("10"),
        billing_v2_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class ReferrerTopupRewardTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[User.__table__, Plan.__table__, Subscription.__table__, Transaction.__table__],
                )
            )
        self.factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _mk_user(
        self,
        session: AsyncSession,
        *,
        referred_by: int | None = None,
        balance: Decimal = Decimal("0"),
    ) -> User:
        code = secrets.token_hex(16)[:32]
        u = User(
            telegram_id=secrets.randbelow(2_000_000_000) + 1_000_000_000,
            referral_code=code,
            balance=balance,
            billing_mode="legacy",
            referred_by=referred_by,
        )
        session.add(u)
        await session.flush()
        return u

    @patch("shared.services.referral_service.replace_referrer_bonus_telegram_message", new_callable=AsyncMock)
    async def test_ten_percent_of_topup_to_referrer(self, _mock_msg: AsyncMock) -> None:
        settings = _settings()
        async with self.factory() as session:
            ref = await self._mk_user(session)
            invited = await self._mk_user(session, referred_by=ref.id, balance=Decimal("0"))
            bonus = await grant_referrer_reward_from_topup(
                session,
                referred_user=invited,
                topup_amount_rub=Decimal("100"),
                settings=settings,
                internal_topup_txn_id=42,
            )
            self.assertEqual(bonus, Decimal("10"))
            await session.refresh(ref)
            self.assertEqual(ref.balance, Decimal("10"))
            await session.commit()

    @patch("shared.services.referral_service.replace_referrer_bonus_telegram_message", new_callable=AsyncMock)
    async def test_idempotent_second_call(self, _mock_msg: AsyncMock) -> None:
        settings = _settings()
        async with self.factory() as session:
            ref = await self._mk_user(session)
            invited = await self._mk_user(session, referred_by=ref.id)
            b1 = await grant_referrer_reward_from_topup(
                session,
                referred_user=invited,
                topup_amount_rub=Decimal("50"),
                settings=settings,
                internal_topup_txn_id=99,
            )
            b2 = await grant_referrer_reward_from_topup(
                session,
                referred_user=invited,
                topup_amount_rub=Decimal("50"),
                settings=settings,
                internal_topup_txn_id=99,
            )
            self.assertEqual(b1, Decimal("5"))
            self.assertEqual(b2, Decimal("0"))
            await session.refresh(ref)
            self.assertEqual(ref.balance, Decimal("5"))
            await session.commit()


if __name__ == "__main__":
    import unittest

    unittest.main()
