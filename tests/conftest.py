"""
Test fixtures.

Razorpay is stubbed at the module boundary - `razorpay_client.request` is the
single function every call goes through, so replacing it replaces the whole
gateway with no network and no keys.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import settings
from app.core.crypto import encrypt, generate_secret, hash_password, hash_token
from app.core.database import SessionLocal, engine
from app.main import app
from app.models import AdminUser, ApiKey, Base, Plan, Product
from app.services import razorpay_client, reference

settings.RAZORPAY_KEY_ID = "rzp_test_stub"
settings.RAZORPAY_KEY_SECRET = "stub_secret"
settings.RAZORPAY_WEBHOOK_SECRET = "stub_webhook_secret"


@pytest.fixture(scope="session", autouse=True)
def schema():
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def clean():
    """Truncate between tests so each one starts from nothing."""
    with engine.begin() as conn:
        tables = ",".join(f'"{t}"' for t in reversed(Base.metadata.sorted_tables)
                          for t in [t.name])
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def product(db):
    p = Product(slug="pgdesk", name="PGDesk",
                webhook_url="https://example.invalid/hook",
                webhook_secret_encrypted=encrypt("hook_secret"))
    db.add(p)
    db.flush()
    db.add(Plan(product_id=p.id, code="pro", name="PGDesk Pro",
                amount_paise=149900, interval="monthly"))
    db.commit()
    return p


@pytest.fixture
def api_auth(db, product):
    secret = generate_secret(32)
    key = ApiKey(product_id=product.id, mode="test",
                 key_id=reference.api_key_id("test"),
                 secret_hash=hash_token(secret), secret_hint=secret[:6])
    db.add(key)
    db.commit()
    return (key.key_id, secret)


@pytest.fixture
def admin(db):
    u = AdminUser(email="admin@dygine.com", name="Owner",
                  password_hash=hash_password("admin1234"))
    db.add(u)
    db.commit()
    return u


class FakeRazorpay:
    """Records calls and returns realistic shapes."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.orders: dict[str, dict] = {}

    def __call__(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls.append((method, path, body))

        if method == "POST" and path == "/orders":
            oid = f"order_{uuid.uuid4().hex[:14]}"
            self.orders[oid] = {"id": oid, "amount": body["amount"],
                                "currency": "INR", "receipt": body["receipt"],
                                "status": "created"}
            return self.orders[oid]

        if method == "GET" and path.startswith("/payments/"):
            pid = path.rsplit("/", 1)[-1]
            return {"id": pid, "status": "captured", "method": "upi",
                    "amount": 176882,
                    # 2% + 18% GST on the fee, the real Razorpay shape
                    "fee": 3538, "tax": 637, "order_id": "order_stub"}

        if method == "POST" and "/refund" in path:
            return {"id": f"rfnd_{uuid.uuid4().hex[:14]}", "status": "processed",
                    "amount": (body or {}).get("amount", 0)}

        return {}


@pytest.fixture
def fake_rzp(monkeypatch):
    fake = FakeRazorpay()
    monkeypatch.setattr(razorpay_client, "request", fake)
    return fake
