"""
DRPL Collector - the ledger.

What lets you answer "did we miss anything?" with evidence instead of a shrug.
It ships with the first fetcher rather than after it, because a coverage claim
you start keeping in month three only covers month three.

ORDER IS THE WHOLE POINT
------------------------
``record()`` runs AFTER ``sink.post()`` returns success -- never before. The old
extension marked tenders done before uploading them, so any failed upload lost
that tender permanently and silently, and the next incremental pass skipped it
because the local "done" set said it was handled. Every call site in this
module is arranged so that a raised SinkError leaves the ledger untouched and
the tender therefore still unknown, which is exactly what makes it retryable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collector.models import CollectDrift, CollectGap, CollectSeen, CollectSweep, utcnow
from collector.portals.base import Batch

logger = logging.getLogger(__name__)


# -- Reading -------------------------------------------------------------


def known_ids(db: Session, portal: str) -> set[str]:
    """Every tender_id already shipped for this portal.

    Loaded once per sweep and held in memory: the incremental stop condition
    checks it per row, and a query per row would be tens of thousands of round
    trips to save a few megabytes.
    """
    rows = db.execute(
        select(CollectSeen.tender_id).where(CollectSeen.portal == portal)
    ).scalars()
    return {r for r in rows if r}


def cursor(db: Session, portal: str) -> dict:
    """What the fetcher needs to resume: the known set plus the high-water mark."""
    known = known_ids(db, portal)
    last = db.execute(
        select(func.max(CollectSeen.last_seen_at)).where(CollectSeen.portal == portal)
    ).scalar()
    return {"known": known, "known_count": len(known), "last_seen_at": last}


# -- Sweeps --------------------------------------------------------------


def begin_sweep(
    db: Session,
    run_id: str,
    portal: str,
    fetcher_version: str,
    mode: str = "incremental",
) -> CollectSweep:
    sweep = CollectSweep(
        run_id=run_id,
        portal=portal,
        fetcher_version=fetcher_version,
        mode=mode,
        started_at=utcnow(),
        status="running",
    )
    db.add(sweep)
    db.commit()
    db.refresh(sweep)
    return sweep


def close_sweep(
    db: Session,
    sweep: CollectSweep,
    status: str = "completed",
    error: Optional[str] = None,
) -> CollectSweep:
    sweep.finished_at = utcnow()
    sweep.status = status
    if error:
        sweep.error = error[:2000]
    db.commit()
    db.refresh(sweep)
    return sweep


def sweep_summary(sweep: CollectSweep) -> dict:
    """The payload for a ``portal_done`` event."""
    return {
        "portal": sweep.portal,
        "fetcher": sweep.fetcher_version,
        "mode": sweep.mode,
        "pages_walked": sweep.pages_walked,
        "rows_seen": sweep.rows_seen,
        "rows_new": sweep.rows_new,
        "rows_duplicate": sweep.rows_duplicate,
        "rows_skipped": sweep.rows_skipped,
        "expected_total": sweep.expected_total,
        "status": sweep.status,
    }


# -- Recording -----------------------------------------------------------


def record(db: Session, portal: str, batch: Batch, result, sweep: Optional[CollectSweep] = None) -> int:
    """Write one shipped batch to the ledger. Call AFTER a successful POST.

    ``result`` is the SinkResult. Returns how many collect_seen rows were
    inserted (as opposed to touched).

    Every tender in the batch is recorded, new or duplicate: the ledger's job
    is "have we ever shipped this", and a duplicate confirms the answer is yes.
    """
    from collector.portals.gem import sequence_parts  # local: gem-specific parse

    inserted = 0
    now = utcnow()
    for tender in batch.tenders:
        tid = tender.get("tenderId")
        if not tid:
            continue
        row = db.get(CollectSeen, {"portal": portal, "tender_id": tid})
        if row is None:
            prefix, number = sequence_parts(tid) if portal == "gem" else (None, None)
            row = CollectSeen(
                portal=portal,
                tender_id=tid,
                first_seen_at=now,
                last_seen_at=now,
                source_fetcher=batch.fetcher or None,
                match_term=batch.term,
                seq_prefix=prefix,
                seq_number=number,
            )
            db.add(row)
            inserted += 1
        else:
            row.last_seen_at = now
            if batch.fetcher:
                row.source_fetcher = batch.fetcher
            if batch.term and not row.match_term:
                row.match_term = batch.term

    if sweep is not None:
        sweep.pages_walked = max(sweep.pages_walked, batch.pages_done)
        sweep.rows_seen += batch.rows_seen
        sweep.rows_skipped += batch.rows_skipped
        sweep.rows_new += int(getattr(result, "new", 0) or 0)
        sweep.rows_duplicate += int(getattr(result, "duplicates", 0) or 0)
        if batch.expected_total is not None:
            # Keep the largest total any page reported: a search term's own
            # numFound, not a running sum across terms.
            sweep.expected_total = max(sweep.expected_total or 0, batch.expected_total)

    db.commit()
    return inserted


def trace(db: Session, run_id: str) -> list[dict]:
    """The compact step list written to ``AgentRun.partial_trace``.

    A sweep that dies at page 40 of 60 leaves this behind saying so, which is
    what the stopped-run message in drpl-backend renders. Deliberately the same
    shape the Master's timeline emits ({type, step, tool, is_error}) so the
    existing renderer needs no special case for collect runs.
    """
    sweeps = (
        db.query(CollectSweep)
        .filter(CollectSweep.run_id == run_id)
        .order_by(CollectSweep.id.asc())
        .all()
    )
    out: list[dict] = []
    for i, s in enumerate(sweeps, start=1):
        out.append({"type": "action", "step": i, "tool": f"collect:{s.portal}"})
        if s.status != "running":
            out.append(
                {
                    "type": "observation",
                    "step": i,
                    "is_error": s.status == "failed",
                    "summary": (
                        f"{s.rows_new} new / {s.rows_duplicate} dup / "
                        f"{s.rows_skipped} out-of-scope over {s.pages_walked} page(s)"
                    ),
                }
            )
    return out


# -- Gaps ----------------------------------------------------------------


def record_gaps(db: Session, portal: str, missing: Iterable[str]) -> int:
    """Insert gap rows, skipping ones already open. Returns how many are new."""
    added = 0
    for mid in missing:
        if not mid:
            continue
        exists = (
            db.query(CollectGap.id)
            .filter(CollectGap.portal == portal, CollectGap.missing_id == mid)
            .first()
        )
        if exists:
            continue
        db.add(CollectGap(portal=portal, missing_id=mid, detected_at=utcnow()))
        added += 1
    if added:
        db.commit()
    return added


def resolve_gap(db: Session, portal: str, missing_id: str, resolution: str) -> bool:
    row = (
        db.query(CollectGap)
        .filter(CollectGap.portal == portal, CollectGap.missing_id == missing_id)
        .first()
    )
    if row is None:
        return False
    row.resolved_at = utcnow()
    row.resolution = resolution
    db.commit()
    return True


def open_gaps(db: Session, portal: str, limit: int = 200) -> list[CollectGap]:
    return (
        db.query(CollectGap)
        .filter(CollectGap.portal == portal, CollectGap.resolved_at.is_(None))
        .order_by(CollectGap.detected_at.desc())
        .limit(limit)
        .all()
    )


# -- Drift ---------------------------------------------------------------


def record_drift(db: Session, run_id: Optional[str], portal: str, fetcher: str, findings: list[dict]) -> int:
    """Persist fill-rate findings. Returns how many rows were written."""
    n = 0
    for f in findings or []:
        db.add(
            CollectDrift(
                portal=portal,
                fetcher=fetcher,
                field=str(f.get("field") or "")[:64],
                fill_rate=float(f.get("fill_rate") or 0.0),
                baseline=float(f.get("baseline") or 0.0),
                sample_size=int(f.get("sample_size") or 0),
                run_id=run_id,
                detected_at=utcnow(),
            )
        )
        n += 1
    if n:
        db.commit()
    return n
