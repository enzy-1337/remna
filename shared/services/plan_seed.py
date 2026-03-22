"""Дефолтные тарифы как в миграции 0001 — если строк с такими именами нет, создаём."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.plan import Plan

_DEFAULTS: tuple[tuple[str, int, Decimal, Decimal, int | None, bool, int], ...] = (
    ("Триал", 3, Decimal("0"), Decimal("0"), 1, True, 0),
    ("1 месяц", 30, Decimal("130"), Decimal("0"), None, True, 10),
    ("2 месяца", 60, Decimal("255"), Decimal("2"), None, True, 20),
    ("3 месяца", 90, Decimal("370"), Decimal("5"), None, True, 30),
)


async def ensure_default_plans_if_needed(session: AsyncSession) -> bool:
    """Возвращает True, если были вставки или правки."""
    changed = False
    for name, days, price, disc, traffic_gb, active, sort in _DEFAULTS:
        q = await session.execute(select(Plan).where(Plan.name == name))
        p = q.scalar_one_or_none()
        if p is None:
            session.add(
                Plan(
                    name=name,
                    duration_days=days,
                    price_rub=price,
                    discount_percent=disc,
                    traffic_limit_gb=traffic_gb,
                    is_active=active,
                    sort_order=sort,
                )
            )
            changed = True
        elif name == "Триал" and not p.is_active:
            p.is_active = True
            changed = True
    return changed
