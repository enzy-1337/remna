"""Интеграционные тесты charge_gb_step на SQLite+aiosqlite (без PostgreSQL)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from zoneinfo import ZoneInfo

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
from shared.services.billing_v2.rating_service import charge_gb_step


def _settings(**overrides: object) -> SimpleNamespace:
    base = dict(
        billing_gb_step_rub=Decimal("5"),
        billing_mobile_gb_extra_rub=Decimal("2.5"),
        billing_optimized_route_gb_extra_rub=Decimal("2.5"),
        billing_balance_floor_rub=Decimal("-50"),
        billing_calendar_timezone="Europe/Moscow",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class RatingChargeGbIntegrationTests(IsolatedAsyncioTestCase):
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

    async def _mk_hybrid_user(
        self,
        session: AsyncSession,
        *,
        balance: Decimal = Decimal("100"),
        optimized_route_enabled: bool = False,
    ) -> User:
        code = secrets.token_hex(16)[:32]
        u = User(
            telegram_id=secrets.randbelow(2_000_000_000) + 1_000_000_000,
            referral_code=code,
            balance=balance,
            billing_mode="hybrid",
            optimized_route_enabled=optimized_route_enabled,
        )
        session.add(u)
        await session.flush()
        return u

    async def _mk_package_plan(
        self,
        session: AsyncSession,
        *,
        monthly_gb_limit: int = 5,
    ) -> Plan:
        p = Plan(
            name="Pkg",
            duration_days=30,
            price_rub=Decimal("300"),
            monthly_gb_limit=monthly_gb_limit,
            is_package_monthly=True,
        )
        session.add(p)
        await session.flush()
        return p

    async def _mk_active_sub(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        plan_id: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        sub = Subscription(
            user_id=user_id,
            plan_id=plan_id,
            status="active",
            expires_at=now + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()

    async def test_package_five_steps_no_debit_sixth_debits(self) -> None:
        settings = _settings()
        event_ts = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
        async with self.factory() as session:
            u = await self._mk_hybrid_user(session)
            p = await self._mk_package_plan(session, monthly_gb_limit=5)
            await self._mk_active_sub(session, user_id=u.id, plan_id=p.id)
            start_bal = u.balance
            for i in range(5):
                ok = await charge_gb_step(
                    session,
                    user=u,
                    event_id=f"gb-pkg-{i}",
                    event_ts=event_ts,
                    is_mobile_internet=False,
                    settings=settings,
                )
                self.assertTrue(ok)
            n_usage = (
                await session.execute(select(func.count()).select_from(BillingUsageEvent))
            ).scalar_one()
            self.assertEqual(int(n_usage), 5)
            self.assertEqual(u.balance, start_bal)

            ok6 = await charge_gb_step(
                session,
                user=u,
                event_id="gb-pkg-5-pay",
                event_ts=event_ts,
                is_mobile_internet=False,
                settings=settings,
            )
            self.assertTrue(ok6)
            self.assertEqual(u.balance, start_bal - Decimal("5"))

            n_usage2 = (
                await session.execute(select(func.count()).select_from(BillingUsageEvent))
            ).scalar_one()
            self.assertEqual(int(n_usage2), 6)

            await session.commit()

    async def test_balance_floor_no_usage_row(self) -> None:
        settings = _settings()
        event_ts = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
        async with self.factory() as session:
            u = await self._mk_hybrid_user(session, balance=Decimal("-46.00"))
            p = await self._mk_package_plan(session, monthly_gb_limit=0)
            await self._mk_active_sub(session, user_id=u.id, plan_id=p.id)
            ok = await charge_gb_step(
                session,
                user=u,
                event_id="gb-floor-fail",
                event_ts=event_ts,
                is_mobile_internet=False,
                settings=settings,
            )
            self.assertFalse(ok)
            row = (
                await session.execute(
                    select(BillingUsageEvent).where(BillingUsageEvent.event_id == "gb-floor-fail")
                )
            ).scalar_one_or_none()
            self.assertIsNone(row)
            await session.commit()

    async def test_duplicate_event_id_idempotent(self) -> None:
        settings = _settings()
        event_ts = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
        async with self.factory() as session:
            u = await self._mk_hybrid_user(session)
            p = await self._mk_package_plan(session, monthly_gb_limit=0)
            await self._mk_active_sub(session, user_id=u.id, plan_id=p.id)
            bal0 = u.balance
            ok1 = await charge_gb_step(
                session,
                user=u,
                event_id="gb-dup",
                event_ts=event_ts,
                is_mobile_internet=False,
                settings=settings,
            )
            self.assertTrue(ok1)
            bal1 = u.balance
            ok2 = await charge_gb_step(
                session,
                user=u,
                event_id="gb-dup",
                event_ts=event_ts,
                is_mobile_internet=False,
                settings=settings,
            )
            self.assertTrue(ok2)
            self.assertEqual(u.balance, bal1)
            self.assertEqual(bal0 - bal1, Decimal("5"))
            cnt = (
                await session.execute(
                    select(func.count()).select_from(BillingUsageEvent).where(BillingUsageEvent.event_id == "gb-dup")
                )
            ).scalar_one()
            self.assertEqual(int(cnt), 1)
            await session.commit()

    async def test_payg_updates_daily_summary(self) -> None:
        settings = _settings()
        event_ts = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
        async with self.factory() as session:
            u = await self._mk_hybrid_user(session)
            p = await self._mk_package_plan(session, monthly_gb_limit=0)
            await self._mk_active_sub(session, user_id=u.id, plan_id=p.id)
            await charge_gb_step(
                session,
                user=u,
                event_id="gb-sum-1",
                event_ts=event_ts,
                is_mobile_internet=True,
                settings=settings,
            )
            await session.flush()
            day = event_ts.astimezone(ZoneInfo("Europe/Moscow")).date()
            sm = (
                await session.execute(
                    select(BillingDailySummary).where(
                        BillingDailySummary.user_id == u.id,
                        BillingDailySummary.day == day,
                    )
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(sm)
            assert sm is not None
            self.assertEqual(sm.gb_units, 1)
            self.assertEqual(sm.mobile_gb_units, 0)
            self.assertEqual(sm.total_amount_rub, Decimal("5"))
            await session.commit()

    async def test_payg_optimized_route_extra_on_gb_step(self) -> None:
        settings = _settings()
        event_ts = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
        async with self.factory() as session:
            u = await self._mk_hybrid_user(session, optimized_route_enabled=True)
            p = await self._mk_package_plan(session, monthly_gb_limit=0)
            await self._mk_active_sub(session, user_id=u.id, plan_id=p.id)
            bal0 = u.balance
            await charge_gb_step(
                session,
                user=u,
                event_id="gb-opt-1",
                event_ts=event_ts,
                is_mobile_internet=False,
                settings=settings,
            )
            await session.flush()
            self.assertEqual(u.balance, bal0 - Decimal("7.50"))
            ev = (
                await session.execute(
                    select(BillingUsageEvent).where(BillingUsageEvent.event_id == "gb-opt-1")
                )
            ).scalar_one()
            self.assertTrue(ev.meta.get("optimized_route"))
            self.assertEqual(ev.meta.get("optimized_route_extra_rub"), "2.5")
            day = event_ts.astimezone(ZoneInfo("Europe/Moscow")).date()
            sm = (
                await session.execute(
                    select(BillingDailySummary).where(
                        BillingDailySummary.user_id == u.id,
                        BillingDailySummary.day == day,
                    )
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(sm)
            assert sm is not None
            self.assertEqual(sm.gb_amount_rub, Decimal("7.50"))
            self.assertEqual(sm.total_amount_rub, Decimal("7.50"))
            await session.commit()


if __name__ == "__main__":
    import unittest

    unittest.main()
