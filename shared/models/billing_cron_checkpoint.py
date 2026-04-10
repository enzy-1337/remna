from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class BillingCronCheckpoint(Base):
    """Идемпотентность фоновых биллинг-джобов (например, суточное списание за устройства)."""

    __tablename__ = "billing_cron_checkpoints"

    job_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_completed_day: Mapped[date] = mapped_column(Date, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
