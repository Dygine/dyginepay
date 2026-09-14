from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import KeyMode, ProductStatus


class Product(Base, UUIDMixin, TimestampMixin):
    """One of your tools. pgdesk, hrlens, invoicepro."""
    __tablename__ = "products"

    slug: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=ProductStatus.ACTIVE, nullable=False)

    # Where Dygine posts outbound webhooks for this tool.
    webhook_url: Mapped[str | None] = mapped_column(Text)
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text)

    # Optional hardening: comma-separated IPs allowed to use this product's keys.
    ip_allowlist: Mapped[str] = mapped_column(Text, default="")

    keys: Mapped[list["ApiKey"]] = relationship(back_populates="product",
                                                cascade="all, delete-orphan")


class ApiKey(Base, UUIDMixin, TimestampMixin):
    """
    The secret is never stored. Only its SHA-256 hash is, and the plaintext is
    shown once at creation. A key that is lost is rotated, not recovered.
    """
    __tablename__ = "api_keys"

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), default=KeyMode.TEST, nullable=False)

    key_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # First few characters of the secret, for identifying a key in the UI.
    secret_hint: Mapped[str] = mapped_column(String(12), default="")

    label: Mapped[str] = mapped_column(String(120), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped[Product] = relationship(back_populates="keys")

    __table_args__ = (Index("ix_apikey_lookup", "key_id", "is_active"),)
