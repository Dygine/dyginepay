"""
Invoice issuing.

The number is the part that has to be right. GST requires a consecutive series
per financial year with no gaps, so it is taken by locking a counter row inside
the same transaction that writes the invoice:

    SELECT ... FROM invoice_sequences WHERE fy=... FOR UPDATE
    last_number += 1
    INSERT invoice

Two concurrent captures serialise on that lock and get 41 and 42. `count(*) + 1`
would give both of them 41, and a unique constraint would then throw away a real
payment's invoice.

An issued invoice is never edited. A mistake becomes a credit note.
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError
from app.models import Customer, Invoice, InvoiceLine, InvoiceSequence, Payment, Product
from app.models.enums import InvoiceKind, InvoiceStatus, TaxTreatment
from app.services import gst
from app.services.gst import TaxedDocument


def next_number(db: Session, fy: str, kind: str) -> str:
    """Take the next number in the series. Caller must be inside a transaction."""
    # Create the counter row if this is the first invoice of the year, safely
    # under concurrency. Twelve payments captured in the same second would
    # otherwise all try to INSERT it and all but one would fail - throwing away
    # real invoices for real money.
    #
    # DO UPDATE rather than DO NOTHING for the same reason as in wallet_service:
    # DO NOTHING does not wait for a concurrent uncommitted insert, so the
    # SELECT that follows would find nothing.
    db.execute(
        pg_insert(InvoiceSequence)
        .values(fy=fy, kind=kind, last_number=0)
        .on_conflict_do_update(index_elements=["fy", "kind"],
                               set_={"kind": kind}))
    db.flush()

    row = db.execute(
        select(InvoiceSequence)
        .where(InvoiceSequence.fy == fy, InvoiceSequence.kind == kind)
        .with_for_update()
    ).scalar_one()

    row.last_number += 1
    tag = {InvoiceKind.TAX_INVOICE: "",
           InvoiceKind.BILL_OF_SUPPLY: "BOS/",
           InvoiceKind.CREDIT_NOTE: "CN/"}.get(kind, "")
    return f"{settings.INVOICE_PREFIX}/{tag}{fy}/{row.last_number:05d}"


def issue(db: Session, *, product: Product, customer: Customer,
          line_items: list[dict], payment: Payment | None = None,
          issue_date: date | None = None, notes: dict | None = None) -> Invoice:
    """Issue an invoice from raw line items. Tax treatment is derived, not passed."""
    issue_date = issue_date or date.today()
    fy = gst.financial_year(issue_date)
    doc: TaxedDocument = gst.compute(line_items, customer.state_code)
    number = next_number(db, fy, doc.kind)

    invoice = Invoice(
        number=number, fy=fy, kind=doc.kind, status=InvoiceStatus.ISSUED,
        product_id=product.id, customer_id=customer.id,
        payment_id=payment.id if payment else None,
        issue_date=issue_date,
        # Snapshot both parties. A later address change must not rewrite history.
        seller_name=settings.BUSINESS_NAME,
        seller_address=settings.BUSINESS_ADDRESS,
        seller_gstin=settings.BUSINESS_GSTIN or None,
        seller_state_code=settings.BUSINESS_STATE_CODE,
        buyer_name=customer.name,
        buyer_address=customer.billing_address or "",
        buyer_gstin=customer.gstin,
        buyer_state_code=customer.state_code,
        place_of_supply=customer.state_code or settings.BUSINESS_STATE_CODE,
        tax_treatment=doc.treatment,
        subtotal_paise=doc.subtotal_paise,
        cgst_paise=doc.cgst_paise,
        sgst_paise=doc.sgst_paise,
        igst_paise=doc.igst_paise,
        total_paise=doc.total_paise,
        # The product is recorded as a reference, never as the seller. See the
        # note in pdf_service about why the seller block cannot change.
        notes={**(notes or {}), "product": product.name, "product_slug": product.slug},
    )
    db.add(invoice)
    db.flush()

    for position, line in enumerate(doc.lines):
        db.add(InvoiceLine(
            invoice_id=invoice.id, position=position,
            description=line.description, sac=line.sac, quantity=line.quantity,
            unit_price_paise=line.unit_price_paise,
            taxable_paise=line.taxable_paise, tax_rate=line.tax_rate,
            cgst_paise=line.cgst_paise, sgst_paise=line.sgst_paise,
            igst_paise=line.igst_paise, total_paise=line.total_paise))

    db.flush()
    return invoice


def credit_note(db: Session, invoice: Invoice, *, reason: str = "",
                amount_paise: int | None = None) -> Invoice:
    """
    Reverse an invoice, fully or partially. The original is untouched - it stays
    issued and filed, and the credit note is what nets it off.
    """
    if invoice.kind == InvoiceKind.CREDIT_NOTE:
        raise ConflictError("Cannot issue a credit note against a credit note")

    customer = db.get(Customer, invoice.customer_id)
    product = db.get(Product, invoice.product_id)
    if customer is None or product is None:
        raise NotFoundError("Invoice is missing its customer or product")

    full = amount_paise is None or amount_paise >= invoice.subtotal_paise
    taxable = invoice.subtotal_paise if full else int(amount_paise)
    if taxable <= 0:
        raise ConflictError("Credit note amount must be positive")

    issue_date = date.today()
    fy = gst.financial_year(issue_date)
    doc = gst.compute(
        [{"description": f"Credit note against {invoice.number}"
                         + (f" - {reason}" if reason else ""),
          "amount": taxable, "quantity": 1,
          "sac": invoice.lines[0].sac if invoice.lines else settings.DEFAULT_SAC}],
        invoice.buyer_state_code)

    number = next_number(db, fy, InvoiceKind.CREDIT_NOTE)
    note = Invoice(
        number=number, fy=fy, kind=InvoiceKind.CREDIT_NOTE,
        status=InvoiceStatus.ISSUED,
        product_id=invoice.product_id, customer_id=invoice.customer_id,
        payment_id=invoice.payment_id, reverses_invoice_id=invoice.id,
        issue_date=issue_date,
        seller_name=invoice.seller_name, seller_address=invoice.seller_address,
        seller_gstin=invoice.seller_gstin, seller_state_code=invoice.seller_state_code,
        buyer_name=invoice.buyer_name, buyer_address=invoice.buyer_address,
        buyer_gstin=invoice.buyer_gstin, buyer_state_code=invoice.buyer_state_code,
        place_of_supply=invoice.place_of_supply,
        tax_treatment=doc.treatment,
        subtotal_paise=doc.subtotal_paise, cgst_paise=doc.cgst_paise,
        sgst_paise=doc.sgst_paise, igst_paise=doc.igst_paise,
        total_paise=doc.total_paise,
        notes={"reason": reason, "reverses": invoice.number})
    db.add(note)
    db.flush()

    for position, line in enumerate(doc.lines):
        db.add(InvoiceLine(
            invoice_id=note.id, position=position, description=line.description,
            sac=line.sac, quantity=line.quantity,
            unit_price_paise=line.unit_price_paise, taxable_paise=line.taxable_paise,
            tax_rate=line.tax_rate, cgst_paise=line.cgst_paise,
            sgst_paise=line.sgst_paise, igst_paise=line.igst_paise,
            total_paise=line.total_paise))

    db.flush()
    return note


def for_payment(db: Session, payment_id: uuid.UUID) -> Invoice | None:
    return db.scalars(select(Invoice).where(
        Invoice.payment_id == payment_id,
        Invoice.kind != InvoiceKind.CREDIT_NOTE)).first()
