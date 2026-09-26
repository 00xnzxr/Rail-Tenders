"""
DRPL Backend - BOQ Parser Service
Extracts the tender's bidding schedule (NIT "Schedule of Items" table) from
tender PDFs. NIT-aware: source documents are picked by their classified
`doc_type` (from the per-doc Haiku pass), and the full NIT column set is
captured — Item Code / Qty / Unit / Unit Rate / Basic Value / Escl.(%) /
Amount / Bidding Unit, grouped by Schedule (A, B, …) — so the costing
agent can mirror the schedule 1:1.

Plan: ~/.claude/plans/now-i-need-to-synchronous-taco.md
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.cost_breakdown import CostBreakdownLine
from app.models.costing_template import BOQItem, BOQScheduleTotal, CostingTemplate
from app.models.document_analysis import DocumentExtractionResult
from app.models.tender import TenderDocument
from app.services.costing.nit_schedule_parser import parse_nit_text, is_ireps_schedule_format

logger = logging.getLogger(__name__)

# NIT column header patterns. Matches BOTH thin BOQ tables (the legacy
# Sr.No./Description/Qty/Unit/Rate shape) and full NIT schedules (which
# add Item Code, Basic Value, Escl.%, Bidding Unit).
_BOQ_HEADER_PATTERNS = [
    r"sr\.?\s*no|s\.?\s*no|sl\.?\s*no|item\s*no",
    r"description|particulars|item\s*description|name\s*of\s*item|scope\s*of\s*work",
    r"qty|quantity|nos|numbers",
    r"unit|uom|u\.o\.m",
    r"rate|unit\s*rate|price",
    r"amount|total|value",
    r"item\s*code",
    r"basic\s*value",
    r"escl|escalation",
    r"bidding\s*unit",
]

# Doc-type tags (from the per-doc Haiku pass, vocabulary in
# `document_analysis_agent`) that positively mean 'this document carries no
# priced line items'. These are the ONLY documents costing skips: an
# unrecognised tag, or none at all, is silence rather than evidence, and a
# skipped document is scope that vanishes without anything looking wrong.
# See `_pick_nit_source_docs`.
_NON_SCHEDULE_DOC_TYPES = frozenset({"drawing", "terms_conditions"})

# Tax line detection. A tax line is a row that IS a tax charge ("Provision of
# GST @ 18% on SCHEDULE-A") — it carries no scope and gets no researched rate.
#
# Matching the tax token anywhere in the description is NOT enough: NIT work
# items routinely qualify how their rate is quoted ("Supply & Fitment SS Metal
# RMPU Trough … (Rates are inclusive of GST @ 18%)"). Flagging those as tax
# rows removes them from `cost_rows` in the batched costing node, so a tender
# whose every row carries that qualifier gets priced ZERO rows and silently
# returns "[NEEDS RATE]" (Command Center session 285 / tender 3809).
_TAX_TOKEN_RE = re.compile(r"\b(?:C|S|I)?GST\b", re.IGNORECASE)
_TAX_PROVISION_RE = re.compile(r"provision\s+of\s+tax", re.IGNORECASE)

# "(Rates are inclusive of GST @ 18%)", "incl. of GST", "excluding GST",
# "inclusive of all taxes and GST" — a qualifier describing how a WORK item's
# rate is quoted. Stripped before deciding whether the ROW ITSELF is a tax row.
_TAX_QUALIFIER_RE = re.compile(
    r"\(?\s*(?:rates?\s+(?:are|is)\s+)?"
    r"(?:inclusive|including|incl\.?|exclusive|excluding|excl\.?|net)\s*"
    r"(?:of\s+)?(?:all\s+)?(?:taxes?\s*(?:,|and|&)?\s*)*"
    r"(?:C|S|I)?GST\b[^)]*\)?",
    re.IGNORECASE,
)

# The row's subject is the tax itself: "GST @ 18%", "Provision of GST on
# Schedule-A", "Add: IGST 18%", "18. GST @ 18%".
_TAX_SUBJECT_RE = re.compile(
    r"^[\s\d.)\-]*"
    r"(?:(?:add|plus|less|deduct)\s*:?\s*)?"
    r"(?:provision\s+(?:of|for)\s+)?"
    r"(?:C|S|I)?GST\b",
    re.IGNORECASE,
)

# A short row whose whole content is about the tax ("Total GST payable") is a
# tax row even though the tax word isn't the leading token. Long descriptions
# are work items that merely mention a tax.
_TAX_SHORT_ROW_MAX_CHARS = 60

# Longest string still plausible as a unit of measure. Real ones are short —
# "Nos", "MT", "Cum", "Sqm", "Job", "Running Metre", "Metric Tonne". A whole
# sentence that landed in the unit column is not a unit, and this is what stops
# the quantified-row branch of `_is_valid_schedule_item` from leaning on it.
_MAX_UNIT_CHARS = 24

# A serial-only row (see `_is_valid_schedule_item` (g)) is accepted only when
# its description reads like a table cell, not a sentence: at most this many
# words and no terminal full stop. A material description -- "Synthetic enamel
# paint, signal red, conforming to IS 2932" -- fits; a clause does not.
_SERIAL_ROW_MAX_WORDS = 20


def _serial_of(item: dict) -> Optional[int]:
    """The row's printed serial as a positive int, or None. Accepts "7" and
    7.0; rejects 0 (the pdfplumber path writes 0 when the column is absent)."""
    raw = item.get("sr_no")
    try:
        n = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _reads_like_a_cell(description: str) -> bool:
    """A table cell, as opposed to a sentence of prose."""
    d = (description or "").strip()
    if not d or d.endswith((".", ";", ":")):
        return False
    return len(d.split()) <= _SERIAL_ROW_MAX_WORDS


# A compliance-matrix cell -- "Yes No", "No No Not Allowed", "N/A" -- has a
# serial, few words and no full stop, which is exactly the shape the serial-only
# branch accepts. It is never a line item.
#: "RDSO/CG/DRG/21032", "ICF/Drg No WL...", "Drg. No. K-1034", "as per drawing no".
_DRAWING_REGISTER_RE = re.compile(
    r"\b(?:[A-Z]{2,5}\s*/\s*[A-Z]{1,5}\s*/\s*DRG\b|DRG\.?\s*NO\b|DRAWING\s+NO\b)",
    re.IGNORECASE,
)

_YES_NO_CELL_RE = re.compile(
    r"^\s*(?:(?:yes|no|not\s+allowed|allowed|n\s*/?\s*a|nil|none|-+)\s*)+$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Annexures
# ---------------------------------------------------------------------------
#
# A NIT schedule item can cite an annexure as its breakdown:
#
#     Material Cost for Conversion work from ICF to NMGHSR coaches
#     (As per Annexure-II of Material list uploaded in Document Section of NIT)
#
# The annexure is a table of the materials that make up that ONE item, per
# coach set. Its rows are captured as BOQItems so they can be costed, and the
# two patterns below are how a captured row finds the item it belongs to: the
# heading printed above the table names the annexure, and the schedule item's
# description cites it. Without that link the annexure was costed as extra
# scope on top of the item it breaks down (the Liluah tender, fourth look).
#
# `ann+exure` because the Liluah NIT prints "Annnexure- IV" -- a typo the
# Railway has no reason to fix and we have no reason to fail on.
_ANNEXURE_HEADING_RE = re.compile(
    r"^[ \t]*ann+exure\s*[-–—:]?\s*([IVXLC]+|\d{1,3}|[A-Z])\b",
    re.IGNORECASE | re.MULTILINE,
)
_ANNEXURE_CITATION_RE = re.compile(
    r"ann+exure\s*[-–—:]?\s*([IVXLC]+|\d{1,3}|[A-Z])\b",
    re.IGNORECASE,
)
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _roman_to_int(s: str) -> Optional[int]:
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        v = _ROMAN_VALUES.get(ch)
        if v is None:
            return None
        total += -v if v < prev else v
        prev = max(prev, v)
    return total if total > 0 else None


def _int_to_roman(n: int) -> str:
    out = []
    for value, sym in ((100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"),
                       (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def normalize_annexure_ref(raw) -> Optional[str]:
    """Canonical annexure label: "2", "II", "ii" and "Annexure-II" all become
    "II"; a lettered annexure keeps its letter ("B"). None when it is not a
    label at all."""
    if raw is None:
        return None
    s = str(raw).strip()
    m = _ANNEXURE_CITATION_RE.search(s)
    if m:
        s = m.group(1)
    s = s.strip().upper()
    if not s:
        return None
    if s.isdigit():
        n = int(s)
        return _int_to_roman(n) if 0 < n < 400 else None
    if re.fullmatch(r"[IVXLC]+", s):
        n = _roman_to_int(s)
        return _int_to_roman(n) if n else None
    if re.fullmatch(r"[A-Z]", s):
        return s
    return None


def _cited_annexures(description: Optional[str]) -> list[str]:
    """The annexures a schedule item's description cites, in order, deduped."""
    out: list[str] = []
    for m in _ANNEXURE_CITATION_RE.finditer(description or ""):
        ref = normalize_annexure_ref(m.group(1))
        if ref and ref not in out:
            out.append(ref)
    return out


def _annexure_carry(pages: list[dict]) -> tuple[list[Optional[str]], list[bool]]:
    """Per page: the annexure whose table the page continues at its start, and
    whether the page begins a new annexure. The label carries forward until
    the next heading, the way a schedule banner does."""
    carry: list[Optional[str]] = []
    starts: list[bool] = []
    current: Optional[str] = None
    for p in pages:
        carry.append(current)
        found = [normalize_annexure_ref(m.group(1))
                 for m in _ANNEXURE_HEADING_RE.finditer(p.get("text") or "")]
        found = [f for f in found if f]
        starts.append(bool(found) and found[0] != current)
        if found:
            current = found[-1]
    return carry, starts


def _link_annexure_components(items: list[dict]) -> dict:
    """Bind annexure rows to the schedule item that cites their annexure.

    In place: every row without a schedule whose `annexure_ref` is cited by a
    schedule row gets `_parent_idx` = that row's position in `items` -- a
    position, not a (schedule, code) key, because an IREPS NIT repeats word
    codes ("CONVERSION MAT" on six rows of the Liluah schedule) and a key
    would bind to the wrong one. An annexure cited by several schedule items
    is bound to the first, and said so. Returns the reconciliation the run
    reports: which annexures the schedule cites, which arrived, which are
    missing.
    """
    parents_by_ref: dict[str, list[int]] = {}
    for idx, it in enumerate(items):
        if (it.get("schedule_name") or "").strip():
            for ref in _cited_annexures(it.get("description")):
                parents_by_ref.setdefault(ref, []).append(idx)

    captured: dict[str, int] = {}
    for it in items:
        ref = it.get("annexure_ref")
        if ref and not (it.get("schedule_name") or "").strip():
            captured[ref] = captured.get(ref, 0) + 1

    linked: dict[str, dict] = {}
    for it in items:
        ref = it.get("annexure_ref")
        if not ref or (it.get("schedule_name") or "").strip():
            continue
        parents = parents_by_ref.get(ref)
        if not parents:
            continue
        parent_idx = parents[0]
        parent = items[parent_idx]
        it["_parent_idx"] = parent_idx
        entry = linked.setdefault(ref, {
            "annexure": ref, "rows": 0,
            "parent": _row_identity(parent),
            "parent_description": (parent.get("description") or "")[:120],
            "ambiguous": len(parents) > 1,
        })
        entry["rows"] += 1

    cited = sorted(parents_by_ref.keys(), key=_annexure_sort_key)
    return {
        "cited": cited,
        "captured": {k: captured[k] for k in sorted(captured, key=_annexure_sort_key)},
        "missing": [r for r in cited if r not in captured],
        "unlinked": sorted((r for r in captured if r not in parents_by_ref), key=_annexure_sort_key),
        "linked": [linked[k] for k in sorted(linked, key=_annexure_sort_key)],
    }


def _annexure_sort_key(ref: str) -> tuple:
    n = _roman_to_int(ref) if re.fullmatch(r"[IVXLC]+", ref or "") else None
    return (0, n, "") if n else (1, 0, ref or "")


def _row_identity(it: dict) -> tuple:
    """(schedule, sr_no, item_code) -- how a schedule row is named in the
    capture report. For display and the log only; the component link itself
    is by position (see `_link_annexure_components`)."""
    sched = (it.get("schedule_name") or "").strip()
    code = (it.get("item_code") or "").strip()
    return (sched, it.get("sr_no"), code)


def _assign_annexure_codes(items: list[dict]) -> None:
    """In place: give every code-less annexure row its synthetic code, numbered
    by its position within that annexure in reading order."""
    seq: dict[str, int] = {}
    for it in items:
        ref = it.get("annexure_ref")
        if not ref or (it.get("schedule_name") or "").strip():
            continue
        code = (_str_or_none(it.get("item_code")) or "").strip()
        # A bare number in the code column of an annexure row is the
        # sub-table's number ("12" on every row under heading 12), not a
        # per-row code: it repeats, and a repeated code is what the synthetic
        # code exists to prevent.
        if code and not code.isdigit():
            continue
        seq[ref] = seq.get(ref, 0) + 1
        it["item_code"] = _synth_annexure_code(ref, seq[ref])


def _drop_separately_scheduled_labour(items: list[dict]) -> list[dict]:
    """A material annexure's labour footer can repeat a separate NIT item.

    Drop only that exact published charge, in the same schedule as the
    material parent. An inclusive assembly's own labour (Annexure-III in
    Liluah) has no such schedule counterpart and must remain in its build-up.
    """
    out = []
    for item in items:
        ref = item.get("annexure_ref")
        desc = (item.get("description") or "").strip()
        if ref and not item.get("schedule_name") and re.fullmatch(r"labou?r\s+(?:cost|charges?)(?:\s*\([A-Z]\))?", desc, re.I):
            value = _row_value(item)
            material_schedules = {
                r.get("schedule_name") for r in items
                if r.get("schedule_name") and ref in _cited_annexures(r.get("description"))
                and re.search(r"\bmaterial\s+cost\b", r.get("description") or "", re.I)
            }
            matches = [r for r in items if r.get("schedule_name") in material_schedules
                       and re.search(r"\blabou?r\b", r.get("description") or "", re.I)
                       and value is not None and _row_rate(r) is not None
                       and abs(float(_row_rate(r)) - value) < 0.5]
            if len(matches) == 1:
                logger.info("[boq_parser] annexure %s labour charge already present as schedule item %s/%s",
                            ref, matches[0].get("schedule_name"), matches[0].get("sr_no"))
                continue
        out.append(item)
    return out


#: A quantity that misses the printed total by more than this is a misread.
_ANNEXURE_QTY_TOLERANCE = 0.02


def _rounding_slack(count: float) -> float:
    """How far a printed total can sit from count x a two-decimal rate
    through the rate's own rounding: half a paisa per counted item."""
    return max(0.02, abs(count) * 0.005 + 0.01)


def _power_of_ten_apart(a: float, b: float) -> bool:
    """Whether a and b differ by a clean factor of 10, 100, ... (either way):
    the signature of a shifted digit or decimal point, not of a wrong cell."""
    import math
    if a <= 0 or b <= 0:
        return False
    k = round(math.log10(a / b))
    return k != 0 and abs(a / b / (10 ** k) - 1) <= 0.01


def _normalize_annexure_operands(items: list[dict]) -> None:
    """Compute weight quantities from separately transcribed source cells,
    and hold every row to its printed total.

    Never infer a tenfold quantity increase from a misread money cell when
    the printed quantity and weight are available. A total-only material is
    priced as one accounting lot, not as an invented physical quantity.

    A material list's arithmetic is Qty x Weight/Item x % x Rate/Item = Total,
    and the cells the extractor returns are checked against it, not trusted.
    Tender 5157 put a Rs 4,360.13 assembly price in the Weight/Item cell of
    one of its five parts (279,048 kg of screws), a drawing length of
    "15304 mm" in a plate's Qty cell (4.4 million kg), and, on rows that
    print no weight, the Rate/Item in the weight cell and the total in the
    rate cell (Rs 2,145.53 a kg for four angles). Three such rows put
    Rs 2,300 crore on a Rs 2 crore schedule. Each shape is recognised by the
    arithmetic alone; a row it cannot settle keeps what was read, marked low.
    """
    import math

    def number(value):
        try:
            n = float(value)
            return n if math.isfinite(n) and n >= 0 else None
        except (TypeError, ValueError):
            return None

    def set_rate(it, rate):
        it["unit_rate"] = rate
        if it.get("estimated_rate") is not None:
            it["estimated_rate"] = rate

    def per_item_unit(it):
        if (it.get("unit") or "").strip().lower() in ("", "kg"):
            it["unit"] = "Nos"

    for it in items:
        if not it.get("annexure_ref") or it.get("schedule_name"):
            continue
        if it.get("_numeric_source_issue"):
            # Do not manufacture a new basis from whichever cells remain.
            it["quantity"] = it["unit_rate"] = it["estimated_rate"] = it["basic_value"] = None
            continue
        total = number(it.get("basic_value")) or None
        rate = number(it.get("unit_rate") if it.get("unit_rate") is not None
                      else it.get("estimated_rate")) or None
        if "printed_quantity" in it:
            qty = number(it.get("printed_quantity"))
            weight = number(it.get("weight_per_item"))
            percent = number(it.get("quantity_percent"))
            factor = percent / 100 if percent else 1.0
            if (weight is not None and rate is None and total is None
                    and it.get("_weight_cell_is_money")):
                # The page prints this "weight" only as a rupee amount and the
                # row has no other price: it is the Rate/Item, wrapped out of
                # its cell (the louvre frame: 8 at Rs 1,213.04 read as 9,704 kg).
                rate, weight = weight, None
                set_rate(it, rate)
                it["weight_per_item"] = None
                per_item_unit(it)
                it["extraction_confidence"] = "low"
            if qty is not None:
                count = qty * factor
                quantity = count * (weight if weight is not None else 1.0)
                settled = False
                if weight:
                    paid = total if total is not None else rate
                    consistent = (rate is not None and total is not None
                                  and abs(quantity * rate - total) <= max(0.5, _ANNEXURE_QTY_TOLERANCE * total))
                    if (paid is not None and not consistent and weight < paid
                            and abs(count * weight - paid) <= _rounding_slack(count)):
                        # No weight is printed on this row: the "weight" is
                        # its Rate/Item and the next money cell its total.
                        rate, total, weight, quantity = weight, paid, None, count
                        set_rate(it, rate)
                        it["basic_value"] = total
                        it["weight_per_item"] = None
                        per_item_unit(it)
                        settled = True
                it["quantity"] = round(quantity, 6)
                if weight is not None:
                    it["unit"] = "kg"
                elif rate is not None and (it.get("unit") or "").strip().lower() == "kg":
                    # The list prints "kg" in the unit column and no weight:
                    # the quantity is a count of pieces at a per-piece rate
                    # (Angle 6 mm: 4 at Rs 536.38 = Rs 2,145.53). Left as
                    # "kg", the researcher priced four kilograms of angle at
                    # Rs 52 -- a tenth of the printed figure.
                    it["unit"] = "Nos"
                it["_quantity_from_printed_cells"] = True
                if (not settled and rate is not None and total is not None
                        and abs(quantity * rate - total) > max(0.5, _ANNEXURE_QTY_TOLERANCE * total)):
                    it["extraction_confidence"] = "low"
                    implied = total / rate
                    if abs(total - rate) <= max(0.5, _ANNEXURE_QTY_TOLERANCE * total) and count != 1:
                        # One price printed against several parts -- a merged
                        # cell: the assembly's lot price, not a price per part.
                        it["quantity"] = 1.0
                        it["unit"] = "Lot (printed total)"
                        set_rate(it, total)
                        it["_merged_assembly_price"] = True
                        logger.warning(
                            "[boq_parser] annexure %s row %s: Rs %.2f is printed once for its "
                            "assembly; priced as one lot, not %.6g x Rs %.2f",
                            it.get("annexure_ref"), it.get("sr_no"), total, quantity, rate,
                        )
                    elif _power_of_ten_apart(implied, quantity):
                        # A shifted digit, and nothing says which cell has it:
                        # the printed quantity stands, for review.
                        logger.warning(
                            "[boq_parser] annexure %s row %s: printed quantity/rate imply %.2f, "
                            "captured total %.2f; retaining the printed quantity for review",
                            it.get("annexure_ref"), it.get("sr_no"), quantity * rate, total,
                        )
                    else:
                        # The printed total is the check: a quantity that
                        # cannot reach it was read from the wrong cell.
                        it["quantity"] = round(implied, 3)
                        logger.warning(
                            "[boq_parser] annexure %s row %s: %.6g x Rs %.2f cannot make the "
                            "printed total Rs %.2f; quantity taken from the total (%.3f)",
                            it.get("annexure_ref"), it.get("sr_no"), quantity, rate, total, implied,
                        )
            else:
                it["quantity"] = None
            if it.get("_printed_nil_total") and rate is not None and it.get("quantity"):
                # Printed with a rate and a total of Rs 0.00, and left out of
                # its sub-assembly's total (the Liluah side wall's louvre frame
                # is priced on its own sheet): the list adds nothing for it
                # here, and neither does the costing.
                it["quantity"] = 0.0
                desc = (it.get("description") or "").strip()
                if "printed total Rs 0.00" not in desc:
                    it["description"] = f"{desc} (printed total Rs 0.00)"
        if (it.get("quantity") is None and not (it.get("unit") or "").strip()
                and number(it.get("printed_quantity")) is None
                and number(it.get("weight_per_item")) is None):
            # One printed amount and nothing to count it by (the guard room's
            # "Bib Cock ... Rs 1,965.25"), whichever money cell it was read
            # into: the stated scope, priced as one lot.
            lot = total if total is not None else rate
            if lot and (rate is None or total is None or abs(rate - total) <= 0.5):
                it["quantity"] = 1.0
                it["unit"] = "Lot (printed total)"
                set_rate(it, lot)
                it["basic_value"] = lot
                it["_quantity_from_printed_cells"] = True


def _fold_merged_assembly_parts(items: list[dict]) -> list[dict]:
    """Fold the unpriced parts of an assembly into its one printed lot price.

    Where a sub-assembly prints one merged price across its parts ("Mounting
    of Sliding Door": five parts, one Rs 4,360.13), the part carrying the
    price became the lot (`_normalize_annexure_operands`) and the other parts
    arrive with a quantity and no price. Costed on their own they would be
    researched and added on top of the lot that already includes them.

    The parts are the lot's table: the unbroken run of serials around it
    (1, 2, 3, 4, 5) in the same annexure, and the same sub-assembly heading
    when the extractor gave one. It is folded only when no other row of that
    table carries printed money; the lot keeps every part's name.
    """
    def priced(r):
        return any((r.get(k) or 0) for k in ("unit_rate", "estimated_rate", "basic_value"))

    def serial(r):
        return _serial_of(r)

    def same_table(r, lot):
        return (r.get("annexure_ref") == lot.get("annexure_ref")
                and not (r.get("schedule_name") or "").strip()
                and (r.get("subassembly") or "").strip() == (lot.get("subassembly") or "").strip())

    drop: set = set()
    for idx, lot in enumerate(items):
        if not lot.get("_merged_assembly_price") or serial(lot) is None:
            continue
        lo = idx
        while (lo > 0 and same_table(items[lo - 1], lot) and serial(items[lo - 1]) is not None
               and serial(items[lo - 1]) == serial(items[lo]) - 1):
            lo -= 1
        hi = idx
        while (hi + 1 < len(items) and same_table(items[hi + 1], lot) and serial(items[hi + 1]) is not None
               and serial(items[hi + 1]) == serial(items[hi]) + 1):
            hi += 1
        parts = [items[k] for k in range(lo, hi + 1) if k != idx]
        if not parts or any(priced(r) or r.get("_merged_assembly_price") for r in parts):
            continue
        names = "; ".join((items[k].get("description") or "").strip() for k in range(lo, hi + 1)
                          if (items[k].get("description") or "").strip())
        heading = (lot.get("subassembly") or "").strip() or "Assembly"
        lot["description"] = f"{heading} -- one printed price for: {names}"
        drop.update(k for k in range(lo, hi + 1) if k != idx)
        logger.info("[boq_parser] annexure %s: %d part(s) of %r folded into its one printed price",
                    lot.get("annexure_ref"), len(parts), heading[:60])
    return [it for k, it in enumerate(items) if k not in drop]


def _reconcile_annexure_quantities(items: list[dict]) -> int:
    """In place: where an annexure row prints a rate AND a total, and the
    quantity the extractor read does not multiply out to that total, replace
    the quantity with total / rate. Returns how many rows were corrected.

    The material list is read by vision, and a weight column reads wrong
    more often than a money column does: the Liluah Annexure-II came back
    with "12243.2 kg" for a chequered plate whose printed total is Rs 22,523
    at Rs 39.18/kg (575 kg). The sum of the printed totals reconciled to 99%
    of the published per-set rate, so the totals are the trusted figure; the
    quantity is what the costing multiplies the researched rate by, so a
    wrong one puts a whole coach set's steel on one row and the parent's
    built-up rate twenty times over the published one. Only rows carrying
    both printed figures are touched; a row with no total is left as read.
    """
    fixed = 0
    _normalize_annexure_operands(items)
    for it in items:
        if not it.get("annexure_ref") or (it.get("schedule_name") or "").strip():
            continue
        if it.get("_quantity_from_printed_cells"):
            continue
        if it.get("_numeric_source_issue"):
            continue
        try:
            rate = float(it.get("estimated_rate") if it.get("estimated_rate") is not None
                         else it.get("unit_rate") or 0)
            total = float(it.get("basic_value") or 0)
            qty = it.get("quantity")
            qty = float(qty) if qty is not None else None
        except (TypeError, ValueError):
            continue
        if (rate >= 100 and total <= 0 and qty is not None
                and abs(qty - rate) <= 1e-6 * max(1.0, rate)):
            # (>= 100: a money figure, not "4 pcs at Rs 4" which is harmless.)
            # The same number in the quantity and rate columns and no total:
            # the row's printed total landed in both cells ("SL to CSK HD
            # Screw M6x30: 4360.13 kg at Rs 4360.13/kg" put Rs 1.5 crore of
            # screws in one coach set). The number is the lot's total; the
            # row is one lot at that rate.
            it["basic_value"] = rate
            it["quantity"] = 1.0
            fixed += 1
            continue
        if rate <= 0 or total <= 0:
            continue
        derived = round(total / rate, 3)
        if qty is not None and qty > 0 and abs(qty * rate - total) <= _ANNEXURE_QTY_TOLERANCE * total:
            continue
        it["quantity"] = derived
        fixed += 1
    return fixed


def _synth_annexure_code(ref: str, seq) -> str:
    """The item code an annexure row is persisted under. Annexure tables print
    no code, and two annexures both start at serial 1, so without one the
    breakdown's (schedule, code) identity collides across annexures. `seq` is
    the row's position within its annexure (the printed serial restarts per
    sub-table in Annexure-II), so the code is unique within the tender."""
    return f"ANX-{ref}-{seq}"

# Instruction / prose patterns that sometimes leak out of the schedule region
# during reconciliation re-sweeps (e.g. "All the bidders/tenderers should ensure
# …"). These are NEVER priced line items; reject them even if a stray qty/rate
# got mis-parsed onto the row. Match near the start of the description.
# Eligibility / compliance / undertaking prose that is never a priced line.
#
# This carries more weight than it used to. `_is_valid_schedule_item` now
# accepts a priced row inside a schedule even without a code or quantity, so
# this pattern is the guard standing between an eligibility clause with a
# mis-parsed number and a fake line item. It is deliberately anchored at the
# start of the description: a schedule item may well *mention* bidders or
# documents mid-sentence, but one that OPENS this way is prose.
_INSTRUCTION_PROSE_RE = re.compile(
    r"^\s*(?:"
    # "All the bidders…", "All bidders…", "Any tenderer…"
    r"(?:all|any|every|each)\s+(?:the\s+)?(?:bidders?|tenderers?|firms?|"
    r"applicants?|agenc(?:y|ies))\b|"
    r"(?:the\s+)?(?:bidders?|tenderers?|firms?|applicants?)\s+"
    r"(?:should|shall|must|will|are|is|has|have)\b|"
    r"(?:bidders?|tenderers?)\s+(?:should|shall|must)\s+ensure\b|"
    r"i\s*/\s*we\s+the\s+tenderer\b|"
    # Document / annexure checklists that follow the schedule section.
    r"documents?\s+(?:attached|enclosed|submitted|required|to\s+be)\b|"
    # Deliberately NOT matching "copy/copies of …": real schedules carry
    # deliverable lines like "Copies of approved drawings to be supplied
    # in triplicate". Dropping a priced row is invisible; an extra prose
    # row is not, so this guard is biased toward keeping.
    r"(?:please\s+)?(?:refer|see)\s+(?:to\s+)?(?:annexure|clause|para)\b|"
    # Bare instruction openers.
    r"note\s*:-?\s|"
    r"terms?\s+(?:and|&)\s+conditions?\b|"
    r"(?:general|special)\s+(?:instructions?|conditions?)\b"
    r"|"
    # Numbered eligibility / commercial clauses. These matter because a row
    # with a serial and a description and nothing else is now a line item
    # (see `_is_valid_schedule_item` (g)), and "1. Minimum annual turnover
    # of Rs 50 lakh" has a serial and a description. None of these opens a
    # material or work row.
    r"minimum\s+(?:annual\s+|average\s+)?(?:turnover|experience)\b|"
    r"(?:(?:similar|prior|past|relevant)\s+)?(?:work\s+)?experience\s+(?:of|in)\b|(?:work|past|similar|prior)\s+experience\b|"
    r"earnest\s+money|\bemd\b|security\s+deposit|performance\s+(?:guarantee|security)|"
    r"validity\s+of\b|payment\s+terms?\b|delivery\s+(?:period|schedule)\b|"
    r"liquidated\s+damages?\b|penalt(?:y|ies)\s+(?:for|clause|of|shall|will|as|@)\b|warranty\s+period|guarantee\s+period|"
    r"(?:the\s+)?(?:successful\s+)?tenderers?\s+(?:must|should|shall|will)\b|"
    r"rates?\s+(?:quoted|shall|should|must)\b|taxes?\s+(?:and|&)\s+duties\b|"
    r"(?:completion|contract)\s+period\b|arbitration\b|jurisdiction\b|force\s+majeure\b"
    r")",
    re.IGNORECASE,
)


def _is_instruction_prose(description: Optional[str]) -> bool:
    """True for eligibility/compliance/undertaking prose lines that are not
    priced schedule items (see ``_INSTRUCTION_PROSE_RE``)."""
    return bool(_INSTRUCTION_PROSE_RE.search(description or ""))

# Schedule header pattern. Matches "Schedule () A-Engine removal…",
# "Schedule () A7-Cost of spares…", "Schedule B — Provision of GST".
# Captures the FULL schedule code — a letter plus an OPTIONAL digit
# (A, A7, B, B7, C, C7, D, D7) — so the spares schedules (A7/B7/…) are
# preserved as distinct schedules rather than collapsed into A/B/….
_SCHEDULE_HEADER_RE = re.compile(r"[Ss]chedule\s*\(?\s*\)?\s*([A-Z]\d?)\b")

# Schedule banner (header line with a trailing "-Description") used to locate
# the printed schedule SUB-TOTAL. In IREPS NITs the schedule's total value is
# printed right after the multi-line banner and before the first item row, e.g.
#   Schedule () A-Engine removal … (Rates are inclusive of GST)
#   3604012.65
# We capture the code at the banner, then take the first rupee-style decimal in
# the window after it as the sub-total. Used only as a completeness *signal* —
# a mis-parse at worst triggers a bounded (idempotent) re-extraction pass.
_SCHEDULE_BANNER_RE = re.compile(r"[Ss]chedule\s*\(?\s*\)?\s*([A-Z]\d?)\s*-")
_RUPEE_DECIMAL_RE = re.compile(r"(\d[\d,]*\.\d{2})\b")

# Section headers that terminate the "Schedule of Items" in an IREPS NIT.
# Everything AFTER the first of these (once the schedule has begun) is
# eligibility / compliance / undertakings / annexures / document lists.
#
# Each alternative is ANCHORED at the start of a line (`^`, MULTILINE) and,
# where the NIT numbers it, requires that number. Two rules learned from
# tender 3822 (session 290):
#
#   1. "ITEM BREAKUP" is NOT a terminator. IREPS section `2. SCHEDULE` is a
#      summary whose every row reads "Please see Item Breakup for details.";
#      matching that phrase truncated the NIT at page 1 and threw away the
#      real line items. Section `3. ITEM BREAKUP` *starts* the region we want.
#   2. A bare unanchored keyword (the old `COMPLIANCE\b`) matches inside work
#      descriptions. Anchoring keeps prose from ending the schedule.
_SCHEDULE_END_RE = re.compile(
    r"^[ \t]*(?:\d+\.[ \t]*)?(?:"
    r"ELIGIBILITY\s+CONDITIONS?|"
    r"Special\s+(?:Financial|Technical)\s+Criteria|"
    r"Bidders\s+shall\s+confirm|"
    r"COMPLIANCE|"
    r"General\s+Instructions?|"
    r"Undertakings?|"
    r"Documents\s+attached\s+with\s+tender"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)


#: How far a schedule's summed line amounts may sit from its printed total
#: before the deterministic parse is declared incomplete. IREPS prints both
#: to the paisa, so this is rounding room (the same ±Rs 1 the reconciliation
#: gate allows), not tolerance for a missing row -- a Rs 6,820 row on a
#: Rs 1.05 crore schedule is 0.07%, and it was a row.
_DETERMINISTIC_TOTAL_TOLERANCE_INR = 1.0


def _deterministic_parse_gap(text: str, items: list[dict], sched_totals: list[dict]) -> Optional[str]:
    """Why a deterministic IREPS parse cannot be trusted whole, or None.

    Two independent checks. The NIT prints one "Description:-" line per row
    (the section-2 summary placeholders excluded), so fewer rows than that is
    rows lost. And a schedule whose summed amounts differ from its printed
    total by more than rounding is missing (or doubling) rows. Either is a
    layout the token reader did not expect; the caller then runs the AI
    path, which reconciles.
    """
    n_desc = sum(
        1 for ln in (text or "").splitlines()
        if ln.strip().startswith("Description:-") and not _SUMMARY_PLACEHOLDER_RE.search(ln)
    )
    if n_desc and len(items) < n_desc:
        return f"{len(items)} row(s) parsed but the text prints {n_desc} Description:- lines"
    sums: dict[str, float] = {}
    for it in items:
        code = (it.get("schedule_name") or "").upper()
        amt = it.get("amount")
        if code and amt is not None:
            try:
                sums[code] = sums.get(code, 0.0) + float(amt)
            except (TypeError, ValueError):
                pass
    for s in sched_totals:
        code = (s.get("schedule_code") or "").upper()
        stated = s.get("stated_total")
        if not code or not stated:
            continue
        got = sums.get(code, 0.0)
        delta = abs(float(stated) - got)
        if delta > _DETERMINISTIC_TOTAL_TOLERANCE_INR:
            return (f"schedule {code} sums to {got:,.2f} against a printed total of "
                    f"{float(stated):,.2f} (Rs {delta:,.2f} off)")
    return None


def build_boq_items_from_text(text: str) -> tuple[list[dict], list[dict]]:
    """Deterministic-first BOQ extraction for IREPS-format NITs. Returns
    (boq_item_dicts, schedule_total_dicts). Empty lists when the text is not
    the IREPS schedule format (caller then falls back to the AI/vision path).
    """
    if not is_ireps_schedule_format(text):
        return [], []
    parsed = parse_nit_text(text)
    items: list[dict] = []
    for s in parsed.schedules:
        for l in s.lines:
            items.append({
                "sr_no": l.sr_no,
                "item_code": l.item_code,
                "description": l.description,
                "quantity": l.qty,
                "unit": l.unit,
                "estimated_rate": l.unit_rate,      # tender rate, verbatim
                "basic_value": l.basic_value,
                "escalation_pct": l.escalation_pct,
                "amount": l.amount,
                "bidding_unit": l.bidding_unit,
                "schedule_name": l.schedule_code,
                "is_tax_line": False,
                "extraction_confidence": "high",
            })
    sched_totals = [
        {"schedule_code": s.code, "stated_total": s.stated_total,
         "advertised_value": parsed.advertised_value,
         "title": s.full_title or s.title}
        for s in parsed.schedules
    ]
    return items, sched_totals


async def parse_boq_from_tender(
    db: Session,
    tender_id: int,
    force: bool = False,
) -> list[BOQItem]:
    """
    Extract the tender's NIT bidding schedule (or fall-back BOQ) into BOQItem
    rows. After persisting, attempt to rebind any existing CostBreakdownLine
    rows back to the freshly-inserted BOQItem rows via (schedule_name,
    item_code) so prior costing survives a re-parse without orphaning.

    Args:
        db: Database session
        tender_id: Tender ID
        force: If True, re-parse even if BOQ items already exist

    Returns:
        List of persisted BOQItem records
    """
    # Idempotency — skip if rows already exist and force is False.
    if not force:
        existing = db.query(BOQItem).filter(BOQItem.tender_id == tender_id).all()
        if existing:
            logger.info(
                f"BOQ items already parsed for tender {tender_id}: {len(existing)} items"
            )
            return existing

    # Pick NIT-class source documents based on the per-doc Haiku pass's
    # `doc_type` field. Falls back to all attached PDFs when classification
    # is missing (e.g., very old tenders pre-v2 analyzer).
    docs = _pick_nit_source_docs(db, tender_id)
    if not docs:
        logger.warning(f"No source PDFs found for tender {tender_id}")
        return []

    min_rows_per_page = _get_int_setting(db, "costing.boq_min_rows_per_page", 4)
    force_ai = _get_bool_setting(db, "costing.boq_force_ai_extraction", True)

    # All file IO MUST go through storage_service so both the local and R2
    # (S3) backends work — `doc.file_path` is a storage KEY, not necessarily a
    # local filesystem path. We materialise each doc to a local temp file and
    # pass that path to every extraction helper (pdfplumber / advanced parser /
    # vision). Opening the raw key directly silently fails on R2, which left
    # BOQItem empty and forced costing into the freeform single-call path.
    from app.services.storage_service import get_storage_service
    storage = get_storage_service()

    parsed_items: list[dict] = []
    low_conf_all: set = set()
    recon_subtotals: dict = {}
    recon_over: dict = {}
    recon_short: set = set()
    schedule_totals_all: list[dict] = []

    # Skip byte-identical documents. Re-sending in the same Command Center
    # session uploads the files again, so a tender routinely ends up holding
    # two copies of the same NIT and TD. Parsing all of them roughly doubled a
    # nine-minute run for an identical result. Matched on content, not on
    # filename: the uploader prefixes a timestamp, so the names always differ.
    seen_digests: set[str] = set()
    total_docs = len(docs)

    for doc_no, doc in enumerate(docs, start=1):
        key = getattr(doc, "file_path", None)
        if not key:
            continue
        _progress(
            f"Reading tender document {doc_no} of {total_docs}", run_id="boq-docs"
        )
        try:
            with storage.as_local_file(key, suffix=".pdf") as local_path:
                digest = _file_digest(local_path)
                if digest and digest in seen_digests:
                    logger.info(
                        "[boq_parser] skipping duplicate document %s — identical "
                        "content already parsed in this pass",
                        getattr(doc, "id", "?"),
                    )
                    continue
                if digest:
                    seen_digests.add(digest)
                # Deterministic-first pass: try the zero-LLM IREPS schedule
                # parser (Task 1) on this doc's raw text BEFORE any AI/vision
                # work runs. When the doc matches the IREPS "2. SCHEDULE"
                # format, its rows are the authoritative transcription of the
                # tender's own numbers, so we use them as-is and skip the
                # AI+vision union entirely for this document — no fragile
                # 3-pass merge needed when the deterministic parse already
                # got every row exactly right.
                det_items: list[dict] = []
                det_sched_totals: list[dict] = []
                try:
                    det_text = _extract_text_pymupdf(local_path)
                    det_items, det_sched_totals = build_boq_items_from_text(det_text)
                except Exception as e:
                    logger.warning(
                        f"[boq_parser] tender {tender_id}: deterministic IREPS "
                        f"parse attempt failed for doc {getattr(doc, 'id', '?')} "
                        f"(falling back to AI/vision): {type(e).__name__}: {e}"
                    )
                    det_items, det_sched_totals = [], []

                if det_items:
                    # The deterministic parse wins outright, so it has to be
                    # whole. It is checked two ways before it is trusted: the
                    # NIT prints one "Description:-" per row (summary
                    # placeholders excluded), and each schedule prints its
                    # own total. A parse short of either is a layout variant
                    # the token reader did not expect (the Liluah NIT wrapped
                    # its Item Code onto two lines and ten of sixteen rows
                    # went missing, silently, with nothing to reconcile
                    # against). Then the AI/vision union runs instead, and
                    # reconciles against the printed sub-totals.
                    _gap = _deterministic_parse_gap(det_text, det_items, det_sched_totals)
                    if _gap:
                        logger.warning(
                            f"[boq_parser] tender {tender_id}: deterministic IREPS parse "
                            f"of doc {getattr(doc, 'id', '?')} is incomplete ({_gap}) -- "
                            f"falling back to the AI/vision extraction for this document"
                        )
                        det_items, det_sched_totals = [], []
                if det_items:
                    logger.info(
                        "[boq_parser] deterministic IREPS parse: %d rows, %d schedules",
                        len(det_items), len(det_sched_totals),
                    )
                    items = det_items
                    schedule_totals_all.extend(det_sched_totals)
                else:
                    # Not an IREPS-format doc (or parse yielded nothing) — fall
                    # back to the existing pdfplumber + AI/vision union path.
                    # pdfplumber first — fast supplement for clean IREPS NITs.
                    items = _extract_with_pdfplumber(local_path)
                    page_count = _pdf_page_count(local_path)

                    # The common failure mode: pdfplumber captures the simple
                    # service schedules (A1–A6, B1–B6, …) but misses the dense
                    # spares schedules (A7/B7/C7/D7 — the bulk of the line items),
                    # and a partial-but-plausible result clears the rows-per-page
                    # bar so the AI pass never runs. So by default we ALWAYS run the
                    # complete chunked AI extractor (pdfplumber is unioned in), then
                    # reconcile every schedule against its printed sub-total and
                    # re-extract any that come up short. The legacy heuristic is
                    # kept as an additional OR-trigger for when force_ai is disabled.
                    legacy_incomplete = (
                        not items
                        or not _has_nit_structure(items)
                        or (page_count > 0 and len(items) < page_count * min_rows_per_page)
                    )
                    items, low_conf, doc_subtotals, doc_over = await _extract_doc_complete(
                        db, local_path, items,
                        force_ai=force_ai, legacy_incomplete=legacy_incomplete,
                    )
                    low_conf_all |= low_conf
                    # accumulate reconcile inputs across docs for status derivation
                    recon_subtotals.update(doc_subtotals)
                    recon_over.update(doc_over)
                    recon_short |= low_conf
                # Final guard: drop any prose rows that slipped past the
                # schedule-section boundary trim (eligibility/undertaking text
                # with no item code and no qty/rate).
                _before = len(items)
                _rejected = [it for it in items if not _is_valid_schedule_item(it)]
                items = [it for it in items if _is_valid_schedule_item(it)]
                # A large drop is how a costing silently ends up with almost no
                # scope: the run still "succeeds" and produces a plausible
                # sheet. Log what went, and shout when most of the schedule
                # goes, so it is diagnosable from the run's own log.
                if _rejected and len(_rejected) > max(2, _before // 2):
                    logger.warning(
                        "[boq_parser] tender %s: dropped %d of %d row(s) as "
                        "non-schedule/prose — that is most of the schedule. "
                        "Samples: %s",
                        tender_id, len(_rejected), _before,
                        [str(r.get("description") or "")[:60] for r in _rejected[:3]],
                    )
                elif _rejected:
                    logger.info(
                        "[boq_parser] tender %s: dropped %d row(s): %s",
                        tender_id, len(_rejected),
                        [str(r.get("description") or "")[:40] for r in _rejected[:3]],
                    )
                if len(items) < _before:
                    logger.info(
                        f"[boq_parser] tender {tender_id}: dropped "
                        f"{_before - len(items)} non-schedule/prose row(s) "
                        f"(no item code, no qty/rate)"
                    )
        except FileNotFoundError:
            logger.warning(
                f"[boq_parser] tender {tender_id}: doc {getattr(doc, 'id', '?')} "
                f"not found in storage (key={key!r})"
            )
            continue
        except Exception as e:
            logger.warning(
                f"[boq_parser] tender {tender_id}: doc {getattr(doc, 'id', '?')} "
                f"extraction failed (key={key!r}): {type(e).__name__}: {e}"
            )
            continue
        _doc_name = getattr(doc, "file_name", None) or f"doc#{getattr(doc, 'id', '?')}"
        for it in items:
            it["_source_doc_id"] = getattr(doc, "id", None)
        if items:
            _unquantified = sum(1 for it in items if not it.get("quantity"))
            _by_anx: dict[str, int] = {}
            for it in items:
                if it.get("annexure_ref") and not (it.get("schedule_name") or "").strip():
                    _by_anx[it["annexure_ref"]] = _by_anx.get(it["annexure_ref"], 0) + 1
            logger.info(
                f"[boq_parser] tender {tender_id}: {_doc_name!r} -> {len(items)} row(s)"
                + (f", {_unquantified} with no quantity (rate-only lines)" if _unquantified else "")
                + (f"; annexure tables: {_by_anx}" if _by_anx else "")
            )
        else:
            logger.warning(
                f"[boq_parser] tender {tender_id}: {_doc_name!r} -> 0 rows -- this "
                f"document contributes nothing to the schedule"
            )
        parsed_items.extend(items)

    if not parsed_items:
        logger.info(f"No BOQ items found in tender {tender_id} documents")
        return []

    # Safety net: collapse cross-document duplicates (e.g. a corrigendum that
    # re-prints an earlier doc's schedule) using the same (schedule, sr_no,
    # item_code) key the AI branch's merge/dedup path uses, first-doc-wins.
    parsed_items = _dedup_boq_rows(parsed_items)
    # Drop IREPS section-2 summary placeholders at the universal choke point.
    # Every extraction path converges here, so this is the one place that
    # guarantees a "Please see Item Breakup for details." row never reaches
    # costing as a phantom line item (session 290 / tender 3822).
    parsed_items, _placeholders_dropped = _drop_summary_placeholder_rows(parsed_items)
    if _placeholders_dropped:
        logger.info(
            f"[boq_parser] tender {tender_id}: dropped "
            f"{_placeholders_dropped} section-2 summary placeholder row(s)"
        )
    # Value-redundancy twin collapse at the universal choke point — every
    # extraction path (deterministic IREPS, pdfplumber+AI/vision union, and
    # the early-return branches inside _extract_doc_complete) converges here
    # before persistence. The per-doc collapse inside _extract_doc_complete is
    # bypassed on the deterministic path (which produced the original A{n}+{n}
    # duplication, e.g. tender 3750), so this cross-doc pass is authoritative.
    # NOT reconciliation-gated: empty-shell twins leave the schedule value-sum
    # correct so subtotal reconciliation never flags them.
    parsed_items, _twin_dropped, _twin_unresolvable = _collapse_value_redundant_twins(parsed_items)
    if _twin_dropped:
        logger.info(
            f"[boq_parser] tender {tender_id}: twin-collapse dropped "
            f"{_twin_dropped} value-redundant twin row(s) before persistence"
        )
    if _twin_unresolvable:
        logger.warning(
            f"[boq_parser] tender {tender_id}: twin-collapse left "
            f"{len(_twin_unresolvable)} (schedule, sr_no) group(s) with distinct "
            f"non-zero values untouched: "
            f"{[(g['schedule'], g['sr_no']) for g in _twin_unresolvable]}"
        )

    # An annexure row is persisted under a synthetic code (ANX-<ref>-<sr>) so
    # its (schedule, code) identity is unique across annexures, and bound to
    # the schedule item whose description cites its annexure. The binding is
    # what stops the annexure being costed on top of the item it breaks down.
    # The code carries the row's SEQUENCE in its annexure, not the printed
    # serial: Annexure-II is thirty sub-tables and every one restarts at 1,
    # so ANX-II-1 would name thirty different rows.
    parsed_items = _drop_separately_scheduled_labour(parsed_items)
    _fixed_qty = _reconcile_annexure_quantities(parsed_items)
    parsed_items = _fold_merged_assembly_parts(parsed_items)
    _assign_annexure_codes(parsed_items)
    if _fixed_qty:
        logger.warning(
            f"[boq_parser] tender {tender_id}: {_fixed_qty} annexure row(s) had a "
            f"quantity that contradicted the printed rate x total -- quantity "
            f"re-derived as total / rate"
        )
    annexure_report = _link_annexure_components(parsed_items)
    if annexure_report["cited"] or annexure_report["captured"]:
        logger.info(
            f"[boq_parser] tender {tender_id}: annexures cited by the schedule: "
            f"{annexure_report['cited'] or '[]'}; captured (rows): "
            f"{annexure_report['captured'] or '{}'}; linked: "
            f"{[(l['annexure'], l['parent'], l['rows']) for l in annexure_report['linked']] or '[]'}"
        )
        if annexure_report["missing"]:
            logger.warning(
                f"[boq_parser] tender {tender_id}: the schedule cites annexure(s) "
                f"{annexure_report['missing']} and no uploaded document carries "
                f"them -- the items citing them cost without their breakdown"
            )
        if annexure_report["unlinked"]:
            logger.warning(
                f"[boq_parser] tender {tender_id}: annexure(s) "
                f"{annexure_report['unlinked']} were captured but no schedule item "
                f"cites them -- their rows are costed as standalone scope"
            )

    # Snapshot existing CostBreakdownLine → boq_item_id bindings before we
    # delete the BOQItem rows, so we can rebind after the re-parse.
    pre_rebind = _snapshot_cost_line_bindings(db, tender_id)

    # Clear old items and persist new ones.
    db.query(BOQItem).filter(BOQItem.tender_id == tender_id).delete()
    db.flush()

    boq_records: list[BOQItem] = []
    for i, item in enumerate(parsed_items):
        boq = BOQItem(
            tender_id=tender_id,
            sr_no=item.get("sr_no") or (i + 1),
            description=(item.get("description") or "").strip(),
            quantity=item.get("quantity"),
            unit=item.get("unit"),
            estimated_rate=(
                item["estimated_rate"] if item.get("estimated_rate") is not None
                else item.get("unit_rate")
            ),
            # NIT structural fields. All nullable on the model so missing
            # values stay None rather than collapsing to defaults.
            item_code=_str_or_none(item.get("item_code")),
            schedule_name=_str_or_none(item.get("schedule_name")),
            bidding_unit=_str_or_none(item.get("bidding_unit")),
            basic_value=item.get("basic_value"),
            escalation_pct=item.get("escalation_pct"),
            is_tax_line=bool(item.get("is_tax_line")) or _detect_tax_line(item.get("description")),
            extraction_confidence=item.get("extraction_confidence"),
            annexure_ref=_str_or_none(item.get("annexure_ref")),
            source_document_id=item.get("_source_doc_id"),
        )
        db.add(boq)
        boq_records.append(boq)

    db.commit()
    for boq in boq_records:
        db.refresh(boq)

    # Resolve the component links now that the parents have ids. The link is
    # by position (see `_link_annexure_components`), and `boq_records` is
    # parallel to `parsed_items`.
    linked_rows = 0
    for item, boq in zip(parsed_items, boq_records):
        pidx = item.get("_parent_idx")
        if pidx is not None and 0 <= pidx < len(boq_records):
            boq.parent_item_id = boq_records[pidx].id
            linked_rows += 1
    if linked_rows:
        db.commit()

    # Persist the deterministic pass's printed schedule sub-totals (dedup on
    # (tender_id, schedule_code) — later docs never duplicate an earlier
    # doc's schedule row). These are the immutable benchmark the
    # reconciliation gate checks BOQItem line sums against.
    if schedule_totals_all:
        statuses = _derive_schedule_statuses(recon_subtotals, recon_short, recon_over)
        _persist_schedule_totals(db, tender_id, schedule_totals_all, statuses)

    # Rebind cost lines to their fresh BOQItem rows so the user's prior
    # rate edits don't lose their schedule anchor.
    _rebind_cost_lines(db, tender_id, boq_records, pre_rebind)

    # Per-tender completeness summary (A5): how many schedules captured, and
    # which (if any) stayed short of their printed sub-total after all passes.
    sched_counts: dict[str, int] = {}
    for b in boq_records:
        sched_counts[b.schedule_name or "?"] = sched_counts.get(b.schedule_name or "?", 0) + 1
    n_schedules = len(sched_counts)
    if low_conf_all:
        logger.warning(
            f"[boq_parser] tender {tender_id}: extracted {len(boq_records)} row(s) "
            f"across {n_schedules} schedule(s); {len(low_conf_all)} flagged "
            f"LOW confidence (could not reconcile to printed sub-total): "
            f"{sorted(low_conf_all)}. by_schedule={sched_counts}"
        )
    else:
        logger.info(
            f"[boq_parser] tender {tender_id}: extracted {len(boq_records)} row(s) "
            f"across {n_schedules} schedule(s); all reconciled. "
            f"by_schedule={sched_counts}"
        )
    return boq_records


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Both timestamps compared below default to `datetime.now(timezone.utc)`
    and come back naive from SQLite and aware from Postgres. Naive here means
    UTC; make that explicit so the comparison never mixes the two."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _latest_schedule_upload(db: Session, tender_id: int) -> Optional[datetime]:
    """When the newest document that could carry priced rows arrived.

    Two records, because a re-upload leaves only one of them. A file uploaded
    under a NEW name becomes a `TenderDocument`, and only the schedule-bearing
    ones count -- a drawing set arriving late must not re-capture anything. A
    file uploaded again under its EXISTING name creates no `TenderDocument`
    at all (the Command Center dual-write de-duplicates by file name), so the
    `ChatAttachment` on a session bound to this tender is the only trace that
    anything arrived. That second case is the ordinary one: a user told to
    re-upload does exactly that, with the same files.
    """
    from app.models.chat_attachment import ChatAttachment
    from app.models.proposal import ProposalSession

    latest: Optional[datetime] = None
    for d in _pick_nit_source_docs(db, tender_id):
        t = _as_utc(getattr(d, "uploaded_at", None))
        if t and (latest is None or t > latest):
            latest = t

    row = (
        db.query(ChatAttachment.created_at)
        .join(ProposalSession, ProposalSession.id == ChatAttachment.session_id)
        .filter(
            ProposalSession.tender_id == tender_id,
            (ChatAttachment.file_type == "application/pdf")
            | ChatAttachment.file_name.ilike("%.pdf"),
        )
        .order_by(ChatAttachment.created_at.desc())
        .first()
    )
    if row and row[0]:
        t = _as_utc(row[0])
        if latest is None or t > latest:
            latest = t
    return latest


def _schedule_predates_latest_upload(
    db: Session, tender_id: int, existing_rows: list
) -> Optional[tuple[datetime, datetime]]:
    """(captured_at, uploaded_at) when a document arrived AFTER the schedule
    was captured -- the schedule is then derived from a document set that no
    longer exists and must be re-captured. None when it is current, or when
    it cannot be dated: a row with no `created_at` is treated as current
    rather than stale, because "unknown" must not become "re-extract on every
    run".

    This is the precise trigger the rows-per-page ratio never was. It fires
    once per change to the document set and then goes quiet, because a
    successful re-capture writes rows newer than every upload. It repeats
    only if the re-capture yields nothing and the old rows are kept -- and
    then only when the user next asks for a costing, not in the background.
    """
    stamps = [r.created_at for r in existing_rows]
    if not stamps or any(t is None for t in stamps):
        return None
    captured_at = max(_as_utc(t) for t in stamps)
    latest = _latest_schedule_upload(db, tender_id)
    if latest is not None and latest > captured_at:
        return captured_at, latest
    return None


#: A re-capture cannot outlive the run that started it, so the lock's TTL is
#: the run's own hard cap: a worker killed mid-parse frees the tender in at
#: most that long, and nothing needs to remember to clean up.
_RECAPTURE_LOCK_TTL_SECONDS = 60 * 30
#: How long a second run waits for a re-capture already in flight before it
#: gives up and costs from whatever rows exist. A parse is usually a minute or
#: two; three is generous without holding a worker slot for a whole job.
_RECAPTURE_WAIT_SECONDS = 180
_RECAPTURE_POLL_SECONDS = 3


def _recapture_lock_key(tender_id: int) -> str:
    return f"drpl:boq:{tender_id}:recapture"


def _acquire_recapture_lock(tender_id: int) -> Optional[bool]:
    """True when this run holds the lock, False when another run does, None
    when there is no Redis to ask -- in which case the caller proceeds
    unlocked, which is exactly the behaviour before the lock existed.

    Same primitive as the cancel flag (`run_service.request_cancel`): a
    `SET NX` with a TTL, because the two runs racing here are in different
    worker processes and a process-local lock would guard nothing.
    """
    try:
        from app.core.redis_client import get_redis
        client = get_redis()
    except Exception:
        return None
    if client is None:
        return None
    try:
        return bool(client.set(
            _recapture_lock_key(tender_id), "1",
            nx=True, ex=_RECAPTURE_LOCK_TTL_SECONDS,
        ))
    except Exception as e:
        logger.debug(f"[costing] tender {tender_id}: lock acquire failed: {e}")
        return None


def _release_recapture_lock(tender_id: int) -> None:
    try:
        from app.core.redis_client import get_redis
        client = get_redis()
        if client is not None:
            client.delete(_recapture_lock_key(tender_id))
    except Exception as e:
        logger.debug(f"[costing] tender {tender_id}: lock release failed: {e}")


# ---------------------------------------------------------------------------
# "A costing is running on this tender" flag
# ---------------------------------------------------------------------------
#
# The analyzer re-captures the schedule with force=True on every run, which
# deletes and re-inserts every BOQItem row under new ids. A costing running
# at the same time (skeleton built, batches in flight) then merges its rates
# against ids that no longer exist. The costing raises this flag for its
# budget; the analyzer, holding the same re-capture lock the costing takes,
# sees it and leaves the schedule alone for that run. Counted, not boolean,
# because a user can run two costings on one tender. No Redis: no flag, and
# the analyzer proceeds as before.

_COSTING_FLAG_TTL_SECONDS = 60 * 35


def _costing_flag_key(tender_id: int) -> str:
    return f"drpl:boq:{tender_id}:costing"


def mark_costing_running(tender_id: int, ttl_seconds: int = _COSTING_FLAG_TTL_SECONDS) -> bool:
    """Raise the flag for this costing. True when it was raised (and must be
    cleared with `clear_costing_running`), False when there is no Redis."""
    try:
        from app.core.redis_client import get_redis
        client = get_redis()
    except Exception:
        return False
    if client is None:
        return False
    try:
        key = _costing_flag_key(tender_id)
        client.incr(key)
        client.expire(key, max(60, int(ttl_seconds)))
        return True
    except Exception as e:
        logger.debug(f"[costing] tender {tender_id}: costing flag set failed: {e}")
        return False


def clear_costing_running(tender_id: int) -> None:
    try:
        from app.core.redis_client import get_redis
        client = get_redis()
        if client is None:
            return
        key = _costing_flag_key(tender_id)
        left = client.decr(key)
        if left is None or int(left) <= 0:
            client.delete(key)
    except Exception as e:
        logger.debug(f"[costing] tender {tender_id}: costing flag clear failed: {e}")


def costing_is_running(tender_id: int) -> bool:
    """True when at least one costing has the flag up. False without Redis."""
    try:
        from app.core.redis_client import get_redis
        client = get_redis()
        if client is None:
            return False
        v = client.get(_costing_flag_key(tender_id))
        if v is None:
            return False
        try:
            return int(v) > 0
        except (TypeError, ValueError):
            return True
    except Exception:
        return False


async def recapture_schedule_for_analysis(db: Session, tender_id: int) -> list:
    """The analyzer's schedule capture: `parse_boq_from_tender(force=True)`
    under the same one-per-tender lock `ensure_boq_parsed` takes, skipped
    while a costing is running on the tender.

    force=True is the repair path (a schedule captured under an older rule
    gets re-read on the next analysis), and it stays. What it must not do is
    replace every BOQItem id while a costing is merging against them, or
    race a costing's own re-capture -- both were possible because it took no
    lock. While a costing runs, the schedule it costs from is left alone and
    the capture that is skipped is logged; a schedule that does not exist yet
    is still captured (force=False is a no-op only when rows exist).
    """
    if costing_is_running(tender_id):
        logger.warning(
            f"[boq_parser] tender {tender_id}: a costing is running on this "
            f"tender -- the analysis will not re-capture its schedule now "
            f"(re-run the analysis after the costing to refresh it)"
        )
        return await parse_boq_from_tender(db, tender_id, force=False)
    acquired = _acquire_recapture_lock(tender_id)
    if acquired is False:
        logger.info(
            f"[boq_parser] tender {tender_id}: another run is re-capturing the "
            f"schedule -- waiting for it instead of capturing twice"
        )
        await _wait_for_recapture(tender_id)
        return db.query(BOQItem).filter(BOQItem.tender_id == tender_id).all()
    try:
        return await parse_boq_from_tender(db, tender_id, force=True)
    finally:
        if acquired:
            _release_recapture_lock(tender_id)


async def _wait_for_recapture(tender_id: int) -> bool:
    """Poll until the other run's lock is gone or the wait budget is spent.
    True when the lock cleared (the schedule is now fresh), False on timeout."""
    try:
        from app.core.redis_client import get_redis
        client = get_redis()
    except Exception:
        return False
    if client is None:
        return False
    key = _recapture_lock_key(tender_id)
    waited = 0.0
    while waited < _RECAPTURE_WAIT_SECONDS:
        try:
            if not client.exists(key):
                return True
        except Exception:
            return False
        await asyncio.sleep(_RECAPTURE_POLL_SECONDS)
        waited += _RECAPTURE_POLL_SECONDS
    return False


async def ensure_boq_parsed(db: Session, tender_id: Optional[int]) -> int:
    """Idempotently ensure the tender's NIT bidding schedule is captured.

    The single self-heal used by EVERY costing entry point so costing always
    gets a 1:1 NIT schedule (and the deterministic batched/skeleton path)
    instead of a freeform single-call that summarises large NITs. Gated by the
    `costing_auto_parse_boq` setting. Re-extracts only when the captured
    schedule is missing, looks incomplete (no rows / thin rows with no item
    codes), or is STALE -- a document that could carry priced rows arrived
    after it was captured (`_schedule_predates_latest_upload`). Otherwise a
    cheap no-op, so a complete, current schedule is never re-extracted. One
    re-capture per tender at a time (a Redis `SET NX` lock; unlocked when
    there is no Redis). Non-fatal: never raises; returns the
    current BOQItem count (best effort).
    """
    if not tender_id:
        return 0
    from app.services.settings_service import get_setting_value

    def _count() -> int:
        try:
            return db.query(BOQItem).filter(BOQItem.tender_id == tender_id).count()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            return 0

    try:
        if not get_setting_value(db, "costing_auto_parse_boq", True):
            return _count()
    except Exception:
        return _count()

    try:
        existing_rows = db.query(BOQItem).filter(BOQItem.tender_id == tender_id).all()
        existing_count = len(existing_rows)
        thin = existing_count > 0 and all(not r.item_code for r in existing_rows)
        # A document that arrived after the capture makes the capture stale.
        # This is what a re-upload looks like from here (the Liluah tender:
        # NIT plus three annexures re-uploaded into the same session, the
        # schedule still the NIT-only one captured before the annexures were
        # read, and costing served it again). Best-effort: a failure to date
        # things means "current", never "re-extract".
        stale: Optional[tuple[datetime, datetime]] = None
        if existing_count > 0:
            try:
                stale = _schedule_predates_latest_upload(db, tender_id, existing_rows)
            except Exception as e:
                logger.debug(f"[costing] tender {tender_id}: staleness check failed: {e}")
                stale = None
        # Page-count ratio test (only needed when some rows already exist).
        # NOTE — this ratio test is inert on remote storage, and that is load
        # bearing. `_d.file_path` is a storage KEY, not a filesystem path (see
        # `parse_boq_from_tender`, which materialises every doc through
        # `storage_service` for exactly this reason). `_pdf_page_count` opens
        # it directly, so on R2 it raises, returns 0, and `page_count` stays 0
        # — leaving `existing_count == 0` and `thin` as the only re-extraction
        # triggers in production. This is the one call site in the codebase
        # that passes a raw key to a page counter.
        #
        # Before "fixing" it: making it work turns this into a full chunked AI
        # re-extraction on EVERY costing run for any tender that never clears
        # four rows per page, because nothing here records that an attempt was
        # already made. `parse_boq_from_tender` also returns early WITHOUT
        # replacing the old rows when a parse yields nothing, so a tender whose
        # files have gone missing from storage would retry forever. Both need
        # answering first, and materialising every PDF from R2 on the hot path
        # of every costing run is its own cost. The trigger that IS safe is
        # the one above it: a document arriving after the capture, which fires
        # once per change and then goes quiet. A schedule captured under an
        # older rule with no new upload is re-captured by re-running the
        # tender analysis (`POST /tenders/{id}/analyze` ->
        # `parse_boq_from_tender(force=True)`).
        page_count = 0
        if existing_count > 0 and not thin:
            try:
                for _d in _pick_nit_source_docs(db, tender_id):
                    page_count += _pdf_page_count(_d.file_path)
            except Exception:
                page_count = 0
        min_rows_per_page = _get_int_setting(db, "costing.boq_min_rows_per_page", 4)
        incomplete = (
            existing_count == 0
            or thin
            or stale is not None
            or (page_count > 0 and existing_count < page_count * min_rows_per_page)
        )
        if not incomplete:
            return existing_count
        if stale is not None:
            logger.warning(
                f"[costing] tender {tender_id}: schedule of {existing_count} row(s) "
                f"captured at {stale[0].isoformat()} predates a document uploaded "
                f"at {stale[1].isoformat()} -- re-capturing from the current documents"
            )

        def _emit(phase: str, rows: Optional[int] = None) -> None:
            try:
                from app.services.ai_service import _emit_reliability_event
                payload = {"phase": phase, "tender_id": tender_id}
                if rows is not None:
                    payload["rows"] = rows
                _emit_reliability_event(None, "costing_boq_extract", payload)
            except Exception:
                pass

        # One re-capture per tender at a time. Two costings on the same tender
        # in the same window (twelve worker slots, and a user can run more than
        # one) would otherwise both delete-and-reinsert the schedule -- a full
        # AI parse spent twice and, in one interleaving, duplicate rows. The
        # loser waits for the winner and costs from the schedule it wrote.
        acquired = _acquire_recapture_lock(tender_id)
        if acquired is False:
            logger.info(
                f"[costing] tender {tender_id}: another run is re-capturing the "
                f"schedule -- waiting for it"
            )
            cleared = await _wait_for_recapture(tender_id)
            fresh = _count()
            logger.info(
                f"[costing] tender {tender_id}: re-capture by another run "
                f"{'finished' if cleared else 'still running after the wait'} -- "
                f"using its {fresh} row(s)"
            )
            return fresh

        try:
            _emit("running")
            # force a fresh full extraction when partial rows exist (replace the
            # incomplete schedule); a no-op force for 0 rows.
            parsed = await parse_boq_from_tender(db, tender_id, force=(existing_count > 0))
            n = len(parsed or [])
            logger.info(
                f"[costing] tender {tender_id}: ensured NIT schedule → {n} BOQItem "
                f"row(s) (was {existing_count}, pages={page_count})"
            )
            _emit("complete", rows=n)
            return n
        finally:
            if acquired:
                _release_recapture_lock(tender_id)
    except Exception as e:
        logger.warning(f"[costing] ensure_boq_parsed failed (non-fatal): {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return _count()


# ---------------------------------------------------------------------------
# Source document selection
# ---------------------------------------------------------------------------


def _pick_nit_source_docs(db: Session, tender_id: int) -> list[TenderDocument]:
    """Pick the PDF(s) that may carry priced schedule rows.

    Exclusion here is by positive evidence only. The per-doc Haiku pass tags
    every document from a fixed vocabulary (see `document_analysis_agent`),
    and two of those tags — `drawing`, `terms_conditions` — mean the document
    holds no priced line items. Everything else is read: the NIT, a schedule
    of rates, an addendum that revises prices, an ANNEXURE carrying the price
    bid, and any document the pass has not classified yet.

    It used to work the other way round — an allowlist of ("BOQ",
    "schedule_of_rates", "RFP"), scanned in priority order, taking the FIRST
    non-empty bucket and discarding every other document. That lost scope
    three ways, all of them silent (issue #7, the Liluah tender: Annexure 2
    held 30 priced items and 6 were costed):

      - `annexure` is its own tag and belonged to no bucket, so a price bid
        printed as an annexure could not be read at all;
      - first-bucket-wins meant a tender holding both a BOQ and a schedule of
        rates costed only the BOQ — and an annexure mis-tagged `BOQ` won the
        bucket and evicted the NIT itself;
      - a document with no classification row yet belongs to no bucket
        either, so every chat upload was dropped the moment one sibling had
        been classified. A user uploading four files and asking for a costing
        in the same breath is the ordinary case, not the edge case.

    Parsing a document that turns out to hold nothing costs a parse. Skipping
    one that holds thirty items costs the bid, and nothing about the run
    looks wrong afterwards. The tie goes to reading it.
    """
    all_pdfs = (
        db.query(TenderDocument)
        .filter(
            TenderDocument.tender_id == tender_id,
            TenderDocument.mime_type == "application/pdf",
        )
        .order_by(TenderDocument.id.asc())
        .all()
    )
    if not all_pdfs:
        return []

    # A document can carry MORE than one extraction row — nothing deletes the
    # previous ones, so a re-analysis simply adds another. Collect every tag a
    # document has rather than letting an arbitrary last-one-wins decide, and
    # skip it only when they ALL say it holds no priced lines. One tag saying
    # "schedule" is enough to read the file; that is the same
    # exclude-only-on-positive-evidence rule applied to a disagreeing pass.
    doc_types_by_id: dict[int, set] = {}
    for ext in (
        db.query(DocumentExtractionResult)
        .filter(DocumentExtractionResult.tender_id == tender_id)
        .all()
    ):
        summary = ext.summary_json or {}
        if not isinstance(summary, dict):
            continue
        doc_type = (summary.get("doc_type") or "").strip().lower()
        if doc_type and ext.document_id is not None:
            doc_types_by_id.setdefault(ext.document_id, set()).add(doc_type)

    if not doc_types_by_id:
        logger.info(
            f"[boq_parser] tender {tender_id}: no per-doc classification — "
            f"reading all {len(all_pdfs)} PDF(s)"
        )
        return all_pdfs

    kept, skipped = [], []
    for d in all_pdfs:
        tags = doc_types_by_id.get(d.id) or set()
        # `tags <= _NON_SCHEDULE_DOC_TYPES` — every tag this document has says
        # it holds no priced lines. An empty set is a subset of anything, so
        # the emptiness check comes first: no classification means read it.
        if tags and tags <= _NON_SCHEDULE_DOC_TYPES:
            skipped.append((d.file_name, sorted(tags)))
        else:
            kept.append(d)

    if not kept:
        # Every document was tagged as carrying no schedule. A classification
        # that leaves nothing to cost is far more likely to be wrong than a
        # tender is to have no priced scope, and costing nothing is not a safe
        # way to be wrong.
        logger.warning(
            f"[boq_parser] tender {tender_id}: classification excluded all "
            f"{len(all_pdfs)} PDF(s) — ignoring it and reading them all"
        )
        return all_pdfs

    if skipped:
        logger.info(
            f"[boq_parser] tender {tender_id}: reading {len(kept)} of "
            f"{len(all_pdfs)} PDF(s); skipped as non-schedule: {skipped}"
        )
    return kept


def _file_digest(path: str) -> Optional[str]:
    """Content hash of a file, or None if it cannot be read.

    Used to skip byte-identical duplicate uploads. Returns None rather than
    raising: a hashing failure should cost us the de-duplication, not the parse.
    """
    import hashlib

    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except Exception:
        return None


def _progress(message: str, *, run_id: str = "boq", done: bool = False) -> None:
    """Tell the user what this stage is doing.

    BOQ parsing is the longest silent stretch of a costing run — on a 149-page
    tender it ran for roughly seven minutes emitting nothing a user could see,
    so the UI showed "DRPL is working" and gave no way to tell a working
    platform from a hung one. `_emit_reliability_event` picks the streaming
    callback up from a ContextVar, so these reach the SSE pipe without having
    to be threaded through every call site.
    """
    try:
        from app.services.ai_service import _emit_reliability_event

        payload = {
            "phase": "tool_done" if done else "tool_running",
            "run_id": run_id,
            "agent_key": "boq_parser",
        }
        if not done:
            payload["message"] = message
        _emit_reliability_event(None, "agent_status", payload)
    except Exception:  # progress must never break a parse
        pass


def _has_nit_structure(items: list[dict]) -> bool:
    """True if at least one row has any NIT-specific structural field set —
    item_code, basic_value, escalation_pct, or bidding_unit. Used to decide
    whether the pdfplumber result is rich enough or we should run the AI
    fallback to enrich.
    """
    for item in items:
        if (
            item.get("item_code")
            or item.get("basic_value") is not None
            or item.get("escalation_pct") is not None
            or item.get("bidding_unit")
            or item.get("schedule_name")
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# pdfplumber path — text + tables, schedule-aware
# ---------------------------------------------------------------------------


def _extract_with_pdfplumber(file_path: str) -> list[dict]:
    """Extract NIT schedule rows from a PDF using pdfplumber.

    Walks each page in document order: scans page text for schedule headers
    ("Schedule () A-Stripping…"), then extracts tables and tags each parsed
    row with the most recent schedule code seen.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed, skipping table extraction")
        return []

    items: list[dict] = []
    current_schedule: Optional[str] = None
    current_annexure: Optional[str] = None
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                # Pick up any schedule headers on this page so subsequent
                # rows on the same page get tagged correctly.
                page_text = page.extract_text() or ""
                for match in _SCHEDULE_HEADER_RE.finditer(page_text):
                    current_schedule = match.group(1).upper()
                # Likewise the annexure heading printed above a material list.
                for match in _ANNEXURE_HEADING_RE.finditer(page_text):
                    current_annexure = normalize_annexure_ref(match.group(1)) or current_annexure

                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    header = table[0]
                    if not _is_boq_header(header):
                        continue
                    col_map = _map_boq_columns(header)
                    if "description" not in col_map:
                        continue
                    for row in table[1:]:
                        if not row or all(
                            cell is None or str(cell).strip() == "" for cell in row
                        ):
                            continue
                        item = _parse_boq_row(row, col_map)
                        if not item or not item.get("description"):
                            continue
                        if current_schedule and not item.get("schedule_name"):
                            item["schedule_name"] = current_schedule
                        if current_annexure and not item.get("schedule_name"):
                            item["annexure_ref"] = current_annexure
                        # Confidence is "medium" — pdfplumber found a clean
                        # tabular layout but we can't independently verify
                        # the cells map to the right columns.
                        item.setdefault("extraction_confidence", "medium")
                        items.append(item)
    except Exception as e:
        logger.warning(f"pdfplumber extraction failed for {file_path}: {e}")

    return items


def _is_valid_schedule_item(item: dict, *, table_context: bool = True) -> bool:
    """True only for genuine priced schedule rows. Rejects prose rows
    (eligibility clauses, undertakings, general instructions, annexure/document
    lists) that carry a description but no item code and no qty/rate. Tax rows
    (GST provision) are kept.

    Acceptance signals:
      (a) an alphanumeric schedule item code (e.g. A1, A71, B7138, C786), OR
      (b) a WORD-style item code (e.g. "Mechanical", "ELEC FUR") that NITs use
          in the Item Code column — accepted as long as the row also carries
          some pricing signal (qty OR rate OR basic_value), OR
      (c) BOTH a positive quantity AND a positive rate/value (rows whose code
          failed to parse but are clearly priced), OR
      (d) a positive value on a row tagged with a schedule (lump-sum lines
          whose code sits inside the description), OR
      (e) a positive quantity AND a unit of measure — the price-bid shape,
          where the tender prints the scope and quantity and the BIDDER
          supplies the rate, so there is no printed number to find at all, OR
      (f) a printed serial number with a unit but no quantity, OR
      (g) a printed serial number with a cell-like description and nothing
          else — a materials list with no quantity column. The rate is
          researched per item; the amount is left to the user.

    (f) and (g) are taken only with `table_context`: the caller has seen a
    table header in the text the row came from and no IREPS schedule banner.
    An IREPS schedule prints a code and a quantity on every row, so a
    serial-only row inside one is a fragment, not an item; and the NIT's own
    numbered clauses, past the schedule cut, have serials too. A caller with
    no text to judge (the pdfplumber path reads rows out of a detected table)
    leaves it True.

    Instruction/eligibility prose ("All the bidders should ensure…") is ALWAYS
    rejected, even when a stray qty/rate mis-parsed onto the row.
    """
    # Instruction prose is never a priced line — check FIRST, before the tax-line
    # acceptance. Eligibility text like "All the bidders should ensure they are
    # GST compliant…" trips the tax-line keyword detector but is NOT a tax row.
    if _is_instruction_prose(item.get("description")):
        return False

    if item.get("is_tax_line") or _detect_tax_line(item.get("description")):
        return True

    def _pos(v) -> bool:
        try:
            return v is not None and float(v) > 0
        except (TypeError, ValueError):
            return False

    qty_ok = _pos(item.get("quantity"))
    rate_ok = (
        _pos(item.get("unit_rate"))
        or _pos(item.get("estimated_rate"))
        or _pos(item.get("basic_value"))
        or _pos(item.get("amount"))
    )
    if (table_context and rate_ok and re.fullmatch(
            r"labou?r\s+(?:cost|charges?)(?:\s*\([A-Z]\))?",
            (item.get("description") or "").strip(), re.I)):
        return True

    raw_code = (item.get("item_code") or "").strip()
    code_norm = re.sub(r"[^A-Za-z0-9]", "", raw_code).upper()

    # (a) Alphanumeric schedule code (letter(s) + digits) — strong signal on its own.
    if re.match(r"^[A-Za-z]{1,2}\d+", code_norm):
        return True

    # (b) Word-style item code ("Mechanical", "ELEC FUR"): accept when the row
    # also has any pricing signal. Guards against prose (already rejected above)
    # and bare codes with no numbers at all.
    if raw_code and (qty_ok or rate_ok):
        return True

    # (c) No usable code, but clearly a priced row.
    if qty_ok and rate_ok:
        return True

    # (d) A priced row inside a schedule, with neither a parsed code nor a
    # quantity.
    #
    # This branch exists because (a)-(c) threw away real work. On a Rs 12.9
    # crore IREPS tender the extractor produced rows that reconciled to the
    # printed schedule total EXACTLY (delta 0.0%), and then 17 of the surviving
    # 18 were dropped here: they carried a value and a schedule, but the item
    # code sat inside the description rather than its own column and the line
    # was lump-sum, so it had no quantity. The costing then ran on a single row
    # and looked finished.
    #
    # A positive value is the strongest pricing signal there is, and a row
    # tagged with a schedule came from inside the schedule section. Prose is
    # still rejected above, which is the guard that matters — that check runs
    # first precisely so a mis-parsed number on an eligibility clause cannot
    # reach here.
    schedule = (item.get("schedule_name") or "").strip()
    if rate_ok and schedule:
        return True

    # (e) A QUANTIFIED row whose rate the bidder is the one to supply.
    #
    # In a price bid / schedule of quantities the tender prints the item, its
    # quantity and its unit, and leaves Rate and Amount EMPTY — quoting them
    # is the bidder's job, and computing them is what this platform is for.
    # Such a row carries no code, no rate and no value, so (a)-(d) all reject
    # it: every branch above demands either a code or a printed number.
    #
    # That is the second half of issue #7. On the Liluah tender an annexure of
    # 30 items contributed 6 — the only ones that happened to print a serial
    # in the Item Code column. The other 24 were the ordinary shape above and
    # were discarded here, after the selection fix had finally opened the file
    # to read them.
    #
    # The unit of measure is what separates this from prose. A description
    # with a positive quantity AND a short, alphabetic unit ("Nos", "MT",
    # "Cum", "Job") is a table row; prose does not have a unit column. The
    # quantity alone would be too weak — a stray number lands on an
    # eligibility clause often enough that the guard below exists — so both
    # are required, and instruction prose is rejected before we ever get here.
    unit = (item.get("unit") or "").strip()
    description = (item.get("description") or "").strip()
    unit_ok = bool(unit) and len(unit) <= _MAX_UNIT_CHARS and bool(re.search(r"[A-Za-z]", unit))
    if qty_ok and description and unit_ok:
        return True

    # A scope-of-work document lists the drawings that apply -- "27. Louver-1
    # RDSO/CG/DRG/21032" -- serial, name, drawing number, nothing else. That
    # is a reference, not something to buy; without a quantity, a unit or a
    # price it is never a line item. (A real material row that cites its
    # drawing carries a quantity or a unit and never reaches this test.)
    if not qty_ok and not unit_ok and not rate_ok and _DRAWING_REGISTER_RE.search(description):
        return False

    # (f) A serial-numbered row with a unit but no quantity -- a format that
    # prints "Unit: Litre" and leaves Qty for the bidder, or a list where the
    # quantity simply did not parse. The unit is still a table signal.
    #
    # (g) A serial-numbered row with a description and nothing else -- the
    # approved-materials list, the list of items to be supplied, the third
    # Liluah report: `Sr | Description of Material | Specification`, no Qty
    # column at all. Every branch above needs a number the table does not
    # print, so every one of its rows was discarded. Costing needs the row:
    # the rate is researched per item and the amount is left for the user.
    #
    # The serial is what makes this a table row rather than a fragment, and
    # the description has to read like a cell rather than a sentence. That
    # is a weaker signal than a quantity, so the prose guard above -- which
    # runs first -- carries the openers of the numbered clauses this could
    # otherwise admit.
    serial = _serial_of(item)
    if (
        table_context
        and serial is not None
        and description
        and not _YES_NO_CELL_RE.match(description)
    ):
        if unit_ok:
            return True
        if _reads_like_a_cell(description):
            return True
        # A printed amount makes it a priced row; the full stop is the
        # table's own ("Health Faucet.", Rs 982.63 in the Liluah guard room)
        # and is all that stood between it and the costing.
        if rate_ok and _reads_like_a_cell(description.rstrip(". ")):
            return True

    return False


def _is_boq_header(header: list) -> bool:
    if not header:
        return False
    header_text = " ".join(str(cell or "").lower() for cell in header)
    matches = sum(
        1 for pattern in _BOQ_HEADER_PATTERNS if re.search(pattern, header_text)
    )
    return matches >= 2  # description + at least one other column


def _map_boq_columns(header: list) -> dict[str, int]:
    col_map: dict[str, int] = {}
    for i, cell in enumerate(header):
        cell_text = str(cell or "").lower().strip()
        if not cell_text:
            continue
        # Order matters — check the more-specific patterns before generic ones.
        if re.search(r"item\s*code", cell_text):
            col_map["item_code"] = i
        elif re.search(r"sr\.?\s*no|s\.?\s*no|sl\.?\s*no|item\s*no", cell_text):
            col_map["sr_no"] = i
        elif re.search(r"description|particulars|item\s*desc|name\s*of\s*item|scope", cell_text):
            col_map["description"] = i
        elif re.search(r"item\s*qty|^qty$|quantity|nos$|^numbers$", cell_text):
            col_map["quantity"] = i
        elif re.search(r"qty\s*unit|^unit$|^uom$|u\.o\.m", cell_text):
            col_map["unit"] = i
        elif re.search(r"unit\s*rate|^rate$|^price$", cell_text):
            col_map["unit_rate"] = i
        elif re.search(r"basic\s*value", cell_text):
            col_map["basic_value"] = i
        elif re.search(r"escl|escalation", cell_text):
            col_map["escalation_pct"] = i
        elif re.search(r"^amount$|^total$|^value$", cell_text):
            col_map["amount"] = i
        elif re.search(r"bidding\s*unit", cell_text):
            col_map["bidding_unit"] = i
    return col_map


def _parse_boq_row(row: list, col_map: dict) -> Optional[dict]:
    try:
        item: dict = {}

        if "sr_no" in col_map:
            val = str(row[col_map["sr_no"]] or "").strip()
            digits = re.sub(r"[^\d]", "", val)
            item["sr_no"] = int(digits) if digits else 0
        else:
            item["sr_no"] = 0

        if "item_code" in col_map:
            item["item_code"] = str(row[col_map["item_code"]] or "").strip() or None

        if "description" in col_map:
            item["description"] = str(row[col_map["description"]] or "").strip()

        if "quantity" in col_map:
            item["quantity"] = _parse_number(row[col_map["quantity"]])

        if "unit" in col_map:
            item["unit"] = str(row[col_map["unit"]] or "").strip() or None

        if "unit_rate" in col_map:
            item["unit_rate"] = _parse_number(row[col_map["unit_rate"]])
            # Keep the legacy `estimated_rate` alias populated for the thin
            # downstream consumers that still read that field.
            item["estimated_rate"] = item["unit_rate"]

        if "basic_value" in col_map:
            item["basic_value"] = _parse_number(row[col_map["basic_value"]])

        if "escalation_pct" in col_map:
            raw = str(row[col_map["escalation_pct"]] or "").strip()
            # "AT Par" / "At Par" / blank → 0; numeric → parse.
            if re.search(r"at\s*par", raw, flags=re.IGNORECASE) or not raw:
                item["escalation_pct"] = 0.0
            else:
                item["escalation_pct"] = _parse_number(raw)

        if "amount" in col_map:
            item["amount"] = _parse_number(row[col_map["amount"]])

        if "bidding_unit" in col_map:
            item["bidding_unit"] = str(row[col_map["bidding_unit"]] or "").strip() or None

        # Tax line detection from description.
        if item.get("description") and _detect_tax_line(item["description"]):
            item["is_tax_line"] = True

        return item if item.get("description") else None
    except Exception:
        return None


def _parse_number(value) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    text = re.sub(r"[₹$,\s]", "", text)
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _str_or_none(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _detect_tax_line(description: Optional[str]) -> bool:
    """True when the row IS a tax charge, not merely a row that mentions tax.

    See ``_TAX_QUALIFIER_RE`` for why a bare token match is wrong: a work item
    quoting a GST-inclusive rate must stay costable.
    """
    if not description:
        return False

    text = description.strip()
    if _TAX_PROVISION_RE.search(text):
        return True

    # Drop "inclusive of GST"-style qualifiers before looking for the token, so
    # they can't make a work item look like a tax row.
    stripped = _TAX_QUALIFIER_RE.sub(" ", text)
    if not _TAX_TOKEN_RE.search(stripped):
        return False

    if _TAX_SUBJECT_RE.match(stripped):
        return True

    return len(re.sub(r"\s+", " ", stripped).strip()) <= _TAX_SHORT_ROW_MAX_CHARS


# IREPS section `2. SCHEDULE` is a SUMMARY table: one row per schedule whose
# description is a pointer to the real breakup ("Please see Item Breakup for
# details.") and whose value is the schedule's total. It carries NO scope.
#
# Counting it as an extracted line is what let tender 3822's Schedule D pass
# reconciliation at +0.0% with zero real rows captured (session 290): the
# placeholder's value equals the printed subtotal by construction, so it always
# reconciles perfectly no matter how badly extraction did.
_SUMMARY_PLACEHOLDER_RE = re.compile(
    r"(?:please\s+)?(?:see|refer(?:\s+to)?|as\s+per)\s+(?:the\s+)?item\s*break\s*-?\s*up",
    re.IGNORECASE,
)


def _is_summary_placeholder_row(item: dict) -> bool:
    """True for an IREPS section-2 summary row (a pointer to the Item Breakup).

    Requires BOTH the pointer description AND the absence of any per-unit
    pricing: a real work item that merely cites the breakup keeps its qty and
    rate, so it is never discarded here.
    """
    if not _SUMMARY_PLACEHOLDER_RE.search(item.get("description") or ""):
        return False

    def _positive(value) -> bool:
        try:
            return value is not None and float(value) > 0
        except (TypeError, ValueError):
            return False

    has_qty = _positive(item.get("quantity"))
    has_rate = _positive(item.get("unit_rate")) or _positive(item.get("estimated_rate"))
    return not (has_qty or has_rate)


def _drop_summary_placeholder_rows(items: list[dict]) -> tuple[list[dict], int]:
    """Remove IREPS section-2 summary rows before persistence.

    A placeholder persisted as a BOQItem becomes a phantom line item carrying
    the whole schedule's value with no quantity and no rate -- which is what
    rendered as "Schedule D -- Tender Value 0.00" in session 290. Returns
    (survivors, dropped_count), preserving order.
    """
    survivors = [it for it in items if not _is_summary_placeholder_row(it)]
    return survivors, len(items) - len(survivors)


# ---------------------------------------------------------------------------
# AI fallback path
# ---------------------------------------------------------------------------


def _extract_text_pymupdf(file_path: str) -> str:
    """Raw per-page text via PyMuPDF, joined with newlines — the same
    extraction shape `nit_schedule_parser` is designed against (a rigid
    line-by-line token stream). Used ONLY for the deterministic-first IREPS
    probe; the AI/vision fallback path uses its own advanced-parser text
    extraction (`_load_pdf_pages`)."""
    import fitz  # PyMuPDF
    doc = fitz.open(file_path)
    try:
        return "\n".join(pg.get_text() for pg in doc)
    finally:
        doc.close()


def _pdf_page_count(file_path: str) -> int:
    """Best-effort page count for a PDF (0 on failure). Used by the
    completeness heuristic that decides whether to run the chunked AI pass."""
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def _boq_row_key(it: dict) -> tuple:
    """Dedup/merge key for a BOQ row.

    Deterministic identity: (schedule, sr_no, item_code) when BOTH sr_no and
    item_code are present — sr_no+code is unique per NIT line (sub-codes like
    a/b share sr_no but differ by code), so this never collapses distinct
    lines and never keeps a duplicate of the same line. This kills the
    coded/code-less duplicate twins that the AI/vision passes sometimes both
    emit for the same physical row.

    Falls back to (schedule, item_code) ONLY when the code is a per-row UNIQUE
    alphanumeric code (e.g. "A1", "B713") but sr_no is missing. NITs that print
    a repeated category label in the Item Code column ("Mechanical", "ELEC
    FUR" on every row of a schedule) must NOT key on that alone — otherwise
    every row in the schedule collapses to one key and dedup destroys the
    schedule. For those (and for missing codes) fall back to (schedule,
    sr_no, description-prefix), which is per-row distinct.
    """
    sched = (it.get("schedule_name") or "").strip().upper()
    if not sched and it.get("annexure_ref"):
        # Two annexures both start at serial 1 and a material list for the
        # ICF coaches reads much like the one for the Hybrid coaches. Without
        # the annexure in the key, cross-document dedup collapsed them.
        sched = f"ANX:{it['annexure_ref']}"
    code = re.sub(r"\s+", " ", (it.get("item_code") or "").strip()).upper()
    sr = str(it.get("sr_no") or "")
    if sr and code:
        return (sched, sr, code)
    # An alphanumeric schedule code (letter(s) + digits) is a true per-row id.
    if code and re.match(r"^[A-Z]{1,3}\d+$", re.sub(r"[^A-Za-z0-9]", "", code)):
        return (sched, code)
    # Word-style / non-unique / missing code → key on the per-row distinct fields.
    if sched.startswith("ANX:"):
        # A sub-table restarts the serial, and one part name recurs across
        # sub-assemblies ("Hook Lock", "Door Hinge Pin"): the quantity is
        # what tells two such rows apart. A row emitted twice for the same
        # physical line still matches on all three.
        return (sched, sr, (it.get("description") or "").strip()[:40], str(it.get("quantity") or ""),
                (it.get("subassembly") or "").strip())
    return (sched, sr, (it.get("description") or "").strip()[:40])


def _merge_boq_rows(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """Union two row sets, preserving `primary` order and appending only the
    rows from `secondary` whose key isn't already present in `primary`."""
    seen = {_boq_row_key(it) for it in primary}
    merged = list(primary)
    for it in secondary:
        k = _boq_row_key(it)
        if k not in seen:
            merged.append(it)
            seen.add(k)
    return merged


def _parse_ai_json_array(result: str) -> list[dict]:
    """Tolerant parse of an LLM JSON-array response (handles code fences and
    trailing prose). Returns a list of dict rows (possibly empty)."""
    if not result:
        return []
    text = result.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    try:
        data = json.loads(text)
    except Exception:
        a, b = text.find("["), text.rfind("]")
        if a == -1 or b == -1 or b <= a:
            return []
        try:
            data = json.loads(text[a:b + 1])
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


_BOQ_AI_SYSTEM_PROMPT = """You are a NIT bidding-schedule extraction specialist for Indian railway tenders (IREPS, GeM, CRIS portals).

Extract EVERY line item from the "Schedule of Items" / "Bill of Quantities" / "Schedule of Rates" / "Price Bid" tables in the TEXT you are given. The text may come from an ANNEXURE rather than the NIT body -- a material list, a list of items to be supplied, a schedule of quantities, a price-bid format -- with columns such as Sr No, Description / Material / Item, Specification / Make, Qty, Unit. Every row of such a table is a line item: extract it exactly as you would a NIT schedule row. Such a table may have NO Qty column at all (an approved-materials list, a list of items to be supplied) -- still emit EVERY row, with `quantity: null` and `unit: null`; the serial number in `sr_no` is what identifies the row, so always carry it. There is no schedule banner in an annexure, so leave `schedule_name` null for those rows; the "annexure references" exclusion below means citations TO an annexure inside prose (e.g. "as per Annexure-III"), never the table of the annexure itself. Do NOT summarise, sample, group, or skip rows — return ONE JSON object per line item, including every spare-part row, even when there are hundreds.

ONLY extract SCHEDULE rows. A valid row has an Item Code (e.g. A1, A71, B7138, C786, D745) OR an Item Qty together with its Qty Unit. A BLANK Unit Rate does NOT make a row invalid: in a price bid / schedule of quantities the tender prints the item, its quantity and its unit and leaves Rate and Amount EMPTY for the bidder to quote. Those rows are the ones that most need extracting — emit them with `unit_rate: null` and `amount: null` rather than skipping them. The Item Code may ALSO be a short descriptive label that the NIT prints in the Item Code column — e.g. "Mechanical", "ELEC FUR", "ELEC", "CIVIL" — capture it VERBATIM into `item_code`, never fold it into `description`. DO NOT extract prose: eligibility criteria, undertakings, declarations ("I/we the tenderer…"), general instructions, compliance clauses, annexure references, or document lists. The schedule section ENDS at headers like "ITEM BREAKUP", "ELIGIBILITY CONDITIONS", "Special Financial/Technical Criteria", "Bidders shall confirm", "COMPLIANCE", "General Instruction", "Undertakings", or "Documents attached with tender" — return NOTHING from at or after such a header. If a line has only descriptive prose — no item code, and no quantity with a unit — it is NOT a line item — skip it.

NITs group rows under schedule banners like "Schedule () A-Engine removal…", "Schedule () A7-Cost of spares…", "Schedule () B7-Cost of Spares…". A schedule code is a LETTER optionally followed by ONE DIGIT (A, A7, B, B7, C, C7, D, D7). PRESERVE THE FULL CODE — never collapse "A7" into "A". Each row sits under the most recent schedule banner above it.

MERGED CELLS — emit each logical line item EXACTLY ONCE. NIT tables often merge the Item Code / Description cell across several unmerged sub-rows (sub-quantities, component breakdowns, or continuation lines). This is ONE line item, not many. Do NOT repeat the item once per grid row it visually spans. If the sub-rows carry their own quantities that belong to the same item, SUM them into that item's single `quantity`. Before returning, sanity-check: within a schedule, the number of objects you emit must equal the number of DISTINCT printed serial numbers — never a multiple of it.

For EACH line item return a JSON object. Use null when a field is genuinely absent; do NOT guess.
- sr_no              : integer — serial number within its schedule
- item_code          : string  — the tender's "Item Code" (e.g. "A1", "A71", "B741", "C71", "D727", OR a word label like "Mechanical" / "ELEC FUR")
- description        : string  — full description VERBATIM (the text after "Description:-")
- quantity           : number  — "Item Qty"
- printed_quantity   : number | null — REQUIRED for every annexure row: literal Qty cell before weight/percentage conversion; null if absent
- weight_per_item    : number | null — REQUIRED for every annexure row: literal Weight/Item cell in kg; null if absent
- quantity_percent   : number | null — literal percentage printed in the Qty cell; null if absent
- subassembly        : string | null — full nearest numbered assembly heading and drawing; null if absent
- unit               : string  — "Qty Unit" (e.g. "Numbers")
- unit_rate          : number  — "Unit Rate"
- basic_value        : number  — "Basic Value"
- escalation_pct     : number  — numeric "Escl.(%)". "AT Par"/"At Par"/blank → 0.
- amount             : number  — "Amount"

MATERIAL LISTS: transcribe numbers, NEVER calculate them. In a table `Qty | Unit | Weight/Item | Rate/Item | Total Cost`, emit `printed_quantity` = the exact Qty cell, `weight_per_item` = the exact Weight/Item cell (kg), `quantity` = the exact Qty cell, `unit_rate` = Rate/Item, and `basic_value` = the exact Total Cost cell. The backend multiplies quantity and weight. If the Qty cell explicitly applies a percentage (such as 80%), also transcribe quantity_percent as 80; otherwise null. Never compute the percentage or use dimensions as quantities. Use null for a missing weight; do not treat a missing weight as 1 kg. Preserve Indian comma grouping accurately: 1,02,783.33 is 102783.33, NOT 1027833.3. Never put a dimension, drawing number, weight or currency value in printed_quantity. For material rows without a weight column, still emit printed_quantity and set weight_per_item null. For a total-only row leave quantity, printed_quantity, unit and unit_rate null and transcribe basic_value; the backend can price its stated scope as one lot. A merged price spanning several parts is ONE assembly price, never the price of each part. Keep all part descriptions in that assembly description. Exclude subtotal/grand-total rows, but retain a separate Labour cost charge that is not itself a subtotal. Transcribe the nearest full sub-assembly heading into subassembly (including its number and drawing) so repeated parts in different assemblies remain distinct. A numbered sub-assembly heading is not a schedule code: leave schedule_name and item_code null beneath it.
- bidding_unit       : string  — "Bidding Unit" (e.g. "AT Par", "Rs.")
- schedule_name      : string  — the FULL schedule code the row belongs to (A, A7, B, B7, …)
- annexure           : string  — for a row read from an ANNEXURE table, the annexure label printed in the heading above that table ("VII" for "Annexure-VII", "II" for "Annexure-2", "B" for "Annexure-B"); null for a row under a NIT schedule banner. When one text carries more than one annexure heading, each row takes the heading nearest above it.
- is_tax_line        : boolean — true for GST/tax provision rows; false otherwise
- extraction_confidence : "high" | "medium" | "low"

Respond with ONLY a valid JSON array of these objects. No markdown, no commentary, no wrapping object. If the text has no schedule rows, return []."""


def _get_int_setting(db: Session, key: str, default: int) -> int:
    from app.services.settings_service import get_setting_value
    try:
        return int(get_setting_value(db, key, default) or default)
    except Exception:
        return default


def _get_float_setting(db: Session, key: str, default: float) -> float:
    from app.services.settings_service import get_setting_value
    try:
        return float(get_setting_value(db, key, default) or default)
    except Exception:
        return default


def _get_bool_setting(db: Session, key: str, default: bool) -> bool:
    from app.services.settings_service import get_setting_value
    try:
        val = get_setting_value(db, key, default)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("true", "1", "yes")
    except Exception:
        return default


#: A page whose text layer is shorter than this AND carries no row-like
#: lines is treated as an image table for schedule purposes. The Liluah
#: material lists (issue #7, third report) were 13 pages of scanned tables
#: under printed headers: each page's text layer held the header -- tender
#: number, "Annexure-VII", "Material List" -- which clears the platform-wide
#: `pdf_vision_density_min_chars` (40) gate, so the page was read as "text",
#: never sent to vision, and the extractor saw a title and no rows.
_SPARSE_PAGE_MAX_CHARS = 1500
#: Fewer row-like lines than this (a line with a digit and at least three
#: words) means the page's text layer is not describing a table.
_ROW_LIKE_MIN_LINES = 3
#: Vision is paid per page; bound how many a single document may force.
_VISION_RECOVERY_MAX_PAGES = 40


def _page_looks_like_image_table(text: Optional[str]) -> bool:
    """True for a page whose extracted text is a header with no rows under it.

    Empty pages are not this -- the cascade already sends those to vision.
    Long pages are not this -- a real text page describes its rows. What is
    left is the shape a scanned table under a printed heading produces.
    """
    t = (text or "").strip()
    if not t or len(t) >= _SPARSE_PAGE_MAX_CHARS:
        return False
    row_like = sum(
        1 for ln in t.splitlines()
        if re.search(r"\d", ln) and len(ln.split()) >= 3
    )
    return row_like < _ROW_LIKE_MIN_LINES


async def _recover_image_table_pages(file_path: str, pages: list[dict]) -> list[dict]:
    """Force vision on text-layer pages that look like image tables, in place.

    Only pages the cascade tagged `text` are candidates; it already handled
    the genuinely empty ones. A page is replaced only when vision returns
    MORE than the text layer had -- a header is never traded for nothing.
    """
    candidates = [
        p for p in pages
        if p.get("method") == "text" and _page_looks_like_image_table(p.get("text"))
    ]
    if not candidates:
        return pages
    skipped = max(0, len(candidates) - _VISION_RECOVERY_MAX_PAGES)
    candidates = candidates[:_VISION_RECOVERY_MAX_PAGES]
    recovered = 0
    for p in candidates:
        vt = await _force_page_vision_text(file_path, p.get("page_num"))
        if vt and len(vt.strip()) > len((p.get("text") or "").strip()):
            p["text"] = vt.strip()
            p["method"] = "vision"
            recovered += 1
    logger.info(
        f"[boq_parser] {os.path.basename(file_path)}: {len(candidates)} page(s) "
        f"looked like image tables under a printed header -- vision recovered "
        f"{recovered}" + (f"; {skipped} beyond the per-document cap" if skipped else "")
    )
    return pages


async def _load_pdf_pages(
    db: Session, file_path: str
) -> tuple[list[dict], list[Optional[str]], list[set]]:
    """Load per-page text (pdfplumber → Claude vision → Tesseract OCR cascade)
    and precompute, for each kept page, the carry-in schedule (active at the
    page's start) and the SET of schedule codes that appear on that page
    (carry-in + banners). Used for both full extraction and per-schedule
    targeted re-extraction. Returns ([], [], []) when no text is extractable.
    """
    import asyncio
    from app.services.advanced_document_parser import extract_text_from_pdf_advanced

    try:
        extract_result = await asyncio.to_thread(extract_text_from_pdf_advanced, file_path)
    except Exception as e:
        logger.warning(f"BOQ advanced text extraction failed for {file_path}: {e}")
        return [], [], []

    pages = [
        p for p in (extract_result.get("pages") or [])
        if (p.get("text") or "").strip()
    ]
    if not pages:
        return [], [], []

    # A scanned table under a printed heading reaches here tagged "text" with
    # only the heading in it. Give those pages to vision before anything
    # downstream decides there are no rows.
    try:
        pages = await _recover_image_table_pages(file_path, pages)
    except Exception as e:
        logger.warning(f"[boq_parser] image-table recovery failed for {file_path}: {e}")

    # Trim to the "Schedule of Items" section: once the schedule has begun (a
    # "Schedule () X-" banner seen), cut at the first section-end marker so the
    # eligibility/undertaking/annexure/document pages (and prose on the boundary
    # page) are never extracted. The boundary page keeps its schedule rows (the
    # text BEFORE the marker) and everything after is dropped.
    _seen_schedule = False
    _trimmed: list[dict] = []
    for p in pages:
        text = p.get("text") or ""
        if not _seen_schedule and _SCHEDULE_BANNER_RE.search(text):
            _seen_schedule = True
        if _seen_schedule:
            m = _SCHEDULE_END_RE.search(text)
            if m:
                head = text[:m.start()].rstrip()
                if head.strip():
                    _trimmed.append({**p, "text": head})
                break  # drop this marker text + all subsequent pages
        _trimmed.append(p)
    if _trimmed:
        if len(_trimmed) < len(pages):
            logger.info(
                f"[boq_parser] trimmed PDF to schedule section: "
                f"{len(_trimmed)}/{len(pages)} page(s) kept ({file_path})"
            )
        pages = _trimmed

    carry_in: list[Optional[str]] = []
    page_schedules: list[set] = []
    last_sched: Optional[str] = None
    for p in pages:
        carry_in.append(last_sched)
        scheds: set = set()
        if last_sched:
            scheds.add(last_sched)
        for m in _SCHEDULE_HEADER_RE.finditer(p.get("text") or ""):
            last_sched = m.group(1).upper()
            scheds.add(last_sched)
        page_schedules.append(scheds)
    return pages, carry_in, page_schedules


def _normalize_schedule_names(items: list[dict]) -> None:
    """In-place: normalise schedule_name to its full code (A, A7, …) and
    fill any gaps from the running last-seen schedule in document order."""
    running: Optional[str] = None
    for it in items:
        sched = it.get("schedule_name")
        if isinstance(sched, str) and sched.strip():
            code = sched.strip().upper()
            m = re.search(r"\b([A-Z]\d*)\b", code)
            it["schedule_name"] = m.group(1) if m else code
            running = it["schedule_name"]
        elif it.get("annexure_ref"):
            # An annexure row printed after the schedule is a component of
            # a schedule item, not a continuation of the last schedule.
            continue
        elif running:
            it["schedule_name"] = running


def _dedup_boq_rows(items: list[dict]) -> list[dict]:
    """Dedup by the same key the merge uses ((schedule, item_code) or
    (schedule, sr_no, desc-prefix)), preserving first-seen order."""
    out: list[dict] = []
    seen: set = set()
    for it in items:
        k = _boq_row_key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


async def _force_page_vision_text(file_path: str, page_num) -> Optional[str]:
    """Force Claude vision on a single page (1-based page_num → 0-based index),
    bypassing the text-density gate. Opens its own DB session so it's safe to
    run in a worker thread. Returns the vision text or None."""
    def _work():
        from app.core.database import SessionLocal
        from app.services.advanced_document_parser import _try_page_vision_cached
        from app.services.settings_service import get_setting_value
        s = SessionLocal()
        try:
            model = str(get_setting_value(s, "pdf_vision_model", "claude-haiku-4-5-20251001"))
            res = _try_page_vision_cached(
                s, file_path, int(page_num) - 1, source_key_cached=None, model=model,
            )
            return res.get("text") if res else None
        finally:
            try:
                s.close()
            except Exception:
                pass

    try:
        import asyncio
        return await asyncio.to_thread(_work)
    except Exception as e:
        logger.debug(f"[boq_parser] forced vision for page {page_num} failed: {e}")
        return None


_LABOUR_FOOTER_DESC_RE = re.compile(r"labou?r\s+(?:cost|charges?)(?:\s*\([A-Z]\))?", re.I)


def _recover_annexure_labour_rows(rows: list[dict], source: str,
                                  carry: Optional[str] = None) -> list[dict]:
    """Retain standalone printed labour charges the extractor can mistake for
    totals, each under the annexure it is printed in.

    A footer's annexure is the last heading above it in the text; above the
    first heading, the annexure the chunk continues (`carry`). The
    extractor's label is not used for it: Annexure-II's "Labour Cost (B)
    Rs 3,33,949" is printed on the page that opens Annexure-III, and was
    taken as Annexure-III's labour -- Rs 3.34 lakh in a Rs 65,000 item, with
    Annexure-III's own Rs 36,386.76 skipped as a second labour row.
    """
    headings = list(_ANNEXURE_HEADING_RE.finditer(source))
    first_ref = next((normalize_annexure_ref(r.get("annexure")) for r in rows if r.get("annexure")), None)
    carry = normalize_annexure_ref(carry)
    if not headings and not first_ref and not carry:
        return rows
    pattern = re.compile(
        r"^\s*(labou?r\s+(?:cost|charges?)(?:\s*\([A-Z]\))?)\s*(?:₹|Rs\.?)?\s*([\d,]+\.\d{2})\s*$", re.I | re.M,
    )
    footers = []
    # Replacing table separators preserves offsets used for heading lookup.
    for match in pattern.finditer(source.replace("|", " ")):
        prior = [h for h in headings if h.start() < match.start()]
        ref = normalize_annexure_ref(prior[-1].group(1)) if prior else (carry or first_ref)
        if ref:
            footers.append((ref, match.group(1).strip(), float(match.group(2).replace(",", ""))))
    if not footers:
        return rows

    # The extractor's own copy of a printed footer is replaced by the
    # recovered one, which carries the annexure it is printed in.
    values = [v for _, _, v in footers]
    kept = []
    for r in rows:
        if _LABOUR_FOOTER_DESC_RE.fullmatch((r.get("description") or "").strip()):
            v = _row_value(r)
            if v is None or any(abs(v - fv) < 0.5 for fv in values):
                continue
        kept.append(r)
    rows = kept
    for ref, desc, value in footers:
        row = {"sr_no": None, "description": desc, "annexure": ref,
               "quantity": None, "printed_quantity": None, "weight_per_item": None,
               "unit": None, "unit_rate": None, "basic_value": value,
               "schedule_name": None, "extraction_confidence": "high"}
        # Keep it after the other rows of its annexure, before the next one.
        positions = [i for i, r in enumerate(rows) if normalize_annexure_ref(r.get("annexure")) == ref]
        if positions:
            at = positions[-1] + 1
        elif ref == carry:
            # Its rows continue from the previous chunk unlabelled: before
            # the first row that names another annexure.
            at = next((i for i, r in enumerate(rows)
                       if normalize_annexure_ref(r.get("annexure")) not in (None, ref)), len(rows))
        else:
            at = len(rows)
        rows.insert(at, row)
    return rows


def _money_only_numbers(source: str) -> set:
    """Numbers the page prints only as rupee amounts (`_money_marked_numbers`)."""
    money, plain = _money_marked_numbers(source)
    return money - plain


def _money_marked_numbers(source: str) -> tuple[set, set]:
    """(rupee amounts, other numbers) as printed on the page. A rupee amount
    follows a ₹/Rs on its line, or is wrapped onto the next line under a ₹
    left dangling at the end of its cell ("₹", and "1,213.04" beneath it)."""
    token = re.compile(r"(?<![\w.,/])\d[\d,]*(?:\.\d+)?(?![\w.])")
    dangling_re = re.compile(r"(?:₹|\bRs\.?)(?=[ \t]*(?:₹|$))")
    money: set = set()
    plain: set = set()
    lines = (source or "").split("\n")
    wrapped: set = set()
    for i, line in enumerate(lines):
        for m in token.finditer(line):
            try:
                v = round(float(m.group(0).replace(",", "")), 2)
            except ValueError:
                continue
            before = line[:m.start()].rstrip()
            if before.endswith("₹") or re.search(r"\bRs\.?$", before) or m.start() in wrapped:
                money.add(v)
            else:
                plain.add(v)
        wrapped = set()
        if i + 1 < len(lines):
            below = [m for m in token.finditer(lines[i + 1]) if "." in m.group(0)]
            for d in (m.start() for m in dangling_re.finditer(line)):
                if below:
                    wrapped.add(min(below, key=lambda m: abs(m.start() - d)).start())
    return money, plain


def _ungrounded_annexure_numbers(rows: list[dict], source: str) -> list[tuple[int, str]]:
    """Find transcribed numeric cells absent from the supplied page text.

    Only literal source operands are checked, never calculated quantities.
    This catches digit insertion between OCR and JSON without guessing a
    replacement. It cannot certify the OCR itself.
    """
    import math
    numbers = {
        float(m.replace(",", ""))
        for m in re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w.])", source)
    }
    if not numbers:
        return []
    issues = []
    for idx, row in enumerate(rows):
        if row.get("schedule_name") or not (row.get("annexure") or row.get("annexure_ref")):
            continue
        for key in ("printed_quantity", "weight_per_item"):
            if key not in row:
                issues.append((idx, key))
        for key in ("printed_quantity", "weight_per_item", "quantity_percent", "unit_rate", "basic_value"):
            value = row.get(key)
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                issues.append((idx, key))
                continue
            if not math.isfinite(value) or not any(abs(value - n) < 0.005 for n in numbers):
                issues.append((idx, key))
    return issues


def _restore_dropped_weight_rates(rows: list[dict], money_marked: set) -> None:
    """In place: a weight row read with one money cell and no total, where
    that cell is the row's total and the Rate/Item was dropped.

    Tender 5157's "Top Angle ... 4 | 15.97 | ₹ 29.29 | ₹ 1,871.07" came back
    as 63.88 kg at Rs 1,871.07 a kg -- Rs 1.2 lakh for Rs 1,871 of steel.
    The rate is restored only when Qty x Weight x % divides the cell into an
    amount the same page prints with a rupee sign, to the paisa, and that
    amount is at least a rupee: a kg rate is printed as money, a weight never
    is, so a real rate with a missing total does not match by chance.
    """
    import math

    def number(value):
        try:
            n = float(value)
            return n if math.isfinite(n) and n > 0 else None
        except (TypeError, ValueError):
            return None

    for row in rows:
        if row.get("schedule_name") or row.get("basic_value") is not None or row.get("_numeric_source_issue"):
            continue
        qty = number(row.get("printed_quantity"))
        weight = number(row.get("weight_per_item"))
        cell = number(row.get("unit_rate") if row.get("unit_rate") is not None else row.get("estimated_rate"))
        if not (qty and weight and cell):
            continue
        percent = number(row.get("quantity_percent"))
        basis = qty * weight * (percent / 100 if percent else 1.0)
        x = cell / basis
        slack = 0.005 + 0.005 / basis
        found = {v for v in money_marked if abs(v - x) <= slack}
        if x < 1 or basis <= 1 or len(found) != 1:
            continue
        rate = found.pop()
        row["basic_value"] = cell
        row["unit_rate"] = rate
        if row.get("estimated_rate") is not None:
            row["estimated_rate"] = rate
        row["extraction_confidence"] = "low"
        logger.warning(
            "[boq_parser] annexure row %r: Rs %.2f is its total (%.6g kg at the page's Rs %.2f); "
            "the rate cell had been dropped", (row.get("description") or "")[:40], cell, basis, rate,
        )


async def _call_boq_chunk(db: Session, chunk_pages: list[dict], carry: Optional[str]):
    """One LLM extraction call for a page chunk. Returns parsed rows (list),
    [] on a clean empty parse, or None if the LLM call itself raised."""
    from app.services.ai_service import call_ai_with_documents

    chunk_text = "\n\n".join(
        f"[page {p.get('page_num')}]\n{p.get('text')}" for p in chunk_pages
    )
    carry_note = (
        f"NOTE: rows appearing BEFORE the first 'Schedule ()' banner in the "
        f"text below continue Schedule {carry}.\n\n" if carry else ""
    )
    user_prompt = (
        "Extract ALL line items from the NIT schedule text below "
        f"(pages {chunk_pages[0].get('page_num')}–{chunk_pages[-1].get('page_num')}). "
        "Return one JSON object per row; do not skip or summarise any.\n\n"
        f"{carry_note}{chunk_text}"
    )
    try:
        result = await call_ai_with_documents(
            system_prompt=_BOQ_AI_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            document_paths=None,  # text-only — per-page text already vision/OCR'd
            db=db,
            agent_name="boq_parser",
            max_tokens_override=32000,  # clamped to the model ceiling
            thinking_mode_override="disabled",
            temperature_override=0.0,
        )
    except Exception as e:
        logger.warning(
            f"[boq_parser] chunk pages {chunk_pages[0].get('page_num')}–"
            f"{chunk_pages[-1].get('page_num')} LLM call failed: {e}"
        )
        return None
    rows = _parse_ai_json_array(result)
    issues = _ungrounded_annexure_numbers(rows, chunk_text)
    if issues:
        # One bounded repair, using the same source rather than accepting
        # a number that did not occur in the page text.
        try:
            repair = await call_ai_with_documents(
                system_prompt=_BOQ_AI_SYSTEM_PROMPT,
                user_prompt=user_prompt + "\n\nRecheck these numeric cells against the source: "
                + json.dumps([{"row": i + 1, "field": key, "value": rows[i].get(key)} for i, key in issues])
                + ". Values are missing or do not occur in the source. Include printed_quantity and weight_per_item on EVERY annexure row. Return ALL rows with literal source values; use null if unreadable.",
                document_paths=None, db=db, agent_name="boq_parser", max_tokens_override=32000,
                thinking_mode_override="disabled", temperature_override=0.0,
            )
            repaired = _parse_ai_json_array(repair)
            if repaired and len(repaired) == len(rows):
                rows = repaired
        except Exception as exc:
            logger.warning("[boq_parser] numeric repair failed: %s", type(exc).__name__)
        for idx, key in _ungrounded_annexure_numbers(rows, chunk_text):
            rows[idx][key] = None
            rows[idx]["extraction_confidence"] = "low"
            rows[idx]["_numeric_source_issue"] = True
            if key in {"printed_quantity", "weight_per_item", "quantity_percent"}:
                rows[idx]["quantity"] = None
    money_marked, plain_numbers = _money_marked_numbers(chunk_text)
    money_only = money_marked - plain_numbers
    _restore_dropped_weight_rates(rows, money_marked)
    # A zero total is believed only as often as the page prints one, so an
    # extractor writing 0 for a blank cell cannot zero a real row.
    nil_printed = len(re.findall(r"(?:₹|\bRs\.?)[ \t]*0\.00(?![\d.,])", chunk_text))
    nil_rows = []
    for row in rows:
        try:
            w = row.get("weight_per_item")
            if w is not None and round(float(w), 2) in money_only:
                row["_weight_cell_is_money"] = True
            bv = row.get("basic_value")
            if not row.get("schedule_name") and bv is not None and float(bv) == 0.0:
                nil_rows.append(row)
        except (TypeError, ValueError):
            pass
    if nil_rows and len(nil_rows) <= nil_printed:
        for row in nil_rows:
            row["_printed_nil_total"] = True
    return _recover_annexure_labour_rows(rows, chunk_text, chunk_pages[0].get("annexure_carry"))


def _text_looks_like_schedule(text: str) -> bool:
    """Whether a chunk's text plausibly holds line items worth a retry.

    The IREPS markers are sufficient on their own. Otherwise, two of the
    generic table-header signals (`_BOQ_HEADER_PATTERNS`: serial, description,
    quantity, unit, rate, amount ...) -- the shape a material list, a schedule
    of quantities or a price-bid annexure prints. It used to be the IREPS
    markers alone, so a non-IREPS document whose first extraction came back
    empty was never retried, and the zero-row result was not logged either.
    """
    if re.search(r"Description:-|Item\s*Code", text or ""):
        return True
    low = (text or "").lower()
    return sum(1 for pat in _BOQ_HEADER_PATTERNS if re.search(pat, low)) >= 2


async def _extract_chunk_with_retry(
    db: Session, chunk_pages: list[dict], carry: Optional[str], *, max_retries: int
) -> list[dict]:
    """Extract one chunk's rows, retrying on an LLM error OR an empty parse when
    the text plausibly contains schedule rows. The retry SPLITS the chunk's
    pages in half — truncated JSON is almost always output-token overflow on a
    too-dense chunk, and a smaller chunk fits. Bounded by max_retries; a chunk
    that still yields nothing has its page range logged, never dropped silently.
    """
    rows = await _call_boq_chunk(db, chunk_pages, carry)
    if rows:  # non-empty list
        return rows
    errored = rows is None
    joined = "\n".join(p.get("text") or "" for p in chunk_pages)
    plausible = _text_looks_like_schedule(joined)
    if max_retries <= 0 or not (errored or plausible):
        # Always say so. A chunk that yields nothing is either a page with no
        # rows on it or a page whose rows were not read -- and the second is
        # the one that loses a bid. The log is the only way to tell them apart.
        logger.warning(
            f"[boq_parser] chunk pages {chunk_pages[0].get('page_num')}–"
            f"{chunk_pages[-1].get('page_num')} yielded 0 rows "
            f"(errored={errored}, looked_like_schedule={plausible}, "
            f"chars={len(joined)}, head={joined.strip()[:100]!r})"
        )
        return []
    if len(chunk_pages) <= 1:
        # Can't split a single page further — one direct retry.
        retried = await _call_boq_chunk(db, chunk_pages, carry)
        if not retried:
            logger.warning(
                f"[boq_parser] single page {chunk_pages[0].get('page_num')} "
                f"yielded 0 rows after retry"
            )
        return retried or []
    mid = len(chunk_pages) // 2
    left = await _extract_chunk_with_retry(
        db, chunk_pages[:mid], carry, max_retries=max_retries - 1
    )
    right = await _extract_chunk_with_retry(
        db, chunk_pages[mid:], carry, max_retries=max_retries - 1
    )
    return left + right


async def _walk_pages_chunked(
    db: Session,
    pages: list[dict],
    carry_in: list[Optional[str]],
    page_indices: list[int],
    *,
    pages_per_chunk: int,
    max_retries: int,
    file_path: Optional[str] = None,
    force_vision: bool = False,
) -> list[dict]:
    """Run the chunked AI extractor over the given page indices (into `pages`),
    grouping them into chunks of `pages_per_chunk`. When `force_vision`, each
    page's text is re-derived via Claude vision (for genuinely scanned
    schedules). Returns schedule-tagged rows (not yet deduped)."""
    if not page_indices:
        return []
    pages_per_chunk = max(1, pages_per_chunk)
    out: list[dict] = []

    # Annexure headings work like schedule banners: the label carries forward
    # page to page. A chunk is also cut BEFORE a page that starts a new
    # annexure, so that most chunks hold one annexure and a row's label is
    # not a guess. A page that ends one annexure and starts the next inside
    # it is the one case left to the extractor's own `annexure` field.
    anx_carry, anx_starts = _annexure_carry(pages)
    chunks: list[list[int]] = []
    current: list[int] = []
    for i in page_indices:
        if current and (len(current) >= pages_per_chunk or anx_starts[i]):
            chunks.append(current)
            current = []
        current.append(i)
    if current:
        chunks.append(current)

    total_chunks = len(chunks)
    for chunk_no, idx_chunk in enumerate(chunks, start=1):
        _progress(
            f"Reading the price schedule (part {chunk_no} of {total_chunks})",
            run_id="boq-chunks",
        )
        chunk_pages: list[dict] = []
        for i in idx_chunk:
            p = pages[i]
            text = p.get("text") or ""
            if force_vision and file_path:
                vt = await _force_page_vision_text(file_path, p.get("page_num"))
                if vt:
                    text = vt
            chunk_pages.append({"page_num": p.get("page_num"), "text": text,
                                "annexure_carry": anx_carry[i]})
        carry = carry_in[idx_chunk[0]]
        joined = "\n".join(p["text"] for p in chunk_pages)
        # The labels this chunk's text can vouch for: the carry-in plus every
        # heading on its pages. A row's own `annexure` field is honoured only
        # when it names one of these -- the extractor may not invent one.
        chunk_refs: list[str] = []
        if anx_carry[idx_chunk[0]]:
            chunk_refs.append(anx_carry[idx_chunk[0]])
        for m in _ANNEXURE_HEADING_RE.finditer(joined):
            ref = normalize_annexure_ref(m.group(1))
            if ref and ref not in chunk_refs:
                chunk_refs.append(ref)
        # Serial-only rows ((f)/(g) in `_is_valid_schedule_item`) are taken
        # only when this chunk's text shows a table header and no IREPS
        # schedule banner -- see that function for why.
        table_context = bool(chunk_refs) or (
            _text_looks_like_schedule(joined) and not _SCHEDULE_BANNER_RE.search(joined)
        )
        rows = await _extract_chunk_with_retry(
            db, chunk_pages, carry, max_retries=max_retries
        )
        # A chunk that is an annexure table and nothing else: no schedule
        # banner in its text and no schedule carried into it. Any
        # `schedule_name` the extractor emits there is the number of a
        # sub-table ("24 | Door Frame Complete" in Annexure-II), not a
        # schedule -- left as one, the rows were persisted as "Schedule 10"
        # and "Schedule 11", never bound to the item that cites the annexure,
        # and costed as scope of their own.
        annexure_only = bool(chunk_refs) and not carry and not _SCHEDULE_BANNER_RE.search(joined)
        # Rows come back in document order. A row the extractor did not
        # label takes the annexure the chunk is in at that point: the
        # carry-in first, then whichever heading the last labelled row
        # named. The last heading in the chunk is wrong for the rows before
        # it -- the tail of Annexure-II's table 30 shares a page with the
        # ANNEXURE-III heading and was stamped III.
        running_ref = None
        if chunk_refs:
            first_text = pages[idx_chunk[0]].get("text") or ""
            hm = _ANNEXURE_HEADING_RE.search(first_text)
            first_heading = normalize_annexure_ref(hm.group(1)) if hm else None
            # A chunk whose first page opens with a heading begins with that
            # annexure. A heading further down the page (rows above it
            # continue the previous table) means the chunk begins with the
            # annexure carried in, and switches when a labelled row says so.
            opens_with_heading = bool(hm) and not re.search(
                r"^\s*\d{1,4}[.)]?\s+\S", first_text[:hm.start()], re.MULTILINE
            )
            running_ref = (first_heading if opens_with_heading and first_heading in chunk_refs
                           else chunk_refs[0])
        for it in rows:
            if annexure_only and (it.get("schedule_name") or "").strip():
                it["schedule_name"] = None
            if not it.get("schedule_name") and carry:
                it["schedule_name"] = carry
            if not (it.get("schedule_name") or "").strip() and chunk_refs:
                emitted = normalize_annexure_ref(it.get("annexure"))
                if emitted in chunk_refs:
                    running_ref = emitted
                it["annexure_ref"] = running_ref
            it.pop("annexure", None)
        # Apply the same row-validity gate the pdfplumber path uses (line ~191)
        # so instruction prose ("All the bidders should ensure…") re-swept by the
        # vision reconcile pass can't slip into the persisted schedule.
        out.extend(
            it for it in rows if _is_valid_schedule_item(it, table_context=table_context)
        )
    _progress("", run_id="boq-chunks", done=True)
    _normalize_schedule_names(out)
    return out


def _extract_schedule_subtotals(pages: list[dict]) -> dict[str, float]:
    """Parse the printed per-schedule SUB-TOTAL from the NIT text (the value
    printed right after each schedule banner). Best-effort: a schedule whose
    sub-total we can't parse simply isn't reconciled (no false flags)."""
    full = "\n".join(p.get("text") or "" for p in pages)
    subtotals: dict[str, float] = {}
    banners = list(_SCHEDULE_BANNER_RE.finditer(full))
    for i, m in enumerate(banners):
        code = m.group(1).upper()
        if code in subtotals:
            continue  # first occurrence wins (banner repeats in page headers)
        start = m.end()
        end = banners[i + 1].start() if i + 1 < len(banners) else min(len(full), start + 1500)
        nm = _RUPEE_DECIMAL_RE.search(full, start, end)
        if nm:
            try:
                subtotals[code] = float(nm.group(1).replace(",", ""))
            except ValueError:
                pass
    return subtotals


def _reconcile_schedules(
    items: list[dict], subtotals: dict[str, float], tolerance_pct: float
) -> tuple[bool, list[str], set, dict[str, int], set]:
    """Compare each schedule's extracted value-sum against the NIT's printed
    sub-total. Returns (all_ok, report_lines, short_codes, over_multiples,
    dup_over_codes).
    A schedule is SHORT when it has zero extracted rows, or its extracted sum
    is below the printed sub-total by more than tolerance_pct. A schedule is
    OVER when its extracted sum is an integer multiple (>=2) of the printed
    sub-total. A schedule is in `dup_over` when its extracted sum exceeds the
    printed sub-total beyond tolerance AND it has at least one repeated
    `sr_no` among its non-tax rows — this catches NON-uniform duplication
    (e.g. some sr_nos duplicated 3x, others 2x) that isn't a clean integer
    multiple and so is missed by `over`. Clean-integer OVER schedules are
    naturally a subset of `dup_over` too. A SHORT schedule is never in
    `dup_over`. Schedules with no parsed sub-total are skipped (never
    flagged)."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    srno_counts: dict[str, dict] = {}
    for it in items:
        code = (it.get("schedule_name") or "").upper()
        # Summary placeholders are excluded from BOTH the row count and the
        # value sum. Their value equals the printed subtotal by construction,
        # so counting them makes a zero-detail schedule reconcile at +0.0%
        # (tender 3822 / session 290) and hides the miss.
        if not code or it.get("is_tax_line") or _is_summary_placeholder_row(it):
            continue
        counts[code] = counts.get(code, 0) + 1
        sr = it.get("sr_no")
        sc = srno_counts.setdefault(code, {})
        sc[sr] = sc.get(sr, 0) + 1
        val = it.get("basic_value")
        if val is None:
            q = it.get("quantity")
            r = it.get("unit_rate") if it.get("unit_rate") is not None else it.get("estimated_rate")
            if q is not None and r is not None:
                try:
                    val = float(q) * float(r)
                except (TypeError, ValueError):
                    val = None
        if val is None:
            val = it.get("amount")
        if val is not None:
            try:
                sums[code] = sums.get(code, 0.0) + float(val)
            except (TypeError, ValueError):
                pass

    report: list[str] = []
    short: set = set()
    over: dict[str, int] = {}
    dup_over: set = set()
    OVER_RATIO_TOL = 0.02  # how close sum/printed must be to an integer >= 2
    for code, printed in sorted(subtotals.items()):
        if not printed or printed <= 0:
            continue
        got = sums.get(code, 0.0)
        n = counts.get(code, 0)
        delta_pct = (printed - got) / printed * 100.0
        ratio = got / printed if printed else 0.0
        # OVER: extracted is a clean integer multiple (>=2) of printed.
        multiple = round(ratio)
        is_over = multiple >= 2 and abs(ratio - multiple) <= OVER_RATIO_TOL
        # SHORT: extracted materially below printed (existing behavior).
        is_short = (not is_over) and ((n == 0) or (delta_pct > tolerance_pct))
        if is_over:
            over[code] = multiple
        if is_short:
            short.add(code)
        # dup_over: extracted exceeds printed beyond tolerance AND the
        # schedule has at least one repeated sr_no among its non-tax rows.
        # Broader than `over` — catches non-uniform duplication too.
        exceeds = (got > printed) and (-delta_pct > tolerance_pct)
        has_repeated_srno = any(v > 1 for v in srno_counts.get(code, {}).values())
        if exceeds and has_repeated_srno:
            dup_over.add(code)
        status = "OVER x%d" % multiple if is_over else ("SHORT" if is_short else "ok")
        report.append(
            f"schedule {code}: {n} row(s), extracted Rs {got:,.2f} vs printed "
            f"Rs {printed:,.2f} (delta {delta_pct:+.1f}%) [{status}]"
        )
    return (len(short) == 0 and len(over) == 0 and len(dup_over) == 0, report, short, over, dup_over)


def _collapse_over_schedules(
    items: list[dict], over
) -> tuple[list[dict], int]:
    """Drop merged-summary artifacts and dedup value/qty twins in over-counted schedules.

    Live-prod simulation (tenders 2693/2732/2733) validated a composite rule
    that reconciles all 11 mismatched schedules to +0.0% exact, replacing the
    prior "one row per sr_no" rule (which destroyed real distinct sub-lines,
    e.g. a schedule legitimately having coded sub-items `a` and `b` under one
    `sr_no`).

    `over` may be a dict[str,int] (clean-multiple schedules, as detected by
    `_reconcile_schedules`'s `over` return) or a plain set/iterable of
    schedule codes (e.g. `set(over) | dup_over`, to also collapse
    non-uniform sr_no duplication) — only membership is used; a dict's keys
    are read, its values are ignored.

    For rows in a schedule listed in `over`, apply IN ORDER: (1) drop rows
    whose `item_code` is None/empty/whitespace — merged-summary artifacts;
    (2) among survivors, dedup exact twins within each schedule by the key
    `(sr_no, round(basic_value, 2), round(quantity, 3))`, keeping the
    first-seen row and dropping later duplicates. The twin key deliberately
    excludes `description` — twins can differ by a Unicode ligature encoding
    (e.g. `ﬁ` vs `fi`), so description is unreliable for matching. Rows in
    schedules not in `over` pass through untouched, preserving original
    order. Deterministic; no LLM/DB/IO. Returns (survivors, dropped_count).
    """
    if not over:
        return items, 0
    if isinstance(over, dict):
        over_up = {c.upper() for c in over.keys()}
    else:
        over_up = {str(c).upper() for c in over}

    def _has_code(it: dict) -> bool:
        code = it.get("item_code")
        return bool(code and str(code).strip())

    def _twin_key(sched: str, it: dict) -> tuple:
        try:
            val = round(float(it.get("basic_value")), 2)
        except (TypeError, ValueError):
            val = it.get("basic_value")
        try:
            qty = round(float(it.get("quantity")), 3)
        except (TypeError, ValueError):
            qty = it.get("quantity")
        return (sched, it.get("sr_no"), val, qty)

    out: list[dict] = []
    seen_twins: set[tuple] = set()
    dropped = 0
    for it in items:
        code = (it.get("schedule_name") or "").upper()
        if code not in over_up:
            out.append(it)
            continue
        if not _has_code(it):
            dropped += 1
            continue
        key = _twin_key(code, it)
        if key in seen_twins:
            dropped += 1
            continue
        seen_twins.add(key)
        out.append(it)
    return out, dropped


def _row_rate(it: dict):
    r = it.get("unit_rate")
    if r is None:
        r = it.get("estimated_rate")
    return r


def _row_value(it: dict) -> Optional[float]:
    """Best value estimate for a row: basic_value if non-zero, else qty*rate."""
    v = it.get("basic_value")
    try:
        if v is not None and float(v) != 0.0:
            return float(v)
    except (TypeError, ValueError):
        pass
    q, r = it.get("quantity"), _row_rate(it)
    try:
        if q is not None and r is not None:
            prod = float(q) * float(r)
            return prod if prod != 0.0 else None
    except (TypeError, ValueError):
        pass
    return None


def _is_shell(it: dict) -> bool:
    """A row carrying no value AND no rate — an empty duplicate placeholder."""
    if _row_value(it) not in (None, 0.0):
        return False
    r = _row_rate(it)
    try:
        return r is None or float(r) == 0.0
    except (TypeError, ValueError):
        return False


def _is_bare_numeric_code(code) -> bool:
    """True for NIT-faithful integer codes ('1', '001'); False for 'A1', '3a'."""
    if code is None:
        return False
    s = re.sub(r"\s+", "", str(code))
    return s.isdigit()


def _twin_survivor_rank(it: dict) -> tuple:
    """Higher tuple wins. Prefer valued rows, then bare-numeric NIT codes."""
    has_value = 1 if _row_value(it) is not None else 0
    bare = 1 if _is_bare_numeric_code(it.get("item_code")) else 0
    return (has_value, bare)


def _collapse_value_redundant_twins(
    items: list[dict],
) -> tuple[list[dict], int, list[dict]]:
    """Collapse value-redundant twin rows sharing one (schedule, sr_no).

    Merges ONLY: (A) shells absorbed into the single distinct valued row, and
    (B) non-shell rows with identical (round(value,2), round(qty,3)). Rows with
    distinct non-zero values are NEVER merged — a group with >=2 distinct
    non-zero values and no shell is left untouched and reported in the third
    return value. Tax lines pass through untouched. Pure, deterministic,
    idempotent. Returns (survivors, dropped_count, unresolvable_groups).
    """
    # Group indices by (schedule, sr_no); tax + no-sr_no rows never grouped.
    groups: dict[tuple, list[int]] = {}
    passthrough: list[int] = []
    for i, it in enumerate(items):
        sr = it.get("sr_no")
        if it.get("is_tax_line") or sr is None:
            passthrough.append(i)
            continue
        sched = (it.get("schedule_name") or "").upper()
        if not sched and it.get("annexure_ref"):
            sched = f"ANX:{it['annexure_ref']}"  # see _boq_row_key
        key = (sched, sr)
        groups.setdefault(key, []).append(i)

    drop_idx: set[int] = set()
    unresolvable: list[dict] = []

    for (sched, sr), idxs in groups.items():
        if len(idxs) < 2:
            continue
        shells = [i for i in idxs if _is_shell(items[i])]
        non_shells = [i for i in idxs if i not in shells]

        # Distinct non-zero values among non-shell members.
        val_of = {i: round(_row_value(items[i]), 2) for i in non_shells
                  if _row_value(items[i]) is not None}
        distinct_vals = set(val_of.values())

        # Inside an annexure the serial is not an identity: Annexure-II is
        # thirty sub-tables that each restart at 1, so its "(ANX:II, 1)"
        # group holds thirty different parts. There a twin is a row with the
        # same description as well; distinct descriptions are distinct rows,
        # never a conflict to report, and a shell is absorbed only by a
        # valued row of the same description.
        in_annexure = sched.startswith("ANX:")

        def _desc(i: int) -> str:
            return ((items[i].get("description") or "").strip().lower()[:40]
                    + "|" + (items[i].get("subassembly") or "").strip().lower())

        if len(distinct_vals) >= 2 and not in_annexure:
            # Legitimate distinct sub-items — never merge; report and skip.
            unresolvable.append({
                "schedule": sched, "sr_no": sr,
                "codes": [items[i].get("item_code") for i in idxs],
                "values": sorted(distinct_vals),
            })
            continue

        # Rule A — shell absorption: exactly one distinct value among non-shells.
        if in_annexure:
            valued_descs = {_desc(i) for i in val_of}
            drop_idx.update(i for i in shells if _desc(i) in valued_descs)
        elif shells and len(distinct_vals) == 1:
            drop_idx.update(shells)

        # Rule B — dedup identical-value non-shell twins by (value, qty).
        seen: dict[tuple, int] = {}
        for i in sorted(non_shells, key=lambda j: (-_twin_survivor_rank(items[j])[0],
                                                   -_twin_survivor_rank(items[j])[1], j)):
            v = val_of.get(i)
            q = items[i].get("quantity")
            try:
                qk = round(float(q), 3) if q is not None else None
            except (TypeError, ValueError):
                qk = q
            tk = (v, qk, _desc(i)) if in_annexure else (v, qk)
            if tk in seen:
                drop_idx.add(i)
            else:
                seen[tk] = i

    survivors = [it for i, it in enumerate(items) if i not in drop_idx]
    return survivors, len(drop_idx), unresolvable


def _derive_schedule_statuses(
    subtotals: dict[str, float], short: set, over: dict[str, int]
) -> dict[str, str]:
    """Map each schedule that HAS a printed subtotal to reconciled|failed.
    A schedule is 'failed' if it is SHORT or OVER; else 'reconciled'. Schedules
    with no printed subtotal are omitted (caller defaults them to 'no_anchor')."""
    short_up = {c.upper() for c in short}
    over_up = {c.upper() for c in over}
    out: dict[str, str] = {}
    for code in subtotals:
        cu = code.upper()
        out[code] = "failed" if (cu in short_up or cu in over_up) else "reconciled"
    return out


async def _extract_doc_complete(
    db: Session,
    file_path: str,
    pdfplumber_items: list[dict],
    *,
    force_ai: bool,
    legacy_incomplete: bool,
) -> tuple[list[dict], set, dict[str, float], dict[str, int]]:
    """Extract one NIT PDF's line items COMPLETELY, with reconciliation.

    Strategy (always-AI by default, see costing.boq_force_ai_extraction):
      1. Union the pdfplumber rows with a full chunked AI extraction.
      2. Reconcile each schedule's extracted value-sum against the schedule
         sub-total the NIT prints; targeted-re-extract short schedules (bounded).
      3. Last-resort: force Claude vision on still-short schedules' pages.
    Rows in schedules that remain short after all passes are flagged
    extraction_confidence='low'. Returns (items, low_confidence_schedule_codes).
    """
    items = list(pdfplumber_items)
    subtotals: dict[str, float] = {}
    over: dict[str, int] = {}
    if not (force_ai or legacy_incomplete):
        return items, set(), subtotals, over

    pages, carry_in, page_schedules = await _load_pdf_pages(db, file_path)
    if not pages:
        logger.warning(f"[boq_parser] no extractable text for {file_path}")
        return items, set(), subtotals, over

    pages_per_chunk = max(1, _get_int_setting(db, "costing.boq_pages_per_chunk", 4))
    max_retries = max(0, _get_int_setting(db, "costing.boq_chunk_max_retries", 2))
    tolerance = _get_float_setting(db, "costing.boq_reconcile_tolerance_pct", 2.0)
    max_passes = max(0, _get_int_setting(db, "costing.boq_reconcile_max_passes", 2))
    vision_fallback = _get_bool_setting(db, "costing.boq_vision_reconcile_fallback", True)

    # 1) Full chunked AI extraction, unioned with pdfplumber.
    ai_items = _dedup_boq_rows(
        await _walk_pages_chunked(
            db, pages, carry_in, list(range(len(pages))),
            pages_per_chunk=pages_per_chunk, max_retries=max_retries, file_path=file_path,
        )
    )
    if ai_items:
        base = ai_items if len(ai_items) >= len(items) else items
        other = items if base is ai_items else ai_items
        items = _merge_boq_rows(base, other)

    # 2) Reconcile against the printed schedule sub-totals.
    subtotals = _extract_schedule_subtotals(pages)
    short: set = set()
    dup_over: set = set()
    if subtotals:
        _ok, report, short, over, dup_over = _reconcile_schedules(items, subtotals, tolerance)
        for line in report:
            logger.info(f"[boq_parser] reconcile {os.path.basename(file_path)}: {line}")

        # 3) Targeted re-extraction for short schedules (smaller chunk size so
        # dense schedules don't overflow output tokens). Bounded passes.
        passes = 0
        while short and passes < max_passes:
            passes += 1
            for code in sorted(short):
                idx = [i for i, s in enumerate(page_schedules) if code in s]
                if not idx:
                    continue
                re_items = await _walk_pages_chunked(
                    db, pages, carry_in, idx,
                    pages_per_chunk=max(1, pages_per_chunk // 2),
                    max_retries=max_retries, file_path=file_path,
                )
                if re_items:
                    items = _merge_boq_rows(items, _dedup_boq_rows(re_items))
            _ok, report, short, _over, _dup_over = _reconcile_schedules(items, subtotals, tolerance)
            logger.info(
                f"[boq_parser] reconcile pass {passes} for "
                f"{os.path.basename(file_path)}: {len(short)} schedule(s) still "
                f"short ({sorted(short)})"
            )

        # 4) Last-resort: force vision on still-short schedules' pages.
        if short and vision_fallback:
            for code in sorted(short):
                idx = [i for i, s in enumerate(page_schedules) if code in s]
                if not idx:
                    continue
                logger.info(
                    f"[boq_parser] vision-reconcile schedule {code} "
                    f"({len(idx)} page(s)) for {os.path.basename(file_path)}"
                )
                vis_items = await _walk_pages_chunked(
                    db, pages, carry_in, idx,
                    pages_per_chunk=max(1, pages_per_chunk // 2),
                    max_retries=max_retries, file_path=file_path, force_vision=True,
                )
                if vis_items:
                    items = _merge_boq_rows(items, _dedup_boq_rows(vis_items))
            _ok, report, short, _over, dup_over = _reconcile_schedules(items, subtotals, tolerance)

        # OVER handling: integer-multiple duplication AND non-uniform sr_no
        # duplication (`dup_over`) from merged NIT cells. Deterministic
        # collapse (no re-extraction — re-extracting can't fix a duplication).
        # Recompute reconciliation afterward so `over`/`short`/`dup_over`
        # reflect the collapsed set for the caller's low-confidence flagging.
        collapse_codes = set(over) | dup_over
        if subtotals and collapse_codes:
            items, n_dropped = _collapse_over_schedules(items, collapse_codes)
            logger.info(
                f"[boq_parser] collapse OVER for {os.path.basename(file_path)}: "
                f"dropped {n_dropped} duplicate row(s) in {sorted(collapse_codes)}"
            )
            _ok, report, short, over, dup_over = _reconcile_schedules(items, subtotals, tolerance)

    items = _dedup_boq_rows(items)
    # Unconditional value-redundancy twin collapse. NOT gated on reconciliation:
    # empty-shell twins (A{n} with null value) leave the schedule value-sum
    # correct, so subtotal reconciliation never flags them — proven on live
    # tenders 3750 (no_anchor) and 2624 (no captured subtotal). See design
    # docs/superpowers/specs/2026-07-21-boq-twin-collapse-design.md.
    items, twin_dropped, twin_unresolvable = _collapse_value_redundant_twins(items)
    if twin_dropped:
        logger.info(
            f"[boq_parser] twin-collapse for {os.path.basename(file_path)}: "
            f"dropped {twin_dropped} value-redundant twin row(s)"
        )
    if twin_unresolvable:
        logger.warning(
            f"[boq_parser] twin-collapse for {os.path.basename(file_path)}: "
            f"{len(twin_unresolvable)} (schedule, sr_no) group(s) with distinct "
            f"non-zero values left untouched: "
            f"{[(g['schedule'], g['sr_no']) for g in twin_unresolvable]}"
        )
    # Schedules still SHORT, still over-counted (`over`), or still duplicated
    # (`dup_over`) after all passes and the collapse are the "not reconciled"
    # set — never left silently ok. Fold `over` in too so the row-level low
    # flag matches the schedule-level `failed` status for every unreconciled
    # schedule (e.g. an integer-multiple over whose renumbered twins the
    # collapse could not merge).
    short = short | dup_over | {c.upper() for c in over}
    # Flag rows in still-unreconciled schedules as low confidence.
    if short:
        for it in items:
            if (it.get("schedule_name") or "").upper() in short:
                it["extraction_confidence"] = "low"

    by_sched: dict[str, int] = {}
    for it in items:
        by_sched[it.get("schedule_name") or "?"] = by_sched.get(it.get("schedule_name") or "?", 0) + 1
    logger.info(
        f"[boq_parser] complete extraction for {os.path.basename(file_path)}: "
        f"{len(items)} row(s); by_schedule={by_sched}; "
        f"low_confidence_schedules={sorted(short) if short else '[]'}"
    )
    return items, short, subtotals, over


def _persist_schedule_totals(
    db: Session, tender_id: int, schedule_totals: list[dict],
    statuses: Optional[dict[str, str]] = None,
) -> None:
    """Persist the deterministic pass's printed per-schedule totals, deduped
    on (tender_id, schedule_code). Replaces any existing row for the same
    schedule_code (a re-parse re-reads the tender's own printed numbers, so
    the freshest read wins) rather than accumulating duplicates across
    repeated parses. First-wins for duplicate codes within a single call.
    """
    by_code: dict[str, dict] = {}
    for s in schedule_totals:
        code = (s.get("schedule_code") or "").strip().upper()
        if not code or code in by_code:
            continue
        by_code[code] = s

    if not by_code:
        return

    db.query(BOQScheduleTotal).filter(
        BOQScheduleTotal.tender_id == tender_id,
        BOQScheduleTotal.schedule_code.in_(list(by_code.keys())),
    ).delete(synchronize_session=False)

    for code, s in by_code.items():
        db.add(
            BOQScheduleTotal(
                tender_id=tender_id,
                schedule_code=code,
                stated_total=s.get("stated_total"),
                advertised_value=s.get("advertised_value"),
                title=(s.get("title") or None),
                reconciliation_status=(
                    (statuses or {}).get(code) or "no_anchor"
                ),
            )
        )
    db.commit()


# ---------------------------------------------------------------------------
# Cost-line rebind after re-parse
# ---------------------------------------------------------------------------


def _snapshot_cost_line_bindings(db: Session, tender_id: int) -> list[dict]:
    """Capture (CostBreakdownLine.id, schedule_name, item_code, sr_no) for
    rows that point to the about-to-be-deleted BOQItem rows. Returned list
    is the input to `_rebind_cost_lines` after the re-parse.
    """
    rows = (
        db.query(CostBreakdownLine)
        .join(
            BOQItem,
            BOQItem.id == CostBreakdownLine.boq_item_id,
            isouter=False,
        )
        .filter(BOQItem.tender_id == tender_id)
        .all()
    )
    snapshot: list[dict] = []
    for line in rows:
        snapshot.append(
            {
                "id": line.id,
                "schedule_name": line.schedule_name,
                "item_code": line.item_code,
                "sr_no": line.sr_no,
            }
        )
    return snapshot


def _rebind_cost_lines(
    db: Session,
    tender_id: int,
    fresh_boq: list[BOQItem],
    pre_rebind: list[dict],
) -> None:
    """After a BOQItem re-parse, walk the captured `pre_rebind` snapshot and
    re-bind each CostBreakdownLine to the matching new BOQItem by
    (schedule_name, item_code), falling back to sr_no when item_code is
    missing. Lines that can't be matched have boq_item_id cleared (the FK
    is ON DELETE SET NULL so the rows survive a clean DB but we make the
    NULLing explicit here for SQLite too).
    """
    if not pre_rebind:
        return

    # Keyed on (schedule or "", code): an annexure row has no schedule, and
    # its synthetic ANX-<ref>-<sr> code is stable across re-parses, so it
    # rebinds like any other row instead of being cleared every time.
    by_sched_code: dict[tuple, int] = {}
    by_sched_sr: dict[tuple, int] = {}
    parent_of: dict[int, Optional[int]] = {}
    for boq in fresh_boq:
        parent_of[boq.id] = boq.parent_item_id
        if boq.item_code:
            by_sched_code[(boq.schedule_name or "", boq.item_code)] = boq.id
        if boq.schedule_name and boq.sr_no:
            by_sched_sr[(boq.schedule_name, boq.sr_no)] = boq.id

    rebound = 0
    cleared = 0
    for entry in pre_rebind:
        new_id: Optional[int] = None
        if entry["item_code"]:
            new_id = by_sched_code.get((entry["schedule_name"] or "", entry["item_code"]))
        if not new_id and entry["schedule_name"] and entry["sr_no"]:
            new_id = by_sched_sr.get((entry["schedule_name"], entry["sr_no"]))

        line = db.query(CostBreakdownLine).filter(CostBreakdownLine.id == entry["id"]).first()
        if not line:
            continue
        if new_id:
            line.boq_item_id = new_id
            # The component link follows the fresh row: the parent's id
            # changed in the same re-parse.
            line.parent_boq_item_id = parent_of.get(new_id)
            rebound += 1
        else:
            line.boq_item_id = None
            line.parent_boq_item_id = None
            cleared += 1

    if rebound or cleared:
        db.commit()
        logger.info(
            f"[boq_parser] tender {tender_id}: rebound {rebound} cost lines, "
            f"cleared {cleared} stale bindings"
        )


# ---------------------------------------------------------------------------
# What the capture found -- said to the user, not only to the log
# ---------------------------------------------------------------------------


def _published_rate(row: BOQItem) -> Optional[float]:
    """The NIT's rate per bidding unit for a schedule row: the printed unit
    rate, else basic value over quantity."""
    try:
        if row.estimated_rate is not None and float(row.estimated_rate) > 0:
            return round(float(row.estimated_rate), 2)
        if row.basic_value and row.quantity and float(row.quantity) > 0:
            return round(float(row.basic_value) / float(row.quantity), 2)
    except (TypeError, ValueError):
        pass
    return None


def schedule_capture_report(db: Session, tender_id: Optional[int]) -> dict:
    """What the tender's captured schedule is made of, from the rows.

    Per source document: how many rows it contributed. For the annexures: the
    ones the schedule items cite, the ones that arrived, the ones missing, and
    which schedule item each captured annexure was bound to. Computed from
    the persisted BOQItem rows so it is true of the schedule costing will
    actually use, whichever run captured it. Empty dict when there is no
    schedule.
    """
    if not tender_id:
        return {}
    rows = db.query(BOQItem).filter(BOQItem.tender_id == tender_id).all()
    if not rows:
        return {}

    doc_names: dict[int, str] = {}
    doc_ids = {r.source_document_id for r in rows if r.source_document_id is not None}
    if doc_ids:
        for d in db.query(TenderDocument).filter(TenderDocument.id.in_(list(doc_ids))).all():
            doc_names[d.id] = d.file_name or f"document #{d.id}"

    documents: dict[Optional[int], dict] = {}
    for r in rows:
        entry = documents.setdefault(r.source_document_id, {
            "document_id": r.source_document_id,
            "name": doc_names.get(r.source_document_id) or (
                "(captured before documents were recorded)"
                if r.source_document_id is None else f"document #{r.source_document_id}"
            ),
            "rows": 0, "schedules": set(), "annexures": {},
        })
        entry["rows"] += 1
        if r.schedule_name:
            entry["schedules"].add(r.schedule_name)
        elif r.annexure_ref:
            entry["annexures"][r.annexure_ref] = entry["annexures"].get(r.annexure_ref, 0) + 1

    by_id = {r.id: r for r in rows}
    cited: dict[str, list[BOQItem]] = {}
    for r in rows:
        if r.schedule_name:
            for ref in _cited_annexures(r.description):
                cited.setdefault(ref, []).append(r)
    captured: dict[str, int] = {}
    linked: dict[str, dict] = {}
    for r in rows:
        if r.schedule_name or not r.annexure_ref:
            continue
        captured[r.annexure_ref] = captured.get(r.annexure_ref, 0) + 1
        if r.parent_item_id and r.parent_item_id in by_id:
            p = by_id[r.parent_item_id]
            entry = linked.setdefault(r.annexure_ref, {
                "annexure": r.annexure_ref, "rows": 0,
                "parent_schedule": p.schedule_name,
                "parent_code": p.item_code or str(p.sr_no),
                "parent_description": (p.description or "")[:120],
                "parent_quantity": p.quantity, "parent_unit": p.unit,
                # The annexure prints a value per row and the schedule item
                # prints a rate per set: their ratio is how much of the
                # breakdown was read. Annexure-II's 176 rows summed to 99% of
                # the published Rs 8,16,051 -- 80 rows short would show as 45%.
                "published_rate": _published_rate(p),
                "captured_value": 0.0,
            })
            entry["rows"] += 1
            try:
                entry["captured_value"] = round(entry["captured_value"] + float(r.basic_value or 0), 2)
            except (TypeError, ValueError):
                pass
    for entry in linked.values():
        pr, cv = entry.get("published_rate"), entry.get("captured_value")
        entry["coverage_pct"] = round(cv / pr * 100, 1) if pr and cv else None

    cited_refs = sorted(cited, key=_annexure_sort_key)
    return {
        "row_count": len(rows),
        "documents": [
            {**d, "schedules": sorted(d["schedules"])}
            for d in sorted(documents.values(), key=lambda d: (d["document_id"] is None, d["document_id"] or 0))
        ],
        "annexures": {
            "cited": cited_refs,
            "captured": {k: captured[k] for k in sorted(captured, key=_annexure_sort_key)},
            "missing": [r for r in cited_refs if r not in captured],
            "unlinked": sorted((r for r in captured if r not in cited), key=_annexure_sort_key),
            "linked": [linked[k] for k in sorted(linked, key=_annexure_sort_key)],
        },
    }


# ---------------------------------------------------------------------------
# Template matching (unchanged)
# ---------------------------------------------------------------------------


def match_costing_template(
    db: Session,
    zone: Optional[str] = None,
) -> Optional[CostingTemplate]:
    """Find the best matching costing template for the given zone, falling
    back to the default template when no zone match exists.
    """
    if zone:
        template = (
            db.query(CostingTemplate)
            .filter(CostingTemplate.zone == zone.upper())
            .first()
        )
        if template:
            return template

    return (
        db.query(CostingTemplate)
        .filter(CostingTemplate.is_default == True)  # noqa: E712
        .first()
    )
