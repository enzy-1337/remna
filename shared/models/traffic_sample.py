from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class TrafficSample(Base):
    """Сырые (несглаженные) замеры кумулятивного трафика пользователя — вход для детектора
    traffic_spike (существующий BillingTrafficMeter хранит только округлённые до ГБ шаги
    без истории по времени). Хранится ~48ч, чистится фоновой задачей.
    """

    __tablename__ = "traffic_samples"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    raw_bytes_total: Mapped[int] = mapped_column(BigInteger)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
