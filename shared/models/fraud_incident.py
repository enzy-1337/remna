from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class FraudIncident(Base):
    """Одно срабатывание любого детектора антифрода (детали: shared/services/fraud/)."""

    __tablename__ = "fraud_incidents"
    __table_args__ = (
        Index("ix_fraud_incidents_user_detector_ts", "user_id", "detector", "event_ts"),
        Index("ix_fraud_incidents_status_detector", "status", "detector"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    detector: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    stage_at_detection: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    action_taken: Mapped[str] = mapped_column(String(24), default="none", server_default="none")
    status: Mapped[str] = mapped_column(String(16), index=True, default="open", server_default="open")
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    topic_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
