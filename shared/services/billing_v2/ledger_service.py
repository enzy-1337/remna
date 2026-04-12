from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.billing_ledger_entry import BillingLedgerEntry
from shared.models.transaction import Transaction
from shared.models.user import User


@dataclass(slots=True)
class LedgerResult:
    applied: bool
    user_balance: Decimal
    entry_id: int | None


async def apply_debit(
    session: AsyncSession,
    *,
    user: User,
    amount_rub: Decimal,
    idempotency_key: str,
    source: str,
    source_ref: str | None,
    settings: Settings,
    meta: dict | None = None,
) -> LedgerResult:
    existing = (
        await session.execute(
            select(BillingLedgerEntry).where(BillingLedgerEntry.idempotency_key == idempotency_key).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return LedgerResult(applied=False, user_balance=user.balance, entry_id=existing.id)

    next_balance = (user.balance - amount_rub).quantize(Decimal("0.01"))
    if next_balance < settings.billing_balance_floor_rub:
        reject_key = f"{idempotency_key}:reject"
        reject_exists = (
            await session.execute(
                select(BillingLedgerEntry).where(BillingLedgerEntry.idempotency_key == reject_key).limit(1)
            )
        ).scalar_one_or_none()
        if reject_exists is None:
            session.add(
                BillingLedgerEntry(
                    user_id=user.id,
                    entry_type="reject",
                    amount_rub=amount_rub,
                    balance_after_rub=user.balance,
                    idempotency_key=reject_key,
                    source=source,
                    source_ref=source_ref,
                    meta={"reason": "balance_floor", "floor": str(settings.billing_balance_floor_rub), **(meta or {})},
                )
            )
            await session.flush()
        return LedgerResult(applied=False, user_balance=user.balance, entry_id=None)

    user.balance = next_balance
    entry = BillingLedgerEntry(
        user_id=user.id,
        entry_type="debit",
        amount_rub=amount_rub,
        balance_after_rub=next_balance,
        idempotency_key=idempotency_key,
        source=source,
        source_ref=source_ref,
        meta=meta or {},
    )
    session.add(entry)
    session.add(
        Transaction(
            user_id=user.id,
            type="usage_charge",
            amount=amount_rub,
            currency="RUB",
            payment_provider="billing_v2",
            payment_id=idempotency_key,
            status="completed",
            description=f"Списание {source}",
            meta={"source": source, "source_ref": source_ref, **(meta or {})},
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.flush()
    if user.billing_mode == "hybrid" and settings.billing_v2_enabled:
        from shared.services.billing_v2.balance_floor_panel_service import sync_hybrid_balance_floor_panel_state

        await sync_hybrid_balance_floor_panel_state(session, user, settings)
    return LedgerResult(applied=True, user_balance=next_balance, entry_id=entry.id)
