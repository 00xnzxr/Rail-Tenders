"""
DRPL Collector - drift detection.

The thing the old extension had no equivalent of.

These portals do not throw when they change. They return a perfectly valid
page with the fields renamed, and a selector-based parser reports zero rows as
if it were a quiet day. ``selectors.json`` lost that bet four months ago and
nobody found out from an exception -- they found out from an empty tender list.

So the alarm is on FILL RATE, not on exceptions: measure how often each field
actually came back populated, compare it against what that field normally
does, and shout when it collapses. A parser degrading is a different event from
a parser dying, and both need to be visible.

A drift finding is also where a model finally earns a place in this system:
hand the saved page to one, let it propose a new field mapping, replay that
mapping against ``tests/fixtures/``, and promote it only if every fixture still
passes. Deterministic in steady state, model on change -- never the other way
round.
"""

from __future__ import annotations

from typing import Iterable, Optional

#: A field has to fall to this fraction of its baseline before it is a
#: finding. Loose enough that ordinary variation (a page of tenders that
#: genuinely have no closing date) does not page anyone.
DEGRADED_AT = 0.7

#: Below this many rows the fill rate is noise, not a signal.
MIN_SAMPLE = 5


def fill_rates(rows: Iterable[dict], fields: Iterable[str]) -> dict[str, float]:
    """How often each field came back non-empty, 0.0-1.0."""
    rows = list(rows)
    n = len(rows)
    if n == 0:
        return {f: 0.0 for f in fields}
    out = {}
    for f in fields:
        hits = sum(1 for r in rows if _populated(r.get(f)))
        out[f] = hits / n
    return out


def _populated(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict, set)):
        return len(v) > 0
    return True


def check_fill(
    rows: list[dict],
    baseline: dict[str, float],
    portal: str,
    fetcher: str = "",
) -> list[dict]:
    """Findings for every field that has fallen well below its baseline.

    Returns a list rather than yielding so the caller can count it, log it and
    ship it as one ``drift`` event without draining a generator twice.
    """
    n = len(rows)
    if n < MIN_SAMPLE:
        return []

    rates = fill_rates(rows, baseline.keys())
    findings: list[dict] = []
    for field, expected in baseline.items():
        got = rates.get(field, 0.0)
        if expected <= 0:
            continue
        if got < expected * DEGRADED_AT:
            findings.append(
                {
                    "portal": portal,
                    "fetcher": fetcher,
                    "field": field,
                    "fill_rate": round(got, 3),
                    "baseline": round(expected, 3),
                    "sample_size": n,
                }
            )
    return findings


def summarise(findings: list[dict]) -> Optional[str]:
    """One line for a log or an amber banner. None when there is nothing wrong."""
    if not findings:
        return None
    parts = [f"{f['field']} {f['fill_rate']:.0%} (was {f['baseline']:.0%})" for f in findings]
    return "parser degrading: " + ", ".join(parts)
