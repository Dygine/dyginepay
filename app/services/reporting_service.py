"""
The numbers that answer "which tool actually makes money".

Revenue comes from the ledger, not the payments table, because a gateway fee and
a refund are money movements that have no payment row of their own.

Gateway fee is the *real* one, read off the Razorpay payment object at capture.
Estimating it at 2% would be wrong for UPI on RuPay credit (2.15%), for
international cards (3%), and for every month you are inside the zero-platform-fee
window - where estimating would show a loss that is not there.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Cost, Customer, LedgerEntry, Payment, Product
from app.models.enums import LedgerKind, PaymentStatus


def _sum(db: Session, product_id, kind: str, since: date, until: date) -> int:
    q = select(func.coalesce(func.sum(LedgerEntry.amount_paise), 0)).where(
        LedgerEntry.kind == kind,
        LedgerEntry.entry_date >= since,
        LedgerEntry.entry_date <= until)
    if product_id:
        q = q.where(LedgerEntry.product_id == product_id)
    return int(db.scalar(q) or 0)


def product_margin(db: Session, product: Product, since: date, until: date) -> dict:
    revenue = _sum(db, product.id, LedgerKind.PAYMENT, since, until)
    fee = _sum(db, product.id, LedgerKind.GATEWAY_FEE, since, until)
    fee_tax = _sum(db, product.id, LedgerKind.GATEWAY_TAX, since, until)
    refunds = _sum(db, product.id, LedgerKind.REFUND, since, until)

    costs = int(db.scalar(
        select(func.coalesce(func.sum(Cost.amount_paise), 0)).where(
            Cost.product_id == product.id,
            Cost.incurred_on >= since,
            Cost.incurred_on <= until)) or 0)

    net = revenue - refunds
    margin = net - fee - fee_tax - costs
    return {
        "product_id": str(product.id),
        "slug": product.slug,
        "name": product.name,
        "revenue_paise": revenue,
        "refunds_paise": refunds,
        "net_revenue_paise": net,
        "gateway_fee_paise": fee + fee_tax,
        "costs_paise": costs,
        "margin_paise": margin,
        "margin_percent": round(margin * 100 / net, 1) if net else 0.0,
    }


def profitability(db: Session, since: date, until: date) -> list[dict]:
    rows = [product_margin(db, p, since, until)
            for p in db.scalars(select(Product).order_by(Product.name))]
    return sorted(rows, key=lambda r: r["margin_paise"], reverse=True)


def totals(db: Session, since: date, until: date) -> dict:
    revenue = _sum(db, None, LedgerKind.PAYMENT, since, until)
    fee = _sum(db, None, LedgerKind.GATEWAY_FEE, since, until)
    fee_tax = _sum(db, None, LedgerKind.GATEWAY_TAX, since, until)
    refunds = _sum(db, None, LedgerKind.REFUND, since, until)
    costs = int(db.scalar(
        select(func.coalesce(func.sum(Cost.amount_paise), 0)).where(
            Cost.incurred_on >= since, Cost.incurred_on <= until)) or 0)

    captured = int(db.scalar(select(func.count(Payment.id)).where(
        Payment.status == PaymentStatus.CAPTURED,
        Payment.captured_at.isnot(None))) or 0)
    customers = int(db.scalar(select(func.count(Customer.id))) or 0)

    net = revenue - refunds
    return {
        "revenue_paise": revenue,
        "refunds_paise": refunds,
        "net_revenue_paise": net,
        "gateway_fee_paise": fee + fee_tax,
        "costs_paise": costs,
        "margin_paise": net - fee - fee_tax - costs,
        "payments_captured": captured,
        "customers": customers,
    }


def daily_revenue(db: Session, days: int = 30) -> list[dict]:
    until = date.today()
    since = until - timedelta(days=days - 1)
    rows = db.execute(
        select(LedgerEntry.entry_date,
               func.coalesce(func.sum(LedgerEntry.amount_paise), 0))
        .where(LedgerEntry.kind == LedgerKind.PAYMENT,
               LedgerEntry.entry_date >= since)
        .group_by(LedgerEntry.entry_date)
        .order_by(LedgerEntry.entry_date)).all()
    found = {d: int(v) for d, v in rows}
    return [{"date": (since + timedelta(days=i)).isoformat(),
             "amount_paise": found.get(since + timedelta(days=i), 0)}
            for i in range(days)]
