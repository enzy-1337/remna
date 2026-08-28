from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class TelegramBlacklistEntry(Base):
    """Один telegram_id из внешнего/ручного списка нарушителей."""

    __tablename__ = "telegram_blacklist_entries"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String(32), default="bedolaga_dev", server_default="bedolaga_dev")
