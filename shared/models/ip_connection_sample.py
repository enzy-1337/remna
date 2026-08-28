from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class IpConnectionSample(Base):
    """Сырые наблюдения IP пользователя (Remnawave connections API) — вход для детектора ip_hop
    и источник «последней активности» в алертах. Хранится недолго, чистится фоновой задачей.
    """

    __tablename__ = "ip_connection_samples"
    __table_args__ = (
        Index("ix_ip_connection_samples_user_seen_desc", "user_id", "seen_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ip: Mapped[str] = mapped_column(String(64))
    node_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    asn_org: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
