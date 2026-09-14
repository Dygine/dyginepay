from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Date, DateTime, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import InvoiceKind, InvoiceStatus, TaxTreatment


class InvoiceSequence(Base, TimestampMixin):
    """
    The invoice counter, one row per (financial year, kind).

    GST requires a consecutive series with no gaps, so the number is taken by
    locking this row inside the same transaction that writes the invoice - never
    by counting existing invoices, which races and skips under concurrency.
    """
    __tablename__ = "invoice_sequences"

    fy: Mapped[str] = mapped_column(String(7), primary_key=True)      # "26-27"
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Invoice(Base, UUIDMixin, TimestampMixin):
    """
    Immutable once issued. A mistake becomes a credit note, never an edit -
    which is both the GST rule and the only way the ledger stays trustworthy.
    """
    __tablename__ = "invoices"

    number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    fy: Mapped[str] = mapped_column(String(7), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default=InvoiceKind.TAX_INVOICE)
    status: Mapped[str] = mapped_column(String(20), default=InvoiceStatus.ISSUED)

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"))
    # Set on a credit note, pointing at what it reverses.
    reverses_invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))

    issue_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Snapshot of both parties at issue time. A customer who later changes their
    # address must not retroactively change an invoice already filed.
    seller_name: Mapped[str] = mapped_column(String(200), nullable=False)
    seller_address: Mapped[str] = mapped_column(Text, default="")
    seller_gstin: Mapped[str | None] = mapped_column(String(15))
    seller_state_code: Mapped[str] = mapped_column(String(2), default="29")
    buyer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    buyer_address: Mapped[str] = mapped_column(Text, default="")
    buyer_gstin: Mapped[str | None] = mapped_column(String(15))
    buyer_state_code: Mapped[str | None] = mapped_column(String(2))
    place_of_supply: Mapped[str | None] = mapped_column(String(2))

    tax_treatment: Mapped[str] = mapped_column(String(20), default=TaxTreatment.NONE)
    subtotal_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cgst_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sgst_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    igst_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)

    notes: Mapped[dict | None] = mapped_column(JSONB)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan",
        order_by="InvoiceLine.position")

    __table_args__ = (
        Index("ix_invoice_customer", "customer_id", "issue_date"),
        Index("ix_invoice_product", "product_id", "issue_date"),
    )


class InvoiceLine(Base, UUIDMixin):
    __tablename__ = "invoice_lines"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    sac: Mapped[str | None] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    taxable_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_rate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cgst_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sgst_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    igst_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
