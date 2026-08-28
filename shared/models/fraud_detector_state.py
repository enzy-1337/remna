from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class FraudDetectorState(Base):
    """Системная (не per-user) стадия зрелости одного детектора: learning/advisory/autonomous.

    Только для детекторов со staged rollout (ip_hop, hwid_collision, traffic_spike).
    Жёсткое IP-правило и внешний blacklist всегда работают немедленно и сюда не попадают.
    """

    __tablename__ = "fraud_detector_state"

    detector: Mapped[str] = mapped_column(String(32), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16), default="learning", server_default="learning")
    stage_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    thresholds: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    auto_block_confidence_threshold: Mapped[float] = mapped_column(Float, default=0.9, server_default="0.9")
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
