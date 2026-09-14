from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import DeliveryStatus


class InboundEvent(Base, UUIDMixin, TimestampMixin):
    """
    Every webhook Razorpay sends, stored raw before anything is parsed.

    event_id is Razorpay's own id and is unique here, which is what makes
    processing idempotent: a redelivered event hits the constraint and is
    acknowledged without being applied twice.
    """
    __tablename__ = "inbound_events"

    event_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    signature: Mapped[str | None] = mapped_column(String(128))
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)

    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    process_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_inbound_type_created", "event_type", "created_at"),)


class OutboundDelivery(Base, UUIDMixin, TimestampMixin):
    """
    A webhook Dygine owes one of your tools.

    Retry schedule is 0s, 30s, 2m, 10m, 1h, 6h, 24h and then DEAD, which stays
    visible in admin with a replay button rather than disappearing.
    """
    __tablename__ = "outbound_deliveries"

    BACKOFF_SECONDS = (0, 30, 120, 600, 3600, 21600, 86400)

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    event: Mapped[str] = mapped_column(String(60), nullable=False)
    event_id: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=DeliveryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_outbound_due", "status", "next_attempt_at"),
        Index("ix_outbound_product", "product_id", "created_at"),
    )
