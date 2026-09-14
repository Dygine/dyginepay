"""
Subscriptions, and what happens when nobody pays.

    TRIAL/ACTIVE  -- period ends -->  EXPIRED    (grace period starts)
    EXPIRED       -- grace ends  -->  SUSPENDED

Both reversible, neither deletes anything. A customer who pays two months late
gets everything back, because expired and suspended are states on a row, not a
data retention policy.

What suspension means is decided by *your tool*, not here. Dygine sends
`subscription.expired`; pgdesk decides that a suspended PG can still read its
data but cannot add a resident. That asymmetry matters - the residents did not
miss the payment.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models import Customer, Payment, Plan, Product, Subscription
from app.models.enums import BillingInterval, SubscriptionStatus
from app.services import reference, webhook_service

INTERVAL_DAYS = {
    BillingInterval.MONTHLY: 30,
    BillingInterval.QUARTERLY: 91,
    BillingInterval.HALF_YEARLY: 182,
    BillingInterval.YEARLY: 365,
}


def period_end(start: date, interval: str) -> date:
    return start + timedelta(days=INTERVAL_DAYS.get(interval, 30))


def activate(db: Session, *, customer_id: uuid.UUID, product_id: uuid.UUID,
             plan_id: uuid.UUID, payment: Payment | None = None) -> Subscription:
    """
    Start or renew. A renewal extends from the existing period end when that is
    still in the future, so paying early does not cost the customer days.
    """
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise NotFoundError("Plan not found")

    sub = db.scalars(select(Subscription).where(
        Subscription.customer_id == customer_id,
        Subscription.plan_id == plan_id).order_by(
            Subscription.created_at.desc())).first()

    today = date.today()
    if sub is None:
        sub = Subscription(
            reference=reference.subscription_ref(), product_id=product_id,
            customer_id=customer_id, plan_id=plan_id,
            status=SubscriptionStatus.ACTIVE, current_period_start=today,
            current_period_end=period_end(today, plan.interval))
        db.add(sub)
    else:
        start = (sub.current_period_end if sub.current_period_end > today else today)
        sub.current_period_start = start
        sub.current_period_end = period_end(start, plan.interval)
        sub.status = SubscriptionStatus.ACTIVE
        sub.cancelled_at = None
        sub.reminder_sent_on = None

    if payment is not None:
        sub.last_payment_id = payment.id

    db.flush()
    product = db.get(Product, product_id)
    webhook_service.enqueue(db, product, "subscription.activated", payload(db, sub))
    return sub


def cancel(db: Session, sub: Subscription) -> Subscription:
    """Cancel at period end. Access already paid for is not taken away."""
    sub.status = SubscriptionStatus.CANCELLED
    sub.cancelled_at = datetime.now(timezone.utc)
    db.flush()
    product = db.get(Product, sub.product_id)
    webhook_service.enqueue(db, product, "subscription.cancelled", payload(db, sub))
    return sub


def run_lifecycle(db: Session) -> dict:
    """Daily job. Two transitions, each a separate day, never one straight to locked out."""
    today = date.today()
    expired = suspended = 0

    for sub in db.scalars(select(Subscription).where(
            Subscription.status.in_([SubscriptionStatus.TRIAL,
                                     SubscriptionStatus.ACTIVE]),
            Subscription.current_period_end < today)):
        sub.status = SubscriptionStatus.EXPIRED
        expired += 1
        product = db.get(Product, sub.product_id)
        webhook_service.enqueue(db, product, "subscription.expired", payload(db, sub))

    for sub in db.scalars(select(Subscription).where(
            Subscription.status == SubscriptionStatus.EXPIRED)):
        if today > sub.current_period_end + timedelta(days=sub.grace_days):
            sub.status = SubscriptionStatus.SUSPENDED
            suspended += 1
            product = db.get(Product, sub.product_id)
            webhook_service.enqueue(db, product, "subscription.suspended",
                                    payload(db, sub))

    db.commit()
    return {"expired": expired, "suspended": suspended}


def payload(db: Session, sub: Subscription) -> dict:
    plan = db.get(Plan, sub.plan_id)
    customer = db.get(Customer, sub.customer_id)
    return {
        "id": sub.reference,
        "status": sub.status,
        "current_period_start": sub.current_period_start.isoformat(),
        "current_period_end": sub.current_period_end.isoformat(),
        "grace_days": sub.grace_days,
        "plan": {"code": plan.code, "name": plan.name,
                 "amount": plan.amount_paise, "interval": plan.interval} if plan else None,
        "customer": {"external_id": customer.external_id,
                     "name": customer.name} if customer else None,
    }
