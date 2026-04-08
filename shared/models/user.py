"""Модель пользователя Telegram / сервиса."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base

if TYPE_CHECKING:
    from shared.models.subscription import Subscription


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    remnawave_uuid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    billing_mode: Mapped[str] = mapped_column(String(16), default="legacy", server_default="legacy")
    lifetime_exempt_flag: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    risk_notified_24h_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    risk_notified_1h_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    referred_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    referral_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_subscribed_channel: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    trial_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(
        "Subscription",
        back_populates="user",
        foreign_keys="Subscription.user_id",
        cascade="all, delete-orphan",
    )
