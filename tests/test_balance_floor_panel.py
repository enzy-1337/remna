"""Пол баланса hybrid v2: latch в БД + вызовы sync (без реального Telegram)."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.models.base import Base
from shared.models.billing_ledger_entry import BillingLedgerEntry
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.billing_v2.balance_floor_panel_service import (
    reconcile_hybrid_balance_floor_panel_batch,
    sync_hybrid_balance_floor_panel_state,
)
from shared.services.billing_v2.ledger_service import apply_debit


def _settings(**overrides: object) -> SimpleNamespace:
    base = dict(
        billing_v2_enabled=True,
        billing_balance_floor_rub=Decimal("-50"),
        remnawave_stub=True,
        bot_token="",
        remnawave_default_squad_uuid="00000000-0000-0000-0000-000000000001",
        remnawave_optimized_squad_uuid="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class BalanceFloorPanelTests(IsolatedAsyncioTestCase):
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
                        BillingLedgerEntry.__table__,
                        Transaction.__table__,
                    ],
                )
            )
        self.factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _mk_user(self, session: AsyncSession, *, balance: Decimal) -> User:
        code = secrets.token_hex(16)[:32]
        rw_uid = uuid.uuid4()
        u = User(
            telegram_id=secrets.randbelow(2_000_000_000) + 1_000_000_000,
            referral_code=code,
            balance=balance,
            billing_mode="hybrid",
            remnawave_uuid=rw_uid,
        )
        session.add(u)
        await session.flush()
        return u

    async def _mk_sub(self, session: AsyncSession, *, user_id: int, plan_id: int) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            Subscription(
                user_id=user_id,
                plan_id=plan_id,
                status="active",
                expires_at=now + timedelta(days=10),
                devices_count=2,
            )
        )
        await session.flush()

    async def test_sync_at_floor_sets_latch(self) -> None:
        settings = _settings()
        async with self.factory() as session:
            u = await self._mk_user(session, balance=Decimal("-50.00"))
            await sync_hybrid_balance_floor_panel_state(session, u, settings)
            self.assertIsNotNone(u.balance_floor_rw_suspended_at)
            await sync_hybrid_balance_floor_panel_state(session, u, settings)
            await session.commit()

    async def test_apply_debit_touching_floor_triggers_sync(self) -> None:
        settings = _settings()
        async with self.factory() as session:
            u = await self._mk_user(session, balance=Decimal("-49.00"))
            lr = await apply_debit(
                session,
                user=u,
                amount_rub=Decimal("1"),
                idempotency_key="floor-touch",
                source="test",
                source_ref="t1",
                settings=settings,
            )
            self.assertTrue(lr.applied)
            self.assertEqual(u.balance, Decimal("-50.00"))
            self.assertIsNotNone(u.balance_floor_rw_suspended_at)
            await session.commit()

    async def test_restore_clears_latch(self) -> None:
        settings = _settings()
        async with self.factory() as session:
            p = Plan(
                name="Base",
                duration_days=30,
                price_rub=Decimal("100"),
                traffic_limit_gb=5,
            )
            session.add(p)
            await session.flush()
            u = await self._mk_user(session, balance=Decimal("-50.00"))
            await self._mk_sub(session, user_id=u.id, plan_id=p.id)
            await sync_hybrid_balance_floor_panel_state(session, u, settings)
            self.assertIsNotNone(u.balance_floor_rw_suspended_at)
            u.balance = Decimal("100.00")
            await sync_hybrid_balance_floor_panel_state(session, u, settings)
            self.assertIsNone(u.balance_floor_rw_suspended_at)
            await session.commit()

    async def test_reconcile_batch_finds_mismatch(self) -> None:
        settings = _settings()
        async with self.factory() as session:
            u = await self._mk_user(session, balance=Decimal("-50.00"))
            await session.commit()

        async with self.factory() as session:
            u2 = await session.get(User, u.id)
            assert u2 is not None
            await reconcile_hybrid_balance_floor_panel_batch(session, settings)
            await session.refresh(u2)
            self.assertIsNotNone(u2.balance_floor_rw_suspended_at)
            await session.commit()


if __name__ == "__main__":
    import unittest

    unittest.main()
