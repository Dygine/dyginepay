"""
Every state a thing can be in.

Stored as plain strings, not native PG enums. Adding a value to a PG enum needs
a migration and locks the type; adding one here does not. The tradeoff is that
the database will not reject an unknown string, which the service layer does.
"""
from __future__ import annotations

from enum import StrEnum


class KeyMode(StrEnum):
    TEST = "test"
    LIVE = "live"


class ProductStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class CheckoutPurpose(StrEnum):
    ONE_OFF = "one_off"
    WALLET_TOPUP = "wallet_topup"
    SUBSCRIPTION = "subscription"


class CheckoutStatus(StrEnum):
    CREATED = "created"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OrderStatus(StrEnum):
    CREATED = "created"
    ATTEMPTED = "attempted"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class RefundStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class InvoiceKind(StrEnum):
    TAX_INVOICE = "tax_invoice"      # GST registered
    BILL_OF_SUPPLY = "bill_of_supply"  # not registered, or exempt supply
    CREDIT_NOTE = "credit_note"


class InvoiceStatus(StrEnum):
    ISSUED = "issued"
    CANCELLED = "cancelled"


class TaxTreatment(StrEnum):
    INTRA_STATE = "intra_state"   # CGST + SGST
    INTER_STATE = "inter_state"   # IGST
    NONE = "none"                 # not GST registered


class WalletTxnType(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


class WalletTxnSource(StrEnum):
    TOPUP = "topup"
    CONSUMPTION = "consumption"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    SUBSCRIPTION = "subscription"


class SubscriptionStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class BillingInterval(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    HALF_YEARLY = "half_yearly"
    YEARLY = "yearly"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD = "dead"          # retries exhausted, needs a manual replay


class LedgerDirection(StrEnum):
    INFLOW = "inflow"      # money in: a captured payment
    OUTFLOW = "outflow"    # money out: a refund, a gateway fee


class LedgerKind(StrEnum):
    PAYMENT = "payment"
    GATEWAY_FEE = "gateway_fee"
    GATEWAY_TAX = "gateway_tax"
    REFUND = "refund"
    COST = "cost"


class AuditAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    KEY_ISSUED = "key_issued"
    KEY_REVOKED = "key_revoked"
    PAYMENT_CAPTURED = "payment_captured"
    REFUND_CREATED = "refund_created"
    INVOICE_ISSUED = "invoice_issued"
    INVOICE_CANCELLED = "invoice_cancelled"
    WALLET_ADJUSTED = "wallet_adjusted"
    WEBHOOK_REPLAYED = "webhook_replayed"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
