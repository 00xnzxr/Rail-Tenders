"""
DRPL Collector - the audit.

Three questions the ledger can answer that nothing else in DRPL can:

1. SEQUENCE GAPS. GeM bid numbers run sequentially within a year and a prefix.
   Track the observed range and any absent number is a candidate we have never
   seen. This is the closest thing to a proof of coverage either portal offers.

2. COUNT RECONCILIATION. ``expected_total`` is the portal's own numFound.
   Compare it against rows_seen per sweep; divergence beyond ordinary churn is
   an alarm in minutes rather than a discovery next month.

3. DARK DETECTION. Alarm when a fetcher's silence exceeds its own historical
   p99 gap between new rows. This is what separates "quiet day on the portals"
   from "the collector broke on Tuesday" -- and DRPL already defines
   ``portal_dark`` and ``selector_breakage`` alert types waiting to be fed.

Every function here is read-mostly and best-effort: an audit that raises would
fail a sweep whose tenders already landed, which is exactly backwards.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collector.ledger import record_gaps
from collector.models import CollectSeen, CollectSweep

logger = logging.getLogger(__name__)

#: A gap run longer than this is a range we simply have not swept (a fresh
#: install, a portal we joined late), not a set of individually missing
#: tenders. Recording ten thousand gap rows for it helps nobody.
MAX_GAP_RUN = 50

#: How far back a gap scan looks. Bid numbers from two years ago are closed
#: tenders; a gap there is history, not a miss.
GAP_LOOKBACK_DAYS = 120

#: Reconciliation tolerance. The portal's numFound moves under us while we
#: page -- new bids publish mid-sweep -- so only a wide divergence is a signal.
RECONCILE_TOLERANCE = 0.35


# -- 1. Sequence gaps ----------------------------------------------------


def find_sequence_gaps(db: Session, portal: str = "gem", limit: int = 500) -> list[str]:
    """Bid numbers the sequence implies but the ledger has never seen.

    Scoped per prefix (``GEM/2026/B``) and to recently-seen numbers, and it
    refuses to enumerate a run longer than MAX_GAP_RUN -- a 40,000-wide hole
    means we have not swept that range, which is a different problem from
    forty missing tenders and wants a different fix.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=GAP_LOOKBACK_DAYS)
    rows = db.execute(
        select(CollectSeen.seq_prefix, CollectSeen.seq_number)
        .where(
            CollectSeen.portal == portal,
            CollectSeen.seq_prefix.isnot(None),
            CollectSeen.seq_number.isnot(None),
            CollectSeen.last_seen_at >= cutoff,
        )
    ).all()

    by_prefix: dict[str, set[int]] = {}
    for prefix, number in rows:
        by_prefix.setdefault(prefix, set()).add(int(number))

    missing: list[str] = []
    for prefix, numbers in by_prefix.items():
        if len(numbers) < 3:
            continue
        ordered = sorted(numbers)
        for a, b in zip(ordered, ordered[1:]):
            span = b - a - 1
            if span <= 0:
                continue
            if span > MAX_GAP_RUN:
                # An unswept range, not a set of misses. Say so once.
                logger.info(
                    "audit: %s %s has an unswept range of %s between %s and %s",
                    portal, prefix, span, a, b,
                )
                continue
            for n in range(a + 1, b):
                missing.append(f"{prefix}/{n}")
                if len(missing) >= limit:
                    return missing
    return missing


def scan_gaps(db: Session, portal: str = "gem") -> dict:
    """Find gaps and record the new ones. Returns a summary."""
    try:
        missing = find_sequence_gaps(db, portal)
        added = record_gaps(db, portal, missing)
        return {"portal": portal, "candidates": len(missing), "recorded": added}
    except Exception as e:  # noqa: BLE001
        logger.warning("audit.scan_gaps(%s) failed: %s", portal, e)
        return {"portal": portal, "candidates": 0, "recorded": 0, "error": str(e)[:200]}


# -- 2. Count reconciliation --------------------------------------------


def reconcile_sweep(sweep: CollectSweep) -> Optional[dict]:
    """Compare what the portal said it had against what we walked.

    Returns a finding, or None when the numbers agree. Note that a sweep that
    stopped early on purpose (the incremental stop condition fired) is EXPECTED
    to have seen far fewer rows than numFound -- that is the design working, so
    only a full sweep is reconciled.
    """
    if sweep.mode != "full":
        return None
    if not sweep.expected_total or sweep.expected_total <= 0:
        return None
    seen = sweep.rows_seen or 0
    shortfall = (sweep.expected_total - seen) / sweep.expected_total
    if shortfall <= RECONCILE_TOLERANCE:
        return None
    return {
        "portal": sweep.portal,
        "fetcher": sweep.fetcher_version,
        "expected_total": sweep.expected_total,
        "rows_seen": seen,
        "shortfall": round(shortfall, 3),
    }


def reconcile(db: Session, run_id: Optional[str] = None) -> list[dict]:
    """Reconcile every sweep in a run (or the last few when none is given)."""
    q = db.query(CollectSweep)
    if run_id:
        q = q.filter(CollectSweep.run_id == run_id)
    else:
        q = q.order_by(CollectSweep.id.desc()).limit(10)
    findings = []
    for sweep in q.all():
        f = reconcile_sweep(sweep)
        if f:
            findings.append(f)
            logger.warning("audit: reconciliation shortfall %s", f)
    return findings


# -- 3. Dark detection ---------------------------------------------------


def gap_percentile(db: Session, portal: str, fetcher: Optional[str] = None, p: float = 0.99) -> Optional[float]:
    """The fetcher's historical p99 gap, in hours, between first-seen rows.

    Its OWN history, not a global constant: a portal that publishes twice a day
    and one that publishes twice a month should not share a threshold.
    """
    q = select(CollectSeen.first_seen_at).where(CollectSeen.portal == portal)
    if fetcher:
        q = q.where(CollectSeen.source_fetcher == fetcher)
    stamps = sorted(x for x in db.execute(q).scalars() if x)
    if len(stamps) < 20:
        return None
    gaps = [
        (b - a).total_seconds() / 3600.0
        for a, b in zip(stamps, stamps[1:])
        if (b - a).total_seconds() > 0
    ]
    if len(gaps) < 10:
        return None
    try:
        # quantiles(n=100) gives 99 cut points; index 98 is the 99th percentile.
        return statistics.quantiles(gaps, n=100)[98]
    except Exception:  # noqa: BLE001
        return max(gaps)


def is_dark(db: Session, portal: str, fetcher: Optional[str] = None) -> Optional[dict]:
    """Has this fetcher been silent longer than it has ever been before?

    Returns a finding or None. None also means "not enough history to say",
    which is the honest answer for a fetcher that shipped last week.
    """
    q = select(func.max(CollectSeen.first_seen_at)).where(CollectSeen.portal == portal)
    if fetcher:
        q = q.where(CollectSeen.source_fetcher == fetcher)
    last = db.execute(q).scalar()
    if last is None:
        return None

    threshold = gap_percentile(db, portal, fetcher)
    if threshold is None:
        return None

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    silent_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    if silent_hours <= max(threshold, 1.0):
        return None
    return {
        "portal": portal,
        "fetcher": fetcher,
        "silent_hours": round(silent_hours, 1),
        "p99_gap_hours": round(threshold, 1),
        "last_new_row_at": last.isoformat(),
        "alert_type": "portal_dark",
    }


# -- Summary -------------------------------------------------------------


def run_summary(db: Session, run_id: str) -> dict:
    """Totals for the ``run_done`` event. Best-effort; never raises."""
    try:
        sweeps = db.query(CollectSweep).filter(CollectSweep.run_id == run_id).all()
        return {
            "portals": [s.portal for s in sweeps],
            "pages_walked": sum(s.pages_walked or 0 for s in sweeps),
            "rows_seen": sum(s.rows_seen or 0 for s in sweeps),
            "rows_new": sum(s.rows_new or 0 for s in sweeps),
            "rows_duplicate": sum(s.rows_duplicate or 0 for s in sweeps),
            "rows_skipped": sum(s.rows_skipped or 0 for s in sweeps),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("audit.run_summary(%s) failed: %s", run_id, e)
        return {}


def coverage(db: Session, portal: str = "gem") -> dict:
    """What ``GET /api/collect/coverage`` will render. Read-only."""
    from collector.models import CollectGap

    total = db.execute(
        select(func.count()).select_from(CollectSeen).where(CollectSeen.portal == portal)
    ).scalar()
    open_gap_count = db.execute(
        select(func.count())
        .select_from(CollectGap)
        .where(CollectGap.portal == portal, CollectGap.resolved_at.is_(None))
    ).scalar()
    last_sweep = (
        db.query(CollectSweep)
        .filter(CollectSweep.portal == portal)
        .order_by(CollectSweep.id.desc())
        .first()
    )
    return {
        "portal": portal,
        "tenders_seen": int(total or 0),
        "open_gaps": int(open_gap_count or 0),
        "last_sweep": (
            {
                "run_id": last_sweep.run_id,
                "started_at": last_sweep.started_at.isoformat() if last_sweep.started_at else None,
                "finished_at": last_sweep.finished_at.isoformat() if last_sweep.finished_at else None,
                "status": last_sweep.status,
                "rows_new": last_sweep.rows_new,
                "expected_total": last_sweep.expected_total,
            }
            if last_sweep
            else None
        ),
        "dark": is_dark(db, portal),
    }
