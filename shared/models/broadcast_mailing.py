"""Шаблоны рассылки и отложенная отправка (web-admin)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class BroadcastTemplate(Base):
    __tablename__ = "broadcast_templates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text())
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScheduledBroadcast(Base):
    __tablename__ = "scheduled_broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    body_text: Mapped[str] = mapped_column(Text())
    send_to_users: Mapped[bool] = mapped_column(default=True, server_default="true")
    send_to_channel: Mapped[bool] = mapped_column(default=False, server_default="false")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", index=True)
    error_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BroadcastHistory(Base):
    """Лог отправленных массовых рассылок (черновик текста как в админке)."""

    __tablename__ = "broadcast_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    body_text: Mapped[str] = mapped_column(Text())
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    recipients_ok: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    recipients_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source: Mapped[str] = mapped_column(String(32), default="mass", server_default="mass")
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
