"""
Indian GST on a software subscription, and nothing more than that.

Three rules decide everything:

  1. Not registered  -> charge no tax, issue a Bill of Supply. Legal below the
     threshold (20 lakh for services) and the default until BUSINESS_GSTIN is set.
  2. Buyer in your state -> CGST 9% + SGST 9%.
  3. Buyer anywhere else -> IGST 18%.

Place of supply for a B2B service is the buyer's registered state. For B2C where
no address is on file, it falls back to the seller's state, which is the
conservative answer.

Tax is computed per line and then summed, not computed on the summed total. The
two differ by a paisa often enough to matter, and per-line is what GSTR-1 expects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.core.config import settings
from app.core.money import half_up, tax_on
from app.models.enums import InvoiceKind, TaxTreatment


@dataclass
class TaxedLine:
    description: str
    sac: str | None
    quantity: int
    unit_price_paise: int
    taxable_paise: int
    tax_rate: int
    cgst_paise: int = 0
    sgst_paise: int = 0
    igst_paise: int = 0

    @property
    def total_paise(self) -> int:
        return self.taxable_paise + self.cgst_paise + self.sgst_paise + self.igst_paise


@dataclass
class TaxedDocument:
    treatment: str
    kind: str
    lines: list[TaxedLine] = field(default_factory=list)

    @property
    def subtotal_paise(self) -> int:
        return sum(l.taxable_paise for l in self.lines)

    @property
    def cgst_paise(self) -> int:
        return sum(l.cgst_paise for l in self.lines)

    @property
    def sgst_paise(self) -> int:
        return sum(l.sgst_paise for l in self.lines)

    @property
    def igst_paise(self) -> int:
        return sum(l.igst_paise for l in self.lines)

    @property
    def tax_paise(self) -> int:
        return self.cgst_paise + self.sgst_paise + self.igst_paise

    @property
    def total_paise(self) -> int:
        return self.subtotal_paise + self.tax_paise


def determine_treatment(buyer_state_code: str | None) -> str:
    if not settings.gst_registered:
        return TaxTreatment.NONE
    seller = settings.BUSINESS_STATE_CODE
    if not buyer_state_code:
        return TaxTreatment.INTRA_STATE      # no address: assume local
    return (TaxTreatment.INTRA_STATE if buyer_state_code == seller
            else TaxTreatment.INTER_STATE)


def invoice_kind(treatment: str) -> str:
    return (InvoiceKind.BILL_OF_SUPPLY if treatment == TaxTreatment.NONE
            else InvoiceKind.TAX_INVOICE)


def compute(line_items: list[dict], buyer_state_code: str | None,
            rate: int | None = None) -> TaxedDocument:
    """
    line_items: [{description, amount (paise, per unit, tax-exclusive),
                  quantity, sac}]
    """
    treatment = determine_treatment(buyer_state_code)
    rate = settings.DEFAULT_GST_RATE if rate is None else rate
    doc = TaxedDocument(treatment=treatment, kind=invoice_kind(treatment))

    for item in line_items:
        qty = int(item.get("quantity") or 1)
        if qty < 1:
            raise ValueError("quantity must be at least 1")
        unit = int(item["amount"])
        if unit < 0:
            raise ValueError("amount cannot be negative")
        taxable = unit * qty

        line = TaxedLine(
            description=str(item["description"])[:500],
            sac=item.get("sac") or settings.DEFAULT_SAC,
            quantity=qty,
            unit_price_paise=unit,
            taxable_paise=taxable,
            tax_rate=0 if treatment == TaxTreatment.NONE else rate,
        )

        if treatment == TaxTreatment.INTRA_STATE:
            # Half each. Computed as half the total rather than two separate
            # roundings, so CGST + SGST always equals the full tax exactly.
            total_tax = tax_on(taxable, rate)
            line.cgst_paise = half_up(total_tax, 2)
            line.sgst_paise = total_tax - line.cgst_paise
        elif treatment == TaxTreatment.INTER_STATE:
            line.igst_paise = tax_on(taxable, rate)

        doc.lines.append(line)

    return doc


def financial_year(on: date) -> str:
    """Indian FY runs April to March. 14 Sep 2026 -> '26-27'."""
    start = on.year if on.month >= 4 else on.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"
