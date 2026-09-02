"""Персистентная копия ERROR+ логов (см. shared/services/admin_error_log_handler.py) —
позволяет искать/просматривать логи из бота, не заходя на сервер."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class AppErrorLog(Base):
    __tablename__ = "app_error_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(32), index=True)
    logger_name: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
