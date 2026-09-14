"""
Inbound from Razorpay.

Two rules, both non-negotiable:

  1. Verify the signature over the RAW BYTES, before parsing. Parsing first and
     re-serialising changes the bytes and the signature will never match - and
     the tempting "fix" for that is to stop verifying, which turns this into an
     open endpoint where anyone can mark a payment captured.

  2. Store the event before acting on it, keyed on Razorpay's event id. A
     redelivery hits the unique constraint and is acknowledged without being
     applied twice.

Always return 200 once the event is stored. A 500 makes Razorpay retry, and
retrying an event we have already recorded just produces noise.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import session as new_session
from app.models import InboundEvent
from app.services import checkout_service, razorpay_client

log = logging.getLogger("dygine.webhook.in")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

HANDLED = {"payment.captured", "payment.authorized", "payment.failed",
           "order.paid", "refund.processed", "refund.failed"}


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
):
    raw = await request.body()

    if not razorpay_client.verify_webhook_signature(raw, x_razorpay_signature or ""):
        log.warning("rejected webhook with bad signature")
        # 400, not 401: Razorpay treats 4xx as "do not retry", which is right.
        # A body we cannot verify will not become verifiable on a second try.
        return _json(400, {"error": "invalid signature"})

    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return _json(400, {"error": "malformed body"})

    event_type = payload.get("event", "")
    event_id = x_razorpay_event_id or payload.get("id") or f"{event_type}:{hash(raw)}"

    db: Session = new_session()
    try:
        event = InboundEvent(
            event_id=event_id, event_type=event_type,
            signature=x_razorpay_signature, raw_body=raw.decode("utf-8", "replace"),
            payload=payload)
        db.add(event)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            log.info("duplicate webhook %s ignored", event_id)
            return _json(200, {"status": "duplicate"})

        if event_type in HANDLED:
            try:
                _apply(db, event_type, payload)
                event.processed_at = datetime.now(timezone.utc)
                db.commit()
            except Exception as exc:                 # noqa: BLE001
                db.rollback()
                # Record the failure on the stored event and still return 200.
                # The event is safe in the table; admin can replay it. A 500
                # would have Razorpay redeliver into the same failing code.
                log.exception("failed to apply %s", event_id)
                fresh = db.scalars(select(InboundEvent).where(
                    InboundEvent.event_id == event_id)).first()
                if fresh:
                    fresh.process_error = str(exc)[:2000]
                    db.commit()
                return _json(200, {"status": "stored", "applied": False})

        return _json(200, {"status": "ok"})
    finally:
        db.close()


def _apply(db: Session, event_type: str, payload: dict) -> None:
    entity = payload.get("payload", {})

    if event_type in ("payment.captured", "payment.authorized", "order.paid"):
        pay = entity.get("payment", {}).get("entity", {})
        if not pay:
            return
        if event_type == "payment.authorized" and pay.get("status") != "captured":
            return                       # authorized is not money yet
        order_id, payment_id = pay.get("order_id"), pay.get("id")
        if order_id and payment_id:
            checkout_service.complete(
                db, razorpay_order_id=order_id, razorpay_payment_id=payment_id,
                verified_by="webhook", gateway_payload=pay)

    elif event_type == "payment.failed":
        pay = entity.get("payment", {}).get("entity", {})
        if pay.get("order_id"):
            checkout_service.fail(
                db, razorpay_order_id=pay["order_id"],
                razorpay_payment_id=pay.get("id"),
                reason=pay.get("error_description") or "Payment failed",
                gateway_payload=pay)


def _json(status: int, body: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status, content=body)
