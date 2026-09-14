from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import BillingInterval, SubscriptionStatus


class Plan(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "plans"

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    interval: Mapped[str] = mapped_column(String(20), default=BillingInterval.MONTHLY)
    trial_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sac: Mapped[str | None] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    meta: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (Index("uq_plan_code", "product_id", "code", unique=True),)


class Subscription(Base, UUIDMixin, TimestampMixin):
    """
    Deliberately not Razorpay Subscriptions with a mandate. Renewal here is an
    explicit checkout each cycle, because a failed auto-debit on a mandate is a
    support problem you own and a reminder email is one the customer owns.
    """
    __tablename__ = "subscriptions"

    reference: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"), nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=SubscriptionStatus.TRIAL)
    current_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    current_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    grace_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)

    last_payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_sent_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_sub_status_end", "status", "current_period_end"),
        Index("ix_sub_customer", "customer_id"),
    )
