"""
The page the customer actually sees, and the callback the browser posts back to.

Server-rendered on purpose. A payment page that waits on a JS framework to boot
is a payment page people abandon, and this one has to work on a mid-range phone
on mobile data.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db, session as new_session
from app.core.money import rupee_display
from app.models import Customer, Order
from app.models.enums import CheckoutStatus
from app.services import checkout_service, razorpay_client

log = logging.getLogger("dygine.checkout")

router = APIRouter(tags=["checkout"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["rupees"] = rupee_display


@router.get("/c/{token}", response_class=HTMLResponse)
def checkout_page(token: str, request: Request):
    db: Session = new_session()
    try:
        session = checkout_service.load_session(db, token)
        db.commit()

        if session.status != CheckoutStatus.CREATED:
            return templates.TemplateResponse("checkout/closed.html", {
                "request": request, "session": session,
                "business": settings.BUSINESS_NAME,
                "message": {
                    CheckoutStatus.PAID: "This payment has already been completed.",
                    CheckoutStatus.EXPIRED: "This payment link has expired.",
                    CheckoutStatus.CANCELLED: "This payment was cancelled.",
                }.get(session.status, "This link is no longer valid."),
            })

        order = db.get(Order, session.order_id)
        customer = db.get(Customer, session.customer_id)
        return templates.TemplateResponse("checkout/pay.html", {
            "request": request, "session": session, "order": order,
            "customer": customer, "business": settings.BUSINESS_NAME,
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
            "callback_url": f"{settings.BASE_URL.rstrip('/')}/c/{token}/callback",
        })
    finally:
        db.close()


@router.post("/c/{token}/callback")
def checkout_callback(
    token: str,
    razorpay_payment_id: str = Form(default=""),
    razorpay_order_id: str = Form(default=""),
    razorpay_signature: str = Form(default=""),
):
    """
    Where Razorpay Checkout posts after a successful payment.

    The signature is verified before anything is written. If it does not check
    out we do nothing at all and let the webhook be the source of truth - a
    forged callback must never be able to mark a payment captured.
    """
    db: Session = new_session()
    try:
        session = checkout_service.load_session(db, token)

        if not razorpay_client.verify_checkout_signature(
                razorpay_order_id, razorpay_payment_id, razorpay_signature):
            log.warning("bad callback signature for session %s", session.id)
            return RedirectResponse(f"/c/{token}/status?verified=0", status_code=303)

        checkout_service.complete(
            db, razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id, verified_by="callback")
        db.commit()

        if session.success_url:
            joiner = "&" if "?" in session.success_url else "?"
            return RedirectResponse(
                f"{session.success_url}{joiner}payment_id={razorpay_payment_id}",
                status_code=303)
        return RedirectResponse(f"/c/{token}/status", status_code=303)
    finally:
        db.close()


@router.get("/c/{token}/status", response_class=HTMLResponse)
def checkout_status(token: str, request: Request, verified: int = 1):
    db: Session = new_session()
    try:
        session = checkout_service.load_session(db, token)
        db.commit()
        paid = session.status == CheckoutStatus.PAID
        return templates.TemplateResponse("checkout/done.html", {
            "request": request, "session": session, "paid": paid,
            "verified": bool(verified), "business": settings.BUSINESS_NAME,
        })
    finally:
        db.close()
