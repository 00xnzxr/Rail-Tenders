"""Pure helpers for the auto tender-scoring agent — no DB, no LLM."""
from __future__ import annotations

import re

_MATCH_RE = re.compile(r"MATCH:\s*(-?\d+(?:\.\d+)?)\s*%?", re.IGNORECASE)
_REASON_RE = re.compile(r"REASON:\s*(.+?)(?=^\s*ELIGIBLE\s*:|\Z)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
_ELIGIBLE_RE = re.compile(r"^\s*ELIGIBLE\s*:\s*(yes|no|true|false)\b", re.IGNORECASE | re.MULTILINE)


def parse_score_reply(text: str) -> tuple[float, str]:
    """Parse a `MATCH: <0-100>` / `REASON: <text>` reply into (relevance_0_1, reasoning).

    Neutral 0.5 default on unparseable input, matching score_relevance's error default.
    """
    if not text:
        return 0.5, ""
    m = _MATCH_RE.search(text)
    if not m:
        return 0.5, ""
    pct = float(m.group(1))
    score = max(0.0, min(1.0, pct / 100.0))
    r = _REASON_RE.search(text)
    reason = r.group(1).strip() if r else ""
    return score, reason


def parse_eligible_reply(text: str) -> "bool | None":
    """The scorer's optional `ELIGIBLE: yes|no` line; None when absent.

    Absent is "unknown", never "not eligible": the separate eligibility call
    this replaced wrote not_eligible on any failure, which hid real bids
    behind the eligibility filter during every API outage.
    """
    m = _ELIGIBLE_RE.search(text or "")
    if not m:
        return None
    return m.group(1).lower() in ("yes", "true")


def is_below_threshold(estimated_value: "float | None", threshold: float) -> bool:
    """True iff we have a real value and it is under the threshold. None → False (never flag)."""
    if estimated_value is None:
        return False
    return estimated_value < threshold


def compute_segment(
    score: "float | None",
    estimated_value: "float | None",
    *,
    discard_below: float,
    bidable_at: float,
    value_threshold: float,
) -> "str | None":
    """Bucket a tender by AI score + value. score is 0-1; None score -> None."""
    if score is None:
        return None
    if score < discard_below:
        return "discarded"
    if score < bidable_at:
        return "not_bidable"
    # score >= bidable_at
    if estimated_value is None or estimated_value >= value_threshold:
        return "to_bid"
    return "not_bidable"
