"""
DRPL LangChain Tool - Cost Calculator

Pure-arithmetic tool that takes a list of line items + percentage knobs and
returns precise subtotal / overhead / margin / GST / grand_total values plus
per-line amount fields. The costing agent is required to call this for every
total instead of computing arithmetic by hand (LLMs hallucinate decimals,
forget to skip needs_input lines, and disagree with the agent's own JSON).

Range mode: when line items carry rate_low/rate_high (and optionally
rate_expected) OR amount_low/amount_high, the tool returns three sets of
totals (low / expected / high) so the agent can present a defensible
estimation range rather than a single hard number. Per-line profit is
computed using either the line's own profit_pct (preferred — agent varies
by category) or the global margin_percent fallback.

The output mirrors the schema persisted by `cost_breakdown_service` so the
agent can paste the returned numbers straight into its final COSTING_JSON
block.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _CalcLine(BaseModel):
    """One line item to cost.

    Provide ONE of:
      - quantity + (rate OR rate_low/rate_high)
      - amount  OR  amount_low/amount_high
    Optionally provide rate_expected / amount_expected to override the
    derived midpoint. Per-line profit_pct (e.g. 12 for 12%) lets the agent
    vary margin by category; falls back to the global margin_percent.

    Phase 3b margin-analysis fields:
      - tender_rate     : the rate stated in the tender BOQ for this line
      - tender_amount   : tender_rate × quantity (computed if absent)
      - schedule_section: grouping label (passed through to per-line output)
    When tender_rate is present, the calculator emits margin_amount_* per
    band (tender_amount - amount_band) and aggregates a top-level
    `margin_totals` block.
    """
    sr_no: Optional[int] = None
    description: Optional[str] = ""
    quantity: Optional[float] = None

    # Single-rate (legacy / backward compat)
    rate: Optional[float] = None
    amount: Optional[float] = None

    # Range fields (new) — agent provides low/high; expected defaults to mid
    rate_low: Optional[float] = None
    rate_expected: Optional[float] = None
    rate_high: Optional[float] = None
    amount_low: Optional[float] = None
    amount_expected: Optional[float] = None
    amount_high: Optional[float] = None

    # Per-line profit override (percent, e.g. 12 for 12%)
    profit_pct: Optional[float] = None

    # Phase 3b — tender-rate / margin-analysis fields
    tender_rate: Optional[float] = None
    tender_amount: Optional[float] = None
    schedule_section: Optional[str] = None

    rate_source: Optional[str] = None
    oem_manufacturer: Optional[str] = None
    source_url: Optional[str] = None


class CostCalculatorInput(BaseModel):
    """Input schema — line items plus the three percentage knobs."""
    line_items: list[_CalcLine] = Field(
        ...,
        description=(
            "Array of line items. Each item should provide quantity + "
            "rate_low/rate_high (range mode, preferred) or quantity + rate "
            "(legacy single-value mode), OR explicit amount(_low/_high). "
            "Items with rate_source='needs_user_input' (or null/missing rates) "
            "are skipped from totals."
        ),
    )
    overhead_percent: float = Field(
        ...,
        description="Overhead percentage to apply to subtotal (e.g. 10 for 10%).",
    )
    margin_percent: float = Field(
        ...,
        description=(
            "Default profit margin percentage applied to (subtotal + overhead). "
            "Used as a fallback when a line item does not carry its own "
            "profit_pct. Per-line profit_pct (varied by category) takes "
            "precedence and is summed into per-line profit_amount."
        ),
    )
    gst_percent: float = Field(
        ...,
        description="GST percentage applied to (subtotal + overhead + margin).",
    )


def _line_skipped(line: _CalcLine) -> bool:
    """Skip lines that the user hasn't priced yet."""
    if (line.rate_source or "").lower() in ("needs_user_input", "needs_input"):
        return True
    has_any_rate = any(
        v is not None for v in (line.rate, line.rate_low, line.rate_high, line.rate_expected)
    )
    has_any_amount = any(
        v is not None for v in (line.amount, line.amount_low, line.amount_high, line.amount_expected)
    )
    if not has_any_amount and (line.quantity is None or not has_any_rate):
        return True
    return False


def _resolve_amounts(ln: _CalcLine) -> Optional[tuple[float, float, float]]:
    """Return (low, expected, high) amount for a line, or None if unresolvable.

    Resolution order per band:
      1. explicit amount_<band> if set
      2. quantity * rate_<band> if both set
      3. fall back across bands so we always end up with three numbers
         (a single-value line collapses to low=expected=high).
    """
    qty = ln.quantity

    def _amt(amount_val: Optional[float], rate_val: Optional[float]) -> Optional[float]:
        if amount_val is not None:
            return float(amount_val)
        if rate_val is not None and qty is not None:
            return round(float(qty) * float(rate_val), 2)
        return None

    a_low = _amt(ln.amount_low, ln.rate_low)
    a_exp = _amt(ln.amount_expected, ln.rate_expected)
    a_high = _amt(ln.amount_high, ln.rate_high)

    # Legacy single-value pathway — if only `amount` / `rate` is given,
    # treat it as the expected band so the result still works.
    legacy = _amt(ln.amount, ln.rate)
    if a_exp is None:
        a_exp = legacy

    # Fill missing bands so we always emit three numbers.
    if a_low is None and a_high is None and a_exp is None:
        return None

    if a_exp is None:
        # Average of low/high if both present; otherwise the one we have.
        if a_low is not None and a_high is not None:
            a_exp = round((a_low + a_high) / 2.0, 2)
        else:
            a_exp = a_low if a_low is not None else a_high

    if a_low is None:
        a_low = a_exp
    if a_high is None:
        a_high = a_exp

    # Guard against inverted bands (agent fed high < low).
    if a_low > a_high:
        a_low, a_high = a_high, a_low

    return round(a_low, 2), round(a_exp, 2), round(a_high, 2)


class CostCalculatorTool(BaseTool):
    """Compute precise cost totals from a list of line items."""

    name: str = "cost_calculator"
    description: str = (
        "Compute exact cost totals (subtotal, overhead, margin, GST, grand "
        "total) from a list of line items and three percentage knobs. "
        "Cost-only sanity check: pass margin_percent=0, overhead_percent=0, "
        "gst_percent=0 to just verify subtotals add up — that is the standard "
        "use during initial costing, since margin/overhead/GST are added by "
        "the user later in the cost-breakdown editor. "
        "Single-rate lines (rate + amount, no _low/_high) are handled — bands "
        "collapse to a single value. "
        "MARGIN-ANALYSIS MODE: when lines carry `tender_rate` (the rate "
        "stated in the tender BOQ), the tool computes per-line gross margin "
        "(tender_amount - amount) and returns a `margin_totals` block. "
        "Lines with rate_source='needs_user_input' or no priced rate are "
        "skipped from totals but counted as needs_input_count. Returns JSON."
    )
    args_schema: Type[BaseModel] = CostCalculatorInput

    def _run(
        self,
        line_items: list[dict],
        overhead_percent: float,
        margin_percent: float,
        gst_percent: float,
    ) -> str:
        """Compute totals; LLM-callable. Returns a JSON string."""
        try:
            parsed = [
                _CalcLine(**(item if isinstance(item, dict) else item.model_dump()))
                for item in line_items
            ]
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Invalid line_items: {e}"})

        try:
            ov = float(overhead_percent)
            mg = float(margin_percent)
            gp = float(gst_percent)
        except (TypeError, ValueError) as e:
            return json.dumps({"status": "error", "message": f"Invalid percentages: {e}"})

        sub_low = sub_exp = sub_high = 0.0
        profit_low = profit_exp = profit_high = 0.0
        # Margin aggregates — only include lines that have a tender_rate, so a
        # mixed costing (some BOQ-rate lines, some not) still produces a clean
        # roll-up over just the BOQ-priced lines.
        tender_total = 0.0
        margin_low = margin_exp = margin_high = 0.0
        margin_lines = 0
        needs_input = 0
        out_lines: list[dict] = []

        for ln in parsed:
            if _line_skipped(ln):
                needs_input += 1
                out_lines.append({
                    "sr_no": ln.sr_no,
                    "description": ln.description,
                    "schedule_section": ln.schedule_section,
                    "amount_low": None,
                    "amount_expected": None,
                    "amount_high": None,
                    "skipped": True,
                    "reason": "needs_user_input or missing rate",
                })
                continue

            amounts = _resolve_amounts(ln)
            if amounts is None:
                needs_input += 1
                out_lines.append({
                    "sr_no": ln.sr_no,
                    "description": ln.description,
                    "schedule_section": ln.schedule_section,
                    "amount_low": None,
                    "amount_expected": None,
                    "amount_high": None,
                    "skipped": True,
                    "reason": "could not compute amount",
                })
                continue

            a_low, a_exp, a_high = amounts
            sub_low += a_low
            sub_exp += a_exp
            sub_high += a_high

            # Per-line profit: line's own profit_pct wins; otherwise org default mg.
            # In cost-only mode (mg=0 and ln.profit_pct=None) line_pct is 0 — the
            # profit fields collapse to zero and we skip emitting them per-line.
            line_pct = float(ln.profit_pct) if ln.profit_pct is not None else mg
            p_low = round(a_low * line_pct / 100.0, 2)
            p_exp = round(a_exp * line_pct / 100.0, 2)
            p_high = round(a_high * line_pct / 100.0, 2)
            profit_low += p_low
            profit_exp += p_exp
            profit_high += p_high

            # Margin analysis — present when the agent supplied tender_rate
            # (or tender_amount). Compute both per-line and roll up to top-level.
            t_amount = None
            if ln.tender_amount is not None:
                t_amount = float(ln.tender_amount)
            elif ln.tender_rate is not None and ln.quantity is not None:
                t_amount = round(float(ln.tender_rate) * float(ln.quantity), 2)

            line_out = {
                "sr_no": ln.sr_no,
                "description": ln.description,
                "schedule_section": ln.schedule_section,
                "amount_low": a_low,
                "amount_expected": a_exp,
                "amount_high": a_high,
                "skipped": False,
                "oem_manufacturer": ln.oem_manufacturer,
                "source_url": ln.source_url,
            }
            # Only emit profit fields when there's a real margin to apply
            # (either per-line profit_pct or a non-zero global margin).
            if line_pct > 0:
                line_out["profit_pct"] = round(line_pct, 2)
                line_out["profit_amount_low"] = p_low
                line_out["profit_amount_expected"] = p_exp
                line_out["profit_amount_high"] = p_high

            if t_amount is not None:
                m_low = round(t_amount - a_high, 2)   # high-cost band → low-margin
                m_exp = round(t_amount - a_exp, 2)
                m_high = round(t_amount - a_low, 2)   # low-cost band → high-margin
                m_pct = round((m_exp / t_amount * 100.0), 2) if t_amount else 0.0

                tender_total += t_amount
                margin_low += m_low
                margin_exp += m_exp
                margin_high += m_high
                margin_lines += 1

                line_out.update({
                    "tender_rate": ln.tender_rate,
                    "tender_amount": t_amount,
                    "margin_amount_low": m_low,
                    "margin_amount": m_exp,
                    "margin_amount_high": m_high,
                    "margin_pct": m_pct,
                })

            out_lines.append(line_out)

        def _band_totals(subtotal: float, profit_sum: float) -> dict:
            overhead_amount = round(subtotal * (ov / 100.0), 2)
            sub_with_overhead = round(subtotal + overhead_amount, 2)
            # If the agent supplied per-line profit_pct on every priced line,
            # profit_sum already reflects the variable margins. We use that as
            # the line-level profit. Apply the global mg only on the overhead
            # contribution (so overhead carries its share of margin too).
            margin_on_overhead = round(overhead_amount * (mg / 100.0), 2)
            margin_amount = round(profit_sum + margin_on_overhead, 2)
            pre_tax = round(sub_with_overhead + margin_amount, 2)
            gst_amount = round(pre_tax * (gp / 100.0), 2)
            grand_total = round(pre_tax + gst_amount, 2)
            return {
                "subtotal": round(subtotal, 2),
                "overhead_amount": overhead_amount,
                "margin_amount": margin_amount,
                "pre_tax_total": pre_tax,
                "gst_amount": gst_amount,
                "grand_total": grand_total,
            }

        totals_low = _band_totals(sub_low, profit_low)
        totals_expected = _band_totals(sub_exp, profit_exp)
        totals_high = _band_totals(sub_high, profit_high)

        # Margin-totals roll-up — present only when ≥1 line had tender_rate
        # (i.e. margin-analysis mode). Surfaces gross margin per band so the
        # agent can drop these straight into strategic_summary.schedule_breakdown
        # and cost_breakdowns.margin_total_*.
        margin_totals = None
        if margin_lines > 0:
            margin_totals = {
                "tender_total": round(tender_total, 2),
                "margin_total_low": round(margin_low, 2),
                "margin_total_expected": round(margin_exp, 2),
                "margin_total_high": round(margin_high, 2),
                "margin_pct_expected": round((margin_exp / tender_total * 100.0), 2) if tender_total else 0.0,
                "lines_with_tender_rate": margin_lines,
            }

        # Backward-compat top-level fields mirror the expected band so existing
        # consumers (cost_breakdown_service.persist_from_agent_output, formatter
        # fallback paths) keep working without changes.
        return json.dumps({
            "status": "ok",
            "subtotal": totals_expected["subtotal"],
            "overhead_percent": ov,
            "overhead_amount": totals_expected["overhead_amount"],
            "margin_percent": mg,
            "margin_amount": totals_expected["margin_amount"],
            "pre_tax_total": totals_expected["pre_tax_total"],
            "gst_percent": gp,
            "gst_amount": totals_expected["gst_amount"],
            "grand_total": totals_expected["grand_total"],
            # Range-mode output: three full sets of totals
            "totals": {
                "low": totals_low,
                "expected": totals_expected,
                "high": totals_high,
            },
            # Margin-analysis output (Phase 3b) — null when no lines had tender_rate
            "margin_totals": margin_totals,
            "needs_input_count": needs_input,
            "lines_costed": len([l for l in out_lines if not l.get("skipped")]),
            "line_amounts": out_lines,
        }, default=str)
