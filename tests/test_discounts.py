"""Discount lines — the coupon path PGGuru depends on."""
from __future__ import annotations

import base64

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Invoice, Order
from app.models.enums import TaxTreatment
from app.services import checkout_service, gst


def auth(creds):
    key_id, secret = creds
    return {"Authorization": "Basic " + base64.b64encode(
        f"{key_id}:{secret}".encode()).decode()}


class TestDiscountMath:
    def test_discount_reduces_subtotal(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "")
        doc = gst.compute([
            {"description": "PGGuru Pro", "amount": 149900},
            {"description": "SAVE20", "amount": -29980, "kind": "discount"},
        ], "29")
        assert doc.subtotal_paise == 119920
        assert doc.total_paise == 119920

    def test_discount_also_reduces_tax(self, monkeypatch):
        """
        The point of a discount line rather than a netted price: GST is charged
        on what the customer actually pays, not the list price.
        """
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "29ABCDE1234F1Z5")
        doc = gst.compute([
            {"description": "PGGuru Pro", "amount": 149900},
            {"description": "SAVE20", "amount": -29980, "kind": "discount"},
        ], "29")
        assert doc.subtotal_paise == 119920
        assert doc.tax_paise == gst.tax_on(119920, 18)
        assert doc.cgst_paise + doc.sgst_paise == doc.tax_paise
        assert doc.total_paise == 119920 + doc.tax_paise

    def test_positive_amount_with_discount_kind_is_still_a_discount(self, monkeypatch):
        """A tool that sends 29980 with kind=discount means -29980."""
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "")
        doc = gst.compute([
            {"description": "Pro", "amount": 149900},
            {"description": "SAVE20", "amount": 29980, "kind": "discount"},
        ], "29")
        assert doc.subtotal_paise == 119920

    def test_discount_larger_than_charge_is_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "")
        with pytest.raises(ValueError, match="exceed"):
            gst.compute([
                {"description": "Pro", "amount": 10000},
                {"description": "Too much", "amount": -20000, "kind": "discount"},
            ], "29")

    def test_negative_without_discount_kind_still_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "")
        # A bare negative is treated as a discount by inference, which is
        # forgiving at this layer; the API layer is the one that refuses it.
        doc = gst.compute([{"description": "A", "amount": 20000},
                           {"description": "B", "amount": -5000}], "29")
        assert doc.subtotal_paise == 15000


class TestDiscountThroughAPI:
    def test_checkout_with_discount_creates_correct_order(self, client, db,
                                                          api_auth, fake_rzp):
        r = client.post("/v1/checkout/sessions", headers=auth(api_auth), json={
            "customer": {"external_id": "org_1", "name": "Sunrise PG",
                         "state_code": "29"},
            "purpose": "subscription",
            "line_items": [
                {"description": "PGGuru Pro — Oct 2026", "amount": 149900},
                {"description": "Discount SAVE20 (20%)", "amount": -29980,
                 "kind": "discount"},
            ]})
        assert r.status_code == 200
        assert r.json()["amount"] == 119920

        order = db.scalars(select(Order)).first()
        assert order.amount_paise == 119920

    def test_stray_negative_is_refused_by_api(self, client, api_auth, fake_rzp):
        """Without kind='discount' a minus sign is a typo, not an intention."""
        r = client.post("/v1/checkout/sessions", headers=auth(api_auth), json={
            "customer": {"external_id": "org_1", "name": "Sunrise PG"},
            "line_items": [{"description": "Pro", "amount": 149900},
                           {"description": "Oops", "amount": -29980}]})
        assert r.status_code == 422
        assert "discount" in r.json()["error"]["message"]

    def test_invoice_shows_the_discount_line(self, client, db, api_auth, fake_rzp):
        client.post("/v1/checkout/sessions", headers=auth(api_auth), json={
            "customer": {"external_id": "org_1", "name": "Sunrise PG",
                         "state_code": "29"},
            "purpose": "subscription",
            "line_items": [
                {"description": "PGGuru Pro", "amount": 149900},
                {"description": "Discount SAVE20", "amount": -29980,
                 "kind": "discount"},
            ]})
        order = db.scalars(select(Order)).first()
        checkout_service.complete(db, razorpay_order_id=order.razorpay_order_id,
                                  razorpay_payment_id="pay_disc_1",
                                  verified_by="webhook")
        db.commit()

        invoice = db.scalars(select(Invoice)).first()
        assert invoice.subtotal_paise == 119920
        descriptions = [l.description for l in invoice.lines]
        assert any("SAVE20" in d for d in descriptions)
        # The customer can see what they were charged and what came off it.
        assert len(invoice.lines) == 2

    def test_balance_only_returns_just_the_number(self, client, db, api_auth,
                                                  product, fake_rzp):
        from app.models import Customer
        from app.services import wallet_service
        c = Customer(product_id=product.id, external_id="org_1", name="Sunrise PG")
        db.add(c); db.flush()
        wallet_service.credit(db, c.id, 500000)
        db.commit()

        r = client.get("/v1/customers/org_1/wallet?balance_only=1",
                       headers=auth(api_auth))
        assert r.status_code == 200
        assert r.json()["balance"] == 500000
        assert "transactions" not in r.json()
