"""
Human-readable references. dgn_ord_…, dgn_pay_…, DGN-SUB-00012.

Deliberately not sequential: an order reference appears in URLs and customer
emails, and a sequential one leaks how many orders you have taken. The invoice
number is the exception - that one must be sequential, and it is handled in
invoice_service under a row lock.
"""
from __future__ import annotations

import secrets
import string

ALPHABET = string.ascii_letters + string.digits


def _suffix(n: int = 14) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def order_ref() -> str:
    return f"dgn_ord_{_suffix()}"


def payment_ref() -> str:
    return f"dgn_pay_{_suffix()}"


def refund_ref() -> str:
    return f"dgn_rfn_{_suffix()}"


def subscription_ref() -> str:
    return f"dgn_sub_{_suffix()}"


def event_id() -> str:
    return f"evt_{_suffix(20)}"


def checkout_token() -> str:
    # 32 bytes of entropy. This value is the URL, so it is the only thing
    # standing between a stranger and someone else's payment page.
    return secrets.token_urlsafe(32)


def api_key_id(mode: str) -> str:
    return f"dgn_{mode}_{_suffix(16)}"
