"""The path a real payment takes, including the ways it goes wrong."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Invoice, LedgerEntry, Order, Payment
from app.models.enums import LedgerKind, PaymentStatus
from app.services import checkout_service


def auth(creds):
    key_id, secret = creds
    return {"Authorization": "Basic " + base64.b64encode(
        f"{key_id}:{secret}".encode()).decode()}


def make_session(client, creds, **over):
    body = {"customer": {"external_id": "org_1", "name": "Sunrise PG",
                         "email": "owner@sunrise.in", "state_code": "29"},
            "line_items": [{"description": "PGDesk Pro", "amount": 149900}]}
    body.update(over)
    return client.post("/v1/checkout/sessions", json=body, headers=auth(creds))


class TestAuth:
    def test_no_credentials_is_401(self, client):
        assert client.post("/v1/checkout/sessions", json={}).status_code == 401

    def test_wrong_secret_is_401(self, client, api_auth):
        key_id, _ = api_auth
        r = client.post("/v1/checkout/sessions", json={},
                        headers=auth((key_id, "wrong")))
        assert r.status_code == 401

    def test_revoked_key_is_401(self, client, db, api_auth, fake_rzp):
        from app.models import ApiKey
        key = db.scalars(select(ApiKey)).first()
        key.is_active = False
        db.commit()
        assert make_session(client, api_auth).status_code == 401


class TestCheckout:
    def test_creates_order_and_url(self, client, api_auth, fake_rzp):
        r = make_session(client, api_auth)
        assert r.status_code == 200
        body = r.json()
        assert body["checkout_url"].startswith(settings.BASE_URL)
        assert body["amount"] == 149900          # unregistered: no tax added
        assert ("POST", "/orders", None) != fake_rzp.calls[0]
        assert fake_rzp.calls[0][1] == "/orders"

    def test_rejects_empty_line_items(self, client, api_auth, fake_rzp):
        r = make_session(client, api_auth, line_items=[])
        assert r.status_code == 422

    def test_rejects_below_one_rupee(self, client, api_auth, fake_rzp):
        r = make_session(client, api_auth,
                         line_items=[{"description": "x", "amount": 50}])
        assert r.status_code == 422

    def test_idempotency_returns_same_order(self, client, api_auth, fake_rzp):
        h = {**auth(api_auth), "Idempotency-Key": "attempt-1"}
        body = {"customer": {"external_id": "org_1", "name": "Sunrise PG"},
                "line_items": [{"description": "Pro", "amount": 149900}]}
        first = client.post("/v1/checkout/sessions", json=body, headers=h).json()
        second = client.post("/v1/checkout/sessions", json=body, headers=h).json()
        assert first["id"] == second["id"]
        # One order at Razorpay, not two - this is the double-click case
        assert sum(1 for c in fake_rzp.calls if c[1] == "/orders") == 1

    def test_same_key_different_body_is_409(self, client, api_auth, fake_rzp):
        h = {**auth(api_auth), "Idempotency-Key": "attempt-1"}
        client.post("/v1/checkout/sessions", headers=h, json={
            "customer": {"external_id": "org_1", "name": "A"},
            "line_items": [{"description": "Pro", "amount": 149900}]})
        r = client.post("/v1/checkout/sessions", headers=h, json={
            "customer": {"external_id": "org_1", "name": "A"},
            "line_items": [{"description": "Pro", "amount": 999900}]})
        assert r.status_code == 409

    def test_customer_is_created_once(self, client, db, api_auth, fake_rzp):
        from app.models import Customer
        make_session(client, api_auth)
        make_session(client, api_auth)
        assert len(db.scalars(select(Customer)).all()) == 1


class TestCapture:
    def _capture(self, db, session_token, rzp_order_id):
        return checkout_service.complete(
            db, razorpay_order_id=rzp_order_id,
            razorpay_payment_id="pay_test_123", verified_by="webhook")

    def test_capture_writes_ledger_and_invoice(self, client, db, api_auth, fake_rzp):
        make_session(client, api_auth)
        order = db.scalars(select(Order)).first()

        payment = self._capture(db, None, order.razorpay_order_id)
        db.commit()

        assert payment.status == PaymentStatus.CAPTURED
        assert payment.fee_paise == 3538        # real fee, from the gateway
        assert payment.fee_tax_paise == 637

        kinds = {e.kind for e in db.scalars(select(LedgerEntry))}
        assert kinds == {LedgerKind.PAYMENT, LedgerKind.GATEWAY_FEE,
                         LedgerKind.GATEWAY_TAX}

        invoice = db.scalars(select(Invoice)).first()
        assert invoice is not None
        assert invoice.number.startswith("DGN/BOS/")   # unregistered

    def test_capture_is_idempotent(self, client, db, api_auth, fake_rzp):
        """The callback and the webhook both arrive. Only one capture happens."""
        make_session(client, api_auth)
        order = db.scalars(select(Order)).first()

        first = self._capture(db, None, order.razorpay_order_id)
        db.commit()
        second = self._capture(db, None, order.razorpay_order_id)
        db.commit()

        assert first.id == second.id
        assert len(db.scalars(select(Payment)).all()) == 1
        assert len(db.scalars(select(Invoice)).all()) == 1
        # and critically: revenue was not counted twice
        revenue = [e for e in db.scalars(select(LedgerEntry))
                   if e.kind == LedgerKind.PAYMENT]
        assert len(revenue) == 1

    def test_poll_endpoint_reflects_capture(self, client, db, api_auth, fake_rzp):
        make_session(client, api_auth)
        order = db.scalars(select(Order)).first()
        payment = self._capture(db, None, order.razorpay_order_id)
        db.commit()

        r = client.get(f"/v1/payments/{payment.reference}", headers=auth(api_auth))
        assert r.status_code == 200
        assert r.json()["status"] == "captured"
        assert r.json()["invoice"]["number"].startswith("DGN/")

    def test_payment_of_another_product_is_invisible(self, client, db, api_auth,
                                                     fake_rzp):
        make_session(client, api_auth)
        order = db.scalars(select(Order)).first()
        payment = self._capture(db, None, order.razorpay_order_id)
        db.commit()

        from app.core.crypto import generate_secret, hash_token
        from app.models import ApiKey, Product
        from app.services import reference
        other = Product(slug="hrlens", name="HRLens")
        db.add(other); db.flush()
        secret = generate_secret(32)
        db.add(ApiKey(product_id=other.id, mode="test",
                      key_id=reference.api_key_id("test"),
                      secret_hash=hash_token(secret)))
        db.commit()
        key = db.scalars(select(ApiKey).where(
            ApiKey.product_id == other.id)).first()

        r = client.get(f"/v1/payments/{payment.reference}",
                       headers=auth((key.key_id, secret)))
        assert r.status_code == 404          # tenant isolation
