"""Webhook signature handling, the wallet API, and admin access control."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Customer, InboundEvent, Order, Payment, Wallet
from app.models.enums import PaymentStatus
from app.services import wallet_service


def auth(creds):
    key_id, secret = creds
    return {"Authorization": "Basic " + base64.b64encode(
        f"{key_id}:{secret}".encode()).decode()}


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def webhook_body(order_id: str, payment_id: str = "pay_wh_1",
                 event: str = "payment.captured") -> bytes:
    return json.dumps({
        "event": event,
        "payload": {"payment": {"entity": {
            "id": payment_id, "order_id": order_id, "status": "captured",
            "method": "upi", "amount": 149900, "fee": 3538, "tax": 637}}},
    }).encode()


class TestWebhookSecurity:
    def test_unsigned_webhook_is_rejected(self, client):
        r = client.post("/webhooks/razorpay", content=webhook_body("order_x"))
        assert r.status_code == 400
        assert "signature" in r.json()["error"]

    def test_wrong_signature_is_rejected(self, client, db):
        body = webhook_body("order_x")
        r = client.post("/webhooks/razorpay", content=body,
                        headers={"X-Razorpay-Signature": "deadbeef"})
        assert r.status_code == 400
        # Nothing stored - an unverified body never enters the system
        assert db.scalars(select(InboundEvent)).first() is None

    def test_signature_over_reserialised_body_fails(self, client):
        """
        Signing a re-serialised version of the body must not verify. This is the
        exact mistake that leads people to disable verification: parse, re-dump,
        signature no longer matches, "fix" it by not checking.
        """
        original = webhook_body("order_x")
        # Compact separators. json.dumps already defaults to (", ", ": "), so
        # re-dumping with the defaults gives identical bytes and would prove
        # nothing - the bytes have to actually differ for this to be a test.
        reserialised = json.dumps(json.loads(original),
                                  separators=(",", ":")).encode()
        assert original != reserialised, "the two encodings must differ"
        r = client.post("/webhooks/razorpay", content=original, headers={
            "X-Razorpay-Signature": sign(settings.RAZORPAY_WEBHOOK_SECRET,
                                         reserialised)})
        assert r.status_code == 400

    def test_valid_webhook_captures(self, client, db, api_auth, fake_rzp):
        client.post("/v1/checkout/sessions", headers=auth(api_auth), json={
            "customer": {"external_id": "org_1", "name": "Sunrise PG"},
            "line_items": [{"description": "Pro", "amount": 149900}]})
        order = db.scalars(select(Order)).first()

        body = webhook_body(order.razorpay_order_id)
        r = client.post("/webhooks/razorpay", content=body, headers={
            "X-Razorpay-Signature": sign(settings.RAZORPAY_WEBHOOK_SECRET, body),
            "X-Razorpay-Event-Id": "evt_real_1"})
        assert r.status_code == 200

        db.expire_all()
        payment = db.scalars(select(Payment)).first()
        assert payment.status == PaymentStatus.CAPTURED
        assert payment.verified_by == "webhook"

    def test_redelivered_webhook_is_ignored(self, client, db, api_auth, fake_rzp):
        client.post("/v1/checkout/sessions", headers=auth(api_auth), json={
            "customer": {"external_id": "org_1", "name": "Sunrise PG"},
            "line_items": [{"description": "Pro", "amount": 149900}]})
        order = db.scalars(select(Order)).first()

        body = webhook_body(order.razorpay_order_id)
        headers = {"X-Razorpay-Signature": sign(settings.RAZORPAY_WEBHOOK_SECRET,
                                                body),
                   "X-Razorpay-Event-Id": "evt_same"}
        first = client.post("/webhooks/razorpay", content=body, headers=headers)
        second = client.post("/webhooks/razorpay", content=body, headers=headers)

        assert first.json()["status"] == "ok"
        assert second.json()["status"] == "duplicate"
        db.expire_all()
        assert len(db.scalars(select(Payment)).all()) == 1


class TestWalletAPI:
    @pytest.fixture
    def customer(self, db, product):
        c = Customer(product_id=product.id, external_id="org_1",
                     name="Sunrise PG", state_code="29")
        db.add(c); db.commit()
        return c

    def test_topup_credits_taxable_value(self, client, db, api_auth, product,
                                         fake_rzp, monkeypatch):
        """
        On a tax invoice the customer pays 11,800 for 10,000 of credit. The
        1,800 is GST and went to the government, not into spendable balance.
        """
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "29ABCDE1234F1Z5")
        r = client.post("/v1/checkout/sessions", headers=auth(api_auth), json={
            "customer": {"external_id": "org_1", "name": "Sunrise PG",
                         "state_code": "29"},
            "purpose": "wallet_topup",
            "line_items": [{"description": "Top-up", "amount": 1000000}]})
        assert r.json()["amount"] == 1180000

        from app.services import checkout_service
        order = db.scalars(select(Order)).first()
        checkout_service.complete(db, razorpay_order_id=order.razorpay_order_id,
                                  razorpay_payment_id="pay_topup",
                                  verified_by="webhook")
        db.commit()

        customer = db.scalars(select(Customer)).first()
        assert wallet_service.balance(db, customer.id) == 1000000

    def test_debit_endpoint_requires_balance(self, client, db, api_auth, customer):
        r = client.post("/v1/wallet/debit", headers=auth(api_auth), json={
            "customer_external_id": "org_1", "amount": 5000})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "insufficient_balance"

    def test_debit_with_idempotency_key_charges_once(self, client, db, api_auth,
                                                     customer):
        wallet_service.credit(db, customer.id, 10000)
        db.commit()
        headers = {**auth(api_auth), "Idempotency-Key": "sms-batch-441"}
        payload = {"customer_external_id": "org_1", "amount": 2500,
                   "description": "SMS credits"}
        for _ in range(3):
            r = client.post("/v1/wallet/debit", headers=headers, json=payload)
            assert r.status_code == 200
        db.expire_all()
        assert wallet_service.balance(db, customer.id) == 7500

    def test_wallet_visible_to_owning_product_only(self, client, db, api_auth,
                                                   customer):
        wallet_service.credit(db, customer.id, 10000)
        db.commit()
        r = client.get("/v1/customers/org_1/wallet", headers=auth(api_auth))
        assert r.json()["balance"] == 10000
        assert len(r.json()["transactions"]) == 1

    def test_no_credit_endpoint_exists(self, client, api_auth):
        """A tool must never be able to mint balance for its own customers."""
        r = client.post("/v1/wallet/credit", headers=auth(api_auth), json={
            "customer_external_id": "org_1", "amount": 100000})
        assert r.status_code in (404, 405)


class TestAdminAccess:
    def test_admin_redirects_to_login_when_signed_out(self, client):
        r = client.get("/admin", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login"

    def test_wrong_password_is_rejected(self, client, admin):
        r = client.post("/admin/login",
                        data={"email": "admin@dygine.com", "password": "wrong"},
                        follow_redirects=False)
        assert r.status_code == 401

    def test_correct_password_signs_in(self, client, admin):
        r = client.post("/admin/login",
                        data={"email": "admin@dygine.com", "password": "admin1234"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert settings.SESSION_COOKIE_NAME in r.cookies

    def test_session_cookie_is_httponly(self, client, admin):
        r = client.post("/admin/login",
                        data={"email": "admin@dygine.com", "password": "admin1234"},
                        follow_redirects=False)
        cookie = r.headers.get("set-cookie", "")
        assert "HttpOnly" in cookie
        assert "Samesite=lax" in cookie or "SameSite=lax" in cookie

    def test_health_reports_database(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["database"] == "up"
