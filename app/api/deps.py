"""
Authenticating a consuming tool, and making its requests idempotent.

Auth is HTTP Basic with key_id as the username and the secret as the password -
the same scheme Razorpay uses, chosen so the integration code in your tools is
code you have already written.

The secret is compared by hash. The plaintext is not stored anywhere and cannot
be recovered, only rotated.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import hash_token, tokens_equal
from app.core.database import get_db
from app.core.exceptions import AuthError, ConflictError, PermissionDeniedError, RateLimitError
from app.models import ApiKey, IdempotencyRecord, Product
from app.models.enums import ProductStatus

# In-process rate limiting. Good enough for one Render instance; it resets on
# deploy and does not coordinate across replicas, which is stated plainly here
# so nobody later assumes it is a security boundary.
_HITS: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit(key: str) -> None:
    now = time.monotonic()
    window = _HITS[key]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= settings.RATE_LIMIT_PER_MINUTE:
        raise RateLimitError("Too many requests. Slow down.")
    window.append(now)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


class Caller:
    """The authenticated tool behind a request."""

    def __init__(self, product: Product, api_key: ApiKey, ip: str):
        self.product = product
        self.api_key = api_key
        self.ip = ip

    @property
    def mode(self) -> str:
        return self.api_key.mode

    @property
    def actor(self) -> str:
        return f"api:{self.product.slug}"


def authenticate(request: Request,
                 authorization: str | None = Header(default=None),
                 db: Session = Depends(get_db)) -> Caller:
    if not authorization or not authorization.lower().startswith("basic "):
        raise AuthError("Send your key id and secret as HTTP Basic auth")

    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode()
        key_id, _, secret = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        raise AuthError("Authorization header is malformed") from None

    if not key_id or not secret:
        raise AuthError("Both a key id and a secret are required")

    api_key = db.scalars(select(ApiKey).where(ApiKey.key_id == key_id)).first()
    # Compare a hash even when the key is missing, so a wrong key id and a wrong
    # secret take the same time and neither can be probed for.
    candidate = hash_token(secret)
    stored = api_key.secret_hash if api_key else hash_token("nonexistent")
    if not tokens_equal(candidate, stored) or api_key is None:
        raise AuthError("Invalid API credentials")
    if not api_key.is_active or api_key.revoked_at is not None:
        raise AuthError("This API key has been revoked")

    product = db.get(Product, api_key.product_id)
    if product is None or product.status != ProductStatus.ACTIVE:
        raise PermissionDeniedError("This product is disabled")

    ip = _client_ip(request)
    if product.ip_allowlist.strip():
        allowed = {a.strip() for a in product.ip_allowlist.split(",") if a.strip()}
        if ip not in allowed:
            raise PermissionDeniedError(f"IP {ip} is not on this product's allowlist")

    _rate_limit(api_key.key_id)
    api_key.last_used_at = datetime.now(timezone.utc)
    return Caller(product, api_key, ip)


# ----------------------------------------------------------- idempotency --
def fingerprint(body: dict | None) -> str:
    raw = json.dumps(body or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def replay_or_none(db: Session, caller: Caller, key: str | None, endpoint: str,
                   body: dict | None) -> dict | None:
    """
    Return the stored response when this key has been seen before.

    A key reused with a *different* body is a client bug, not a retry, and gets
    a 409 - returning the first response would silently confirm an order the
    caller did not place.
    """
    if not key:
        return None
    record = db.scalars(select(IdempotencyRecord).where(
        IdempotencyRecord.api_key_id == caller.api_key.id,
        IdempotencyRecord.key == key)).first()
    if record is None:
        return None
    if record.request_fingerprint != fingerprint(body):
        raise ConflictError(
            "This Idempotency-Key was already used with a different request body")
    return record.response_body


def remember(db: Session, caller: Caller, key: str | None, endpoint: str,
             body: dict | None, response: dict, status_code: int = 200) -> None:
    if not key:
        return
    db.add(IdempotencyRecord(
        api_key_id=caller.api_key.id, key=key, endpoint=endpoint,
        request_fingerprint=fingerprint(body), status_code=status_code,
        response_body=response,
        expires_at=datetime.now(timezone.utc)
        + timedelta(hours=settings.IDEMPOTENCY_RETENTION_HOURS)))
