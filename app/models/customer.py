from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Customer(Base, UUIDMixin, TimestampMixin):
    """
    The paying entity: a PG owner, an HR client.

    Identified by (product_id, external_id) where external_id is whatever the
    consuming tool calls it - a pgdesk organization uuid, for instance. The tool
    keeps its own customer record; this is a reference, not a copy.
    """
    __tablename__ = "customers"

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(20))

    # Billing identity. state_code decides CGST/SGST vs IGST on every invoice.
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str | None] = mapped_column(String(2))
    billing_address: Mapped[str] = mapped_column(Text, default="")

    notes: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("uq_customer_external", "product_id", "external_id", unique=True),
        Index("ix_customer_email", "email"),
    )
