from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import WalletTxnSource, WalletTxnType


class Wallet(Base, UUIDMixin, TimestampMixin):
    """
    A closed-loop prepaid balance. Spendable only on Dygine products.

    The three things that must never become possible, or this stops being a
    closed system prepaid instrument and needs RBI authorisation:
      - withdrawing balance as cash
      - transferring balance to another customer
      - refunding anywhere except back through the original payment

    There is no API that does any of them, and the CHECK constraint below stops
    a balance going negative even if a service-layer guard is ever bypassed.
    """
    __tablename__ = "wallets"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), unique=True, nullable=False)
    balance_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    __table_args__ = (
        CheckConstraint("balance_paise >= 0", name="ck_wallet_non_negative"),
    )


class WalletTransaction(Base, UUIDMixin, TimestampMixin):
    """
    The ledger. balance_after is written at the time of the movement, so the
    history is auditable without replaying every row, and a nightly job can
    assert that sum(signed amounts) still equals Wallet.balance_paise.
    """
    __tablename__ = "wallet_transactions"

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False)   # WalletTxnType
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # WalletTxnSource

    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)  # always positive
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)

    description: Mapped[str] = mapped_column(Text, default="")
    reference_type: Mapped[str | None] = mapped_column(String(40))
    reference_id: Mapped[str | None] = mapped_column(String(80))
    # Deduplicates a retried debit. Unique per wallet.
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    meta: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_wallet_txn_positive"),
        # Partial unique index: many rows may have no idempotency key, but a
        # given key can only be used once per wallet. A plain unique index would
        # reject the second NULL on some engines, so the predicate is required.
        Index("uq_wallet_txn_idem", "wallet_id", "idempotency_key", unique=True,
              postgresql_where=text("idempotency_key IS NOT NULL")),
        Index("ix_wallet_txn_wallet", "wallet_id", "created_at"),
    )
