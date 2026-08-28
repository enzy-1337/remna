from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class TelegramBlacklistSyncState(Base):
    """Синглтон (id=1): состояние синка внешнего blacklist.txt (ETag + счётчик)."""

    __tablename__ = "telegram_blacklist_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_id_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
