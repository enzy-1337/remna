"""Первое пополнение: доп. % на баланс и настраиваемый welcome ГБ (без реального RW)."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.models.base import Base
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.payments.base import ParsedWebhookTopup
from shared.services.topup_service import apply_topup_from_webhook


def _settings(**overrides: object) -> SimpleNamespace:
    base = dict(
        billing_v2_enabled=False,
        billing_first_topup_extra_balance_percent=Decimal("100"),
        billing_first_topup_extra_balance_min_rub=Decimal("10"),
        billing_first_topup_welcome_gb=0,
        remnawave_stub=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FirstTopupBonusesTests(IsolatedAsyncioTestCase):
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

    async def _mk_user(self, session: AsyncSession, *, balance: Decimal = Decimal("0")) -> User:
        code = secrets.token_hex(16)[:32]
        u = User(
            telegram_id=secrets.randbelow(2_000_000_000) + 1_000_000_000,
            referral_code=code,
            balance=balance,
            billing_mode="legacy",
        )
        session.add(u)
        await session.flush()
        return u

    async def test_first_topup_doubles_balance_when_percent_100(self) -> None:
        settings = _settings()
        async with self.factory() as session:
            u = await self._mk_user(session)
            session.add(
                Transaction(
                    user_id=u.id,
                    type="topup",
                    amount=Decimal("20"),
                    currency="RUB",
                    payment_provider="cryptobot",
                    payment_id="ext-1",
                    status="pending",
                    description="test",
                    meta={"telegram_id": int(u.telegram_id)},
                    created_at=datetime.now(timezone.utc),
                )
            )
            await session.flush()
            tid = (
                await session.execute(select(Transaction.id).where(Transaction.user_id == u.id).limit(1))
            ).scalar_one()
            parsed = ParsedWebhookTopup(
                internal_transaction_id=int(tid),
                external_payment_id="ext-1",
                amount_rub=Decimal("20"),
                paid=True,
            )
            st, _tg, total, uid, promo, ft = await apply_topup_from_webhook(
                session,
                provider_name="cryptobot",
                parsed=parsed,
                settings=settings,
            )
            self.assertEqual(st, "completed")
            self.assertEqual(total, Decimal("40"))
            self.assertEqual(promo, Decimal("0"))
            self.assertEqual(ft, Decimal("20"))
            self.assertEqual(u.balance, Decimal("40"))
            bonus = (
                await session.execute(
                    select(Transaction).where(Transaction.type == "first_topup_balance_bonus", Transaction.user_id == u.id)
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(bonus)
            assert bonus is not None
            self.assertEqual(bonus.amount, Decimal("20"))
            await session.commit()

    async def test_welcome_gb_disabled_when_zero(self) -> None:
        settings = _settings(billing_first_topup_extra_balance_percent=Decimal("0"), billing_first_topup_welcome_gb=0)
        async with self.factory() as session:
            u = await self._mk_user(session)
            session.add(
                Transaction(
                    user_id=u.id,
                    type="topup",
                    amount=Decimal("50"),
                    currency="RUB",
                    payment_provider="cryptobot",
                    payment_id="ext-2",
                    status="pending",
                    description="test",
                    meta={"telegram_id": int(u.telegram_id)},
                    created_at=datetime.now(timezone.utc),
                )
            )
            await session.flush()
            tid = (
                await session.execute(select(Transaction.id).where(Transaction.user_id == u.id).limit(1))
            ).scalar_one()
            parsed = ParsedWebhookTopup(
                internal_transaction_id=int(tid),
                external_payment_id="ext-2",
                amount_rub=Decimal("50"),
                paid=True,
            )
            await apply_topup_from_webhook(session, provider_name="cryptobot", parsed=parsed, settings=settings)
            w = (
                await session.execute(
                    select(Transaction.id).where(Transaction.type == "welcome_gb_bonus", Transaction.user_id == u.id)
                )
            ).scalar_one_or_none()
            self.assertIsNone(w)
            await session.commit()


if __name__ == "__main__":
    import unittest

    unittest.main()
