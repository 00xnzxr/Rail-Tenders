"""RQ entry points for auto tender-scoring: on-upload job + self-rescheduling reaper.

Mirrors app/worker/scheduled_tasks.py: each opens its own SessionLocal, the
reaper re-enqueues its own next run, and everything degrades to a no-op when
Redis (the queue) is unavailable.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from app.core.database import SessionLocal
from app.core.redis_client import get_queue
from app.services.auto_scoring_service import find_unscored_tender_ids, score_tenders_batch
from app.services.auto_scoring_settings import get_scoring_settings

log = logging.getLogger(__name__)

_REAPER_JOB_ID = "drpl-auto-scoring-reaper"


def score_new_tenders(tender_ids: list[int]) -> dict:
    """RQ job enqueued on extension upload — score the freshly-ingested tenders."""
    if not tender_ids:
        return {"scored": 0}
    db = SessionLocal()
    try:
        return score_tenders_batch(db, tender_ids)
    finally:
        db.close()


def _reschedule_reaper() -> None:
    q = get_queue()
    if q is None:
        return
    db = SessionLocal()
    try:
        interval = get_scoring_settings(db)["auto_scoring_interval_seconds"]
    finally:
        db.close()
    try:
        q.enqueue_in(
            timedelta(seconds=interval),
            "app.worker.auto_scoring_tasks.reap_unscored_tenders",
            job_id=_REAPER_JOB_ID, result_ttl=86400,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("auto_scoring_tasks: could not reschedule reaper: %s", e)


def reap_unscored_tenders() -> dict:
    """Periodic sweep: score a batch of unscored tenders, then self-reschedule."""
    if get_queue() is None:
        log.info("auto-scoring reaper: queue unavailable — not running")
        return {"rescheduled": False, "reason": "no_queue"}

    db = SessionLocal()
    try:
        scoring = get_scoring_settings(db)
        if not scoring["auto_scoring_enabled"]:
            log.info("auto-scoring reaper: disabled via auto_scoring_enabled")
            return {"rescheduled": False, "reason": "disabled"}
        ids = find_unscored_tender_ids(db)
        out = score_tenders_batch(db, ids) if ids else {"scored": 0, "flagged_below_threshold": 0, "failed": 0}
    finally:
        db.close()

    _reschedule_reaper()
    out["rescheduled"] = True
    return out
