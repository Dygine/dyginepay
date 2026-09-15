"""
Refunds, which for a closed-loop wallet means one thing: money only ever goes
back the way it came.

There is no "refund to bank account" here and there never will be. A refund
reverses through the original Razorpay payment. That is what keeps the wallet a
closed system prepaid instrument rather than something needing RBI authorisation.

Two things here are easy to get wrong and both take money off a customer twice:

  - Clawing back wallet balance on a refund that had nothing to do with the
    wallet. Only a WALLET_TOPUP payment bought spendable credit; refunding a
    subscription must leave the balance alone.
  - Issuing a full credit note for a partial refund. The credit note is the GST
    document that reverses the invoice, so it has to name the amount actually
    returned, not the whole invoice.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models import Customer, LedgerEntry, Order, Payment, Product, Refund
from app.models.enums import (
    AuditAction, CheckoutPurpose, LedgerDirection, LedgerKind, PaymentStatus,
    RefundStatus, WalletTxnSource,
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

    # Claw the credit back only when this payment actually BOUGHT wallet credit.
    #
    # The purpose check is the whole point. Without it, refunding a subscription
    # for a customer who happens to hold a balance debits that balance too - so
    # they get the money back on their card and lose the same amount off their
    # wallet. Only a top-up ever created spendable credit, so only a top-up can
    # have it reversed.
    order = db.get(Order, payment.order_id)
    if order is not None and order.purpose == CheckoutPurpose.WALLET_TOPUP:
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
        note = invoice_service.credit_note(
            db, invoice, reason=reason,
            amount_paise=_reversal_taxable(invoice, payment, amount))

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


def _reversal_taxable(invoice, payment: Payment, amount: int) -> int | None:
    """
    What the credit note should reverse, as a taxable value.

    `credit_note()` takes a taxable amount and computes tax on top of it, while
    `amount` here is the gross that went back to the card. Those are the same
    number only while no tax was charged, so a refund on a tax invoice has to be
    reduced to its taxable share first - otherwise the credit note reverses more
    than the invoice ever charged.

    Returns None for a full refund, which tells credit_note() to reverse the
    whole invoice exactly rather than recomputing it from a rounded figure.
    """
    if amount >= payment.amount_paise:
        return None
    if invoice.total_paise and invoice.subtotal_paise != invoice.total_paise:
        return amount * invoice.subtotal_paise // invoice.total_paise
    return amount