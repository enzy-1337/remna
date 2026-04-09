"""Промокоды и применение наград пользователям."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(32))
    value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # Если тип промокода подразумевает начисление "не деньгами", но у пользователя нет условия
    # (например, нет активной подписки) — админ задаёт запасную денежную награду.
    # Используется, например, для типа "subscription_days": дни выдаются только при наличии активной подписки,
    # иначе начисляются деньги на баланс.
    fallback_value_rub: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    usages: Mapped[list["PromoUsage"]] = relationship(
        "PromoUsage",
        back_populates="promo",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PromoUsage(Base):
    __tablename__ = "promo_usages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Для промокодов типа "topup_bonus_percent": бонус начисляется при первом успешном пополнении.
    # После применения фиксируем дату, чтобы не начислять повторно.
    topup_bonus_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    promo: Mapped["PromoCode"] = relationship("PromoCode", back_populates="usages")
