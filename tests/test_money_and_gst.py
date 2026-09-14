"""Money arithmetic and GST. If these are wrong, nothing else matters."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.money import (
    half_up, paise_to_rupees, rupees_to_paise, split_inclusive, tax_on,
)
from app.models.enums import InvoiceKind, TaxTreatment
from app.services import gst


class TestMoney:
    @pytest.mark.parametrize("given,expected", [
        ("1499", 149900), ("1499.00", 149900), ("1499.5", 149950),
        ("1499.50", 149950), ("0.01", 1), ("1,49,900", 14990000),
        ("\u20b9499", 49900), (1499, 149900),
    ])
    def test_parsing(self, given, expected):
        assert rupees_to_paise(given) == expected

    def test_rejects_nonsense(self):
        with pytest.raises(ValueError):
            rupees_to_paise("abc")

    @pytest.mark.parametrize("paise,expected", [
        (149900, "1,499.00"), (100, "1.00"), (1, "0.01"),
        (14990000, "1,49,900.00"),          # Indian grouping, not 149,900.00
        (100000000, "10,00,000.00"),
    ])
    def test_formatting(self, paise, expected):
        assert paise_to_rupees(paise) == expected

    def test_round_trip_never_drifts(self):
        for paise in (1, 99, 100, 149900, 999999, 100000000):
            assert rupees_to_paise(paise_to_rupees(paise)) == paise

    def test_half_up_rounds_away_from_zero(self):
        assert half_up(5, 2) == 3           # 2.5 -> 3, not banker's 2
        assert half_up(7, 2) == 4
        assert half_up(-5, 2) == -3

    def test_inclusive_split_is_exact(self):
        for gross in (11800, 176882, 100, 999999):
            taxable, tax = split_inclusive(gross, 18)
            assert taxable + tax == gross    # must never lose a paisa


class TestGST:
    def test_unregistered_charges_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "")
        doc = gst.compute([{"description": "Pro", "amount": 149900}], "29")
        assert doc.treatment == TaxTreatment.NONE
        assert doc.kind == InvoiceKind.BILL_OF_SUPPLY
        assert doc.tax_paise == 0
        assert doc.total_paise == 149900

    def test_same_state_splits_cgst_sgst(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "29ABCDE1234F1Z5")
        doc = gst.compute([{"description": "Pro", "amount": 149900}], "29")
        assert doc.treatment == TaxTreatment.INTRA_STATE
        assert doc.cgst_paise == 13491         # 9%
        assert doc.sgst_paise == 13491
        assert doc.igst_paise == 0
        assert doc.total_paise == 176882

    def test_other_state_uses_igst(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "29ABCDE1234F1Z5")
        doc = gst.compute([{"description": "Pro", "amount": 149900}], "27")
        assert doc.treatment == TaxTreatment.INTER_STATE
        assert doc.igst_paise == 26982         # 18%
        assert doc.cgst_paise == doc.sgst_paise == 0

    def test_cgst_plus_sgst_always_equals_total_tax(self, monkeypatch):
        """The odd-paisa case. 9% + 9% computed separately can differ from 18%."""
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "29ABCDE1234F1Z5")
        for amount in (1, 33, 99, 101, 12345, 99999):
            doc = gst.compute([{"description": "x", "amount": amount}], "29")
            assert doc.cgst_paise + doc.sgst_paise == tax_on(amount, 18)

    def test_quantity_multiplies(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "")
        doc = gst.compute([{"description": "Seat", "amount": 10000,
                            "quantity": 7}], "29")
        assert doc.subtotal_paise == 70000

    def test_no_buyer_state_assumes_local(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_GSTIN", "29ABCDE1234F1Z5")
        doc = gst.compute([{"description": "x", "amount": 10000}], None)
        assert doc.treatment == TaxTreatment.INTRA_STATE

    @pytest.mark.parametrize("when,fy", [
        ("2026-09-14", "26-27"), ("2026-04-01", "26-27"),
        ("2026-03-31", "25-26"), ("2027-01-15", "26-27"),
    ])
    def test_financial_year_boundary(self, when, fy):
        from datetime import date
        assert gst.financial_year(date.fromisoformat(when)) == fy
