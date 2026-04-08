from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import text

from shared.config import get_settings
from shared.database import get_session_factory


@dataclass
class CheckResult:
    name: str
    ok: bool
    details: str


REQUIRED_TABLES = [
    "billing_usage_events",
    "billing_ledger_entries",
    "billing_daily_summary",
    "device_history",
    "remnawave_webhook_events",
]


async def check_required_tables() -> CheckResult:
    factory = get_session_factory()
    async with factory() as session:
        missing: list[str] = []
        for table in REQUIRED_TABLES:
            row = (
                await session.execute(
                    text("SELECT 1 FROM information_schema.tables WHERE table_name = :t LIMIT 1"),
                    {"t": table},
                )
            ).first()
            if row is None:
                missing.append(table)
    if missing:
        return CheckResult("db_tables", False, f"missing: {', '.join(missing)}")
    return CheckResult("db_tables", True, "all required tables exist")


def check_flag_consistency() -> CheckResult:
    s = get_settings()
    issues: list[str] = []
    if s.billing_v2_enabled and not s.remnawave_webhooks_enabled:
        issues.append("BILLING_V2_ENABLED=true but REMNAWAVE_WEBHOOKS_ENABLED=false")
    if s.remnawave_webhooks_enabled and not s.remnawave_webhook_secret.strip():
        issues.append("REMNAWAVE_WEBHOOKS_ENABLED=true but REMNAWAVE_WEBHOOK_SECRET is empty")
    if s.billing_min_topup_rub < 1:
        issues.append("BILLING_MIN_TOPUP_RUB must be >= 1")
    if s.billing_balance_floor_rub > 0:
        issues.append("BILLING_BALANCE_FLOOR_RUB should be <= 0")
    if issues:
        return CheckResult("flags", False, "; ".join(issues))
    return CheckResult("flags", True, "flags look consistent")


async def check_transitions_data_path() -> CheckResult:
    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*)::int
                    FROM information_schema.columns
                    WHERE table_name = 'users'
                      AND column_name IN ('billing_mode', 'lifetime_exempt_flag', 'risk_notified_24h_at', 'risk_notified_1h_at')
                    """
                )
            )
        ).scalar_one()
    if int(row or 0) < 4:
        return CheckResult("users_columns", False, "users columns for phase1 are incomplete")
    return CheckResult("users_columns", True, "users columns are present")


async def main() -> None:
    checks = [
        await check_required_tables(),
        check_flag_consistency(),
        await check_transitions_data_path(),
    ]
    failed = 0
    for c in checks:
        status = "OK" if c.ok else "FAIL"
        print(f"[{status}] {c.name}: {c.details}")
        if not c.ok:
            failed += 1
    if failed:
        raise SystemExit(1)
    print("Phase 1 release-check passed.")


if __name__ == "__main__":
    asyncio.run(main())
