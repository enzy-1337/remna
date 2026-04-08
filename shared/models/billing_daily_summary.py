from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class BillingDailySummary(Base):
    __tablename__ = "billing_daily_summary"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_billing_daily_summary_user_day"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    gb_units: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    device_units: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    mobile_gb_units: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    gb_amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    device_amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    mobile_amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    total_amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
