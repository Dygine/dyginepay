"""
Invoice PDFs, generated on demand and never stored.

An invoice is fully determined by its rows, so it re-renders identically for as
long as those rows exist. That means no object storage, no S3 credentials, no
orphaned files - and it survives Render's ephemeral disk, which wipes on every
deploy.

fpdf2 rather than WeasyPrint: WeasyPrint needs cairo and pango system libraries
that Render's native Python runtime does not have, and finding that out at
deploy time is a bad afternoon.
"""
from __future__ import annotations

from fpdf import FPDF

from app.core.config import settings
from app.core.money import paise_to_rupees
from app.models import Invoice
from app.models.enums import InvoiceKind, TaxTreatment

TITLES = {
    InvoiceKind.TAX_INVOICE: "TAX INVOICE",
    InvoiceKind.BILL_OF_SUPPLY: "BILL OF SUPPLY",
    InvoiceKind.CREDIT_NOTE: "CREDIT NOTE",
}

INK = (17, 17, 17)
MUTED = (110, 110, 110)
RULE = (220, 220, 220)


def _rupees(paise: int) -> str:
    # fpdf2's core fonts are latin-1, which has no rupee sign. "Rs." is the
    # honest fallback; embedding a unicode TTF would mean shipping a font file.
    return f"Rs. {paise_to_rupees(paise)}"


#: Characters that routinely arrive from a web form or a formatted string and
#: are not in latin-1. An em dash in a plan description is enough to make the
#: whole invoice 500 - and the customer sees a broken download, not a bad
#: character.
_SUBSTITUTIONS = {
    "\u2014": "-",   # em dash
    "\u2013": "-",   # en dash
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2026": "...",
    "\u20b9": "Rs.",
    "\u00a0": " ",   # non-breaking space
    "\u2022": "-",   # bullet
    "\u2192": "->",
}


def latin1(text: str | None) -> str:
    """
    Make any string safe for fpdf2's core fonts.

    Substitutes the handful of characters that turn up in real descriptions,
    then drops anything else that cannot be encoded. Dropping is deliberate:
    an invoice with one odd character missing is a document; an invoice that
    raises is a 500 and no document at all.
    """
    if not text:
        return ""
    for bad, good in _SUBSTITUTIONS.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


class InvoicePDF(FPDF):
    def __init__(self, invoice: Invoice):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.invoice = invoice
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(15, 15, 15)

    def header(self) -> None:
        inv = self.invoice
        self.set_text_color(*INK)
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 8, latin1(settings.BUSINESS_NAME), new_x="LMARGIN", new_y="NEXT")

        self.set_font("Helvetica", "", 9)
        self.set_text_color(*MUTED)
        for line in [settings.BUSINESS_ADDRESS, settings.BUSINESS_EMAIL,
                     settings.BUSINESS_PHONE]:
            if line:
                self.cell(0, 4.5, latin1(line), new_x="LMARGIN", new_y="NEXT")
        if inv.seller_gstin:
            self.cell(0, 4.5, latin1(f"GSTIN: {inv.seller_gstin}"),
                      new_x="LMARGIN", new_y="NEXT")

        self.set_xy(120, 15)
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*INK)
        self.cell(75, 8, TITLES.get(inv.kind, "INVOICE"), align="R",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_xy(120, 24)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*MUTED)
        self.cell(75, 4.5, latin1(inv.number), align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_xy(120, 28.5)
        self.cell(75, 4.5, inv.issue_date.strftime("%d %b %Y"), align="R",
                  new_x="LMARGIN", new_y="NEXT")

        self.ln(8)
        self.set_draw_color(*RULE)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-16)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        note = ("This is a computer generated document and does not require a "
                "signature.")
        if self.invoice.kind == InvoiceKind.BILL_OF_SUPPLY:
            note = ("Not registered under GST. No tax is charged on this supply. "
                    + note)
        self.multi_cell(0, 3.5, note, align="C")

    def bill_to(self) -> None:
        inv = self.invoice
        self.set_font("Helvetica", "B", 8.5)
        self.set_text_color(*MUTED)
        self.cell(0, 5, "BILL TO", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "B", 10.5)
        self.set_text_color(*INK)
        self.cell(0, 5.5, latin1(inv.buyer_name), new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*MUTED)
        if inv.buyer_address:
            self.multi_cell(110, 4.5, latin1(inv.buyer_address))
        if inv.buyer_gstin:
            self.cell(0, 4.5, latin1(f"GSTIN: {inv.buyer_gstin}"),
                      new_x="LMARGIN", new_y="NEXT")
        if inv.place_of_supply:
            self.cell(0, 4.5, latin1(f"Place of supply: {inv.place_of_supply}"),
                      new_x="LMARGIN", new_y="NEXT")
        self.ln(5)

    def lines_table(self) -> None:
        inv = self.invoice
        inter = inv.tax_treatment == TaxTreatment.INTER_STATE
        taxed = inv.tax_treatment != TaxTreatment.NONE

        if taxed:
            widths = [75, 18, 12, 25, 25, 25]
            heads = ["Description", "SAC", "Qty", "Taxable",
                     "IGST" if inter else "CGST+SGST", "Amount"]
        else:
            widths = [100, 20, 15, 45]
            heads = ["Description", "SAC", "Qty", "Amount"]

        self.set_font("Helvetica", "B", 8.5)
        self.set_text_color(*MUTED)
        self.set_fill_color(248, 248, 248)
        for w, h in zip(widths, heads):
            self.cell(w, 7, h, align="L" if h == "Description" else "R", fill=True)
        self.ln()

        self.set_font("Helvetica", "", 9)
        self.set_text_color(*INK)
        for line in inv.lines:
            y = self.get_y()
            self.multi_cell(widths[0], 6, latin1(line.description)[:90],
                            new_x="RIGHT", new_y="TOP", max_line_height=5)
            self.set_xy(15 + widths[0], y)
            self.cell(widths[1], 6, latin1(line.sac) or "-", align="R")
            self.cell(widths[2], 6, str(line.quantity), align="R")
            if taxed:
                self.cell(widths[3], 6, _rupees(line.taxable_paise), align="R")
                tax = (line.igst_paise if inter
                       else line.cgst_paise + line.sgst_paise)
                self.cell(widths[4], 6, _rupees(tax), align="R")
                self.cell(widths[5], 6, _rupees(line.total_paise), align="R")
            else:
                self.cell(widths[3], 6, _rupees(line.total_paise), align="R")
            self.ln()

        self.ln(2)
        self.set_draw_color(*RULE)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(3)

    def totals(self) -> None:
        inv = self.invoice

        def row(label: str, value: str, bold: bool = False, size: float = 9):
            self.set_x(120)
            self.set_font("Helvetica", "B" if bold else "", size)
            self.set_text_color(*(INK if bold else MUTED))
            self.cell(40, 6, label, align="R")
            self.set_text_color(*INK)
            self.cell(35, 6, value, align="R", new_x="LMARGIN", new_y="NEXT")

        row("Subtotal", _rupees(inv.subtotal_paise))
        if inv.cgst_paise:
            row(f"CGST @ {inv.lines[0].tax_rate // 2 if inv.lines else 9}%",
                _rupees(inv.cgst_paise))
        if inv.sgst_paise:
            row(f"SGST @ {inv.lines[0].tax_rate // 2 if inv.lines else 9}%",
                _rupees(inv.sgst_paise))
        if inv.igst_paise:
            row(f"IGST @ {inv.lines[0].tax_rate if inv.lines else 18}%",
                _rupees(inv.igst_paise))
        self.ln(1)
        row("Total", _rupees(inv.total_paise), bold=True, size=11)


def render(invoice: Invoice) -> bytes:
    pdf = InvoicePDF(invoice)
    pdf.add_page()
    pdf.bill_to()
    pdf.lines_table()
    pdf.totals()
    out = pdf.output()
    return bytes(out)
