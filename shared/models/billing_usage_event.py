from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class BillingUsageEvent(Base):
    __tablename__ = "billing_usage_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    usage_gb_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_hwid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_mobile_internet: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
