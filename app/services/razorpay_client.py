"""
The only place that talks to Razorpay.

Isolated behind a module-level `request` function so tests can replace it
without a network, and so there is exactly one place where a credential is read
and one place where an upstream failure is translated.
"""
from __future__ import annotations

import base64
import json
import logging

import httpx

from app.core.config import settings
from app.core.crypto import verify_hmac_sha256
from app.core.exceptions import UpstreamError

API = "https://api.razorpay.com/v1"
TIMEOUT = 20.0

log = logging.getLogger("dygine.razorpay")


def configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def request(method: str, path: str, body: dict | None = None) -> dict:
    """One call to the Razorpay REST API."""
    if not configured():
        raise UpstreamError("Razorpay is not configured. Set RAZORPAY_KEY_ID "
                            "and RAZORPAY_KEY_SECRET.")
    token = base64.b64encode(
        f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            res = client.request(method, f"{API}{path}", headers=headers,
                                 content=json.dumps(body) if body is not None else None)
    except httpx.RequestError as exc:
        log.warning("razorpay unreachable: %s", exc)
        raise UpstreamError("Could not reach Razorpay. Try again in a minute.") from None

    if res.status_code >= 400:
        try:
            detail = res.json().get("error", {}).get("description")
        except (ValueError, AttributeError):
            detail = None
        if res.status_code == 401:
            raise UpstreamError("Razorpay rejected these keys. Check the key id "
                                "and key secret.")
        log.warning("razorpay %s %s -> %s %s", method, path, res.status_code, detail)
        raise UpstreamError(f"Razorpay refused the request: {detail or res.reason_phrase}")

    return res.json() if res.content else {}


# ------------------------------------------------------------------ api --
def create_order(amount_paise: int, reference: str, notes: dict | None = None,
                 currency: str = "INR") -> dict:
    return request("POST", "/orders", {
        "amount": amount_paise,
        "currency": currency,
        "receipt": reference[:40],
        "payment_capture": 1,
        "notes": notes or {},
    })


def fetch_payment(razorpay_payment_id: str) -> dict:
    return request("GET", f"/payments/{razorpay_payment_id}")


def create_refund(razorpay_payment_id: str, amount_paise: int | None,
                  notes: dict | None = None) -> dict:
    body: dict = {"notes": notes or {}, "speed": "normal"}
    if amount_paise is not None:
        body["amount"] = amount_paise
    return request("POST", f"/payments/{razorpay_payment_id}/refund", body)


# ------------------------------------------------------------ signatures --
def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """
    The browser callback. Razorpay signs "<order_id>|<payment_id>" with the key
    secret. This is what makes it safe to act on something the browser told us.
    """
    return verify_hmac_sha256(settings.RAZORPAY_KEY_SECRET,
                              f"{order_id}|{payment_id}", signature)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """
    The server webhook, signed with the *webhook* secret - a different value
    from the key secret. Verified against the raw bytes, before any parsing:
    re-serialising the JSON first would change the bytes and the signature would
    never match.
    """
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        return False
    return verify_hmac_sha256(settings.RAZORPAY_WEBHOOK_SECRET, raw_body, signature)
