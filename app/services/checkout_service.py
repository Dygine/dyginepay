"""
Creating a payment, and completing one.

The completion path is the important part. A payment reaches CAPTURED through
`complete()`, which is called from two places that race by design:

    browser callback   - fast, but the customer's phone may die first
    Razorpay webhook   - slow, but it always arrives

Whichever runs first does the work. The second finds status == CAPTURED and
returns immediately. That is not an optimisation - it is the only reason a
customer whose browser crashed after paying still gets their subscription.

Nothing here trusts the browser. Both paths verify an HMAC signature before a
single row changes.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models import (
    CheckoutSession, Customer, Invoice, LedgerEntry, Order, Payment, Plan,
    Product, Subscription,
)
from app.models.enums import (
    AuditAction, CheckoutPurpose, CheckoutStatus, LedgerDirection, LedgerKind,
    OrderStatus, PaymentStatus, SubscriptionStatus, WalletTxnSource,
)
from app.services import audit, gst, invoice_service, razorpay_client, reference
from app.services import subscription_service, wallet_service, webhook_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------- customers --
def upsert_customer(db: Session, product: Product, data: dict) -> Customer:
    external_id = str(data.get("external_id") or "").strip()
    if not external_id:
        raise ValidationError("customer.external_id is required")

    customer = db.scalars(select(Customer).where(
        Customer.product_id == product.id,
        Customer.external_id == external_id)).first()

    if customer is None:
        customer = Customer(product_id=product.id, external_id=external_id,
                            name=str(data.get("name") or external_id)[:200])
        db.add(customer)

    for field in ("name", "email", "phone", "gstin", "state_code", "billing_address"):
        if data.get(field) is not None:
            setattr(customer, field, str(data[field])[:255] or None)
    if data.get("notes") is not None:
        customer.notes = data["notes"]

    db.flush()
    return customer


# -------------------------------------------------------------- checkout --
def create_session(db: Session, product: Product, payload: dict) -> CheckoutSession:
    purpose = str(payload.get("purpose") or CheckoutPurpose.ONE_OFF)
    if purpose not in set(CheckoutPurpose):
        raise ValidationError(f"Unknown purpose: {purpose}")

    customer = upsert_customer(db, product, payload.get("customer") or {})

    plan: Plan | None = None
    line_items = payload.get("line_items") or []

    if purpose == CheckoutPurpose.SUBSCRIPTION and payload.get("plan_code"):
        plan = db.scalars(select(Plan).where(
            Plan.product_id == product.id,
            Plan.code == payload["plan_code"],
            Plan.is_active.is_(True))).first()
        if plan is None:
            raise NotFoundError(f"No active plan with code {payload['plan_code']!r}")
        if not line_items:
            line_items = [{"description": plan.name, "amount": plan.amount_paise,
                           "quantity": 1, "sac": plan.sac or settings.DEFAULT_SAC}]

    if not line_items:
        raise ValidationError("line_items is required")

    for item in line_items:
        if "description" not in item or "amount" not in item:
            raise ValidationError("Each line item needs a description and an amount")
        if int(item["amount"]) < 0:
            raise ValidationError("Line item amount cannot be negative")

    doc = gst.compute(line_items, customer.state_code)
    if doc.total_paise < 100:
        # Razorpay's floor is 1 rupee. Rejecting here gives a clear error
        # instead of a confusing gateway refusal three steps later.
        raise ValidationError("Total must be at least \u20b91.00")

    order = Order(
        reference=reference.order_ref(), product_id=product.id,
        customer_id=customer.id, mode=settings.RAZORPAY_MODE,
        amount_paise=doc.total_paise, status=OrderStatus.CREATED,
        purpose=purpose, notes=payload.get("notes"))
    db.add(order)
    db.flush()

    rzp = razorpay_client.create_order(
        doc.total_paise, order.reference,
        notes={"dygine_order": order.reference, "product": product.slug,
               "customer": customer.external_id})
    order.razorpay_order_id = rzp.get("id")

    session = CheckoutSession(
        token=reference.checkout_token(), product_id=product.id,
        customer_id=customer.id, order_id=order.id, purpose=purpose,
        status=CheckoutStatus.CREATED, line_items=line_items,
        subtotal_paise=doc.subtotal_paise, tax_paise=doc.tax_paise,
        total_paise=doc.total_paise,
        success_url=payload.get("success_url"),
        cancel_url=payload.get("cancel_url"),
        plan_id=plan.id if plan else None,
        notes=payload.get("notes"),
        expires_at=_now() + timedelta(minutes=settings.CHECKOUT_SESSION_MINUTES))
    db.add(session)
    db.flush()
    return session


def session_response(session: CheckoutSession) -> dict:
    return {
        "id": str(session.id),
        "checkout_url": f"{settings.BASE_URL.rstrip('/')}/c/{session.token}",
        "status": session.status,
        "purpose": session.purpose,
        "subtotal": session.subtotal_paise,
        "tax": session.tax_paise,
        "amount": session.total_paise,
        "currency": "INR",
        "expires_at": session.expires_at.isoformat(),
    }


def load_session(db: Session, token: str) -> CheckoutSession:
    session = db.scalars(select(CheckoutSession).where(
        CheckoutSession.token == token)).first()
    if session is None:
        raise NotFoundError("This payment link is not valid")
    if (session.status == CheckoutStatus.CREATED
            and session.expires_at < _now()):
        session.status = CheckoutStatus.EXPIRED
        db.flush()
    return session


# ------------------------------------------------------------ completion --
def _find_or_create_payment(db: Session, order: Order,
                            razorpay_payment_id: str) -> Payment:
    existing = db.scalars(select(Payment).where(
        Payment.razorpay_payment_id == razorpay_payment_id)).first()
    if existing:
        return existing

    payment = Payment(
        reference=reference.payment_ref(), order_id=order.id,
        product_id=order.product_id, customer_id=order.customer_id,
        razorpay_payment_id=razorpay_payment_id,
        status=PaymentStatus.CREATED, amount_paise=order.amount_paise)
    db.add(payment)
    db.flush()
    return payment


def complete(db: Session, *, razorpay_order_id: str, razorpay_payment_id: str,
             verified_by: str, gateway_payload: dict | None = None) -> Payment:
    """
    Mark a payment captured and do everything that follows from it.

    Idempotent by construction: if the payment is already CAPTURED it returns
    immediately, so the callback and the webhook can both run and the customer
    is credited once.

    Callers must have verified a signature before calling this. There is no path
    into capture that does not go through a signature check.
    """
    order = db.scalars(select(Order).where(
        Order.razorpay_order_id == razorpay_order_id)).first()
    if order is None:
        raise NotFoundError(f"No order matches {razorpay_order_id}")

    payment = _find_or_create_payment(db, order, razorpay_payment_id)
    if payment.status == PaymentStatus.CAPTURED:
        return payment                      # the other path got here first

    # Read the real payment object: the fee and the GST on it are what make the
    # margin report true, and they are only available from the gateway.
    remote = gateway_payload or {}
    if not remote.get("fee"):
        try:
            remote = razorpay_client.fetch_payment(razorpay_payment_id)
        except Exception:                    # noqa: BLE001 - never block a capture
            remote = gateway_payload or {}

    payment.status = PaymentStatus.CAPTURED
    payment.captured_at = _now()
    payment.verified_by = verified_by
    payment.method = remote.get("method")
    payment.fee_paise = int(remote.get("fee") or 0)
    payment.fee_tax_paise = int(remote.get("tax") or 0)
    payment.gateway_payload = remote or None
    if remote.get("amount"):
        payment.amount_paise = int(remote["amount"])

    order.status = OrderStatus.PAID

    session = db.scalars(select(CheckoutSession).where(
        CheckoutSession.order_id == order.id)).first()
    if session:
        session.status = CheckoutStatus.PAID

    product = db.get(Product, order.product_id)
    customer = db.get(Customer, order.customer_id)

    # --- ledger: revenue in, gateway fee out -------------------------------
    today = date.today()
    db.add(LedgerEntry(
        product_id=product.id, customer_id=customer.id, entry_date=today,
        direction=LedgerDirection.INFLOW, kind=LedgerKind.PAYMENT,
        amount_paise=payment.amount_paise,
        description=f"Payment {payment.reference}",
        reference_type="payment", reference_id=str(payment.id)))
    if payment.fee_paise:
        db.add(LedgerEntry(
            product_id=product.id, customer_id=customer.id, entry_date=today,
            direction=LedgerDirection.OUTFLOW, kind=LedgerKind.GATEWAY_FEE,
            amount_paise=payment.fee_paise,
            description="Razorpay platform fee",
            reference_type="payment", reference_id=str(payment.id)))
    if payment.fee_tax_paise:
        db.add(LedgerEntry(
            product_id=product.id, customer_id=customer.id, entry_date=today,
            direction=LedgerDirection.OUTFLOW, kind=LedgerKind.GATEWAY_TAX,
            amount_paise=payment.fee_tax_paise,
            description="GST on Razorpay fee",
            reference_type="payment", reference_id=str(payment.id)))

    # --- invoice -----------------------------------------------------------
    line_items = (session.line_items if session
                  else [{"description": f"Payment {payment.reference}",
                         "amount": payment.amount_paise, "quantity": 1}])
    invoice = invoice_service.for_payment(db, payment.id)
    if invoice is None:
        invoice = invoice_service.issue(
            db, product=product, customer=customer, line_items=line_items,
            payment=payment, notes={"order": order.reference})

    # --- what the payment was for ------------------------------------------
    if order.purpose == CheckoutPurpose.WALLET_TOPUP:
        # Credit the taxable value, not the gross. The customer bought
        # credit worth the pre-tax amount; the GST went to the government.
        credit_amount = session.subtotal_paise if session else payment.amount_paise
        wallet_service.credit(
            db, customer.id, credit_amount, source=WalletTxnSource.TOPUP,
            description=f"Top-up via {payment.reference}",
            reference_type="payment", reference_id=str(payment.id),
            idempotency_key=f"topup:{payment.id}")

    elif order.purpose == CheckoutPurpose.SUBSCRIPTION and session and session.plan_id:
        subscription_service.activate(
            db, customer_id=customer.id, product_id=product.id,
            plan_id=session.plan_id, payment=payment)

    audit.record(db, actor=f"api:{product.slug}",
                 action=AuditAction.PAYMENT_CAPTURED, entity_type="payment",
                 entity_id=str(payment.id),
                 summary=f"{payment.reference} captured via {verified_by}")

    db.flush()

    webhook_service.enqueue(db, product, "payment.captured",
                            payment_payload(db, payment, invoice))
    webhook_service.enqueue(db, product, "invoice.issued",
                            invoice_payload(invoice))
    return payment


def fail(db: Session, *, razorpay_order_id: str, razorpay_payment_id: str | None,
         reason: str, gateway_payload: dict | None = None) -> Payment | None:
    order = db.scalars(select(Order).where(
        Order.razorpay_order_id == razorpay_order_id)).first()
    if order is None:
        return None

    payment = None
    if razorpay_payment_id:
        payment = _find_or_create_payment(db, order, razorpay_payment_id)
        if payment.status == PaymentStatus.CAPTURED:
            return payment          # a late failure event cannot un-capture
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = reason[:2000]
        payment.gateway_payload = gateway_payload

    order.status = OrderStatus.FAILED
    db.flush()

    product = db.get(Product, order.product_id)
    if payment:
        webhook_service.enqueue(db, product, "payment.failed",
                                payment_payload(db, payment, None))
    return payment


# --------------------------------------------------------------- payloads --
def payment_payload(db: Session, payment: Payment, invoice: Invoice | None) -> dict:
    customer = db.get(Customer, payment.customer_id)
    order = db.get(Order, payment.order_id)
    if invoice is None:
        invoice = invoice_service.for_payment(db, payment.id)
    return {
        "id": payment.reference,
        "status": payment.status,
        "amount": payment.amount_paise,
        "currency": "INR",
        "method": payment.method,
        "captured_at": payment.captured_at.isoformat() if payment.captured_at else None,
        "failure_reason": payment.failure_reason,
        "order": {"id": order.reference, "purpose": order.purpose,
                  "notes": order.notes} if order else None,
        "customer": {"external_id": customer.external_id,
                     "name": customer.name} if customer else None,
        "invoice": {"id": str(invoice.id), "number": invoice.number,
                    "total": invoice.total_paise} if invoice else None,
    }


def invoice_payload(invoice: Invoice) -> dict:
    return {
        "id": str(invoice.id),
        "number": invoice.number,
        "kind": invoice.kind,
        "issue_date": invoice.issue_date.isoformat(),
        "subtotal": invoice.subtotal_paise,
        "cgst": invoice.cgst_paise,
        "sgst": invoice.sgst_paise,
        "igst": invoice.igst_paise,
        "total": invoice.total_paise,
        "pdf_url": f"{settings.BASE_URL.rstrip('/')}/v1/invoices/{invoice.id}/pdf",
    }
