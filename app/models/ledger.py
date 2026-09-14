from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import BigInteger, Date, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import LedgerDirection, LedgerKind


class LedgerEntry(Base, UUIDMixin, TimestampMixin):
    """
    One row per money movement, per product. This - not the payments table - is
    what the profitability report reads, because a fee and a refund are money
    movements too and they do not have a payment row of their own.
    """
    __tablename__ = "ledger_entries"

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))

    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # LedgerDirection
    kind: Mapped[str] = mapped_column(String(20), nullable=False)       # LedgerKind
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)  # positive

    description: Mapped[str] = mapped_column(Text, default="")
    reference_type: Mapped[str | None] = mapped_column(String(40))
    reference_id: Mapped[str | None] = mapped_column(String(80))
    meta: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_ledger_product_date", "product_id", "entry_date"),
        Index("ix_ledger_kind", "kind", "entry_date"),
    )


class Cost(Base, UUIDMixin, TimestampMixin):
    """
    What a product costs you - hosting, domain, an API subscription. Recorded by
    hand in admin. Revenue minus gateway fees minus these is the margin number.
    """
    __tablename__ = "costs"

    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"))
    incurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(60), default="other")
    description: Mapped[str] = mapped_column(Text, default="")
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recurring_monthly: Mapped[bool] = mapped_column(default=False, nullable=False)

    __table_args__ = (Index("ix_cost_product_date", "product_id", "incurred_on"),)
