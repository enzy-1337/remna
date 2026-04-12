"""Интеграционные тесты charge_daily_device_once (SQLite+aiosqlite)."""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.models.base import Base
from shared.models.billing_daily_summary import BillingDailySummary
from shared.models.billing_ledger_entry import BillingLedgerEntry
from shared.models.billing_usage_event import BillingUsageEvent
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.billing_v2.rating_service import charge_daily_device_once


def _settings(**overrides: object) -> SimpleNamespace:
    base = dict(
        billing_device_daily_rub=Decimal("2.5"),
        billing_balance_floor_rub=Decimal("-50"),
        billing_gb_step_rub=Decimal("5"),
        billing_mobile_gb_extra_rub=Decimal("2.5"),
        billing_calendar_timezone="Europe/Moscow",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class RatingDeviceDailyIntegrationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        User.__table__,
                        Plan.__table__,
                        Subscription.__table__,
                        BillingUsageEvent.__table__,
                        BillingDailySummary.__table__,
                        BillingLedgerEntry.__table__,
                        Transaction.__table__,
                    ],
                )
            )
        self.factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _mk_hybrid(self, session: AsyncSession, *, balance: Decimal = Decimal("100")) -> User:
        u = User(
            telegram_id=secrets.randbelow(2_000_000_000) + 1_000_000_000,
            referral_code=secrets.token_hex(16)[:32],
            balance=balance,
            billing_mode="hybrid",
        )
        session.add(u)
        await session.flush()
        return u

    async def _mk_plan_no_device_package(self, session: AsyncSession) -> Plan:
        p = Plan(
            name="NoDevPkg",
            duration_days=30,
            price_rub=Decimal("100"),
            monthly_gb_limit=0,
            is_package_monthly=True,
        )
        session.add(p)
        await session.flush()
        return p

    async def _mk_sub(self, session: AsyncSession, *, user_id: int, plan_id: int) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            Subscription(
                user_id=user_id,
                plan_id=plan_id,
                status="active",
                expires_at=now + timedelta(days=30),
            )
        )
        await session.flush()

    async def test_two_attaches_same_hwid_same_day_one_debit(self) -> None:
        settings = _settings()
        day = date(2026, 4, 10)
        async with self.factory() as session:
            u = await self._mk_hybrid(session)
            p = await self._mk_plan_no_device_package(session)
            await self._mk_sub(session, user_id=u.id, plan_id=p.id)
            bal0 = u.balance
            ok1 = await charge_daily_device_once(
                session, user=u, device_hwid="hw-stable", day=day, settings=settings
            )
            ok2 = await charge_daily_device_once(
                session, user=u, device_hwid="hw-stable", day=day, settings=settings
            )
            self.assertTrue(ok1)
            self.assertTrue(ok2)
            self.assertEqual(u.balance, bal0 - Decimal("2.5"))
            cnt = (
                await session.execute(
                    select(func.count()).select_from(BillingUsageEvent).where(
                        BillingUsageEvent.event_id == f"device_daily:{u.id}:hw-stable:{day.isoformat()}"
                    )
                )
            ).scalar_one()
            self.assertEqual(int(cnt), 1)
            await session.commit()

    async def test_device_daily_balance_floor_no_usage(self) -> None:
        settings = _settings()
        day = date(2026, 4, 11)
        async with self.factory() as session:
            u = await self._mk_hybrid(session, balance=Decimal("-48.60"))
            p = await self._mk_plan_no_device_package(session)
            await self._mk_sub(session, user_id=u.id, plan_id=p.id)
            ok = await charge_daily_device_once(
                session, user=u, device_hwid="hw-floor", day=day, settings=settings
            )
            self.assertFalse(ok)
            row = (
                await session.execute(
                    select(BillingUsageEvent).where(
                        BillingUsageEvent.event_id == f"device_daily:{u.id}:hw-floor:{day.isoformat()}"
                    )
                )
            ).scalar_one_or_none()
            self.assertIsNone(row)
            await session.commit()


if __name__ == "__main__":
    import unittest

    unittest.main()
