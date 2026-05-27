from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.services.subscription_repeat_bonus import (
    count_completed_subscription_purchases,
    grant_repeat_subscription_purchase_bonus,
)


@pytest.mark.asyncio
async def test_grant_repeat_bonus_only_when_repeat():
    settings = MagicMock()
    settings.subscription_repeat_purchase_bonus_percent = Decimal("5")
    user = MagicMock()
    user.id = 1
    user.balance = Decimal("100")
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    bonus = await grant_repeat_subscription_purchase_bonus(
        session,
        user=user,
        price_rub=Decimal("1000"),
        settings=settings,
        purchase_txn_id=42,
        is_repeat_purchase=False,
    )
    assert bonus == Decimal("0")
    session.add.assert_not_called()

    bonus2 = await grant_repeat_subscription_purchase_bonus(
        session,
        user=user,
        price_rub=Decimal("1000"),
        settings=settings,
        purchase_txn_id=43,
        is_repeat_purchase=True,
    )
    assert bonus2 == Decimal("50.00")
    assert user.balance == Decimal("150.00")
    session.add.assert_called_once()
