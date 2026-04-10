"""Когда применять pay-per-use списания (вебхуки, будущие джобы)."""

from __future__ import annotations

from shared.config import Settings
from shared.models.user import User


def applies_pay_per_use_charges(user: User, settings: Settings) -> bool:
    """Списания за ГБ/устройства — только при включённом v2 и режиме пользователя hybrid."""
    if not settings.billing_v2_enabled:
        return False
    return user.billing_mode == "hybrid"
