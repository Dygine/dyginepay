"""
Money is an integer number of paise. Always.

There is no Decimal here and certainly no float. ₹1,499.00 is 149900. Every
rounding bug in a billing system starts the moment someone stores 1499.00 as a
float and a later sum comes out as 1498.9999999999998.

Tax is computed on paise with banker-free half-up rounding, which is what Indian
invoicing expects.
"""
from __future__ import annotations


def rupees_to_paise(rupees: str | int | float) -> int:
    """Parse a human amount into paise. Accepts '1499', '1499.50', 1499."""
    text = str(rupees).strip().replace(",", "").replace("\u20b9", "")
    if not text:
        raise ValueError("empty amount")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if "." in text:
        whole, _, frac = text.partition(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = text, "00"
    if not whole.isdigit() or not frac.isdigit():
        raise ValueError(f"not a valid amount: {rupees!r}")
    value = int(whole) * 100 + int(frac)
    return -value if negative else value


def paise_to_rupees(paise: int) -> str:
    """Format for display. 149900 -> '1,499.00'."""
    sign = "-" if paise < 0 else ""
    paise = abs(int(paise))
    whole, frac = divmod(paise, 100)
    # Indian grouping: last three digits, then pairs.
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{sign}{s}.{frac:02d}"


def rupee_display(paise: int) -> str:
    return f"\u20b9{paise_to_rupees(paise)}"


def half_up(numerator: int, denominator: int) -> int:
    """Integer division rounding .5 away from zero."""
    if denominator == 0:
        raise ZeroDivisionError
    sign = -1 if (numerator < 0) ^ (denominator < 0) else 1
    n, d = abs(numerator), abs(denominator)
    return sign * ((n * 2 + d) // (d * 2))


def tax_on(taxable_paise: int, rate_percent: int) -> int:
    """GST amount on a taxable value. Exclusive of tax."""
    return half_up(taxable_paise * rate_percent, 100)


def split_inclusive(gross_paise: int, rate_percent: int) -> tuple[int, int]:
    """
    Back out tax from a tax-inclusive amount.
    Returns (taxable_value, tax). taxable + tax == gross, exactly.
    """
    taxable = half_up(gross_paise * 100, 100 + rate_percent)
    return taxable, gross_paise - taxable
