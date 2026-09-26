"""Deterministic, defensive parsers for tender commercial strings.

The analyzer captures bid value / EMD / closing date as verbatim strings.
These turn them into typed values for the Tender columns, returning None on
any ambiguity so a wrong value is never written.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_LAKH = 100_000.0
_CRORE = 10_000_000.0


def parse_indian_currency(s: Optional[str]) -> Optional[float]:
    """Parse an Indian-format currency string to a float amount.

    Handles: '₹ 1,12,55,870.40', 'Rs. 2,25,000/-', 'INR 45.6 Lakh', '2.5 Cr',
    and bare decimal amounts like '349603.68'. Returns None for
    empty/None/non-numeric input, and — critically — for any digit-bearing
    prose that lacks a currency signal (percentages, clause/schedule
    numbers, page numbers, counts, tender numbers, plain integers, ...).
    A bare number is only trusted as currency when the string carries one
    of: a ₹/Rs/INR/rupees symbol or word, a lakh/crore unit, Indian
    comma-grouping (e.g. '2,25,000'), or an exact 2-decimal-place amount
    (the canonical bare-rupee format). Otherwise this returns None so a
    fabricated value is never written.
    """
    if not s or not isinstance(s, str):
        return None
    text = s.strip().lower()
    if not text:
        return None
    # A percentage is never a currency amount, regardless of digits present.
    if "%" in text:
        return None
    # Detect a lakh/crore multiplier: the unit must be ADJACENT to the
    # number it modifies (e.g. "45.6 Lakh", "2.5 Cr"). A standalone "Cr"/
    # "lac" elsewhere in the string (e.g. "Contract Ref: CR/2026/45",
    # "Cr. No. 45") is NOT a multiplier and NOT a currency signal — those
    # are corrigendum/contract-ref abbreviations, not crore.
    mult = 1.0
    unit_token = None
    unit_match = re.search(
        r"(\d[\d,]*(?:\.\d+)?)\s*(cr\b|crores?\b|lakhs?\b|lac?s?\b)", text
    )
    if unit_match:
        token_raw = unit_match.group(1).replace(",", "")
        unit = unit_match.group(2)
        mult = _CRORE if unit.startswith("cr") else _LAKH
        unit_token = token_raw

    # Currency signals detected on the original (pre-strip) text.
    has_symbol = bool(re.search(r"(₹|\brs\.?\b|\binr\b|\brupees?\b)", text))
    has_grouping = bool(re.search(r"\d,\d", text))

    if unit_token is not None:
        token = unit_token
    else:
        # Strip currency words/symbols and a trailing '/-'.
        stripped = text.replace("/-", " ")
        stripped = re.sub(r"(₹|rs\.?|inr|/=|/\-)", " ", stripped)
        # Remove Indian digit-group commas.
        stripped = stripped.replace(",", "")
        # Grab the first decimal/integer number token.
        m = re.search(r"\d+(?:\.\d+)?", stripped)
        if not m:
            return None
        token = m.group(0)
        has_2dp = bool(re.fullmatch(r"\d+\.\d{2}", token))
        if not (has_symbol or has_grouping or has_2dp):
            # No currency signal: this is digit-bearing prose (a clause
            # number, a count, a page number, a tender number, ...), not
            # an amount.
            return None
    try:
        value = float(token) * mult
    except ValueError:
        return None
    if value == 0:
        # A "0"/"Rs 0" placeholder is not a real amount.
        return None
    return value


# Ordered list of (strptime format, whether it carries time).
# Day-first only, intentionally: Indian tenders always render dates
# DD/MM/YYYY, so '01/02/2026' must resolve as 1-Feb, never 2-Jan.
_DATE_FORMATS = [
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%d-%b-%Y %H:%M",
    "%d-%b-%Y",
    "%d %b %Y %H:%M",
    "%d %b %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def parse_tender_date(s: Optional[str]) -> Optional[datetime]:
    """Parse a tender date string to a datetime. Returns None on ambiguity.

    Handles '21/07/2026 15:00 hrs', '21-Jul-2026', ISO forms, etc. Strips a
    trailing 'hrs'/'IST' and collapses whitespace before matching.
    """
    if not s or not isinstance(s, str):
        return None
    text = s.strip()
    if not text:
        return None
    # Normalize: drop trailing 'hrs', 'ist', collapse whitespace.
    text = re.sub(r"\b(hrs?|ist)\b\.?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip().strip(",")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
