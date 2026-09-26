"""RQ entry points for eager tender analysis: per-tender job + self-rescheduling
sweep. Mirrors app/worker/auto_scoring_tasks.py — own SessionLocal per job,
self-reschedule, graceful no-Redis no-op."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.core.database import SessionLocal
from app.core.redis_client import get_queue, get_redis
from app.core.run_context import run_id_scope
from app.models.tender import Tender
from app.models.document_analysis import DocumentExtractionResult
from app.services.tender_analysis_service import analyze_all_documents
from app.services.eager_analysis_settings import get_eager_analysis_settings
from app.services.eager_analysis_service import (
    find_eager_analysis_candidates, capacity_ok, gather_capacity,
    daily_count, bump_daily_count, reap_stuck_eager_analysis,
)

log = logging.getLogger(__name__)

_SLOT_PREFIX = "eager-analysis-slot-"
# NOTE: TTL must exceed the longest expected single-tender analysis (v2 analyzer
# is deliberately sequential per tender_analyzer_max_parallel=1; a stuck slot
# self-clears after this TTL so a crashed holder can't wedge the lock forever).
_SLOT_TTL_SECONDS = 1800

# Compare-and-delete: only release the slot if it still holds OUR token.
# Prevents a stale holder (TTL-expired, e.g. after a GC/scheduler pause that
# outlasted _SLOT_TTL_SECONDS) from deleting a live slot that a different
# process has since legitimately acquired.
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

# Sentinel slot key used when there is no Redis connection at all — the
# no-lock path still flows through the same acquire/release shape, but
# _release_analysis_slot is never called for it (token is None).
_NO_LOCK_SLOT = "__nolock__"


def _already_analyzed(db, tender_id: int) -> bool:
    return db.query(DocumentExtractionResult.id).filter(
        DocumentExtractionResult.tender_id == tender_id).first() is not None


def _set_status(db, tender_id: int, status: "str | None") -> None:
    t = db.query(Tender).get(tender_id)
    if t is not None:
        t.eager_analysis_status = status
        t.eager_analysis_at = datetime.now(timezone.utc)
        db.commit()


def _acquire_analysis_slot(redis, max_concurrency: int) -> "tuple[str, str] | None":
    """Try to claim one of `max_concurrency` cross-process slots. Each slot's
    value is a unique per-acquisition token so release can be a compare-and-
    delete (see _release_analysis_slot) instead of an unconditional DEL that
    could clobber a different process's live lock after TTL expiry. Returns
    (slot_key, token), or None if all slots are currently held."""
    for i in range(max(1, max_concurrency)):
        key = f"{_SLOT_PREFIX}{i}"
        token = uuid.uuid4().hex
        try:
            ok = redis.set(key, token, nx=True, ex=_SLOT_TTL_SECONDS)
        except Exception as e:  # noqa: BLE001
            log.warning("eager_analysis: slot acquire failed for %s: %s", key, e)
            continue
        if ok:
            return key, token
    return None


def _release_analysis_slot(redis, slot_key: "str | None", token: "str | None") -> None:
    """Compare-and-delete: only removes the slot if it still holds `token`.
    A holder whose TTL already expired (and whose slot a different process
    has since acquired) must never delete that other process's live lock."""
    if not slot_key or not token:
        return
    try:
        redis.eval(_RELEASE_LUA, 1, slot_key, token)
    except Exception as e:  # noqa: BLE001
        log.warning("eager_analysis: slot release failed for %s: %s", slot_key, e)


def eager_analyze_tender(tender_id: int) -> dict:
    """Run the v2 analyzer for one tender. Idempotent; status-tracked.

    Cross-tender concurrency is bounded by a Redis slot lock so that at most
    `eager_analysis_max_concurrency` (default 1) analyses run at once across
    all worker processes/replicas — honoring tender_analyzer_max_parallel=1's
    single-flight requirement even though RQ enqueues one job per tender.
    """
    db = SessionLocal()
    try:
        if _already_analyzed(db, tender_id):
            _set_status(db, tender_id, "done")
            return {"tender_id": tender_id, "status": "skipped"}

        redis = None
        try:
            redis = get_redis()
        except Exception as e:  # noqa: BLE001
            log.debug("eager_analysis: redis accessor raised; proceeding without slot lock: %s", e)
            redis = None

        token = None
        if redis is not None:
            try:
                max_concurrency = get_eager_analysis_settings(db)["eager_analysis_max_concurrency"]
            except Exception:
                max_concurrency = 1
            slot = _acquire_analysis_slot(redis, max_concurrency)
            if slot is None:
                # No free slot: defer to the next sweep tick rather than run
                # over the concurrency cap.
                _set_status(db, tender_id, None)
                return {"tender_id": tender_id, "status": "deferred"}
            slot_key, token = slot
        else:
            log.debug("eager_analysis: no redis connection — proceeding without slot lock "
                      "(no RQ queue without redis, so concurrency is naturally 1)")
            slot_key = _NO_LOCK_SLOT

        # Everything from here on holds the slot (if any) — status-set is
        # inside the try/finally so a raise from _set_status can't leak the
        # slot past this point; release always fires in the finally below.
        try:
            _set_status(db, tender_id, "running")
            with run_id_scope(f"eageranalyze-{tender_id}"):
                asyncio.run(analyze_all_documents(db, tender_id))
            _set_status(db, tender_id, "done")
            return {"tender_id": tender_id, "status": "done"}
        except Exception as e:  # noqa: BLE001
            log.warning("eager_analyze_tender(%s) failed: %s", tender_id, e)
            _set_status(db, tender_id, "failed")
            return {"tender_id": tender_id, "status": "failed", "error": str(e)}
        finally:
            if redis is not None and token is not None:
                _release_analysis_slot(redis, slot_key, token)
    finally:
        db.close()


_SWEEP_JOB_ID = "drpl-eager-analysis-sweep"


def _reschedule_sweep(interval: int) -> None:
    q = get_queue()
    if q is None:
        return
    try:
        q.enqueue_in(timedelta(seconds=interval),
                     "app.worker.eager_analysis_tasks.enqueue_eager_analysis_sweep",
                     job_id=_SWEEP_JOB_ID, result_ttl=86400)
    except Exception as e:  # noqa: BLE001
        log.warning("eager_analysis: could not reschedule sweep: %s", e)


def enqueue_eager_analysis_sweep() -> dict:
    """Periodic sweep: enqueue eager analysis for in-scope candidates, then
    self-reschedule. Deferral (disabled/backpressure/cap) is never a drop —
    the next tick retries.

    Exactly ONE reschedule happens per tick, in the `finally` below, so it
    fires even if a DB-body call raises unexpectedly — the sweep must never
    wedge forever. `no_queue` is the sole path that returns before the try
    (no queue means no reschedule target either)."""
    q = get_queue()
    if q is None:
        log.info("eager-analysis sweep: queue unavailable — not running")
        return {"rescheduled": False, "reason": "no_queue", "enqueued": 0}

    interval = 180  # safe default so finally can always reschedule even if settings read fails
    db = SessionLocal()
    try:
        try:
            reap_stuck_eager_analysis(db)
        except Exception as e:  # noqa: BLE001
            log.warning("eager-analysis sweep: reap_stuck_eager_analysis failed: %s", e)
        s = get_eager_analysis_settings(db)
        interval = s.get("eager_analysis_interval_seconds", 180)
        if not s["eager_analysis_enabled"]:
            return {"rescheduled": True, "reason": "disabled", "enqueued": 0}

        depth, rl, pool = gather_capacity(db)
        if not capacity_ok(depth, rl, pool, s["eager_analysis_queue_ceiling"]):
            return {"rescheduled": True, "reason": "backpressure", "enqueued": 0}

        remaining = s["eager_analysis_daily_cap"] - daily_count(db)
        if remaining <= 0:
            return {"rescheduled": True, "reason": "daily_cap", "enqueued": 0}

        ids = find_eager_analysis_candidates(db)[:remaining]
        n = 0
        for tid in ids:
            try:
                q.enqueue("app.worker.eager_analysis_tasks.eager_analyze_tender", tid,
                          job_id=f"eager-analyze-{tid}", result_ttl=86400)
                _set_status(db, tid, "queued")
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("eager-analysis: enqueue failed for tender %s: %s", tid, e)
        if n:
            bump_daily_count(db, n)
        return {"rescheduled": True, "reason": "ok", "enqueued": n}
    finally:
        db.close()
        _reschedule_sweep(interval)
