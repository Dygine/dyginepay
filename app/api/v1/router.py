"""The public API your tools call. Everything here needs a valid API key."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Header, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Caller, authenticate, remember, replay_or_none
from app.core.database import get_db
from app.core.exceptions import NotFoundError, ValidationError
from app.models import Customer, Invoice, Payment, Plan, Subscription, Wallet
from app.models.enums import WalletTxnSource
from app.services import (
    checkout_service, invoice_service, pdf_service, refund_service,
    subscription_service, wallet_service,
)

router = APIRouter(prefix="/v1", tags=["api"])


def _customer(db: Session, caller: Caller, external_id: str) -> Customer:
    customer = db.scalars(select(Customer).where(
        Customer.product_id == caller.product.id,
        Customer.external_id == external_id)).first()
    if customer is None:
        raise NotFoundError(f"No customer with external_id {external_id!r}")
    return customer


# ------------------------------------------------------------- checkout --
@router.post("/checkout/sessions")
def create_checkout_session(
    payload: dict = Body(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: Caller = Depends(authenticate),
    db: Session = Depends(get_db),
):
    """
    Create a payment and get back a URL to redirect the customer to.

    purpose is one_off | wallet_topup | subscription. Send an Idempotency-Key:
    without it, a retried request creates a second order and can charge twice.
    """
    cached = replay_or_none(db, caller, idempotency_key, "checkout.sessions", payload)
    if cached is not None:
        return cached

    session = checkout_service.create_session(db, caller.product, payload)
    body = checkout_service.session_response(session)
    remember(db, caller, idempotency_key, "checkout.sessions", payload, body)
    return body


# ------------------------------------------------------------- payments --
@router.get("/payments/{reference}")
def get_payment(reference: str, caller: Caller = Depends(authenticate),
                db: Session = Depends(get_db)):
    """
    Always safe to call. This is the fallback when your tool missed a webhook -
    poll it on boot and reconcile whatever you did not hear about.
    """
    payment = db.scalars(select(Payment).where(
        Payment.reference == reference,
        Payment.product_id == caller.product.id)).first()
    if payment is None:
        raise NotFoundError("Payment not found")
    return checkout_service.payment_payload(db, payment, None)


@router.get("/payments")
def list_payments(
    customer_external_id: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    caller: Caller = Depends(authenticate),
    db: Session = Depends(get_db),
):
    q = select(Payment).where(Payment.product_id == caller.product.id)
    if customer_external_id:
        q = q.where(Payment.customer_id == _customer(db, caller, customer_external_id).id)
    payments = db.scalars(q.order_by(Payment.created_at.desc()).limit(limit))
    return {"data": [checkout_service.payment_payload(db, p, None) for p in payments]}


@router.post("/payments/{reference}/refund")
def refund_payment(reference: str, payload: dict = Body(default={}),
                   caller: Caller = Depends(authenticate),
                   db: Session = Depends(get_db)):
    payment = db.scalars(select(Payment).where(
        Payment.reference == reference,
        Payment.product_id == caller.product.id)).first()
    if payment is None:
        raise NotFoundError("Payment not found")

    refund = refund_service.create(
        db, payment, amount_paise=payload.get("amount"),
        reason=str(payload.get("reason") or ""), actor=caller.actor)
    return {"id": refund.reference, "payment_id": payment.reference,
            "amount": refund.amount_paise, "status": refund.status}


# ------------------------------------------------------------ customers --
@router.post("/customers")
def upsert_customer(payload: dict = Body(...),
                    caller: Caller = Depends(authenticate),
                    db: Session = Depends(get_db)):
    customer = checkout_service.upsert_customer(db, caller.product, payload)
    return _customer_payload(db, customer)


@router.get("/customers/{external_id}")
def get_customer(external_id: str, caller: Caller = Depends(authenticate),
                 db: Session = Depends(get_db)):
    return _customer_payload(db, _customer(db, caller, external_id))


def _customer_payload(db: Session, customer: Customer) -> dict:
    return {
        "id": str(customer.id),
        "external_id": customer.external_id,
        "name": customer.name,
        "email": customer.email,
        "gstin": customer.gstin,
        "state_code": customer.state_code,
        "wallet_balance": wallet_service.balance(db, customer.id),
    }


# --------------------------------------------------------------- wallet --
@router.get("/customers/{external_id}/wallet")
def get_wallet(external_id: str, caller: Caller = Depends(authenticate),
               db: Session = Depends(get_db)):
    customer = _customer(db, caller, external_id)
    txns = wallet_service.transactions(db, customer.id, limit=50)
    return {
        "customer_external_id": external_id,
        "balance": wallet_service.balance(db, customer.id),
        "currency": "INR",
        "transactions": [{
            "id": str(t.id), "type": t.type, "source": t.source,
            "amount": t.amount_paise, "balance_after": t.balance_after,
            "description": t.description, "created_at": t.created_at.isoformat(),
        } for t in txns],
    }


@router.post("/wallet/debit")
def debit_wallet(
    payload: dict = Body(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: Caller = Depends(authenticate),
    db: Session = Depends(get_db),
):
    """
    Consume wallet balance. Send an Idempotency-Key - a retried debit without
    one charges the customer twice.

    There is deliberately no credit endpoint. Balance is created by a paid
    top-up or by an admin adjustment, never by a tool asking for it.
    """
    external_id = str(payload.get("customer_external_id") or "")
    if not external_id:
        raise ValidationError("customer_external_id is required")
    amount = int(payload.get("amount") or 0)

    customer = _customer(db, caller, external_id)
    txn = wallet_service.debit(
        db, customer.id, amount, source=WalletTxnSource.CONSUMPTION,
        description=str(payload.get("description") or "Usage"),
        reference_type=payload.get("reference_type"),
        reference_id=payload.get("reference_id"),
        idempotency_key=idempotency_key)
    return {"id": str(txn.id), "amount": txn.amount_paise,
            "balance": txn.balance_after, "description": txn.description}


# -------------------------------------------------------------- invoices --
@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: uuid.UUID, caller: Caller = Depends(authenticate),
                db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.product_id != caller.product.id:
        raise NotFoundError("Invoice not found")
    payload = checkout_service.invoice_payload(invoice)
    payload["lines"] = [{
        "description": l.description, "sac": l.sac, "quantity": l.quantity,
        "taxable": l.taxable_paise, "tax_rate": l.tax_rate,
        "total": l.total_paise} for l in invoice.lines]
    return payload


@router.get("/invoices/{invoice_id}/pdf")
def get_invoice_pdf(invoice_id: uuid.UUID, caller: Caller = Depends(authenticate),
                    db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.product_id != caller.product.id:
        raise NotFoundError("Invoice not found")
    pdf = pdf_service.render(invoice)
    filename = invoice.number.replace("/", "-") + ".pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'inline; filename="{filename}"'})


@router.get("/customers/{external_id}/invoices")
def list_customer_invoices(external_id: str, limit: int = Query(default=50, le=200),
                           caller: Caller = Depends(authenticate),
                           db: Session = Depends(get_db)):
    customer = _customer(db, caller, external_id)
    invoices = db.scalars(select(Invoice).where(
        Invoice.customer_id == customer.id)
        .order_by(Invoice.issue_date.desc()).limit(limit))
    return {"data": [checkout_service.invoice_payload(i) for i in invoices]}


# --------------------------------------------------------- subscriptions --
@router.get("/subscriptions/{reference}")
def get_subscription(reference: str, caller: Caller = Depends(authenticate),
                     db: Session = Depends(get_db)):
    sub = db.scalars(select(Subscription).where(
        Subscription.reference == reference,
        Subscription.product_id == caller.product.id)).first()
    if sub is None:
        raise NotFoundError("Subscription not found")
    return subscription_service.payload(db, sub)


@router.get("/customers/{external_id}/subscriptions")
def list_customer_subscriptions(external_id: str,
                                caller: Caller = Depends(authenticate),
                                db: Session = Depends(get_db)):
    customer = _customer(db, caller, external_id)
    subs = db.scalars(select(Subscription).where(
        Subscription.customer_id == customer.id)
        .order_by(Subscription.created_at.desc()))
    return {"data": [subscription_service.payload(db, s) for s in subs]}


@router.post("/subscriptions/{reference}/cancel")
def cancel_subscription(reference: str, caller: Caller = Depends(authenticate),
                        db: Session = Depends(get_db)):
    sub = db.scalars(select(Subscription).where(
        Subscription.reference == reference,
        Subscription.product_id == caller.product.id)).first()
    if sub is None:
        raise NotFoundError("Subscription not found")
    return subscription_service.payload(db, subscription_service.cancel(db, sub))


@router.get("/plans")
def list_plans(caller: Caller = Depends(authenticate), db: Session = Depends(get_db)):
    plans = db.scalars(select(Plan).where(
        Plan.product_id == caller.product.id, Plan.is_active.is_(True)))
    return {"data": [{"code": p.code, "name": p.name, "amount": p.amount_paise,
                      "interval": p.interval, "trial_days": p.trial_days}
                     for p in plans]}
