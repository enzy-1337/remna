from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class UserFraudState(Base):
    """Флаг «на учёте» и обучаемый baseline-профиль пользователя по каждому детектору."""

    __tablename__ = "user_fraud_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    is_watched: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    watched_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
