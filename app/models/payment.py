from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import (
    CheckoutPurpose, CheckoutStatus, OrderStatus, PaymentStatus, RefundStatus,
)


class CheckoutSession(Base, UUIDMixin, TimestampMixin):
    """
    What the customer opens. The token is the URL, so it is long and random and
    it expires - a checkout link is not a permanent resource.
    """
    __tablename__ = "checkout_sessions"

    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))

    purpose: Mapped[str] = mapped_column(String(20), default=CheckoutPurpose.ONE_OFF)
    status: Mapped[str] = mapped_column(String(20), default=CheckoutStatus.CREATED)

    # The lines as sent by the tool, kept verbatim so the invoice can be rebuilt.
    line_items: Mapped[list] = mapped_column(JSONB, nullable=False)
    subtotal_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)

    success_url: Mapped[str | None] = mapped_column(Text)
    cancel_url: Mapped[str | None] = mapped_column(Text)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"))
    notes: Mapped[dict | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_checkout_status", "status", "expires_at"),)


class Order(Base, UUIDMixin, TimestampMixin):
    """Dygine's order, mapped one-to-one onto a Razorpay order."""
    __tablename__ = "orders"

    reference: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)

    razorpay_order_id: Mapped[str | None] = mapped_column(String(60), unique=True)
    mode: Mapped[str] = mapped_column(String(10), default="test", nullable=False)

    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.CREATED)
    purpose: Mapped[str] = mapped_column(String(20), default=CheckoutPurpose.ONE_OFF)
    notes: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (Index("ix_order_product_created", "product_id", "created_at"),)


class Payment(Base, UUIDMixin, TimestampMixin):
    """
    A payment reaches CAPTURED only through a verified signature - either the
    browser callback or the Razorpay webhook, whichever arrives first. The other
    one then finds it already captured and does nothing.
    """
    __tablename__ = "payments"

    reference: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)

    razorpay_payment_id: Mapped[str | None] = mapped_column(String(60), unique=True)
    status: Mapped[str] = mapped_column(String(25), default=PaymentStatus.CREATED)
    method: Mapped[str | None] = mapped_column(String(30))    # upi, card, netbanking

    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Razorpay's actual fee and the GST on it, read off the payment object.
    # Never estimated at 2% - the real number is what makes margin reporting true.
    fee_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    fee_tax_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    refunded_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[str | None] = mapped_column(String(20))   # callback | webhook
    gateway_payload: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_payment_product_status", "product_id", "status"),
        Index("ix_payment_captured", "captured_at"),
    )


class Refund(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "refunds"

    reference: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), nullable=False)
    razorpay_refund_id: Mapped[str | None] = mapped_column(String(60), unique=True)

    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=RefundStatus.PENDING)
    reason: Mapped[str] = mapped_column(Text, default="")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gateway_payload: Mapped[dict | None] = mapped_column(JSONB)
