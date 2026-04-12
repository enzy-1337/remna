"""Тесты счётчика трафика ceil(used_gb) по опросу панели."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.models.base import Base
from shared.models.billing_daily_summary import BillingDailySummary
from shared.models.billing_ledger_entry import BillingLedgerEntry
from shared.models.billing_traffic_meter import BillingTrafficMeter
from shared.models.billing_usage_event import BillingUsageEvent
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.billing_v2.traffic_meter_poll_service import (
    gb_steps_due_from_used_gb,
    sync_user_traffic_meter_from_panel,
)


def _meter_settings(**overrides: object) -> SimpleNamespace:
    base = dict(
        billing_v2_enabled=True,
        billing_traffic_rw_meter_enabled=True,
        billing_gb_step_rub=Decimal("5"),
        billing_mobile_gb_extra_rub=Decimal("2.5"),
        billing_optimized_route_gb_extra_rub=Decimal("2.5"),
        billing_balance_floor_rub=Decimal("-50"),
        billing_calendar_timezone="Europe/Moscow",
        remnawave_stub=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class GbStepsDueTests(IsolatedAsyncioTestCase):
    def test_steps(self) -> None:
        self.assertEqual(gb_steps_due_from_used_gb(None), 0)
        self.assertEqual(gb_steps_due_from_used_gb(0), 0)
        self.assertEqual(gb_steps_due_from_used_gb(0.001), 1)
        self.assertEqual(gb_steps_due_from_used_gb(1.0), 1)
        self.assertEqual(gb_steps_due_from_used_gb(1.0000001), 2)


class TrafficMeterPollIntegrationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        User.__table__,
                        BillingUsageEvent.__table__,
                        BillingDailySummary.__table__,
                        BillingLedgerEntry.__table__,
                        Transaction.__table__,
                        BillingTrafficMeter.__table__,
                    ],
                )
            )
        self.factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_meter_charges_from_panel_used(self) -> None:
        settings = _meter_settings()
        uinf_300mb = {
            "uuid": "test-uuid",
            "userTraffic": {"usedTrafficBytes": 300 * 1024 * 1024},
        }

        async with self.factory() as session:
            code = secrets.token_hex(8)
            u = User(
                telegram_id=1_001_000_001,
                referral_code=code,
                balance=Decimal("100"),
                billing_mode="hybrid",
                remnawave_uuid=uuid.uuid4(),
            )
            session.add(u)
            await session.flush()

            with patch(
                "shared.services.billing_v2.traffic_meter_poll_service.RemnaWaveClient"
            ) as mock_rw_cls:
                mock_rw_cls.return_value.get_user = AsyncMock(return_value=uinf_300mb)
                n = await sync_user_traffic_meter_from_panel(session, user=u, settings=settings)

            self.assertEqual(n, 1)
            meter = (
                await session.execute(select(BillingTrafficMeter).where(BillingTrafficMeter.user_id == u.id))
            ).scalar_one()
            self.assertEqual(meter.charged_gb_steps, 1)
            ev = (
                await session.execute(
                    select(BillingUsageEvent).where(BillingUsageEvent.user_id == u.id).limit(1)
                )
            ).scalar_one()
            self.assertTrue(ev.event_id.startswith("traffic_meter:"))
            self.assertEqual(u.balance, Decimal("95"))

            with patch(
                "shared.services.billing_v2.traffic_meter_poll_service.RemnaWaveClient"
            ) as mock_rw_cls:
                mock_rw_cls.return_value.get_user = AsyncMock(return_value=uinf_300mb)
                n2 = await sync_user_traffic_meter_from_panel(session, user=u, settings=settings)

            self.assertEqual(n2, 0)

            uinf_1_1gb = {
                "uuid": "test-uuid",
                "userTraffic": {"usedTrafficBytes": int(1.1 * 1024**3)},
            }
            with patch(
                "shared.services.billing_v2.traffic_meter_poll_service.RemnaWaveClient"
            ) as mock_rw_cls:
                mock_rw_cls.return_value.get_user = AsyncMock(return_value=uinf_1_1gb)
                n3 = await sync_user_traffic_meter_from_panel(session, user=u, settings=settings)

            self.assertEqual(n3, 1)
            self.assertEqual(meter.charged_gb_steps, 2)
            self.assertEqual(u.balance, Decimal("90"))

            await session.commit()

    async def test_new_row_aligns_with_legacy_webhook_steps(self) -> None:
        """Уже записанные traffic_gb_step (вебхук) — не дублируем при первом появлении счётчика."""
        settings = _meter_settings()
        uinf_500mb = {"userTraffic": {"usedTrafficBytes": int(0.5 * 1024**3)}}

        async with self.factory() as session:
            code = secrets.token_hex(8)
            u = User(
                telegram_id=1_002_000_002,
                referral_code=code,
                balance=Decimal("100"),
                billing_mode="hybrid",
                remnawave_uuid=uuid.uuid4(),
            )
            session.add(u)
            await session.flush()
            session.add(
                BillingUsageEvent(
                    user_id=u.id,
                    event_id="legacy-wh-1",
                    event_type="traffic_gb_step",
                    event_ts=datetime.now(timezone.utc),
                    usage_gb_step=1,
                    is_mobile_internet=False,
                    meta={"package_covered": False},
                )
            )
            await session.flush()

            with patch(
                "shared.services.billing_v2.traffic_meter_poll_service.RemnaWaveClient"
            ) as mock_rw_cls:
                mock_rw_cls.return_value.get_user = AsyncMock(return_value=uinf_500mb)
                n = await sync_user_traffic_meter_from_panel(session, user=u, settings=settings)

            self.assertEqual(n, 0)
            meter = (
                await session.execute(select(BillingTrafficMeter).where(BillingTrafficMeter.user_id == u.id))
            ).scalar_one()
            self.assertEqual(meter.charged_gb_steps, 1)
            cnt = (
                await session.execute(select(func.count()).select_from(BillingUsageEvent))
            ).scalar_one()
            self.assertEqual(int(cnt), 1)
            self.assertEqual(u.balance, Decimal("100"))

            await session.commit()
