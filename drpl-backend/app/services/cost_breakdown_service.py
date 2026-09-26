"""
DRPL Backend - Cost Breakdown Service

Persists costing-agent output as editable CostBreakdown rows, recomputes totals
when the user edits lines, and regenerates XLSX artifacts on demand.

Range mode: when the costing agent provides low/expected/high rate bands per
line plus per-line profit_pct, the service stores all three bands and the
per-line profit so the editor + XLSX export can surface the estimation range.
The expected band is mirrored into the legacy single-value columns (rate,
amount, subtotal, margin_amount, etc.) so existing consumers keep working
without a contract change.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine
from app.models.costing_template import BOQItem, BOQScheduleTotal
from app.services.costing_format_service import get_costing_defaults
from app.services.costing.nit_reconciliation import reconcile
from app.services.costing.nit_schedule_parser import NITLine, NITSchedule, ParsedNIT
from app.worker.preview_tasks import enqueue_preview_render

logger = logging.getLogger(__name__)


_NEEDS_INPUT_SOURCES = {"needs_user_input", "needs_input"}


def _line_needs_input(line: dict) -> bool:
    """A line needs input if its source says so OR it has no rate at all."""
    src = (line.get("rate_source") or "").lower()
    if src in _NEEDS_INPUT_SOURCES:
        return True
    # No priced rate in any band
    return all(
        line.get(k) in (None, "")
        for k in ("rate", "rate_expected", "rate_low", "rate_high")
    )


def _coerce_float(val) -> Optional[float]:
    """Best-effort numeric conversion; None on failure."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _coerce_int(val, default: int = 0) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _coerce_id(val) -> Optional[int]:
    """A foreign-key value: a positive int, else None."""
    try:
        n = int(val) if val is not None and val != "" else None
    except (TypeError, ValueError):
        return None
    return n if n and n > 0 else None


def _is_component_line(line) -> bool:
    """A line that is one material of the schedule item it points at (see
    CostBreakdownLine.parent_boq_item_id). Works on a dict or an ORM row."""
    if isinstance(line, dict):
        return _coerce_id(line.get("parent_boq_item_id")) is not None
    return getattr(line, "parent_boq_item_id", None) is not None


_ROMAN_ORDER = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _annexure_order(ref: Optional[str]) -> tuple:
    """Sort key for an annexure label: roman numerals by value (I, II, ...
    VII), digits by value, letters after, unlabelled last."""
    s = (ref or "").strip().upper()
    if not s:
        return (3, 0, "")
    if s.isdigit():
        return (0, int(s), "")
    if re.fullmatch(r"[IVXLC]+", s):
        total, prev = 0, 0
        for ch in reversed(s):
            v = _ROMAN_ORDER[ch]
            total += -v if v < prev else v
            prev = max(prev, v)
        return (0, total, "")
    return (1, 0, s)


def _schedule_order(name: Optional[str]) -> tuple:
    """Sort key for a schedule code: A, A1, A7, B, B7 ... by letters then number."""
    s = (name or "").strip().upper()
    m = re.match(r"^([A-Z]*)(\d*)$", s)
    if not m:
        return (1, s, 0)
    return (0, m.group(1), int(m.group(2)) if m.group(2) else 0)


def _line_field(line, key: str):
    return line.get(key) if isinstance(line, dict) else getattr(line, key, None)


def _num_or_none(v) -> Optional[float]:
    try:
        return float(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def order_lines_for_display(lines) -> list:
    """The order the editor, the workbook and the reply show a breakdown in.

    The NIT schedules first, in schedule order, each in the tender's own
    serial order; then the annexure component groups in annexure order
    (Annexure-I, then II, ... VII), each in the order its table was read --
    NOT by serial, because Annexure-II restarts its serial at 1 for every
    sub-assembly and a serial sort interleaves thirty tables into one.
    Unlinked annexure rows follow in the same order; anything else last.

    The ORM relationship orders lines by serial alone, which put a component
    with serial 1 above Schedule A, and "Schedule 11, B, A, 10" on the sheet.
    Stable on the incoming order, so ties keep the persisted sequence.
    """
    def key(pair):
        idx, ln = pair
        sched = (_line_field(ln, "schedule_name") or "").strip()
        anx = _line_field(ln, "annexure_ref")
        boq_id = _coerce_id(_line_field(ln, "boq_item_id"))
        sr = _num_or_none(_line_field(ln, "sr_no"))
        if _is_component_line(ln):
            return (1, _annexure_order(anx), (0, boq_id if boq_id is not None else 10**9), idx)
        if sched:
            return (0, _schedule_order(sched), (sr if sr is not None else 10**9, idx), idx)
        if anx:
            return (2, _annexure_order(anx), (0, boq_id if boq_id is not None else 10**9), idx)
        return (3, (0, "", 0), (sr if sr is not None else 10**9, idx), idx)

    return [ln for _, ln in sorted(enumerate(list(lines)), key=key)]


# The label a component group renders under in the editor and the workbook.
# It says what the rows are and where their cost goes, because the group's
# own total is deliberately NOT part of the breakdown's totals.
def component_group_label(annexure_ref: Optional[str], parent_schedule: Optional[str],
                          parent_code: Optional[str], parent_sr) -> str:
    ref = f"Annexure-{annexure_ref}" if annexure_ref else "Annexure"
    item = (parent_code or "").strip() or (str(parent_sr) if parent_sr is not None else "?")
    sched = f"Schedule {parent_schedule}" if parent_schedule else "the schedule"
    return f"{ref} — components of {sched} item {item} (per set; rolled into that item)"


def _resolve_line_amounts(
    qty: Optional[float],
    rate: Optional[float],
    amount: Optional[float],
    rate_low: Optional[float],
    rate_high: Optional[float],
    amount_low: Optional[float],
    amount_high: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (amount_low, amount_expected, amount_high) for one line.

    Mirrors `_resolve_amounts` in cost_calculator_tool but operates on the
    flatter set of fields the persistence layer sees. Falls back across bands
    so we always end up with a usable expected value when any rate is given.
    """
    def _amt(amount_val: Optional[float], rate_val: Optional[float]) -> Optional[float]:
        if amount_val is not None:
            return float(amount_val)
        if rate_val is not None and qty is not None:
            return round(float(qty) * float(rate_val), 2)
        return None

    a_low = _amt(amount_low, rate_low)
    a_exp = _amt(amount, rate)
    a_high = _amt(amount_high, rate_high)

    if a_exp is None:
        if a_low is not None and a_high is not None:
            a_exp = round((a_low + a_high) / 2.0, 2)
        elif a_low is not None:
            a_exp = a_low
        elif a_high is not None:
            a_exp = a_high
        else:
            return None, None, None

    if a_low is None:
        a_low = a_exp
    if a_high is None:
        a_high = a_exp

    if a_low > a_high:
        a_low, a_high = a_high, a_low

    return round(a_low, 2), round(a_exp, 2), round(a_high, 2)


def _compute_totals(
    lines: list[dict],
    overhead_percent: float,
    margin_percent: float,
    gst_percent: float,
) -> dict:
    """Recompute the three-band totals (low / expected / high) from lines.

    Lines marked needs_input are excluded from totals (the user hasn't priced
    them). Per-line profit is taken from `profit_pct` when set; otherwise the
    breakdown-level margin_percent is used as the per-line profit fallback.

    Phase 3b: also rolls up `tender_total` + `margin_total_*` across lines
    that have `tender_rate` (margin-analysis mode). Lines without tender_rate
    are excluded from the margin roll-up — letting mixed costings (some BOQ
    lines, some pure cost-only) produce a clean margin view of just the
    BOQ-priced lines.
    """
    sub_low = sub_exp = sub_high = 0.0
    profit_low = profit_exp = profit_high = 0.0
    tender_total = 0.0
    margin_total_low = margin_total_exp = margin_total_high = 0.0
    margin_lines = 0
    needs_input_count = 0

    for line in lines:
        if _is_component_line(line):
            # A component's amount is a per-set cost that has already been
            # rolled into its parent's rate (`rollup_component_parents`).
            # Counting it here too is the double count this link exists to
            # prevent; an unpriced component is the parent's note to carry,
            # not a needs-input line of its own.
            continue
        if _line_needs_input(line):
            needs_input_count += 1
            continue
        qty = _coerce_float(line.get("quantity"))
        a_low, a_exp, a_high = _resolve_line_amounts(
            qty=qty,
            rate=_coerce_float(line.get("rate")),
            amount=_coerce_float(line.get("amount")),
            rate_low=_coerce_float(line.get("rate_low")),
            rate_high=_coerce_float(line.get("rate_high")),
            amount_low=_coerce_float(line.get("amount_low")),
            amount_high=_coerce_float(line.get("amount_high")),
        )
        if a_exp is None:
            needs_input_count += 1
            continue
        sub_low += a_low or 0.0
        sub_exp += a_exp or 0.0
        sub_high += a_high or 0.0

        line_pct = _coerce_float(line.get("profit_pct"))
        if line_pct is None:
            line_pct = margin_percent
        profit_low += (a_low or 0.0) * line_pct / 100.0
        profit_exp += (a_exp or 0.0) * line_pct / 100.0
        profit_high += (a_high or 0.0) * line_pct / 100.0

        # Margin roll-up — only when the line has a tender_rate / tender_amount.
        t_amount = _coerce_float(line.get("tender_amount"))
        if t_amount is None:
            t_rate = _coerce_float(line.get("tender_rate"))
            if t_rate is not None and qty is not None:
                t_amount = round(t_rate * qty, 2)
        if t_amount is not None:
            tender_total += t_amount
            margin_total_low += t_amount - (a_high or 0.0)   # high cost band → low margin
            margin_total_exp += t_amount - (a_exp or 0.0)
            margin_total_high += t_amount - (a_low or 0.0)
            margin_lines += 1

    def _band(subtotal: float, profit_sum: float) -> dict:
        overhead_amount = subtotal * (overhead_percent / 100.0)
        sub_with_overhead = subtotal + overhead_amount
        margin_on_overhead = overhead_amount * (margin_percent / 100.0)
        margin_amount = profit_sum + margin_on_overhead
        pre_tax = sub_with_overhead + margin_amount
        gst_amount = pre_tax * (gst_percent / 100.0)
        grand_total = pre_tax + gst_amount
        return {
            "subtotal": round(subtotal, 2),
            "overhead_amount": round(overhead_amount, 2),
            "margin_amount": round(margin_amount, 2),
            "gst_amount": round(gst_amount, 2),
            "grand_total": round(grand_total, 2),
        }

    low = _band(sub_low, profit_low)
    expected = _band(sub_exp, profit_exp)
    high = _band(sub_high, profit_high)

    has_margin = margin_lines > 0
    margin_pct = (
        round(margin_total_exp / tender_total * 100.0, 2)
        if has_margin and tender_total else None
    )

    return {
        # Expected band — mirrors the legacy single-value totals
        "subtotal": expected["subtotal"],
        "overhead_amount": expected["overhead_amount"],
        "margin_amount": expected["margin_amount"],
        "gst_amount": expected["gst_amount"],
        "grand_total": expected["grand_total"],
        # Range bands
        "subtotal_low": low["subtotal"],
        "subtotal_high": high["subtotal"],
        "overhead_amount_low": low["overhead_amount"],
        "overhead_amount_high": high["overhead_amount"],
        "margin_amount_low": low["margin_amount"],
        "margin_amount_high": high["margin_amount"],
        "gst_amount_low": low["gst_amount"],
        "gst_amount_high": high["gst_amount"],
        "grand_total_low": low["grand_total"],
        "grand_total_high": high["grand_total"],
        # Margin-analysis roll-up (Phase 3b) — None when no tender_rate lines
        "tender_total": round(tender_total, 2) if has_margin else None,
        "margin_total_low": round(margin_total_low, 2) if has_margin else None,
        "margin_total": round(margin_total_exp, 2) if has_margin else None,
        "margin_total_high": round(margin_total_high, 2) if has_margin else None,
        "margin_pct": margin_pct,
        "needs_input_count": needs_input_count,
    }


def _normalize_line_dict(
    line: dict,
    sr_no: int,
    breakdown_margin_percent: float,
) -> dict:
    """Coerce arbitrary input into a clean line dict suitable for persistence.

    Resolves all three amount bands (low/expected/high) and the per-line profit
    amounts from whatever fields the agent or editor supplied. Computes the
    expected amount from quantity × rate when missing.
    """
    # `quantity` wins over the legacy `qty` alias -- by presence, not truth: a
    # component the material list totals at Rs 0.00 has quantity 0, and
    # `quantity or qty` turned that into None (an empty Qty cell in the
    # workbook and a row the Summary counted as unpriced).
    qty = _coerce_float(line.get("quantity") if line.get("quantity") is not None else line.get("qty"))

    # Rates — accept either single `rate` (legacy / editor-edited expected) or
    # explicit ranges from the agent.
    rate = _coerce_float(line.get("rate"))
    rate_low = _coerce_float(line.get("rate_low"))
    rate_high = _coerce_float(line.get("rate_high"))
    # Allow `rate_expected` as an alias for `rate` (agent uses the longer name)
    if rate is None:
        rate = _coerce_float(line.get("rate_expected"))

    # Amounts — same alias treatment
    amount = _coerce_float(line.get("amount"))
    amount_low = _coerce_float(line.get("amount_low"))
    amount_high = _coerce_float(line.get("amount_high"))
    if amount is None:
        amount = _coerce_float(line.get("amount_expected"))

    needs_input = _line_needs_input({
        "rate_source": line.get("rate_source"),
        "rate": rate,
        "rate_expected": rate,
        "rate_low": rate_low,
        "rate_high": rate_high,
    })

    # Phase 3b — tender / margin / schedule fields (margin-analysis mode)
    tender_rate = _coerce_float(line.get("tender_rate"))
    tender_amount = _coerce_float(line.get("tender_amount"))
    # escalation_pct is parsed further below (escalation_pct_val), but we need
    # it here too since tender_amount must be escalation-inclusive to match
    # the NIT's own stated_total / the xlsx Tender Amount formula
    # (qty*tender_rate*(1+escl/100)). `tender_amount` is NIT-authoritative and
    # must NEVER come from the LLM: whenever we have a tender_rate (and qty)
    # to derive it from, we ALWAYS recompute it here, discarding whatever the
    # caller passed in. We only fall back to an incoming `tender_amount` when
    # there is no tender_rate/qty to derive it from (freeform / non-NIT lines
    # that have no NIT-captured rate at all).
    _escl_for_tender_amount = _coerce_float(line.get("escalation_pct")) or 0.0
    if tender_rate is not None and qty not in (None, 0):
        tender_amount = round(tender_rate * qty * (1 + _escl_for_tender_amount / 100.0), 2)
    schedule_section = (line.get("schedule_section") or "").strip() or None
    cost_buildup_note = (line.get("cost_buildup_note") or "").strip() or None

    if needs_input:
        rate = rate_low = rate_high = None
        amount = amount_low = amount_high = None
        profit_pct = None
        p_low = p_exp = p_high = None
        # Margin fields — keep tender_rate / tender_amount visible (they came
        # from the tender, not from us), but null out the margin since we
        # haven't priced this line yet.
        m_low = m_exp = m_high = None
        margin_pct = None
    else:
        a_low, a_exp, a_high = _resolve_line_amounts(
            qty, rate, amount, rate_low, rate_high, amount_low, amount_high,
        )
        amount_low, amount, amount_high = a_low, a_exp, a_high

        # Backfill rate fields from amount/qty when only the amount was given
        if rate is None and qty not in (None, 0) and amount is not None:
            rate = round(amount / qty, 4)
        if rate_low is None and qty not in (None, 0) and amount_low is not None:
            rate_low = round(amount_low / qty, 4)
        if rate_high is None and qty not in (None, 0) and amount_high is not None:
            rate_high = round(amount_high / qty, 4)

        # Per-line profit — keep null when the agent didn't set it. The
        # editor / total-computation path falls back to the breakdown's global
        # margin_percent at display time, so the user's global slider actually
        # moves the line. We do NOT bake the global default into the row at
        # persist time (that's the bug that made global margin appear to "do
        # nothing" — every line already had its own profit_pct frozen in).
        profit_pct = _coerce_float(line.get("profit_pct"))  # may stay None
        p_low = _coerce_float(line.get("profit_amount_low"))
        p_exp = _coerce_float(
            line.get("profit_amount_expected") or line.get("profit_amount")
        )
        p_high = _coerce_float(line.get("profit_amount_high"))
        if profit_pct is not None:
            # Only fill profit amounts if this line has its own explicit
            # override percentage. Otherwise leave them null and let the editor
            # (or total computation) derive them from the global margin.
            if p_low is None and amount_low is not None:
                p_low = round(amount_low * profit_pct / 100.0, 2)
            if p_exp is None and amount is not None:
                p_exp = round(amount * profit_pct / 100.0, 2)
            if p_high is None and amount_high is not None:
                p_high = round(amount_high * profit_pct / 100.0, 2)

        # Margin fields — accept what the agent supplied; otherwise compute
        # from tender_amount - amount_band so the editor never has to.
        m_low = _coerce_float(line.get("margin_amount_low"))
        m_exp = _coerce_float(line.get("margin_amount") or line.get("margin_amount_expected"))
        m_high = _coerce_float(line.get("margin_amount_high"))
        margin_pct = _coerce_float(line.get("margin_pct"))
        if tender_amount is not None:
            if m_low is None and amount_high is not None:
                m_low = round(tender_amount - amount_high, 2)   # high cost → low margin
            if m_exp is None and amount is not None:
                m_exp = round(tender_amount - amount, 2)
            if m_high is None and amount_low is not None:
                m_high = round(tender_amount - amount_low, 2)
            if margin_pct is None and m_exp is not None and tender_amount:
                margin_pct = round(m_exp / tender_amount * 100.0, 2)

    # NIT-mirror fields (RULE 5). Surface them verbatim from the agent's
    # output so the cost-breakdown editor + XLSX/PDF exports can render the
    # row in its exact NIT shape. Tax lines (is_tax_line=true) carry no
    # cost-side rate/amount — the agent passes them through unchanged.
    boq_item_id = line.get("boq_item_id")
    try:
        boq_item_id = int(boq_item_id) if boq_item_id is not None else None
    except (TypeError, ValueError):
        boq_item_id = None
    item_code = (line.get("item_code") or "").strip() or None
    schedule_name = (line.get("schedule_name") or "").strip() or None
    bidding_unit = (line.get("bidding_unit") or "").strip() or None
    basic_value = _coerce_float(line.get("basic_value"))
    escalation_pct_val = _coerce_float(line.get("escalation_pct"))
    is_tax_line = bool(line.get("is_tax_line"))

    # Tax lines are not costed. Force rate/amount to null even if the agent
    # tried to fill them — RULE 5 says tax rows pass through unchanged.
    if is_tax_line:
        rate = rate_low = rate_high = None
        amount = amount_low = amount_high = None
        profit_pct = None
        p_low = p_exp = p_high = None
        m_low = m_exp = m_high = None
        margin_pct = None
        needs_input = False  # not a "needs user input" line — it's a tax line

    return {
        "sr_no": sr_no,
        "description": (line.get("description") or "").strip(),
        "category": (line.get("category") or "").strip() or None,
        "quantity": qty,
        "unit": (line.get("unit") or "").strip() or None,
        "rate": rate,
        "amount": amount,
        "rate_low": rate_low,
        "rate_high": rate_high,
        "amount_low": amount_low,
        "amount_high": amount_high,
        "profit_pct": profit_pct,
        "profit_amount_low": p_low,
        "profit_amount": p_exp,
        "profit_amount_high": p_high,
        # Phase 3b margin-analysis fields
        "tender_rate": tender_rate,
        "tender_amount": tender_amount,
        "margin_amount_low": m_low,
        "margin_amount": m_exp,
        "margin_amount_high": m_high,
        "margin_pct": margin_pct,
        "schedule_section": schedule_section,
        "cost_buildup_note": cost_buildup_note,
        # NIT-mirror fields (RULE 5)
        "boq_item_id": boq_item_id,
        "item_code": item_code,
        "schedule_name": schedule_name,
        "bidding_unit": bidding_unit,
        "basic_value": basic_value,
        "escalation_pct": escalation_pct_val,
        "is_tax_line": is_tax_line,
        # Component build-up grouping ("A".."L") for the client-annexure layout.
        "annexure": (line.get("annexure") or "").strip() or None,
        # Annexure component link (see CostBreakdownLine.parent_boq_item_id).
        "parent_boq_item_id": _coerce_id(line.get("parent_boq_item_id")),
        "annexure_ref": (line.get("annexure_ref") or "").strip() or None,
        "rate_source": (line.get("rate_source") or "").strip() or None,
        "source_ref": (line.get("source_ref") or "").strip() or None,
        "oem_manufacturer": (line.get("oem_manufacturer") or "").strip() or None,
        "source_url": (line.get("source_url") or "").strip() or None,
        "confidence": (line.get("confidence") or "").strip() or None,
        "needs_input": needs_input,
        "notes": (line.get("notes") or "").strip() or None,
    }


def validate_nit_mirror(
    db: Session,
    tender_id: int,
    line_items: list[dict],
) -> list[str]:
    """Validate that the agent's output 1:1-mirrors the captured NIT schedule
    (RULE 5). Returns a list of human-readable error strings — empty when
    valid, OR when no NIT schedule was captured (legacy / freeform tender).

    Checks performed when a captured schedule exists for `tender_id`:
      - Same row count
      - Same `(schedule_name, item_code)` set, with no duplicates
      - Each agent row's `boq_item_id` points to a real BOQItem of this tender
      - Tax-line passthrough: rows the schedule flagged as `is_tax_line`
        must have `is_tax_line=True` and `rate / amount = null` in the output

    The caller decides what to do with the errors — log + surface in the UI,
    or re-prompt the agent for a self-correction retry.
    """
    schedule_rows = (
        db.query(BOQItem)
        .filter(BOQItem.tender_id == tender_id)
        .order_by(BOQItem.schedule_name.asc().nullsfirst(), BOQItem.sr_no.asc())
        .all()
    )
    if not schedule_rows:
        return []  # No captured schedule → nothing to validate.

    expected_keys = {
        (row.schedule_name or "?", row.item_code or str(row.sr_no))
        for row in schedule_rows
    }
    valid_boq_ids = {row.id for row in schedule_rows}
    tax_boq_ids = {row.id for row in schedule_rows if row.is_tax_line}

    errors: list[str] = []

    if len(line_items) != len(schedule_rows):
        errors.append(
            f"Row count mismatch — schedule has {len(schedule_rows)} rows, "
            f"agent output has {len(line_items)} rows (RULE 5: 1:1 mirror)."
        )

    seen_keys: set = set()
    for idx, line in enumerate(line_items):
        sched = (line.get("schedule_name") or "").strip() or "?"
        code = (line.get("item_code") or "").strip() or str(line.get("sr_no") or idx + 1)
        key = (sched, code)
        if key in seen_keys:
            errors.append(
                f"Row {idx + 1}: duplicate (schedule_name={sched!r}, item_code={code!r})"
            )
        seen_keys.add(key)
        if key not in expected_keys:
            errors.append(
                f"Row {idx + 1}: (schedule_name={sched!r}, item_code={code!r}) "
                f"is not in the captured schedule — RULE 5 forbids adding rows."
            )

        bid = line.get("boq_item_id")
        if bid is not None and int(bid) not in valid_boq_ids:
            errors.append(
                f"Row {idx + 1}: boq_item_id={bid} does not belong to tender {tender_id}."
            )

        # Tax-line passthrough — agent must not cost a schedule tax row.
        if bid and int(bid) in tax_boq_ids:
            if line.get("rate") is not None or line.get("amount") is not None:
                errors.append(
                    f"Row {idx + 1}: tax line (boq_item_id={bid}) has rate/amount set — "
                    f"RULE 5 requires rate=null, amount=null for is_tax_line rows."
                )
            if not bool(line.get("is_tax_line")):
                errors.append(
                    f"Row {idx + 1}: tax line (boq_item_id={bid}) is missing "
                    f"`is_tax_line: true` in the agent output."
                )

    missing_keys = expected_keys - seen_keys
    for sched, code in sorted(missing_keys):
        errors.append(
            f"Schedule row (schedule_name={sched!r}, item_code={code!r}) "
            f"missing from agent output — RULE 5 forbids dropping rows."
        )

    return errors


def _partition_lines_against_schedule(
    db: Session,
    tender_id: int,
    line_items: list[dict],
) -> tuple[list[dict], list[str], set]:
    """Split an agent's raw `line_items` into (kept, quarantine_notes) using the
    captured NIT schedule (BOQItem rows) as ground truth — the fabrication
    lockdown gate (Task 7).

    A line is matched back to a captured BOQItem row using the SAME
    fallback chain `merge_batch_rates` uses so this gate is never stricter
    than the rest of the pipeline: `boq_item_id` → exact
    `(schedule_name.strip(), item_code.strip())` → `_norm_code`-normalised
    `(schedule_name, item_code)` (drift-tolerant: "A-1"/" a1 "/"A1" collapse).
    Matching against `sr_no` is only used when the line has no item_code at
    all (mirrors the existing "code or sr_no" identity used elsewhere).

    A line is QUARANTINED (dropped from the returned list, recorded as a
    human-readable note instead) when:
      - it matches NONE of the above against any captured BOQItem row for
        this tender — the agent invented a line the NIT never had (e.g.
        "Robotic AC duct cleaning"), or
      - it matches a BOQItem row already claimed by an earlier line in the
        same output (the agent emitted the same NIT row twice).

    For every KEPT line that matches a captured row, `escalation_pct` is force-
    overwritten with that row's own value — the agent may price the line but
    may never invent its own escalation percentage (Task 7c). Other
    NIT-authoritative identity fields are left as the agent supplied them
    since they were already required to match for the key lookup to succeed.

    No-op (all lines kept, unchanged) when the tender has no captured
    schedule — nothing to validate against (legacy / freeform costing).
    """
    schedule_rows = (
        db.query(BOQItem)
        .filter(BOQItem.tender_id == tender_id)
        .all()
    )
    if not schedule_rows:
        return line_items, [], set()

    def _row_key(row: BOQItem) -> tuple:
        return (
            (row.schedule_name or "?").strip() or "?",
            (row.item_code or "").strip() or str(row.sr_no),
        )

    by_boq: dict[int, BOQItem] = {}
    by_key: dict[tuple, BOQItem] = {}
    by_code_norm: dict[tuple, BOQItem] = {}
    for row in schedule_rows:
        if row.id is not None:
            by_boq[int(row.id)] = row
        by_key[_row_key(row)] = row
        if row.item_code:
            by_code_norm.setdefault(
                (_norm_code(row.schedule_name), _norm_code(row.item_code)), row
            )

    kept: list[dict] = []
    quarantine_notes: list[str] = []
    seen_row_ids: set = set()
    for idx, line in enumerate(line_items):
        sched = (line.get("schedule_name") or "").strip() or "?"
        code = (line.get("item_code") or "").strip() or str(line.get("sr_no") or idx + 1)
        desc = (line.get("description") or "").strip() or "(no description)"

        row: Optional[BOQItem] = None
        bid = line.get("boq_item_id")
        if bid is not None:
            try:
                row = by_boq.get(int(bid))
            except (TypeError, ValueError):
                row = None
        if row is None:
            row = by_key.get((sched, code))
        if row is None:
            nsched = _norm_code(line.get("schedule_name"))
            ncode = _norm_code(line.get("item_code"))
            if nsched and ncode:
                row = by_code_norm.get((nsched, ncode))

        if row is None:
            quarantine_notes.append(
                f"⚠️ FABRICATED LINE DROPPED: \"{desc}\" (schedule={sched!r}, "
                f"item_code={code!r}) is not part of the captured NIT schedule "
                f"— the agent invented this line and it was excluded from the "
                f"cost breakdown."
            )
            continue
        if row.id in seen_row_ids:
            quarantine_notes.append(
                f"⚠️ DUPLICATE LINE DROPPED: \"{desc}\" (schedule={sched!r}, "
                f"item_code={code!r}) duplicates an already-kept NIT row — "
                f"only the first occurrence was persisted."
            )
            continue
        seen_row_ids.add(row.id)

        # RULE 5c — block agent-invented escalation. The NIT's own Escl.(%)
        # (captured verbatim on the BOQItem row) is the only value that may
        # ever reach the persisted line; the agent's own emitted
        # `escalation_pct` (if any) is discarded here.
        line = dict(line)
        line["escalation_pct"] = row.escalation_pct
        kept.append(line)

    missing_keys: set = set()
    for row in sorted(schedule_rows, key=_row_key):
        if row.id not in seen_row_ids:
            sched, code = _row_key(row)
            missing_keys.add((sched, code))
            quarantine_notes.append(
                f"⚠️ SCHEDULE ROW MISSING FROM AGENT OUTPUT: (schedule={sched!r}, "
                f"item_code={code!r}) was captured in the NIT but the agent did "
                f"not return a line for it — it stays absent from this breakdown "
                f"version; re-run costing to fill it in."
            )

    return kept, quarantine_notes, missing_keys


# rate_source evidence ranking (best → weakest). Higher wins a content-dup tie.
_RATE_SOURCE_RANK = {
    "training_data": 6,
    "tender_estimate": 5,
    "web_search": 4,
    "memory": 3,
    "derived_estimate": 2,
    "needs_user_input": 1,
}


def _normalize_desc(s: Optional[str]) -> str:
    """Lowercase, collapse internal whitespace, strip surrounding punctuation."""
    import re
    text = (s or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,:;-_/")


def _content_key(line: dict) -> Optional[tuple]:
    """Content identity for dedup: (norm description, qty, norm unit).

    Returns None when the line must never be merged (blank/placeholder
    description). Callers skip tax lines separately.
    """
    desc = _normalize_desc(line.get("description"))
    if not desc or desc == "(no description)":
        return None
    qty = _coerce_float(line.get("quantity") if line.get("quantity") is not None
                        else line.get("qty"))
    qty_key = round(qty, 3) if qty is not None else None
    unit = _normalize_desc(line.get("unit"))
    return (desc, qty_key, unit)


def _source_rank(line: dict) -> int:
    return _RATE_SOURCE_RANK.get((line.get("rate_source") or "").strip().lower(), 0)


def _dedup_lines_by_content(line_items: list[dict]) -> tuple[list[dict], int]:
    """Collapse lines describing the same physical work item, keeping the
    best-evidence occurrence.

    Two lines are "the same work" when their content key
    (normalized description + quantity + normalized unit) is equal. Tax lines
    and lines with a blank/placeholder description are never merged. When a
    group has more than one line, the one with the highest `rate_source` rank
    wins (ties → first occurrence); it is placed at the position of the group's
    FIRST occurrence so surviving order is stable.

    Returns (survivors, dropped_count).
    """
    # first_index[key] = position in `survivors` of the current winner for key.
    first_index: dict[tuple, int] = {}
    survivors: list[dict] = []
    dropped = 0
    for line in line_items:
        if line.get("is_tax_line"):
            survivors.append(line)
            continue
        key = _content_key(line)
        if key is None:
            survivors.append(line)
            continue
        if key not in first_index:
            first_index[key] = len(survivors)
            survivors.append(line)
            continue
        # Duplicate group — keep whichever line has the stronger source.
        pos = first_index[key]
        if _source_rank(line) > _source_rank(survivors[pos]):
            survivors[pos] = line
        dropped += 1
    return survivors, dropped


def _create_breakdown_with_lines(
    db: Session,
    tender_id: int,
    normalised: list[dict],
    costing: dict,
    defaults: dict,
    *,
    created_by_agent: str = "costing_researcher",
    session_id: Optional[int] = None,
    title: Optional[str] = None,
    cost_sheet_template: Optional[str] = None,
) -> CostBreakdown:
    """Create a new CostBreakdown version from already-normalised line dicts.

    Computes the three-band totals server-side, allocates the next version
    number, writes the header + N CostBreakdownLine rows, commits, and returns
    the refreshed breakdown. Shared by `persist_from_agent_output` (agent-JSON
    path) and `build_skeleton_from_boq` (deterministic NIT-mirror skeleton).
    """
    totals = _compute_totals(
        normalised,
        defaults["overhead_percent"],
        defaults["margin_percent"],
        defaults["gst_percent"],
    )

    last = (
        db.query(CostBreakdown)
        .filter(CostBreakdown.tender_id == tender_id)
        .order_by(CostBreakdown.version.desc())
        .first()
    )
    next_version = (last.version + 1) if last else 1

    # Stamp the owner at creation. Without this every new breakdown would be
    # written with created_by NULL, which the firewall reads as "predates the
    # wall" — master_admin only — and the costing researcher who just produced
    # it could not see their own work.
    from app.core.actor_context import current_actor
    _actor = current_actor()

    breakdown = CostBreakdown(
        tender_id=tender_id,
        version=next_version,
        created_by=_actor.user_id if _actor else None,
        status="draft",
        title=title or f"Cost Breakdown — Tender #{tender_id}",
        overhead_percent=defaults["overhead_percent"],
        margin_percent=defaults["margin_percent"],
        gst_percent=defaults["gst_percent"],
        subtotal=totals["subtotal"],
        overhead_amount=totals["overhead_amount"],
        margin_amount=totals["margin_amount"],
        gst_amount=totals["gst_amount"],
        grand_total=totals["grand_total"],
        subtotal_low=totals["subtotal_low"],
        subtotal_high=totals["subtotal_high"],
        overhead_amount_low=totals["overhead_amount_low"],
        overhead_amount_high=totals["overhead_amount_high"],
        margin_amount_low=totals["margin_amount_low"],
        margin_amount_high=totals["margin_amount_high"],
        gst_amount_low=totals["gst_amount_low"],
        gst_amount_high=totals["gst_amount_high"],
        grand_total_low=totals["grand_total_low"],
        grand_total_high=totals["grand_total_high"],
        # Phase 3b — margin-analysis roll-up
        tender_total=totals.get("tender_total"),
        margin_total_low=totals.get("margin_total_low"),
        margin_total=totals.get("margin_total"),
        margin_total_high=totals.get("margin_total_high"),
        margin_pct=totals.get("margin_pct"),
        needs_input_count=totals["needs_input_count"],
        assumptions_json=json.dumps(costing.get("assumptions") or [], default=str),
        recommendations_json=json.dumps(costing.get("recommendations") or [], default=str),
        manpower_resource_analysis_json=json.dumps(
            costing.get("manpower_resource_analysis") or [], default=str
        ),
        # Phase 3b — cost_assumptions library + strategic_summary
        cost_assumptions_json=json.dumps(
            costing.get("cost_assumptions") or [], default=str
        ),
        strategic_summary_json=json.dumps(
            costing.get("strategic_summary") or {}, default=str
        ) if costing.get("strategic_summary") else None,
        created_by_agent=created_by_agent,
        session_id=session_id,
        cost_sheet_template=cost_sheet_template,
    )
    db.add(breakdown)
    db.flush()

    for line in normalised:
        db.add(CostBreakdownLine(cost_breakdown_id=breakdown.id, **line))

    db.commit()
    db.refresh(breakdown)
    _run_reconciliation_gate(db, breakdown)
    db.commit()
    db.refresh(breakdown)
    logger.info(
        f"[cost_breakdown] persisted v{breakdown.version} for tender {tender_id}: "
        f"{len(normalised)} lines, grand_total ₹{totals['grand_total']:,.2f} "
        f"(range ₹{totals['grand_total_low']:,.2f} – ₹{totals['grand_total_high']:,.2f}), "
        f"{totals['needs_input_count']} need input"
    )
    # /health/costing telemetry — see plan: now-i-need-to-synchronous-taco.md
    try:
        from app.core.costing_metrics import record_persist_ok
        record_persist_ok()
    except Exception as e:
        logger.debug(f"[cost_breakdown] record_persist_ok failed (non-fatal): {e}")
    return breakdown


#: A component whose quantity x published rate misses its printed total by
#: more than this was read from the wrong cell.
_PRINTED_TOTAL_TOLERANCE = 0.02


def component_basis_from_printed_total(quantity, unit, tender_rate, basic_value):
    """(quantity, unit, tender_rate) for an annexure component, held to the
    total the material list prints for it.

    The parser does this for every row it reads now; this is the same check
    for a schedule captured before it did. Tender 5157's Annexure-II reached
    the costing with 279,048 kg of screws against a printed Rs 4,360.13 and
    4.4 million kg of chequered plate against Rs 22,523.19, and costed
    Rs 2,300 crore. A quantity that cannot make its printed total becomes
    total / rate; one printed amount with nothing to count it by is one lot.
    A printed quantity a clean power of ten from the total's is a shifted
    digit whose side is unknown, and is left as it is.
    """
    import math
    try:
        bv = float(basic_value) if basic_value is not None else None
        tr = float(tender_rate) if tender_rate is not None else None
        q = float(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        return quantity, unit, tender_rate
    if bv is None or not math.isfinite(bv) or bv <= 0:
        return quantity, unit, tender_rate
    if q is None:
        if (tr is None or abs(tr - bv) <= 0.5) and not (unit or "").strip():
            return 1.0, "Lot (printed total)", bv
        return quantity, unit, tender_rate
    if tr is None or not math.isfinite(tr) or tr <= 0 or q <= 0:
        return quantity, unit, tender_rate
    if abs(q * tr - bv) <= max(0.5, _PRINTED_TOTAL_TOLERANCE * bv):
        return quantity, unit, tender_rate
    implied = bv / tr
    if abs(tr - bv) <= max(0.5, _PRINTED_TOTAL_TOLERANCE * bv):
        return 1.0, "Lot (printed total)", bv
    k = round(math.log10(implied / q))
    if k != 0 and abs(implied / q / (10 ** k) - 1) <= 0.01:
        return quantity, unit, tender_rate
    return round(implied, 3), unit, tender_rate


def build_skeleton_from_boq(
    db: Session,
    tender_id: int,
    *,
    session_id: Optional[int] = None,
    title: Optional[str] = None,
    created_by_agent: str = "costing_researcher",
) -> Optional[CostBreakdown]:
    """Build a CostBreakdown skeleton that 1:1-mirrors the captured NIT schedule.

    Every BOQItem row becomes a CostBreakdownLine carrying the NIT-mirror fields
    verbatim (item_code, schedule_name, bidding_unit, basic_value,
    escalation_pct, is_tax_line) plus `tender_rate`/`tender_amount` taken from
    the NIT's published rate (this turns on margin-analysis mode — the firm's
    estimated cost is benchmarked against the published rate). Non-tax rows
    start UNPRICED (`rate=None`, `needs_input=True`); the batched costing pass
    fills them in via `merge_batch_rates()`.

    This guarantees all 50–500 line items always exist regardless of any LLM
    output truncation — the skeleton is the authoritative row set and the RULE 5
    mirror is correct by construction. Returns None when the tender has no
    captured schedule (caller should fall back to the single-call agent path).
    """
    boq_rows = (
        db.query(BOQItem)
        .filter(BOQItem.tender_id == tender_id)
        .order_by(BOQItem.schedule_name.asc().nullsfirst(), BOQItem.sr_no.asc())
        .all()
    )
    if not boq_rows:
        return None

    defaults = get_costing_defaults(db)
    by_id = {r.id: r for r in boq_rows}
    normalised: list[dict] = []
    for i, item in enumerate(boq_rows):
        is_tax = bool(item.is_tax_line)
        parent = by_id.get(item.parent_item_id) if getattr(item, "parent_item_id", None) else None
        annexure_ref = getattr(item, "annexure_ref", None)
        if parent is not None:
            # A component of a schedule item: grouped under that item, never
            # under "Other", and never added to the totals on its own.
            section = component_group_label(
                annexure_ref, parent.schedule_name, parent.item_code, parent.sr_no,
            )
        elif item.schedule_name:
            section = f"Schedule {item.schedule_name}"
        elif annexure_ref:
            section = f"Annexure-{annexure_ref}"
        else:
            section = None
        quantity, unit, tender_rate = item.quantity, item.unit, item.estimated_rate
        if parent is not None:
            quantity, unit, tender_rate = component_basis_from_printed_total(
                quantity, unit, tender_rate, item.basic_value,
            )
        printed_nil = parent is not None and quantity is not None and float(quantity) == 0.0
        raw = {
            "sr_no": item.sr_no if item.sr_no is not None else (i + 1),
            "description": item.description or "",
            "quantity": quantity,
            "unit": unit or "",
            # NIT-mirror fields (verbatim)
            "boq_item_id": item.id,
            "item_code": item.item_code or "",
            "schedule_name": item.schedule_name or "",
            "bidding_unit": item.bidding_unit or "",
            "basic_value": item.basic_value,
            "escalation_pct": item.escalation_pct,
            "is_tax_line": is_tax,
            # Group label for the editor (it buckets by schedule_section).
            "schedule_section": section,
            # Annexure component link
            "parent_boq_item_id": parent.id if parent is not None else None,
            "annexure_ref": annexure_ref,
            # NIT published rate → benchmark (margin-analysis mode on)
            "tender_rate": tender_rate,
            # firm rate not yet researched — mark for batch fill. A component
            # the material list totals at Rs 0.00 (quantity 0) is priced at
            # nil here and never sent for research: an empty rate on it read
            # as "incomplete costing" in the Summary.
            "rate": (float(tender_rate or 0.0) if printed_nil else None),
            "amount": (0.0 if printed_nil else None),
            "rate_source": (PRINTED_NIL_SOURCE if printed_nil else None if is_tax else "needs_user_input"),
            "cost_buildup_note": ("Printed total Rs 0.00 in the material list; costed at nil." if printed_nil else None),
        }
        normalised.append(
            _normalize_line_dict(
                raw,
                sr_no=int(raw["sr_no"]),
                breakdown_margin_percent=defaults["margin_percent"],
            )
        )

    costing_meta = {
        "assumptions": [
            "Skeleton built deterministically from the captured NIT bidding "
            "schedule — every line item is present. Firm rates are researched "
            "and filled in per batch by the costing researcher."
        ],
    }
    breakdown = _create_breakdown_with_lines(
        db,
        tender_id,
        normalised,
        costing_meta,
        defaults,
        created_by_agent=created_by_agent,
        session_id=session_id,
        title=title or f"Cost Breakdown — Tender #{tender_id}",
    )
    tax_count = sum(1 for n in normalised if n.get("is_tax_line"))
    logger.info(
        f"[cost_breakdown] tender {tender_id}: built NIT-mirror skeleton "
        f"v{breakdown.version} — {len(normalised)} line(s) "
        f"({tax_count} tax line(s), {len(normalised) - tax_count} to be costed)"
    )
    return breakdown


def _norm_code(s: Optional[str]) -> str:
    """Normalize a schedule_name / item_code for drift-tolerant matching.

    Strips everything but alphanumerics and uppercases, so "A-71", " a71 ",
    and "A71" all collapse to "A71". Used only as a fallback after exact
    (schedule_name, item_code) matching fails.
    """
    return re.sub(r"[^A-Za-z0-9]", "", (s or "")).upper()


_ANNEXURE_CODE_RE = re.compile(r"^ANX[A-Z0-9]+$")


def _is_annexure_code(code: Optional[str]) -> bool:
    """True for the synthetic annexure code (`ANX-II-17`, normalised or not)."""
    return bool(code) and bool(_ANNEXURE_CODE_RE.match(_norm_code(code)))


# The tender's own figure, however the agent names it.
_RATE_NOUN = r"(?:ref(?:erence)?|rate|price|estimate|value)"
_MONEY = r"(?:₹|rs\.?|inr|\d)"
# "published" alone is the tender's figure unless it names a published
# schedule of rates (DSR / SOR / a price list), which is real evidence;
# "tender" / "NIT" / "railway" only when a rate or an amount follows
# ("tender scope", "NIT drawing" are not rates).
_PUBLISHED_WORD = (
    r"(?:(?:tender(?:'s)?\s+)?published(?!\s+(?:schedule|dsr|sor|price\s+list|catalog))"
    r"|(?:nit|tender|railway)(?:'s)?(?=\s+(?:" + _RATE_NOUN + r"|" + _MONEY + r"))"
    r"|benchmark)"
    r"(?:\s+" + _RATE_NOUN + r")*"
)
# Stripping a margin off a figure, not "stripping of coaches" (a work item).
_STRIP_MARGIN = (
    r"\bstrip\w*\b" + r"(?:[^.;|]|(?<=\d)\.(?=\d))" + r"{0,40}?"
    r"(?:margin|overhead|profit|\d+(?:\.\d+)?\s*%)"
)
#: Within one clause; a decimal point inside a number ("Rs 979.65") is not a stop.
_SAME_CLAUSE = r"(?:[^.;|]|(?<=\d)\.(?=\d))"
#: A rate whose stated basis is the tender's published rate: "Derived from
#: published Rs 2,36,000 by stripping 23%", "Published ref rate Rs 979.65
#: stripped of 15% margin", "80% of the published rate", "published / 1.25".
_PUBLISHED_ANCHOR_RE = re.compile(
    r"\bderiv\w*\s+(?:\w+\s+){0,3}?from\s+(?:the\s+)?" + _PUBLISHED_WORD
    + r"|\b" + _PUBLISHED_WORD + r"\b" + _SAME_CLAUSE + r"{0,60}?" + _STRIP_MARGIN
    + r"|\bstrip\w*\b" + _SAME_CLAUSE + r"{0,60}?\b(?:off|from)\s+(?:the\s+)?" + _PUBLISHED_WORD
    + r"|\d+(?:\.\d+)?\s*%\s+(?:of|off)\s+(?:the\s+)?" + _PUBLISHED_WORD
    + r"|\b" + _PUBLISHED_WORD + r"\b" + _SAME_CLAUSE + r"{0,40}?(?:÷|/|\bdivided\s+by)\s*1\.\d",
    re.IGNORECASE,
)


def rate_anchored_to_published(*texts: Optional[str]) -> bool:
    """True when the agent's own source text says its rate was computed from
    the tender's published rate.

    Such a rate is the railway's number scaled by a constant, not a cost: the
    Mid-Life Rehabilitation NIT (285 rows) came back with every row
    "Derived from published Rs X by stripping ~23%" beside a material build-up
    that summed to a fraction of it, and every platform rate was the NIT rate
    times 0.77. A comparison ("Rs 7,250 median, below the published Rs 9,000")
    is not a derivation and does not match.
    """
    blob = " ".join(t for t in texts if t)
    return bool(blob) and bool(_PUBLISHED_ANCHOR_RE.search(blob))


def _without_platform_markers(note: Optional[str]) -> Optional[str]:
    """An agent-written note with the platform's own evidence wording removed.

    `VERIFIED_WEB_PRICE` and the `Basis:` lines are how the platform records
    what IT checked; settlement trusts them. The costing agent, told to cite
    prices, writes "Verified web price ..." as ordinary prose -- and a probe
    kept such a figure (a category-page URL, Rs 450 against a reference of
    Rs 800) and told the bidder it was a current market price.
    """
    if not note:
        return note
    from app.services.costing.cost_buildup import BUILDUP_MARKER

    out = _strip_basis(note)
    out = re.sub(re.escape(VERIFIED_WEB_PRICE), "Web price cited by the agent (not verified)",
                 out, flags=re.IGNORECASE)
    # The platform's own build-up stands outside the band (it has had its
    # second look); an agent that writes the same words has not.
    out = re.sub(re.escape(BUILDUP_MARKER), "Cost build-up by the costing agent",
                 out, flags=re.IGNORECASE)
    return out


def merge_batch_rates(
    db: Session,
    breakdown_id: int,
    batch_line_items: list[dict],
    *,
    platform_verified: bool = False,
) -> dict:
    """Merge one batch of costed line items into an existing skeleton breakdown.

    Matches each returned line to an existing CostBreakdownLine by
    `boq_item_id` -> `(schedule_name, item_code)` -> the annexure row's own
    synthetic `ANX-<ref>-<n>` code -> `(schedule_name, sr_no)` when that serial
    is unique within its schedule. A serial on its own is never an identity:
    schedules restart at 1, every annexure restarts at 1, and Annexure-II's
    thirty sub-tables each restart at 1, so a bare `sr_no` fallback bound
    batch 3's annexure prices to unrelated NIT rows (19 "merged", 41 unmatched,
    none of them right). A row that matches nothing stays `needs_input` for
    the re-cost sweep. Updates ONLY the cost-side fields (rate/amount/profit/
    margin/source/confidence, etc.); never touches the NIT-authoritative
    fields (tender_rate, basic_value, item_code, schedule_name, quantity,
    description, unit). Commits.

    Idempotent and resumable: re-running re-writes the same rows, and rows left
    `needs_input=True` after all batches signal which lines still need costing.
    A schedule row whose rate the agent computed from the published rate
    (`rate_anchored_to_published`) is matched but left needs_input.
    Returns {"matched": n, "unmatched": m}, plus "rejected_anchored" when any
    rate was refused that way.

    `platform_verified=True` is for the platform's own market research only:
    its notes carry the `VERIFIED_WEB_PRICE` marker settlement trusts. Every
    other caller's notes have that marker (and any `Basis:` line) removed.
    """
    breakdown = db.query(CostBreakdown).filter(CostBreakdown.id == breakdown_id).first()
    if not breakdown:
        return {"matched": 0, "unmatched": len(batch_line_items or [])}

    existing = (
        db.query(CostBreakdownLine)
        .filter(CostBreakdownLine.cost_breakdown_id == breakdown_id)
        .all()
    )
    by_boq: dict[int, CostBreakdownLine] = {}
    by_code: dict[tuple, CostBreakdownLine] = {}
    by_code_norm: dict[tuple, CostBreakdownLine] = {}
    # Annexure rows carry no schedule; their synthetic ANX-<ref>-<n> code is
    # unique within the tender (assigned by position within the annexure, see
    # boq_parser_service._synth_annexure_code) and stable across re-parses,
    # so it identifies the row on its own -- including when the agent echoes
    # a boq_item_id that a concurrent re-capture has since replaced.
    by_anx: dict[str, CostBreakdownLine] = {}
    # (schedule, sr_no) for schedule rows only, and only where the serial is
    # unique inside that schedule. Never keyed on the serial alone.
    by_sched_sr: dict[tuple, CostBreakdownLine] = {}
    _sched_sr_dupes: set[tuple] = set()
    code_candidates: dict[tuple, list] = {}
    for ln in existing:
        if ln.boq_item_id is not None:
            by_boq[int(ln.boq_item_id)] = ln
        code = (ln.item_code or "").strip()
        sched = (ln.schedule_name or "").strip()
        if sched and code:
            code_candidates.setdefault((_norm_code(sched), _norm_code(code)), []).append(ln)
            by_code[(sched, code)] = ln
            # Normalized index tolerates item_code formatting drift from the LLM
            # ("A-71"/" a71 " vs "A71"). Within-schedule scoped so it can't
            # mis-bind across schedules.
            by_code_norm.setdefault((_norm_code(sched), _norm_code(code)), ln)
        if code and not sched and _is_annexure_code(code):
            by_anx.setdefault(_norm_code(code), ln)
        if sched and ln.sr_no is not None and ln.parent_boq_item_id is None:
            key = (_norm_code(sched), int(ln.sr_no))
            if key in by_sched_sr:
                _sched_sr_dupes.add(key)
            else:
                by_sched_sr[key] = ln
    for key in _sched_sr_dupes:
        by_sched_sr.pop(key, None)

    # Cost-side fields the merge is allowed to overwrite on the skeleton row.
    # `tender_amount` is deliberately EXCLUDED — like tender_rate/quantity/
    # basic_value/item_code/schedule_name (forced onto `raw` below before
    # calling _normalize_line_dict), it is NIT-authoritative and must never be
    # taken from the agent's output, even indirectly via the merged dict.
    _COST_FIELDS = (
        "rate", "amount", "rate_low", "rate_high", "amount_low", "amount_high",
        "profit_pct", "profit_amount_low", "profit_amount", "profit_amount_high",
        "margin_amount_low", "margin_amount", "margin_amount_high", "margin_pct",
        "category", "cost_buildup_note", "rate_source",
        "source_ref", "oem_manufacturer", "source_url", "confidence", "needs_input", "notes",
    )

    matched = 0
    unmatched = 0
    rejected = 0
    for raw in (batch_line_items or []):
        if not platform_verified and raw.get("cost_buildup_note"):
            raw = {**raw, "cost_buildup_note": _without_platform_markers(raw.get("cost_buildup_note"))}
        target = None
        bid = raw.get("boq_item_id")
        if bid is not None:
            try:
                target = by_boq.get(int(bid))
            except (TypeError, ValueError):
                target = None
        if target is None:
            sched = (raw.get("schedule_name") or "").strip()
            code = (raw.get("item_code") or "").strip()
            if sched and code:
                target = by_code.get((sched, code))
        nsched = _norm_code(raw.get("schedule_name"))
        ncode = _norm_code(raw.get("item_code"))
        # NITs can repeat a word code (e.g. CONVERSION MAT) on many rows.
        # Without a valid BOQ id, that code alone cannot select a price target.
        if bid is None or target is None or target.boq_item_id != _coerce_id(bid):
            candidates = code_candidates.get((nsched, ncode), [])
            if len(candidates) > 1:
                sr = _coerce_id(raw.get("sr_no"))
                candidates = [ln for ln in candidates if ln.sr_no == sr] if sr else []
                target = candidates[0] if len(candidates) == 1 else None
                if target is None:
                    unmatched += 1
                    logger.warning("[cost_breakdown] ambiguous schedule code %s/%s; rate not merged", nsched, ncode)
                    continue
        if target is None:
            # Normalized (drift-tolerant) (schedule, item_code) fallback.
            if nsched and ncode:
                target = by_code_norm.get((nsched, ncode))
        if target is None and ncode and _is_annexure_code(ncode):
            # An annexure component: its ANX code is the identity, whatever
            # schedule label (if any) the agent put next to it.
            target = by_anx.get(ncode)
        if target is None and nsched and not ncode:
            # A schedule row whose code the agent dropped: the serial inside
            # THAT schedule, when unique there. Never a serial without a schedule.
            sr = raw.get("sr_no")
            try:
                target = by_sched_sr.get((nsched, int(sr))) if sr is not None else None
            except (TypeError, ValueError):
                target = None
        if target is None:
            unmatched += 1
            logger.warning(
                f"[cost_breakdown] merge_batch_rates: no skeleton row matched a "
                f"returned line (boq_item_id={raw.get('boq_item_id')}, "
                f"item_code={raw.get('item_code')!r}, sr_no={raw.get('sr_no')})"
            )
            continue
        if target.is_tax_line:
            matched += 1  # tax rows pass through — never costed
            continue

        # Re-normalise the returned line, but force the skeleton's authoritative
        # NIT fields so the agent can't drift quantity/tender_rate/identifiers.
        merged = _normalize_line_dict(
            {
                **raw,
                # Model arithmetic is never authoritative. The skeleton's
                # quantity and the returned rates determine every money band.
                **({k: None for k in (
                    "amount", "amount_expected", "amount_low", "amount_high",
                    "margin_amount", "margin_amount_expected", "margin_amount_low",
                    "margin_amount_high", "margin_pct", "profit_amount",
                    "profit_amount_expected", "profit_amount_low", "profit_amount_high",
                )}),
                **({"rate_source": "needs_user_input"} if target.quantity is None else {}),
                "quantity": target.quantity,
                "tender_rate": target.tender_rate,
                "boq_item_id": target.boq_item_id,
                "item_code": target.item_code,
                "schedule_name": target.schedule_name,
                "bidding_unit": target.bidding_unit,
                "basic_value": target.basic_value,
                "escalation_pct": target.escalation_pct,
                "is_tax_line": False,
                "parent_boq_item_id": target.parent_boq_item_id,
                "annexure_ref": target.annexure_ref,
            },
            sr_no=target.sr_no,
            breakdown_margin_percent=breakdown.margin_percent,
        )
        # A rate the agent computed from the published rate is the railway's
        # number scaled, whatever `rate_source` it wears. Not merged as a
        # cost: the row goes back to needs_input so the re-cost sweep prices
        # it, and if nothing independent is found the copied-rate guard at
        # finalisation derives it under its own label, counted in the Summary.
        # Annexure components are exempt: a printed component rate is a
        # sanctioned provisional basis (_COMPONENT_RESEARCH_RULE).
        if (
            target.parent_boq_item_id is None
            and target.tender_rate is not None
            and not merged.get("needs_input")
            and rate_anchored_to_published(merged.get("source_ref"), merged.get("cost_buildup_note"))
        ):
            merged = _normalize_line_dict(
                {
                    **merged,
                    "rate": None, "rate_expected": None, "rate_low": None, "rate_high": None,
                    "rate_source": "needs_user_input",
                    "source_url": None,
                    "cost_buildup_note": None,
                    "source_ref": (
                        "Agent rate rejected: it was computed from the published rate, "
                        "not from evidence or a cost build-up."
                    ),
                },
                sr_no=target.sr_no,
                breakdown_margin_percent=breakdown.margin_percent,
            )
            rejected += 1
        for field in _COST_FIELDS:
            setattr(target, field, merged.get(field))
        matched += 1

    db.commit()
    out = {"matched": matched, "unmatched": unmatched}
    if rejected:
        out["rejected_anchored"] = rejected
        logger.info(
            f"[cost_breakdown] merge_batch_rates: {rejected} rate(s) computed from the "
            f"published rate were not accepted; left needs_input for the re-cost sweep"
        )
    return out


# ---------------------------------------------------------------------------
# Annexure components roll up into the schedule item that cites them
# ---------------------------------------------------------------------------
#
# The Liluah NIT prices "Material Cost for Conversion work ... (As per
# Annexure-II)" at Rs 8,16,051 per coach set, fifteen coach sets. Annexure-II
# is the thirty materials that make up one coach set. Costing each material
# is what the bidder wants; adding those thirty amounts to a total that also
# holds the item they compose is the double count this section removes.
#
# A component line's quantity is per set (12 litres of primer per paint set)
# and its amount is quantity x researched rate: a per-set cost. The sum of
# the parent's priced components is the parent's estimated unit rate; times
# the parent's own quantity (20 coach sets) it is the parent's amount, and
# against the parent's published rate it is the margin the bid is made on.
# Deterministic, no model call, idempotent: every recompute redoes it from
# the current component lines, so a user editing a component's rate in the
# editor moves the parent on save.

COMPONENT_BUILDUP_SOURCE = "component_buildup"
#: A component the material list totals at Rs 0.00: costed at nil, not researched.
PRINTED_NIL_SOURCE = "printed_nil_total"


def _rank_confidence(values: list[Optional[str]]) -> Optional[str]:
    order = {"high": 3, "medium": 2, "low": 1}
    ranked = [v for v in values if v in order]
    if not ranked:
        return None
    return min(ranked, key=lambda v: order[v])


#: Printed component totals within this fraction of a parent figure "match" it.
_ROLLUP_BASIS_TOLERANCE = 0.10
#: A built-up rate more than this many times the published rate is called out
#: in the parent's note (a researched component rate that is wrong by a unit,
#: or a quantity misread, shows up here first).
_ROLLUP_OVERRUN_FLAG = 1.5


def _component_quantity_divisor(parent: dict, comps: list[dict]) -> float:
    """1.0 when the components' quantities are per set (the usual annexure:
    "12 LTR" of primer per paint set), or the parent's quantity when the
    annexure prints them for the whole contract.

    The Liluah Annexure-I prints "15" against every stripping item -- the
    fifteen coach sets of the schedule -- and its totals sum to Rs 5,70,000,
    which is the schedule's published Rs 38,000 x 15, not Rs 38,000. Summed
    as per-set costs and multiplied by the fifteen sets again, that parent
    came out at twelve times its published rate. The printed totals settle
    the basis: when they sum to the parent's contract value (published rate
    x quantity) and not to its unit rate, the quantities are per contract.
    Without printed totals, or with a parent quantity of one, per set.
    """
    qty = _coerce_float(parent.get("quantity"))
    t_rate = _coerce_float(parent.get("tender_rate"))
    if not qty or qty <= 1.0 or not t_rate or t_rate <= 0:
        return 1.0
    printed = 0.0
    for c in comps:
        v = _coerce_float(c.get("basic_value"))
        if v is None:
            v_rate = _coerce_float(c.get("tender_rate"))
            v_qty = _coerce_float(c.get("quantity"))
            v = v_rate * v_qty if (v_rate and v_qty) else None
        if v:
            printed += v
    if printed <= 0:
        return 1.0
    contract = t_rate * qty
    near_contract = abs(printed - contract) <= _ROLLUP_BASIS_TOLERANCE * contract
    near_unit = abs(printed - t_rate) <= _ROLLUP_BASIS_TOLERANCE * t_rate
    return float(qty) if (near_contract and not near_unit) else 1.0


def rollup_component_parents(lines: list[dict]) -> dict:
    """Set each parent line's rate/amount/margin from its component lines.

    `lines` are normalised line dicts (as `_normalize_line_dict` returns);
    parents are found by `boq_item_id`, components by `parent_boq_item_id`.
    Mutates the parent dicts in place. A parent with no priced component is
    left as it is (it may still be researched or derived on its own path).
    Returns {"parents": n_rolled, "unpriced_components": n, "notes": [...]}.
    """
    by_boq: dict[int, dict] = {}
    for ln in lines:
        bid = _coerce_id(ln.get("boq_item_id"))
        if bid is not None and not _is_component_line(ln):
            by_boq.setdefault(bid, ln)
    children: dict[int, list[dict]] = {}
    for ln in lines:
        pid = _coerce_id(ln.get("parent_boq_item_id"))
        if pid is not None:
            children.setdefault(pid, []).append(ln)

    rolled = 0
    unpriced_total = 0
    notes: list[str] = []
    for pid, comps in children.items():
        parent = by_boq.get(pid)
        if parent is None or parent.get("is_tax_line"):
            continue
        priced = [c for c in comps if not _line_needs_input(c) and _coerce_float(c.get("amount")) is not None]
        unpriced = len(comps) - len(priced)
        unpriced_total += unpriced
        if not priced:
            continue
        qty = _coerce_float(parent.get("quantity"))
        ref = next((c.get("annexure_ref") for c in comps if c.get("annexure_ref")), None)
        unit = (parent.get("unit") or "set").strip() or "set"
        # The components' quantities are per set unless the annexure prints
        # them for the whole contract (`_component_quantity_divisor`).
        divisor = _component_quantity_divisor(parent, comps)
        per_set = round(sum(_coerce_float(c.get("amount")) or 0.0 for c in priced) / divisor, 2)
        per_set_low = round(sum(
            (_coerce_float(c.get("amount_low")) if c.get("amount_low") is not None
             else _coerce_float(c.get("amount")) or 0.0) or 0.0 for c in priced) / divisor, 2)
        per_set_high = round(sum(
            (_coerce_float(c.get("amount_high")) if c.get("amount_high") is not None
             else _coerce_float(c.get("amount")) or 0.0) or 0.0 for c in priced) / divisor, 2)
        has_band = any(c.get("amount_low") is not None or c.get("amount_high") is not None for c in priced)

        parent["rate"] = per_set
        parent["rate_low"] = per_set_low if has_band else None
        parent["rate_high"] = per_set_high if has_band else None
        parent["amount"] = round(per_set * qty, 2) if qty is not None else None
        parent["amount_low"] = round(per_set_low * qty, 2) if (has_band and qty is not None) else None
        parent["amount_high"] = round(per_set_high * qty, 2) if (has_band and qty is not None) else None
        parent["rate_source"] = COMPONENT_BUILDUP_SOURCE
        parent["needs_input"] = False
        parent["confidence"] = "low" if unpriced else (_rank_confidence([c.get("confidence") for c in priced]) or "medium")
        t_rate_for_check = _coerce_float(parent.get("tender_rate"))
        over = (
            per_set / t_rate_for_check
            if t_rate_for_check and t_rate_for_check > 0 and per_set > 0 else None
        )
        parent["cost_buildup_note"] = (
            f"Built up from {len(priced)} of {len(comps)} component line(s) in "
            f"Annexure-{ref or '?'}: Rs {per_set:,.2f} per {unit}"
            + (f" (the annexure lists quantities for all {divisor:g} {unit}s; divided)" if divisor != 1.0 else "")
            + (f"; {unpriced} component(s) still unpriced, so this is a floor" if unpriced else "")
            + (f"; {over:.1f}x the published rate of Rs {t_rate_for_check:,.2f} -- check the "
               f"component rates before bidding" if over is not None and over > _ROLLUP_OVERRUN_FLAG else "")
            + "."
        )
        if not (parent.get("source_ref") or "").strip():
            parent["source_ref"] = f"Sum of Annexure-{ref or '?'} component lines (per {unit})."
        # Margin against the published rate, when there is one.
        t_amount = _coerce_float(parent.get("tender_amount"))
        if t_amount is None:
            t_rate = _coerce_float(parent.get("tender_rate"))
            if t_rate is not None and qty is not None:
                escl = _coerce_float(parent.get("escalation_pct")) or 0.0
                t_amount = round(t_rate * qty * (1 + escl / 100.0), 2)
                parent["tender_amount"] = t_amount
        if t_amount is not None and parent.get("amount") is not None:
            parent["margin_amount"] = round(t_amount - parent["amount"], 2)
            parent["margin_amount_low"] = round(t_amount - parent["amount_high"], 2) if parent.get("amount_high") is not None else parent["margin_amount"]
            parent["margin_amount_high"] = round(t_amount - parent["amount_low"], 2) if parent.get("amount_low") is not None else parent["margin_amount"]
            parent["margin_pct"] = round(parent["margin_amount"] / t_amount * 100.0, 2) if t_amount else None
        rolled += 1
        if unpriced:
            notes.append(
                f"Schedule {parent.get('schedule_name') or '?'} item "
                f"{parent.get('item_code') or parent.get('sr_no')}: {unpriced} of "
                f"{len(comps)} Annexure-{ref or '?'} component(s) unpriced — the rolled-up "
                f"rate is a floor."
            )
        if over is not None and over > _ROLLUP_OVERRUN_FLAG:
            notes.append(
                f"⚠️ Schedule {parent.get('schedule_name') or '?'} item "
                f"{parent.get('item_code') or parent.get('sr_no')}: the Annexure-{ref or '?'} "
                f"build-up (Rs {per_set:,.2f} per {unit}) is {over:.1f}x the published rate "
                f"(Rs {t_rate_for_check:,.2f}) — check the component rates before bidding."
            )
    return {"parents": rolled, "unpriced_components": unpriced_total, "notes": notes}


_ROLLUP_FIELDS = (
    "rate", "rate_low", "rate_high", "amount", "amount_low", "amount_high",
    "rate_source", "needs_input", "confidence", "cost_buildup_note", "source_ref",
    "tender_amount", "margin_amount", "margin_amount_low", "margin_amount_high", "margin_pct",
)


def rollup_component_lines(db: Session, breakdown_id: int) -> dict:
    """ORM form of `rollup_component_parents`: read the breakdown's lines,
    roll the components into their parents, write the parents back. Commits.
    Returns the same summary dict."""
    lines = (
        db.query(CostBreakdownLine)
        .filter(CostBreakdownLine.cost_breakdown_id == breakdown_id)
        .all()
    )
    if not any(ln.parent_boq_item_id is not None for ln in lines):
        return {"parents": 0, "unpriced_components": 0, "notes": []}
    dicts: list[dict] = []
    for ln in lines:
        dicts.append({
            "_orm": ln,
            "boq_item_id": ln.boq_item_id,
            "parent_boq_item_id": ln.parent_boq_item_id,
            "annexure_ref": ln.annexure_ref,
            "is_tax_line": ln.is_tax_line,
            "quantity": ln.quantity, "unit": ln.unit,
            "rate": ln.rate, "rate_low": ln.rate_low, "rate_high": ln.rate_high,
            "amount": ln.amount, "amount_low": ln.amount_low, "amount_high": ln.amount_high,
            "rate_source": ln.rate_source, "needs_input": ln.needs_input,
            "confidence": ln.confidence, "cost_buildup_note": ln.cost_buildup_note,
            "source_ref": ln.source_ref, "schedule_name": ln.schedule_name,
            "item_code": ln.item_code, "sr_no": ln.sr_no,
            "tender_rate": ln.tender_rate, "tender_amount": ln.tender_amount,
            "basic_value": ln.basic_value,
            "escalation_pct": ln.escalation_pct,
            "margin_amount": ln.margin_amount, "margin_amount_low": ln.margin_amount_low,
            "margin_amount_high": ln.margin_amount_high, "margin_pct": ln.margin_pct,
        })
    summary = rollup_component_parents(dicts)
    for d in dicts:
        if d.get("rate_source") == COMPONENT_BUILDUP_SOURCE and not _is_component_line(d):
            orm = d["_orm"]
            for f in _ROLLUP_FIELDS:
                setattr(orm, f, d.get(f))
    db.commit()
    if summary["parents"]:
        logger.info(
            f"[cost_breakdown] breakdown {breakdown_id}: rolled {summary['parents']} "
            f"annexure component group(s) into their schedule items"
            + (f"; {summary['unpriced_components']} component(s) unpriced" if summary["unpriced_components"] else "")
        )
    return summary


def normalize_copied_rates(
    db: Session,
    breakdown_id: int,
    overhead_pct: float,
    margin_pct: float,
) -> int:
    """Deterministic guarantee that no non-tax line is a 1:1 copy of the
    tender's published rate (RULE 2b).

    The LLM frequently echoes the published rate as its cost (zero margin),
    sometimes even labelling it `training_data`. For any non-tax line whose
    rate is missing, flagged needs_input, sourced 'tender_estimate', or equal
    to (within ₹0.01 of) the published `tender_rate`, re-derive the firm cost
    by stripping the org overhead+margin off the published reference:

        rate = tender_rate / (1 + (overhead% + margin%)/100)

    Genuinely-researched rates strictly BELOW the published rate are left
    untouched. Lines with no published reference (tender_rate is None) are left
    as-is (they stay needs_input). Recomputes amount + per-line margin. Commits.
    Returns the number of lines adjusted.
    """
    divisor = 1.0 + (float(overhead_pct or 0) + float(margin_pct or 0)) / 100.0
    if divisor <= 0:
        divisor = 1.0
    lines = (
        db.query(CostBreakdownLine)
        .filter(CostBreakdownLine.cost_breakdown_id == breakdown_id)
        .all()
    )
    adjusted = 0
    parent_ids = {ln.parent_boq_item_id for ln in lines if ln.parent_boq_item_id is not None}
    for ln in lines:
        if ln.is_tax_line or ln.boq_item_id in parent_ids or ln.quantity is None:
            continue
        if ln.rate_source == PRINTED_NIL_SOURCE:
            continue
        tr = ln.tender_rate
        if tr is None:
            continue  # no reference to derive from
        try:
            tr = float(tr)
        except (TypeError, ValueError):
            continue
        r = ln.rate
        has_evidence = (
            (
                ln.rate_source in {"web_search", "training_data"}
                and bool((ln.source_url or ln.source_ref or "").strip())
                or is_platform_buildup(ln)
            )
            and not ln.needs_input and r is not None
        )
        is_copy = (
            ln.rate_source == "tender_estimate"
            or r is None
            or bool(ln.needs_input)
        )
        if not is_copy and r is not None and not has_evidence:
            try:
                is_copy = abs(float(r) - tr) < 0.01
            except (TypeError, ValueError):
                is_copy = True
        if not is_copy:
            continue  # researched rate below the reference — keep it

        new_rate = round(tr / divisor, 2)
        q = float(ln.quantity or 0)
        ln.rate = new_rate
        ln.rate_source = "derived_estimate"
        ln.needs_input = False
        ln.confidence = ln.confidence or "low"
        ln.amount = round(new_rate * q, 2)
        if ln.tender_amount is None:
            # Escalation-inclusive, matching the authoritative formula at ~:284
            # (tender_rate * qty * (1 + escl/100)); an escl-free amount would
            # understate the NIT-authoritative tender value.
            _escl = 0.0
            try:
                _escl = float(ln.escalation_pct or 0.0)
            except (TypeError, ValueError):
                _escl = 0.0
            ln.tender_amount = round(tr * q * (1 + _escl / 100.0), 2)
        if ln.tender_amount:
            ln.margin_amount = round(ln.tender_amount - ln.amount, 2)
            ln.margin_pct = round((ln.margin_amount / ln.tender_amount) * 100.0, 2)
        ln.cost_buildup_note = (
            f"{_FORMULA_NOTE} Rs {tr:,.2f} by stripping "
            f"{overhead_pct:g}% overhead + {margin_pct:g}% margin (÷{divisor:.3f})."
        )
        if not (ln.source_ref or "").strip():
            ln.source_ref = "Derived from published NIT reference rate (RULE 2b fallback)."
        adjusted += 1

    db.commit()
    if adjusted:
        logger.info(
            f"[cost_breakdown] breakdown {breakdown_id}: normalize_copied_rates "
            f"re-derived {adjusted} non-tax line(s) that copied the published rate "
            f"(÷{divisor:.3f} = strip {overhead_pct:g}%+{margin_pct:g}%)"
        )
    return adjusted


#: How `normalize_copied_rates` marks the rows it derived by formula, so a
#: row is told apart from an agent's own first-principles `derived_estimate`.
_FORMULA_NOTE = "Derived from published reference"


def _is_formula_derived(ln) -> bool:
    """A `derived_estimate` whose rate rests on the published rate, not on a
    build-up: written by `normalize_copied_rates`, or (a breakdown saved
    before merge refused them) an agent rate whose own text says it was
    derived from the published rate, or one that shows no build-up at all.
    An agent's first-principles build-up is `derived_estimate` too, and is
    not this."""
    if ln.rate_source != "derived_estimate":
        return False
    note, ref = ln.cost_buildup_note or "", ln.source_ref or ""
    return (
        _FORMULA_NOTE in note
        or rate_anchored_to_published(ref, note)
        or not (note.strip() or ref.strip())
    )


#: Evidence -- a cited market price, or the firm's own rate card / training
#: data / memory -- is kept when it lies within this range of the reference
#: cost (the railway's estimate less the org overhead + margin). Outside it,
#: it is almost certainly a different item: a generic product in place of an
#: RDSO-specification one, a per-metre price against a per-coil item.
_PLAUSIBLE_BAND = (0.5, 2.0)

#: The plain-language basis each settled line states at the head of its
#: build-up note, for readers who will not open a source column.
BASIS_MARKET = "Basis: market price"
BASIS_BUILDUP = "Basis: cost build-up"
BASIS_REFERENCE = "Basis: railway estimate"
_BASIS_PREFIXES = (BASIS_MARKET, BASIS_BUILDUP, BASIS_REFERENCE)

#: How `market_price_research` marks a web price it verified: the product
#: matches the specification exactly or closely, and its URL is one the
#: search in that same call returned.
VERIFIED_WEB_PRICE = "Verified web price"


def _has_market_evidence(ln) -> bool:
    """A rate backed by something outside the model: a web price the
    platform's own research verified, or the firm's own rate card / training
    data / memory with a reference. A web price the costing agent cites on
    its own is not enough -- nothing checks that the page it names showed
    that price for that item (on the Mid-Life NIT one cited an IndiaMART
    category page for a coach window glass)."""
    ref = (ln.source_ref or "").strip()
    if ln.rate_source == "web_search":
        return bool((ln.source_url or "").strip()) and VERIFIED_WEB_PRICE in (ln.cost_buildup_note or "")
    if ln.rate_source in ("training_data", "memory"):
        return bool(ref)
    return False


def is_platform_buildup(ln) -> bool:
    """A row the platform's own cost build-up priced (costing/cost_buildup.py):
    its arithmetic, on verified wages and prices, from the model's quantities
    and hours. Only the platform writes the marker -- `merge_batch_rates`
    removes it from anything else."""
    from app.services.costing.cost_buildup import BUILDUP_MARKER
    return ln.rate_source == "derived_estimate" and BUILDUP_MARKER in (ln.cost_buildup_note or "")


def line_has_firm_rate(ln) -> bool:
    """The firm's own rate data priced the row: its training data, a memory a
    person wrote, or its rate card -- with the reference that says where."""
    return (
        ln.rate_source in ("training_data", "memory", "ratecard")
        and bool((ln.source_ref or "").strip())
        and not ln.needs_input and ln.rate is not None
    )


def line_has_own_evidence(ln) -> bool:
    """Priced from something the platform stands behind: the firm's rate data,
    a market price its own research verified, or its own build-up."""
    return line_has_firm_rate(ln) or _has_market_evidence(ln) or is_platform_buildup(ln)


def _strip_basis(note: str) -> str:
    """A note without the basis line a previous settle wrote, so settling
    twice writes it once."""
    for p in _BASIS_PREFIXES:
        if note.startswith(p):
            head, sep, rest = note.partition("\n")
            return rest if sep else ""
    return note


def _basis_of(ln) -> Optional[str]:
    note = ln.cost_buildup_note or ""
    for p in _BASIS_PREFIXES:
        if note.startswith(p):
            return p
    return None


def settle_rates_on_evidence(
    db: Session,
    breakdown_id: int,
    overhead_pct: float,
    margin_pct: float,
    *,
    gst_pct: float = 0.0,
    taxes_inclusive: Optional[dict] = None,
) -> dict:
    """Give every schedule row the figure its evidence supports, and say in
    plain words which one it is. The railway's rate is what a row is checked
    against, never what it is priced at -- except, labelled, when nothing of
    the platform's own could be finished for it.

    * A market price the platform's research verified, or the firm's own rate
      data, within `_PLAUSIBLE_BAND` of the railway's cost (published rate
      less overhead and margin, and less GST where the schedule's banner
      says its rates include it) stands.
    * The platform's own cost build-up stands (costing/cost_buildup.py). It
      has already been held to the band: a build-up more than 2x from the
      railway's cost was re-derived and the nearer of the two kept.
    * The costing agent's own build-up stands when it is within the band; it
      is priced blind (0.15x-6.45x of the railway's cost on the Mid-Life NIT
      before the platform built its own), so outside the band it is not
      trusted.
    * Only a row left with none of these takes the railway's cost, and its
      note says so and why. With the platform's build-up on, that is a row
      the build-up could not finish in time.
    * A row with no published rate keeps its figure: there is nothing to
      check it against.

    `taxes_inclusive` maps a schedule code to whether its banner says its
    rates include taxes (schedule_context). Tax lines, components, nil rows
    and component roll-ups are left alone. Idempotent. Commits. Returns
    {"market": n, "buildup": n, "reference": n, "replaced": n}; "replaced"
    counts rows whose figure changed this call.
    """
    divisor = 1.0 + (float(overhead_pct or 0) + float(margin_pct or 0)) / 100.0
    if divisor <= 0:
        divisor = 1.0
    lo, hi = _PLAUSIBLE_BAND
    strip = f"{overhead_pct:g}% overhead and {margin_pct:g}% margin"
    gst_div = 1.0 + float(gst_pct or 0) / 100.0
    counts = {"market": 0, "buildup": 0, "reference": 0, "replaced": 0}
    lines = (
        db.query(CostBreakdownLine)
        .filter(CostBreakdownLine.cost_breakdown_id == breakdown_id)
        .all()
    )
    for ln in lines:
        if ln.is_tax_line or _is_component_line(ln) or ln.needs_input:
            continue
        if ln.rate_source in (PRINTED_NIL_SOURCE, COMPONENT_BUILDUP_SOURCE):
            continue
        try:
            rate = float(ln.rate)
        except (TypeError, ValueError):
            continue
        if rate < 0:
            continue
        settled = _basis_of(ln)
        note = _strip_basis(ln.cost_buildup_note or "")
        market = _has_market_evidence(ln)
        firm = line_has_firm_rate(ln)
        platform = is_platform_buildup(ln)
        try:
            tr = float(ln.tender_rate)
        except (TypeError, ValueError):
            tr = 0.0
        if tr <= 0:
            # Nothing published to measure against: the figure is the best there is.
            basis = BASIS_MARKET if (market or firm) else BASIS_BUILDUP
            if market or firm:
                words = ("the firm's own rate data" if firm
                         else "current market price from the cited source")
            elif platform:
                words = "the platform's own cost build-up (materials, labour and current prices)"
            else:
                words = "cost build-up (materials and labour); the tender prints no rate"
            ln.cost_buildup_note = f"{basis} -- {words}.\n{note}".strip()
            counts["market" if (market or firm) else "buildup"] += 1
            continue
        if settled == BASIS_REFERENCE and _is_formula_derived(ln):
            counts["reference"] += 1  # settled already, reason and all
            continue
        inclusive = bool((taxes_inclusive or {}).get((ln.schedule_name or "").strip().upper()))
        reference = tr / divisor / (gst_div if inclusive else 1.0)
        ratio = rate / reference if reference else None
        in_band = ratio is not None and lo <= ratio <= hi
        if (market or firm) and in_band:
            words = ("the firm's own rate data" if firm
                     else "current market price from the cited source")
            ln.cost_buildup_note = f"{BASIS_MARKET} -- {words}.\n{note}".strip()
            counts["market"] += 1
            continue
        taken_off = "overhead, margin and GST" if inclusive else "overhead and margin"
        if platform:
            ln.cost_buildup_note = (
                f"{BASIS_BUILDUP} -- the platform's own cost build-up: materials at current "
                f"prices, labour hours at minimum wages plus statutory costs. The railway's "
                f"rate of Rs {tr:,.2f} implies a cost of Rs {reference:,.2f} once "
                f"{taken_off} are taken off; this build-up is {ratio:.2f} times that.\n{note}"
            ).strip()
            counts["buildup"] += 1
            continue
        if (
            ln.rate_source == "derived_estimate" and not market and not firm
            and not _is_formula_derived(ln) and in_band
        ):
            ln.cost_buildup_note = (
                f"{BASIS_BUILDUP} -- cost build-up (materials and labour), {ratio:.2f} times "
                f"the cost of Rs {reference:,.2f} the railway's rate implies once {taken_off} "
                f"are taken off.\n{note}"
            ).strip()
            counts["buildup"] += 1
            continue
        if _is_formula_derived(ln):
            reason = "neither a verified market price nor a cost build-up could be completed for this row"
        elif market or firm:
            reason = (f"the {'firm rate' if firm else 'market price'} found "
                      f"(Rs {rate:,.2f}) was {ratio:.2f}x the railway's figure, too far off to "
                      f"be the same item and specification, and no cost build-up was completed")
        else:
            reason = (f"the costing agent's own build-up came to Rs {rate:,.2f} ({ratio:.2f}x "
                      f"the railway's figure) and could not be checked by a build-up of the "
                      f"platform's own")
        gst_words = f", less {gst_pct:g}% GST (the schedule's rates include it)" if inclusive else ""
        # The copied-rate guard's own formula line is restated below.
        note = "\n".join(x for x in note.split("\n") if not x.startswith(_FORMULA_NOTE))
        ln.cost_buildup_note = (
            f"{BASIS_REFERENCE} -- {reason}, so the cost is the railway's own estimate "
            f"(built from last accepted rates) less {strip}{gst_words}.\n"
            f"{_FORMULA_NOTE} Rs {tr:,.2f} by stripping {overhead_pct:g}% overhead + "
            f"{margin_pct:g}% margin (÷{divisor:.3f}){' and GST' if inclusive else ''}.\n{note}"
        ).strip()
        counts["reference"] += 1
        new_rate = round(reference, 2)
        if abs(new_rate - rate) < 0.005 and ln.rate_source == "derived_estimate":
            continue
        q = float(ln.quantity or 0)
        ln.rate = new_rate
        ln.rate_low = ln.rate_high = None
        ln.amount = round(new_rate * q, 2)
        ln.amount_low = ln.amount_high = None
        ln.rate_source = "derived_estimate"
        ln.source_url = None
        ln.confidence = "medium"
        if ln.tender_amount:
            ln.margin_amount = round(float(ln.tender_amount) - ln.amount, 2)
            ln.margin_pct = round((ln.margin_amount / float(ln.tender_amount)) * 100.0, 2)
        counts["replaced"] += 1
    db.commit()
    logger.info(
        f"[cost_breakdown] breakdown {breakdown_id}: rates settled on evidence -- "
        f"{counts['market']} market or firm rate, {counts['buildup']} cost build-up, "
        f"{counts['reference']} railway estimate ({counts['replaced']} changed)"
    )
    return counts


def _gst_inclusive_margin_observation(
    db: Session, tender_id: Optional[int], schedule_breakdown: list[dict], gst_pct: float,
) -> Optional[str]:
    """The margin on the schedules whose banner says their rates include all
    taxes, once the GST in their tender value is taken out.

    The estimated cost is before GST and the tender value of such a schedule
    is after it, so the gross margin the table shows for it carries the GST
    as though it were the firm's -- about 15 points at 18%. Liluah's Mid-Life
    NIT prints "(INCLUSIVE OF ALL TAXES AND CHARGES)" on schedules A to O.
    """
    if not tender_id or gst_pct <= 0:
        return None
    try:
        from app.services.costing.schedule_context import schedule_contexts
        ctx = schedule_contexts(db, tender_id, read_documents=False)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None
    inclusive = {c for c, x in ctx.items() if x.taxes_inclusive}
    if not inclusive:
        return None
    rows = [s for s in schedule_breakdown
            if s["schedule"].replace("Schedule ", "", 1).strip().upper() in inclusive
            and s["tender_value_inr"] > 0]
    if not rows:
        return None
    tv = sum(s["tender_value_inr"] for s in rows)
    ec = sum(s["estimated_cost_inr"] for s in rows)
    tv_ex = tv / (1.0 + gst_pct / 100.0)
    gm = tv_ex - ec
    names = ", ".join(sorted(s["schedule"].replace("Schedule ", "", 1) for s in rows))
    return (
        f"Schedule(s) {names} print their rates inclusive of all taxes, so their tender value "
        f"(Rs {tv:,.0f}) includes {gst_pct:g}% GST that is not the firm's margin: against "
        f"Rs {tv_ex:,.0f} without GST, their estimated cost of Rs {ec:,.0f} leaves a margin of "
        f"Rs {gm:,.0f} ({(gm / tv_ex * 100.0) if tv_ex else 0.0:.1f}%)."
    )


def build_strategic_summary(
    db: Session,
    breakdown: CostBreakdown,
    analysis_result: Optional[dict] = None,
    tender_id: Optional[int] = None,
) -> dict:
    """Deterministically build a `strategic_summary` from a costed breakdown so
    the export Summary sheet is never blank (the batched path otherwise never
    sets it). No LLM call — schedule-wise profitability is computed from the
    persisted lines, the snapshot from the Tender row + analysis summary, and a
    few observations from the totals.
    """
    analysis_result = analysis_result or {}
    lines = list(breakdown.lines)

    # Per-schedule profitability (non-tax lines).
    agg: dict[str, list[float]] = {}
    order: list[str] = []
    for ln in lines:
        if ln.is_tax_line or _is_component_line(ln):
            # Components are inside their parent's estimated cost already.
            continue
        sched = (ln.schedule_name or "?").strip() or "?"
        if sched not in agg:
            agg[sched] = [0.0, 0.0]
            order.append(sched)
        q = float(ln.quantity or 0)
        tv = ln.tender_amount if ln.tender_amount is not None else ((ln.tender_rate or 0) * q)
        agg[sched][0] += float(tv or 0)
        agg[sched][1] += float(ln.amount or 0)

    schedule_breakdown: list[dict] = []
    for sched in sorted(order, key=lambda s: (s == "?", s)):
        tv, ec = agg[sched]
        gm = tv - ec
        schedule_breakdown.append({
            "schedule": f"Schedule {sched}" if sched != "?" else "Other",
            "tender_value_inr": round(tv, 2),
            "estimated_cost_inr": round(ec, 2),
            "gross_margin_inr": round(gm, 2),
            "gross_margin_pct": round((gm / tv * 100.0) if tv else 0.0, 2),
        })

    # Tender snapshot from the Tender row (+ analysis summary fallback for scope).
    snapshot: dict = {}
    try:
        from app.models.tender import Tender
        t = db.query(Tender).filter(Tender.id == tender_id).first() if tender_id else None
        if t:
            if getattr(t, "tender_id", None):
                snapshot["tender_no"] = t.tender_id
            if getattr(t, "title", None):
                snapshot["scope_one_liner"] = (t.title or "")[:240]
            if getattr(t, "estimated_value", None):
                snapshot["tender_value_inr"] = float(t.estimated_value)
            if getattr(t, "emd_amount", None):
                snapshot["emd_inr"] = float(t.emd_amount)
    except Exception:
        pass
    if not snapshot.get("scope_one_liner"):
        s = (analysis_result.get("summary") or "").strip()
        if s:
            snapshot["scope_one_liner"] = s.split("\n")[0][:240]

    total_tv = sum(s["tender_value_inr"] for s in schedule_breakdown)
    total_ec = sum(s["estimated_cost_inr"] for s in schedule_breakdown)
    total_gm = total_tv - total_ec
    total_gp = (total_gm / total_tv * 100.0) if total_tv else 0.0

    obs: list[str] = []
    if total_tv:
        obs.append(
            f"Total contract value Rs {total_tv:,.0f}; estimated cost "
            f"Rs {total_ec:,.0f}; blended gross margin {total_gp:.1f}% across "
            f"{len(schedule_breakdown)} schedule(s)."
        )
        priced = [s for s in schedule_breakdown if s["tender_value_inr"] > 0]
        if len(priced) > 1:
            hi = max(priced, key=lambda s: s["gross_margin_pct"])
            lo = min(priced, key=lambda s: s["gross_margin_pct"])
            obs.append(
                f"Highest margin: {hi['schedule']} ({hi['gross_margin_pct']:.1f}%); "
                f"lowest: {lo['schedule']} ({lo['gross_margin_pct']:.1f}%)."
            )
        _gst_obs = _gst_inclusive_margin_observation(
            db, tender_id or getattr(breakdown, "tender_id", None), schedule_breakdown,
            float(getattr(breakdown, "gst_percent", None) or 0.0),
        )
        if _gst_obs:
            obs.append(_gst_obs)
    needs = sum(1 for ln in lines if ln.needs_input and not ln.is_tax_line)
    # How much of the estimated cost rests on the published rate divided by
    # the org's overhead+margin (RULE 2b) rather than on evidence. Such a row
    # always shows the same margin, so a schedule made of them says nothing
    # about real cost, and the Summary has to say so.
    derived_cost = 0.0
    evidenced_tv = 0.0
    evidenced_cost = 0.0
    derived_lines = 0
    for ln in lines:
        if ln.is_tax_line or _is_component_line(ln):
            continue
        amt = float(ln.amount or 0)
        q = float(ln.quantity or 0)
        tv = float(ln.tender_amount if ln.tender_amount is not None else ((ln.tender_rate or 0) * q) or 0)
        if _is_formula_derived(ln):
            derived_cost += amt
            derived_lines += 1
        elif amt or tv:
            evidenced_tv += tv
            evidenced_cost += amt
    inconsistent = sum(
        1 for ln in lines if _is_component_line(ln)
        and ln.quantity is not None and ln.tender_rate is not None and (ln.basic_value or 0) > 0
        and abs(ln.quantity * ln.tender_rate - ln.basic_value) > max(0.5, 0.02 * ln.basic_value)
    )
    if inconsistent:
        obs.append(f"{inconsistent} component row(s) have quantities/rates that disagree with their captured printed totals. Verify these against the source document before bidding.")
    if needs:
        obs.append(f"{needs} line item(s) still need manual pricing (no firm/training/web cost found).")
    basis = {BASIS_MARKET: 0, BASIS_BUILDUP: 0, BASIS_REFERENCE: 0}
    for ln in lines:
        if ln.is_tax_line or _is_component_line(ln):
            continue
        b = _basis_of(ln)
        if b:
            basis[b] += 1
    if any(basis.values()):
        obs.append(
            f"How each line was costed: {basis[BASIS_BUILDUP]} by a cost build-up (what one "
            f"unit is, its materials at current prices, labour hours at the minimum wages "
            f"plus statutory costs), {basis[BASIS_MARKET]} at a verified current market price "
            f"or the firm's own rate data"
            + (f", and {basis[BASIS_REFERENCE]} from the railway's own estimate less overhead "
               f"and margin because no build-up could be completed for them" if basis[BASIS_REFERENCE] else "")
            + ". Each line states its basis at the head of its build-up note; the railway's "
            f"rates are the benchmark for margin, not the cost. Uploading the firm's own "
            f"purchase rates and wages as costing training data lets more lines be costed "
            f"from the firm's real prices."
        )
    if derived_lines and total_ec:
        share = derived_cost / total_ec * 100.0
        ev_margin = ((evidenced_tv - evidenced_cost) / evidenced_tv * 100.0) if evidenced_tv else None
        obs.append(
            f"{derived_lines} line item(s), Rs {derived_cost:,.0f} ({share:.0f}% of the estimated cost), "
            f"are derived from the published rate by formula (published / (1 + overhead + margin)), "
            f"not researched: their margin is the org default by construction and says nothing about "
            f"real cost."
            + (f" Margin on the researched lines alone: {ev_margin:.1f}% on Rs {evidenced_tv:,.0f} of "
               f"tender value." if ev_margin is not None else "")
        )
    obs.append(
        "Estimated costs are the firm's INDEPENDENT costs (the firm's own rate data, "
        "verified market prices, or the platform's own cost build-up) — the NIT "
        "published rate is the benchmark for margin, not the cost."
    )

    bid = (
        f"Bid at/near the tender value; blended gross margin ~{total_gp:.1f}%. "
        "Review the lowest-margin schedules before finalising."
    ) if total_tv else ""
    if inconsistent:
        bid = "Verify the inconsistent source quantities and printed totals before deciding a bid."
    elif needs:
        bid = "Complete the missing quantities/rates before deciding a bid; the estimated cost is incomplete."
    elif total_gm < 0:
        bid = "Estimated cost exceeds the tender value. Review quantities, rate evidence and loss-making schedules before deciding a bid."

    return {
        "tender_snapshot": snapshot,
        "schedule_breakdown": schedule_breakdown,
        "key_observations": obs,
        "recommended_bid_strategy": bid,
    }


def _build_parsed_nit_from_lines(
    db: Session, tender_id: int, lines: list[CostBreakdownLine]
) -> ParsedNIT:
    """Build a `ParsedNIT`-shaped object from persisted `CostBreakdownLine`s so
    the reconciliation gate (Task 2) can check sum(line amounts) per schedule
    against the tender's own captured stated totals (Task 3's
    `BOQScheduleTotal`). Lines are grouped by `schedule_name`; each
    `NITLine.amount` is the line's `tender_amount` (the tender's own published
    amount for that row) falling back to `amount` (our estimated cost) when
    `tender_amount` is absent — the gate is checking "did we capture every
    line", not "does our cost match the tender's price". Lines with no
    `schedule_name` can't be grouped against a stated total and are skipped.
    """
    totals_rows = (
        db.query(BOQScheduleTotal)
        .filter(BOQScheduleTotal.tender_id == tender_id)
        .all()
    )
    stated_by_code = {
        (r.schedule_code or "").strip().upper(): r.stated_total for r in totals_rows
    }
    advertised_value = next(
        (r.advertised_value for r in totals_rows if r.advertised_value is not None),
        None,
    )

    by_sched: dict[str, list[NITLine]] = {}
    order: list[str] = []
    for ln in lines:
        if ln.is_tax_line:
            # Tax lines (e.g. GST) sit on top of the schedule's own stated
            # total — including them would over-count the reconciliation sum
            # against a stated_total that doesn't include tax.
            continue
        code = (ln.schedule_name or "").strip().upper()
        if not code:
            continue
        if code not in by_sched:
            by_sched[code] = []
            order.append(code)
        amount = ln.tender_amount if ln.tender_amount is not None else ln.amount
        by_sched[code].append(NITLine(
            schedule_code=code,
            sr_no=ln.sr_no or 0,
            item_code=ln.item_code or "",
            description=ln.description or "",
            qty=ln.quantity,
            unit=ln.unit,
            unit_rate=None,
            basic_value=ln.basic_value,
            escalation_pct=ln.escalation_pct or 0.0,
            amount=amount,
            bidding_unit=ln.bidding_unit,
        ))

    schedules = tuple(
        NITSchedule(
            code=code, title="", stated_total=stated_by_code.get(code),
            lines=tuple(by_sched[code]),
        )
        for code in order
    )
    return ParsedNIT(advertised_value=advertised_value, schedules=schedules)


def _run_reconciliation_gate(db: Session, breakdown: CostBreakdown) -> None:
    """Reconcile a breakdown's persisted lines against the tender's own
    captured schedule totals and set `reconciliation_json` + `needs_review`
    on the ORM object (caller commits). Never mutates line amounts — flags
    only. No-op (clears any prior flag) when the tender has no captured
    `BOQScheduleTotal` rows to reconcile against."""
    has_totals = (
        db.query(BOQScheduleTotal.id)
        .filter(BOQScheduleTotal.tender_id == breakdown.tender_id)
        .first()
        is not None
    )
    if not has_totals:
        breakdown.reconciliation_json = None
        breakdown.needs_review = False
        return

    parsed = _build_parsed_nit_from_lines(db, breakdown.tender_id, list(breakdown.lines))
    report = reconcile(parsed)
    breakdown.reconciliation_json = json.dumps({
        "ok": report.ok,
        "schedules": [
            {
                "code": s.code,
                "line_sum": s.line_sum,
                "stated_total": s.stated_total,
                "delta": s.delta,
                "ok": s.ok,
            }
            for s in report.schedules
        ],
        "grand_line_sum": report.grand_line_sum,
        "advertised_value": report.advertised_value,
        "grand_delta": report.grand_delta,
    })
    breakdown.needs_review = not report.ok
    if not report.ok:
        logger.warning(
            f"[cost_breakdown] tender {breakdown.tender_id}: reconciliation "
            f"gate flagged breakdown {breakdown.id} for review "
            f"(grand_delta={report.grand_delta})"
        )


def recompute_breakdown_totals(db: Session, breakdown: CostBreakdown) -> CostBreakdown:
    """Recompute + persist a breakdown's three-band totals from its current
    lines (used after batch merges). Rolls annexure components into their
    parents first, so the totals see the parent's built-up rate and never the
    components themselves. Reuses `_compute_totals`. Commits."""
    rollup_component_lines(db, breakdown.id)
    db.refresh(breakdown)
    line_dicts = [
        {
            "quantity": ln.quantity,
            "rate": ln.rate,
            "amount": ln.amount,
            "rate_low": ln.rate_low,
            "rate_high": ln.rate_high,
            "amount_low": ln.amount_low,
            "amount_high": ln.amount_high,
            "profit_pct": ln.profit_pct,
            "tender_rate": ln.tender_rate,
            "tender_amount": ln.tender_amount,
            "rate_source": ln.rate_source,
            "parent_boq_item_id": ln.parent_boq_item_id,
        }
        for ln in breakdown.lines
    ]
    totals = _compute_totals(
        line_dicts,
        breakdown.overhead_percent,
        breakdown.margin_percent,
        breakdown.gst_percent,
    )
    breakdown.subtotal = totals["subtotal"]
    breakdown.overhead_amount = totals["overhead_amount"]
    breakdown.margin_amount = totals["margin_amount"]
    breakdown.gst_amount = totals["gst_amount"]
    breakdown.grand_total = totals["grand_total"]
    breakdown.subtotal_low = totals["subtotal_low"]
    breakdown.subtotal_high = totals["subtotal_high"]
    breakdown.overhead_amount_low = totals["overhead_amount_low"]
    breakdown.overhead_amount_high = totals["overhead_amount_high"]
    breakdown.margin_amount_low = totals["margin_amount_low"]
    breakdown.margin_amount_high = totals["margin_amount_high"]
    breakdown.gst_amount_low = totals["gst_amount_low"]
    breakdown.gst_amount_high = totals["gst_amount_high"]
    breakdown.grand_total_low = totals["grand_total_low"]
    breakdown.grand_total_high = totals["grand_total_high"]
    breakdown.tender_total = totals.get("tender_total")
    breakdown.margin_total_low = totals.get("margin_total_low")
    breakdown.margin_total = totals.get("margin_total")
    breakdown.margin_total_high = totals.get("margin_total_high")
    breakdown.margin_pct = totals.get("margin_pct")
    breakdown.needs_input_count = totals["needs_input_count"]
    breakdown.updated_at = datetime.now(timezone.utc)
    _run_reconciliation_gate(db, breakdown)
    db.commit()
    db.refresh(breakdown)
    return breakdown


def persist_from_agent_output(
    db: Session,
    tender_id: int,
    costing: dict,
    *,
    session_id: Optional[int] = None,
    created_by_agent: str = "costing_researcher",
    title: Optional[str] = None,
    skip_nit_validation: bool = False,
    cost_sheet_template: Optional[str] = None,
) -> Optional[CostBreakdown]:
    """Convert a costing agent JSON payload into a new CostBreakdown row.

    Always creates a NEW version — old versions are preserved for history.
    Returns the persisted CostBreakdown, or None if there are no line items.

    Stores the agent's manpower_resource_analysis decomposition alongside the
    line items so the editor can audit the build-up.

    Runs `validate_nit_mirror` when a captured NIT schedule exists for the
    tender — AUTHORITATIVE as of Task 7 (fabrication lockdown): any agent line
    whose `(schedule_name, item_code/sr_no)` is not a captured NIT row is
    DROPPED from the persisted schedule (never written as a CostBreakdownLine)
    and recorded instead as a quarantine note in `assumptions`. Remaining
    advisory issues on the surviving lines (missing rows, tax-line
    violations, etc.) are still surfaced in `assumptions` but do not block
    persistence — we never lose the agent's legitimate work over those.
    """
    line_items = costing.get("line_items") or []
    if not line_items:
        return None

    # RULE 5 authoritative gate — see plan: now-i-need-to-synchronous-taco.md
    # + Task 7 (fabrication lockdown). Skipped for component build-up output:
    # those lines are sub-components of the NIT scope (one NIT line -> many
    # spare-part lines), so a 1:1 mirror check would spuriously drop every row.
    quarantine_notes: list[str] = []
    quarantine_missing_keys: set = set()
    if not skip_nit_validation:
        line_items, quarantine_notes, quarantine_missing_keys = _partition_lines_against_schedule(
            db, tender_id, line_items,
        )
        if quarantine_notes:
            logger.warning(
                f"[cost_breakdown] tender {tender_id}: fabrication lockdown "
                f"quarantined {len(quarantine_notes)} line(s) against the "
                f"captured NIT schedule"
            )
            for note in quarantine_notes:
                logger.warning(f"[cost_breakdown]   ↳ {note}")
        if not line_items:
            logger.error(
                f"[cost_breakdown] tender {tender_id}: ALL agent line items "
                f"were quarantined against the captured NIT schedule — "
                f"nothing left to persist"
            )
            return None

    # Advisory pass on the surviving (schedule-matched) lines — duplicates and
    # invented rows are already gone, so what's left here is informational
    # (missing schedule rows the agent didn't return, tax-line contract
    # violations). Never drops anything further.
    validation_errors = [] if skip_nit_validation else validate_nit_mirror(db, tender_id, line_items)
    if quarantine_missing_keys:
        # De-dupe: `_partition_lines_against_schedule` already reported these
        # exact (schedule_name, item_code) keys as "missing from agent
        # output" quarantine notes — don't let validate_nit_mirror's advisory
        # pass repeat the same note a second time in `assumptions`.
        validation_errors = [
            err
            for err in validation_errors
            if not any(
                f"(schedule_name={sched!r}, item_code={code!r}) missing from agent output" in err
                for sched, code in quarantine_missing_keys
            )
        ]
    if validation_errors:
        logger.warning(
            f"[cost_breakdown] tender {tender_id}: NIT-mirror validation found "
            f"{len(validation_errors)} advisory issue(s) — persisting anyway "
            f"and surfacing in assumptions for user review"
        )
        for err in validation_errors:
            logger.warning(f"[cost_breakdown]   ↳ {err}")

    all_notes = [f"⚠️ SCHEDULE VALIDATION: {e}" for e in validation_errors] + quarantine_notes
    if all_notes:
        existing_assumptions = costing.get("assumptions") or []
        costing = dict(costing)  # don't mutate caller's payload
        costing["assumptions"] = all_notes + list(existing_assumptions)

    # Content-based dedup (design: 2026-07-13-costing-dedup). Runs on EVERY
    # agent-JSON route — including component build-up, which sets
    # skip_nit_validation=True and thus bypasses _partition_lines_against_schedule
    # entirely. Collapses lines describing the same work (same normalized
    # description + qty + unit) that the LLM emitted twice with different
    # rate_source / SN, keeping the best-evidence one. The deterministic batched
    # path never reaches here and is already 1:1.
    line_items, dup_dropped = _dedup_lines_by_content(line_items)
    if dup_dropped:
        logger.warning(
            f"[cost_breakdown] tender {tender_id}: content-dedup dropped "
            f"{dup_dropped} duplicate line(s)"
        )

    defaults = get_costing_defaults(db)

    # Normalise lines and recompute totals server-side (the agent's totals
    # are advisory; the source of truth is what we save here).
    normalised = [
        _normalize_line_dict(
            line,
            sr_no=int(line.get("sr_no") or i + 1),
            breakdown_margin_percent=defaults["margin_percent"],
        )
        for i, line in enumerate(line_items)
    ]

    # Scrub FK references that don't exist for THIS tender. Without this,
    # an agent hallucinating boq_item_id values (e.g. when the bidding
    # schedule capture produced zero BOQItem rows because the analyzer's
    # DB session dropped mid-run) crashes the whole commit with a
    # ForeignKeyViolation — surfacing as a red `persistence_failed` bubble
    # to the user instead of a saved cost breakdown. We keep the cost data
    # and drop only the bogus linkage.
    valid_boq_ids = {
        row.id
        for row in db.query(BOQItem.id).filter(BOQItem.tender_id == tender_id).all()
    }
    scrubbed_count = 0
    for line in normalised:
        bid = line.get("boq_item_id")
        if bid is not None and int(bid) not in valid_boq_ids:
            line["boq_item_id"] = None
            scrubbed_count += 1
    if scrubbed_count > 0:
        logger.warning(
            f"[cost_breakdown] tender {tender_id}: scrubbed {scrubbed_count} "
            f"invalid boq_item_id reference(s) from agent output "
            f"(tender has {len(valid_boq_ids)} captured BOQItem row(s))"
        )
        warning_text = (
            f"⚠️ SCHEDULE LINK: {scrubbed_count} line(s) referenced BOQ rows that "
            f"don't exist for this tender — the cost values were preserved but "
            f"the link to the captured NIT schedule was dropped. This usually "
            f"means the analyzer's schedule capture didn't complete; re-running "
            f"the tender analyzer will restore the 1:1 NIT mirror."
        )
        existing_assumptions = costing.get("assumptions") or []
        costing = dict(costing)
        costing["assumptions"] = [warning_text] + list(existing_assumptions)
    return _create_breakdown_with_lines(
        db,
        tender_id,
        normalised,
        costing,
        defaults,
        created_by_agent=created_by_agent,
        session_id=session_id,
        title=title,
        cost_sheet_template=cost_sheet_template,
    )


def get_latest_for_tender(db: Session, tender_id: int) -> Optional[CostBreakdown]:
    """Return the highest-version CostBreakdown for a tender (None if none)."""
    return (
        db.query(CostBreakdown)
        .filter(CostBreakdown.tender_id == tender_id)
        .order_by(CostBreakdown.version.desc())
        .first()
    )


def replace_lines(
    db: Session,
    breakdown_id: int,
    lines: list[dict],
    *,
    edited_by: Optional[int] = None,
    overhead_percent: Optional[float] = None,
    margin_percent: Optional[float] = None,
    gst_percent: Optional[float] = None,
) -> Optional[CostBreakdown]:
    """Replace all lines of an existing CostBreakdown with the user-edited set.

    Recomputes totals server-side. Optionally updates the per-breakdown
    overhead/margin/GST percentages (per-tender override of org defaults).
    """
    breakdown = db.query(CostBreakdown).filter(CostBreakdown.id == breakdown_id).first()
    if not breakdown:
        return None

    if overhead_percent is not None:
        breakdown.overhead_percent = float(overhead_percent)
    if margin_percent is not None:
        breakdown.margin_percent = float(margin_percent)
    if gst_percent is not None:
        breakdown.gst_percent = float(gst_percent)

    # Wipe + replace lines
    db.query(CostBreakdownLine).filter(
        CostBreakdownLine.cost_breakdown_id == breakdown.id
    ).delete()
    db.flush()

    normalised = [
        _normalize_line_dict(
            line,
            sr_no=int(line.get("sr_no") or i + 1),
            breakdown_margin_percent=breakdown.margin_percent,
        )
        for i, line in enumerate(lines)
    ]
    for line in normalised:
        # User-edited lines that have a rate get marked as user_override unless
        # the source was already explicit.
        if line.get("rate") is not None and not line.get("rate_source"):
            line["rate_source"] = "user_override"
    # A user who edits a component's rate expects the item it belongs to to
    # move with it. Same deterministic roll-up as the costing run's.
    rollup_component_parents(normalised)
    for line in normalised:
        db.add(CostBreakdownLine(cost_breakdown_id=breakdown.id, **line))

    totals = _compute_totals(
        normalised,
        breakdown.overhead_percent,
        breakdown.margin_percent,
        breakdown.gst_percent,
    )
    breakdown.subtotal = totals["subtotal"]
    breakdown.overhead_amount = totals["overhead_amount"]
    breakdown.margin_amount = totals["margin_amount"]
    breakdown.gst_amount = totals["gst_amount"]
    breakdown.grand_total = totals["grand_total"]
    breakdown.subtotal_low = totals["subtotal_low"]
    breakdown.subtotal_high = totals["subtotal_high"]
    breakdown.overhead_amount_low = totals["overhead_amount_low"]
    breakdown.overhead_amount_high = totals["overhead_amount_high"]
    breakdown.margin_amount_low = totals["margin_amount_low"]
    breakdown.margin_amount_high = totals["margin_amount_high"]
    breakdown.gst_amount_low = totals["gst_amount_low"]
    breakdown.gst_amount_high = totals["gst_amount_high"]
    breakdown.grand_total_low = totals["grand_total_low"]
    breakdown.grand_total_high = totals["grand_total_high"]
    # Phase 3b — margin-analysis roll-up (recomputed from edited lines)
    breakdown.tender_total = totals.get("tender_total")
    breakdown.margin_total_low = totals.get("margin_total_low")
    breakdown.margin_total = totals.get("margin_total")
    breakdown.margin_total_high = totals.get("margin_total_high")
    breakdown.margin_pct = totals.get("margin_pct")
    breakdown.needs_input_count = totals["needs_input_count"]
    breakdown.last_edited_by = edited_by
    breakdown.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(breakdown)
    _run_reconciliation_gate(db, breakdown)
    db.commit()
    db.refresh(breakdown)
    return breakdown


def to_dict(breakdown: CostBreakdown) -> dict:
    """Serialize a CostBreakdown + its lines for API responses."""
    try:
        assumptions = json.loads(breakdown.assumptions_json) if breakdown.assumptions_json else []
    except Exception:
        assumptions = []
    try:
        recommendations = json.loads(breakdown.recommendations_json) if breakdown.recommendations_json else []
    except Exception:
        recommendations = []
    try:
        manpower_resource_analysis = (
            json.loads(breakdown.manpower_resource_analysis_json)
            if breakdown.manpower_resource_analysis_json
            else []
        )
    except Exception:
        manpower_resource_analysis = []
    try:
        cost_assumptions = (
            json.loads(breakdown.cost_assumptions_json)
            if breakdown.cost_assumptions_json
            else []
        )
    except Exception:
        cost_assumptions = []
    try:
        strategic_summary = (
            json.loads(breakdown.strategic_summary_json)
            if breakdown.strategic_summary_json
            else None
        )
    except Exception:
        strategic_summary = None
    try:
        reconciliation = (
            json.loads(breakdown.reconciliation_json)
            if breakdown.reconciliation_json
            else None
        )
    except Exception:
        reconciliation = None

    lines = []
    for ln in order_lines_for_display(breakdown.lines):
        lines.append({
            "id": ln.id,
            "sr_no": ln.sr_no,
            "description": ln.description,
            "category": ln.category,
            "quantity": ln.quantity,
            "unit": ln.unit,
            "rate": ln.rate,
            "amount": ln.amount,
            "rate_low": ln.rate_low,
            "rate_high": ln.rate_high,
            "amount_low": ln.amount_low,
            "amount_high": ln.amount_high,
            "profit_pct": ln.profit_pct,
            "profit_amount_low": ln.profit_amount_low,
            "profit_amount": ln.profit_amount,
            "profit_amount_high": ln.profit_amount_high,
            # Phase 3b — margin-analysis fields
            "tender_rate": ln.tender_rate,
            "tender_amount": ln.tender_amount,
            "margin_amount_low": ln.margin_amount_low,
            "margin_amount": ln.margin_amount,
            "margin_amount_high": ln.margin_amount_high,
            "margin_pct": ln.margin_pct,
            "schedule_section": ln.schedule_section,
            "cost_buildup_note": ln.cost_buildup_note,
            # Component build-up fields (client-annexure layout).
            "item_code": ln.item_code,
            "annexure": ln.annexure,
            # NIT-mirror identity, so an edit round-trips it (see the PUT
            # payload in routes/cost_breakdown.py) and the editor can show it.
            "boq_item_id": ln.boq_item_id,
            "schedule_name": ln.schedule_name,
            "bidding_unit": ln.bidding_unit,
            "basic_value": ln.basic_value,
            "escalation_pct": ln.escalation_pct,
            "is_tax_line": bool(ln.is_tax_line),
            # Annexure component link
            "parent_boq_item_id": ln.parent_boq_item_id,
            "annexure_ref": ln.annexure_ref,
            "rate_source": ln.rate_source,
            "source_ref": ln.source_ref,
            "oem_manufacturer": ln.oem_manufacturer,
            "source_url": ln.source_url,
            "confidence": ln.confidence,
            "needs_input": ln.needs_input,
            "notes": ln.notes,
        })

    return {
        "id": breakdown.id,
        "tender_id": breakdown.tender_id,
        "version": breakdown.version,
        "status": breakdown.status,
        "title": breakdown.title,
        "cost_sheet_template": breakdown.cost_sheet_template,
        "overhead_percent": breakdown.overhead_percent,
        "margin_percent": breakdown.margin_percent,
        "gst_percent": breakdown.gst_percent,
        "subtotal": breakdown.subtotal,
        "overhead_amount": breakdown.overhead_amount,
        "margin_amount": breakdown.margin_amount,
        "gst_amount": breakdown.gst_amount,
        "grand_total": breakdown.grand_total,
        "subtotal_low": breakdown.subtotal_low,
        "subtotal_high": breakdown.subtotal_high,
        "overhead_amount_low": breakdown.overhead_amount_low,
        "overhead_amount_high": breakdown.overhead_amount_high,
        "margin_amount_low": breakdown.margin_amount_low,
        "margin_amount_high": breakdown.margin_amount_high,
        "gst_amount_low": breakdown.gst_amount_low,
        "gst_amount_high": breakdown.gst_amount_high,
        "grand_total_low": breakdown.grand_total_low,
        "grand_total_high": breakdown.grand_total_high,
        # Phase 3b — margin-analysis roll-up
        "tender_total": breakdown.tender_total,
        "margin_total_low": breakdown.margin_total_low,
        "margin_total": breakdown.margin_total,
        "margin_total_high": breakdown.margin_total_high,
        "margin_pct": breakdown.margin_pct,
        "needs_input_count": breakdown.needs_input_count,
        "assumptions": assumptions,
        "recommendations": recommendations,
        "manpower_resource_analysis": manpower_resource_analysis,
        # Phase 3b — cost_assumptions library + strategic_summary
        "cost_assumptions": cost_assumptions,
        "strategic_summary": strategic_summary,
        # Task 5 — reconciliation gate (never mutates line amounts; flag only)
        "reconciliation": reconciliation,
        "needs_review": breakdown.needs_review,
        "created_by_agent": breakdown.created_by_agent,
        "session_id": breakdown.session_id,
        "artifact_id": breakdown.artifact_id,
        "created_at": breakdown.created_at.isoformat() if breakdown.created_at else None,
        "updated_at": breakdown.updated_at.isoformat() if breakdown.updated_at else None,
        "lines": lines,
    }


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_rows_from_breakdown(breakdown: CostBreakdown) -> list[dict]:
    """Build the XLSX row dicts from a persisted breakdown's lines (the NIT-
    mirror columns trigger the IREPS layout in build_cost_xlsx)."""
    rows: list[dict] = []
    for ln in order_lines_for_display(breakdown.lines):
        source_ref = ln.source_ref or ""
        if ln.needs_input and not source_ref:
            source_ref = "NEEDS USER INPUT"

        # Compute total-with-GST per band when range data is available so the
        # XLSX shows the same Low / Expected / High total as the editor.
        gst_factor = 1 + (breakdown.gst_percent / 100.0)
        # NIT-mirror columns (RULE 5). When present on the row, the XLSX
        # builder switches to the IREPS-format layout. We keep `quantity`
        # under the `qty` alias for backward compatibility with the legacy
        # builder, AND set `quantity` directly so the NIT builder's column
        # map (which uses "quantity") resolves cleanly.
        rows.append({
            "description": ln.description or "",
            "category": ln.category or "",
            "qty": ln.quantity,
            "quantity": ln.quantity,
            "unit": ln.unit or "",
            "rate": None if ln.needs_input else ln.rate,
            "rate_low": None if ln.needs_input else ln.rate_low,
            "rate_high": None if ln.needs_input else ln.rate_high,
            "amount": None if ln.needs_input else ln.amount,
            "amount_low": None if ln.needs_input else ln.amount_low,
            "amount_high": None if ln.needs_input else ln.amount_high,
            "profit_pct": None if ln.needs_input else ln.profit_pct,
            "profit_amount": None if ln.needs_input else ln.profit_amount,
            "profit_amount_low": None if ln.needs_input else ln.profit_amount_low,
            "profit_amount_high": None if ln.needs_input else ln.profit_amount_high,
            # Phase 3b — margin-analysis + schedule grouping fields
            "tender_rate": ln.tender_rate,
            "tender_amount": ln.tender_amount,
            "margin_amount_low": None if ln.needs_input else ln.margin_amount_low,
            "margin_amount": None if ln.needs_input else ln.margin_amount,
            "margin_amount_high": None if ln.needs_input else ln.margin_amount_high,
            "margin_pct": None if ln.needs_input else ln.margin_pct,
            "schedule_section": ln.schedule_section or "",
            "cost_buildup_note": ln.cost_buildup_note or "",
            # NIT-mirror — trigger the IREPS-format layout when populated.
            "boq_item_id": ln.boq_item_id,
            "item_code": ln.item_code or "",
            "schedule_name": ln.schedule_name or "",
            "bidding_unit": ln.bidding_unit or "",
            "basic_value": ln.basic_value,
            "escalation_pct": ln.escalation_pct,
            "is_tax_line": bool(ln.is_tax_line),
            # Component build-up grouping — triggers the client-annexure layout.
            "annexure": ln.annexure or "",
            # Annexure component link — the workbook keeps these rows out of
            # its grand total (they are inside their parent's Est. Rate).
            "parent_boq_item_id": ln.parent_boq_item_id,
            "annexure_ref": ln.annexure_ref or "",
            "is_component": ln.parent_boq_item_id is not None,
            "sr_no": ln.sr_no,
            "gst_pct": breakdown.gst_percent,
            "total": None if ln.needs_input else ((ln.amount or 0) * gst_factor),
            "total_low": None if (ln.needs_input or ln.amount_low is None) else (ln.amount_low * gst_factor),
            "total_high": None if (ln.needs_input or ln.amount_high is None) else (ln.amount_high * gst_factor),
            "rate_source": ln.rate_source or "",
            "source_ref": source_ref,
            "oem_manufacturer": ln.oem_manufacturer or "",
            "source_url": ln.source_url or "",
        })
    return rows


def render_breakdown_xlsx_bytes(
    db: Session,
    breakdown: CostBreakdown,
    *,
    single_sheet: Optional[bool] = None,
    cost_sheet_template: Optional[str] = None,
) -> Optional[tuple]:
    """Render the cost-breakdown XLSX from the persisted breakdown.

    Returns ``(fname, data_bytes, summary, rows)`` or ``None`` when the
    breakdown has no lines. The workbook is built to a temp file (build_cost_xlsx
    needs a path), read into memory, and the temp file removed — nothing is left
    on local disk; callers persist the bytes via ``storage_service``.
    """
    import tempfile
    from app.services.langchain.tools.xlsx_generator_tool import build_cost_xlsx

    rows = _xlsx_rows_from_breakdown(breakdown)
    if not rows:
        return None

    title = breakdown.title or f"Cost Breakdown — Tender #{breakdown.tender_id}"
    cost_assumptions = []
    strategic_summary = None
    try:
        if breakdown.cost_assumptions_json:
            cost_assumptions = json.loads(breakdown.cost_assumptions_json) or []
    except Exception:
        cost_assumptions = []
    try:
        if breakdown.strategic_summary_json:
            strategic_summary = json.loads(breakdown.strategic_summary_json)
    except Exception:
        strategic_summary = None

    try:
        reconciliation = (
            json.loads(breakdown.reconciliation_json)
            if breakdown.reconciliation_json
            else None
        )
    except Exception:
        reconciliation = None

    breakdown_meta = {
        "tender_id": breakdown.tender_id,
        "version": breakdown.version,
        "overhead_percent": breakdown.overhead_percent,
        "margin_percent": breakdown.margin_percent,
        "gst_percent": breakdown.gst_percent,
        "subtotal": breakdown.subtotal,
        "grand_total": breakdown.grand_total,
        "tender_total": breakdown.tender_total,
        "margin_total": breakdown.margin_total,
        "margin_total_low": breakdown.margin_total_low,
        "margin_total_high": breakdown.margin_total_high,
        "margin_pct": breakdown.margin_pct,
        # Task 6 — reconciliation gate (Task 5) surfaced in the xlsx.
        "reconciliation": reconciliation,
    }

    # Resolve the single-sheet default from settings when the caller didn't
    # specify. Default is True: users want ALL schedules consolidated into one
    # tab (the NIT-replica view). Set costing.nit_single_sheet=False to fall
    # back to the legacy one-tab-per-schedule layout.
    if single_sheet is None:
        try:
            from app.services.settings_service import get_setting_value
            single_sheet = bool(get_setting_value(db, "costing.nit_single_sheet", True))
        except Exception:
            single_sheet = True

    # Resolve the output layout: explicit override > the value persisted on the
    # breakdown > auto-detect (None). build_cost_xlsx treats a None/blank layout
    # as "sniff the rows" (legacy behaviour).
    layout = (cost_sheet_template or breakdown.cost_sheet_template or "").strip() or None

    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        summary = build_cost_xlsx(
            tmp, title, rows,
            cost_assumptions=cost_assumptions,
            strategic_summary=strategic_summary,
            breakdown_meta=breakdown_meta,
            single_sheet=single_sheet,
            layout=layout,
        )
        with open(tmp, "rb") as f:
            data = f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"cost_tender_{breakdown.tender_id}_{ts}_{uuid.uuid4().hex[:6]}.xlsx"
    return fname, data, summary, rows


def _upload_xlsx(key: str, data: bytes) -> None:
    from app.services.storage_service import get_storage_service

    get_storage_service().upload_file_sync(key, data, content_type=_XLSX_MIME)


def persist_xlsx_artifact(
    db: Session,
    *,
    session_id: int,
    title: str,
    fname: str,
    data: bytes,
    rows: list,
    summary: dict,
    structured_extra: Optional[dict] = None,
    metadata_extra: Optional[dict] = None,
) -> dict:
    """Store a rendered workbook and create its artifact, then queue the preview.

    The single place a `cost_breakdown_xlsx` artifact is born. Both producers
    (this module's `regenerate_xlsx_artifact` and the chat wrapper) call it, so
    the preview render can never be skipped by a new call site.
    """
    from app.services.artifact_service import create_artifact

    key = f"generated_docs/{fname}"
    _upload_xlsx(key, data)

    artifact = create_artifact(
        db=db,
        session_id=session_id,
        artifact_type="cost_breakdown_xlsx",
        title=title,
        content=json.dumps({"rows": rows, **summary}, default=str),
        structured_data={"rows": rows, **summary, **(structured_extra or {})},
        agent_key="costing_researcher",
        metadata={"file_name": fname, **(metadata_extra or {})},
    )
    artifact.file_path = key  # storage KEY (storage-service resolves local/R2)
    artifact.file_name = fname
    db.commit()

    # A preview is a nice-to-have; never let it fail the costing run.
    try:
        enqueue_preview_render(artifact.id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cost_breakdown] preview enqueue failed: {e}")

    return {
        "artifact_id": artifact.id,
        "artifact_type": "cost_breakdown_xlsx",
        "title": title,
        "version": artifact.version,
        "file_name": fname,
        "file_path": key,
    }


def regenerate_xlsx_artifact(
    db: Session,
    breakdown: CostBreakdown,
    *,
    session_id: Optional[int] = None,
    single_sheet: Optional[bool] = None,
    cost_sheet_template: Optional[str] = None,
) -> Optional[dict]:
    """Generate a fresh XLSX from the current breakdown state and create a new
    cost_breakdown_xlsx artifact in the given session. Returns artifact info.

    The workbook is stored through ``storage_service`` (works on both the local
    filesystem and Cloudflare R2); ``artifact.file_path`` holds the storage KEY,
    not an absolute path. ``single_sheet``: True → all schedules in one tab;
    False → one sheet per schedule + Summary; None → `costing.nit_single_sheet`.
    """
    if session_id is None:
        session_id = breakdown.session_id
    if session_id is None:
        logger.warning("[cost_breakdown] regenerate_xlsx: no session_id, skipping artifact")
        return None

    rendered = render_breakdown_xlsx_bytes(
        db, breakdown, single_sheet=single_sheet,
        cost_sheet_template=cost_sheet_template,
    )
    if not rendered:
        return None
    fname, data, summary, rows = rendered

    title = breakdown.title or f"Cost Breakdown — Tender #{breakdown.tender_id}"
    info = persist_xlsx_artifact(
        db,
        session_id=session_id,
        title=title,
        fname=fname,
        data=data,
        rows=rows,
        summary=summary,
        structured_extra={"cost_breakdown_id": breakdown.id, "version": breakdown.version},
        metadata_extra={"tender_id": breakdown.tender_id, "cost_breakdown_id": breakdown.id},
    )
    breakdown.artifact_id = info["artifact_id"]
    db.commit()
    return info
