"""
Refunds, which for a closed-loop wallet means one thing: money only ever goes
back the way it came.

There is no "refund to bank account" here and there never will be. A refund
reverses through the original Razorpay payment. That is what keeps the wallet a
closed system prepaid instrument rather than something needing RBI authorisation.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models import Customer, LedgerEntry, Payment, Product, Refund
from app.models.enums import (
    AuditAction, LedgerDirection, LedgerKind, PaymentStatus, RefundStatus,
    WalletTxnSource,
)
from app.services import audit, invoice_service, razorpay_client, reference
from app.services import wallet_service, webhook_service


def create(db: Session, payment: Payment, *, amount_paise: int | None = None,
           reason: str = "", actor: str = "admin") -> Refund:
    if payment.status not in (PaymentStatus.CAPTURED,
                              PaymentStatus.PARTIALLY_REFUNDED):
        raise ConflictError("Only a captured payment can be refunded")

    refundable = payment.amount_paise - payment.refunded_paise
    amount = refundable if amount_paise is None else int(amount_paise)
    if amount <= 0:
        raise ValidationError("Refund amount must be positive")
    if amount > refundable:
        raise ValidationError(
            f"Only \u20b9{refundable / 100:.2f} is still refundable on this payment")

    remote = razorpay_client.create_refund(
        payment.razorpay_payment_id, amount,
        notes={"dygine_payment": payment.reference, "reason": reason[:200]})

    refund = Refund(
        reference=reference.refund_ref(), payment_id=payment.id,
        razorpay_refund_id=remote.get("id"), amount_paise=amount,
        status=(RefundStatus.PROCESSED if remote.get("status") == "processed"
                else RefundStatus.PENDING),
        reason=reason, gateway_payload=remote,
        processed_at=datetime.now(timezone.utc)
        if remote.get("status") == "processed" else None)
    db.add(refund)

    payment.refunded_paise += amount
    payment.status = (PaymentStatus.REFUNDED
                      if payment.refunded_paise >= payment.amount_paise
                      else PaymentStatus.PARTIALLY_REFUNDED)

    product = db.get(Product, payment.product_id)
    customer = db.get(Customer, payment.customer_id)

    db.add(LedgerEntry(
        product_id=product.id, customer_id=customer.id, entry_date=date.today(),
        direction=LedgerDirection.OUTFLOW, kind=LedgerKind.REFUND,
        amount_paise=amount, description=f"Refund {refund.reference}",
        reference_type="refund", reference_id=str(refund.id)))

    # If this paid for wallet credit, claw the credit back. A customer must not
    # keep spendable balance they have been refunded for.
    balance = wallet_service.balance(db, customer.id)
    if balance > 0:
        clawback = min(balance, amount)
        wallet_service.debit(
            db, customer.id, clawback, source=WalletTxnSource.REFUND,
            description=f"Reversal for refund {refund.reference}",
            reference_type="refund", reference_id=str(refund.id),
            idempotency_key=f"refund:{refund.id}")

    invoice = invoice_service.for_payment(db, payment.id)
    note = None
    if invoice is not None:
        note = invoice_service.credit_note(db, invoice, reason=reason,
                                           amount_paise=None if amount >= payment.amount_paise
                                           else None)

    audit.record(db, actor=actor, action=AuditAction.REFUND_CREATED,
                 entity_type="refund", entity_id=str(refund.id),
                 summary=f"{refund.reference} for {payment.reference}")
    db.flush()

    webhook_service.enqueue(db, product, "refund.processed", {
        "id": refund.reference,
        "payment_id": payment.reference,
        "amount": refund.amount_paise,
        "status": refund.status,
        "reason": refund.reason,
        "credit_note": note.number if note else None,
    })
    return refund
