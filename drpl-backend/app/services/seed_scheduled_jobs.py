"""
DRPL Backend - Seed scheduled background jobs at startup.

Primes the self-rescheduling chain for the two periodic notification tasks
(daily digest + closing-date scan). Each task re-enqueues its own next run at
the end — this seeder only kicks off the very first one.

Idempotent: if a job with the same well-known id already exists in the queue
or in scheduled state, this function is a no-op. Safe to call on every boot.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.redis_client import get_queue


log = logging.getLogger(__name__)


def seed_scheduled_jobs() -> dict:
    q = get_queue()
    if q is None:
        log.info("seed_scheduled_jobs: queue unavailable — scheduled jobs disabled")
        return {"enabled": False}

    out = {"enabled": True, "scheduled": []}

    # Daily digest: next occurrence of notifications_digest_hour_utc.
    settings = get_settings()
    hour = int(settings.notifications_digest_hour_utc or 13) % 24
    now = datetime.now(timezone.utc)
    next_digest = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if next_digest <= now:
        next_digest += timedelta(days=1)

    if not _job_already_pending(q, "drpl-daily-digest"):
        try:
            q.enqueue_at(next_digest, "app.worker.scheduled_tasks.send_daily_digest",
                         job_id="drpl-daily-digest", result_ttl=86400)
            out["scheduled"].append({"job": "drpl-daily-digest", "at": next_digest.isoformat()})
            log.info("seed_scheduled_jobs: queued daily digest at %s", next_digest.isoformat())
        except Exception as e:
            log.warning("seed_scheduled_jobs: could not seed daily digest: %s", e)

    # Closing-date scan: first run in 1 minute, then every 6h.
    if not _job_already_pending(q, "drpl-closing-date-scan"):
        try:
            q.enqueue_in(timedelta(minutes=1), "app.worker.scheduled_tasks.scan_closing_dates",
                         job_id="drpl-closing-date-scan", result_ttl=86400)
            out["scheduled"].append({"job": "drpl-closing-date-scan", "in": "1 minute"})
            log.info("seed_scheduled_jobs: queued first closing-date scan in 1 minute")
        except Exception as e:
            log.warning("seed_scheduled_jobs: could not seed closing-date scan: %s", e)

    # Auto-scoring reaper: first run in 1 minute, then every auto_scoring_interval_seconds.
    if settings.auto_scoring_enabled and not _job_already_pending(q, "drpl-auto-scoring-reaper"):
        try:
            q.enqueue_in(timedelta(minutes=1), "app.worker.auto_scoring_tasks.reap_unscored_tenders",
                         job_id="drpl-auto-scoring-reaper", result_ttl=86400)
            out["scheduled"].append({"job": "drpl-auto-scoring-reaper", "in": "1 minute"})
            log.info("seed_scheduled_jobs: queued first auto-scoring reap in 1 minute")
        except Exception as e:
            log.warning("seed_scheduled_jobs: could not seed auto-scoring reaper: %s", e)

    # Eager-analysis sweep: first run in 1 minute, then re-enqueued by the job itself.
    if not _job_already_pending(q, "drpl-eager-analysis-sweep"):
        try:
            q.enqueue_in(timedelta(minutes=1),
                         "app.worker.eager_analysis_tasks.enqueue_eager_analysis_sweep",
                         job_id="drpl-eager-analysis-sweep", result_ttl=86400)
            out["scheduled"].append({"job": "drpl-eager-analysis-sweep", "in": "1 minute"})
            log.info("seed_scheduled_jobs: queued first eager-analysis sweep in 1 minute")
        except Exception as e:  # noqa: BLE001
            log.warning("seed_scheduled_jobs: could not seed eager-analysis sweep: %s", e)

    # Discard cleanup: first run in 5 minutes, then every auto_discard_interval_hours.
    if settings.auto_discard_enabled and not _job_already_pending(q, "drpl-discard-cleanup"):
        try:
            q.enqueue_in(timedelta(minutes=5), "app.worker.scheduled_tasks.scan_discarded_tenders",
                         job_id="drpl-discard-cleanup", result_ttl=86400)
            out["scheduled"].append({"job": "drpl-discard-cleanup", "in": "5 minutes"})
            log.info("seed_scheduled_jobs: queued first discard cleanup in 5 minutes")
        except Exception as e:
            log.warning("seed_scheduled_jobs: could not seed discard cleanup: %s", e)

    # Archive sweep: first run in 10 minutes, then every archive_sweep_interval_hours.
    # Seeded regardless of the enabled flag — unlike the discard cleanup above,
    # the job itself checks the DB-backed setting on every tick and reschedules
    # even while disabled, so flipping it on in Platform Settings takes effect
    # without a redeploy. Gating the seed on the flag would break that.
    if not _job_already_pending(q, "drpl-archive-sweep"):
        try:
            q.enqueue_in(timedelta(minutes=10), "app.worker.scheduled_tasks.run_archive_sweep",
                         job_id="drpl-archive-sweep", result_ttl=86400)
            out["scheduled"].append({"job": "drpl-archive-sweep", "in": "10 minutes"})
            log.info("seed_scheduled_jobs: queued first archive sweep in 10 minutes")
        except Exception as e:  # noqa: BLE001
            log.warning("seed_scheduled_jobs: could not seed archive sweep: %s", e)

    return out


def _job_already_pending(queue, job_id: str) -> bool:
    """Check the queue + scheduled registry for an existing job with this id."""
    try:
        from rq.job import Job
        Job.fetch(job_id, connection=queue.connection)
        return True
    except Exception:
        # Job doesn't exist or fetch failed → safe to re-enqueue.
        return False
