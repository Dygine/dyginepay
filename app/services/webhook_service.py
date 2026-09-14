"""
Outbound webhooks: what Dygine owes your tools.

Queued to a table rather than sent inline, because a slow or down consumer must
never make a payment capture slow or fail. The capture commits; delivery happens
after, and retries on its own schedule.

Signed the same way Razorpay signs its webhooks - HMAC-SHA256 over the raw body,
in an `X-Dygine-Signature: sha256=<hex>` header - so the verification code in
your tools is the code you have already written once.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt, sign_hmac_sha256
from app.models import OutboundDelivery, Product
from app.models.enums import DeliveryStatus
from app.services import reference

log = logging.getLogger("dygine.webhooks")

TIMEOUT = 10.0
MAX_ATTEMPTS = len(OutboundDelivery.BACKOFF_SECONDS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(db: Session, product: Product | None, event: str, data: dict) -> OutboundDelivery | None:
    """Queue an event. No webhook_url configured means nothing to deliver."""
    if product is None or not product.webhook_url:
        return None

    delivery = OutboundDelivery(
        product_id=product.id, event=event, event_id=reference.event_id(),
        payload={"event": event, "created_at": _now().isoformat(), "data": data},
        status=DeliveryStatus.PENDING, attempts=0, next_attempt_at=_now())
    db.add(delivery)
    db.flush()
    return delivery


def due(db: Session, limit: int = 20) -> list[OutboundDelivery]:
    return list(db.scalars(
        select(OutboundDelivery)
        .where(OutboundDelivery.status == DeliveryStatus.PENDING,
               OutboundDelivery.next_attempt_at <= _now())
        .order_by(OutboundDelivery.next_attempt_at)
        .limit(limit)))


def attempt(db: Session, delivery: OutboundDelivery) -> bool:
    """
    Try one delivery. Returns True on success.

    Any 2xx is success. Everything else - including a timeout and a connection
    refusal - is a retry, until the schedule is exhausted and the row goes DEAD
    where a human can see it and replay it.
    """
    product = db.get(Product, delivery.product_id)
    if product is None or not product.webhook_url:
        delivery.status = DeliveryStatus.DEAD
        delivery.last_error = "Product has no webhook URL"
        return False

    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True)
    secret = decrypt(product.webhook_secret_encrypted) or ""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DyginePay/1.0",
        "X-Dygine-Event": delivery.event,
        "X-Dygine-Event-Id": delivery.event_id,
        "X-Dygine-Delivery": str(delivery.id),
        "X-Dygine-Signature": f"sha256={sign_hmac_sha256(secret, body)}",
    }

    delivery.attempts += 1
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            res = client.post(product.webhook_url, content=body, headers=headers)
        delivery.last_status_code = res.status_code
        ok = 200 <= res.status_code < 300
        if not ok:
            delivery.last_error = (res.text or "")[:1000]
    except httpx.RequestError as exc:
        delivery.last_status_code = None
        delivery.last_error = f"{type(exc).__name__}: {exc}"[:1000]
        ok = False

    if ok:
        delivery.status = DeliveryStatus.DELIVERED
        delivery.delivered_at = _now()
        delivery.next_attempt_at = None
        return True

    if delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = DeliveryStatus.DEAD
        delivery.next_attempt_at = None
        log.warning("delivery %s dead after %s attempts", delivery.id, delivery.attempts)
    else:
        wait = OutboundDelivery.BACKOFF_SECONDS[delivery.attempts]
        delivery.next_attempt_at = _now() + timedelta(seconds=wait)
    return False


def drain(db: Session, limit: int = 20) -> dict:
    """One pass of the queue. Called by the loop and by /internal/tasks/run."""
    sent = failed = 0
    for delivery in due(db, limit):
        if attempt(db, delivery):
            sent += 1
        else:
            failed += 1
        db.commit()
    return {"delivered": sent, "failed": failed}


def replay(db: Session, delivery: OutboundDelivery) -> OutboundDelivery:
    """Put a dead or delivered event back on the queue with a fresh id."""
    delivery.status = DeliveryStatus.PENDING
    delivery.attempts = 0
    delivery.next_attempt_at = _now()
    delivery.last_error = None
    delivery.last_status_code = None
    db.flush()
    return delivery
