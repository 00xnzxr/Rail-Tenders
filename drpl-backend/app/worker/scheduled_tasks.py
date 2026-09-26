"""
DRPL Backend - Scheduled background tasks for notifications.

Two periodic jobs:

* :func:`send_daily_digest`  — once per day at ``notifications_digest_hour_utc``.
* :func:`scan_closing_dates` — every 6 hours.

Self-rescheduling pattern: each task enqueues its own next run at the end via
``Queue.enqueue_at(next_run, fn, ...)``. ``app/services/seed_scheduled_jobs.py``
primes the loop at app startup (idempotent — won't double-schedule if a job is
already pending). This avoids depending on the rq-scheduler package.

If Redis is unavailable, both tasks are no-ops and log a warning.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.redis_client import get_queue, get_redis
from app.models.notification import Notification
from app.models.tender import Tender
from app.models.user import User
from app.services import email_service, notification_service
from app.services.notification_templates import render_digest_html


log = logging.getLogger(__name__)


# ── Daily digest ───────────────────────────────────────────────────────────


def send_daily_digest() -> dict:
    """Send a digest email to every active user with activity in the last 24h.

    The digest aggregates the user's own notification rows (in-app history)
    from the previous 24 hours. Users with no activity get nothing.
    """
    db: Session = SessionLocal()
    sent = 0
    skipped = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        users = (
            db.query(User)
            .filter(User.is_active == True, User.email.isnot(None))  # noqa: E712
            .all()
        )
        for user in users:
            items = (
                db.query(Notification)
                .filter(
                    Notification.user_id == user.id,
                    Notification.created_at >= cutoff,
                    Notification.kind != "digest.daily",  # don't recurse
                )
                .order_by(Notification.created_at.desc())
                .all()
            )
            if not items:
                skipped += 1
                continue
            try:
                html = render_digest_html(user, items)
                email_service.send_email(
                    to=user.email,
                    subject=f"DRPL Daily Digest — {len(items)} updates",
                    html=html,
                )
                sent += 1
            except Exception as e:
                log.warning("scheduled_tasks: digest send failed for user=%s: %s",
                            user.id, e)
    finally:
        db.close()

    # Reschedule next run at the configured hour, tomorrow.
    _reschedule_daily_digest()
    return {"sent": sent, "skipped": skipped}


# ── Closing-date scan ──────────────────────────────────────────────────────


_CLOSING_DEDUPE_TTL_HOURS = 36  # so a tender doesn't get pinged twice within 36h


def scan_closing_dates() -> dict:
    """Find tenders closing within the configured warning window and notify
    their assigned users.
    """
    settings = get_settings()
    # DB-first: admin-edited setting wins over env default.
    db_window: Optional[int] = None
    try:
        from app.services.settings_service import get_setting_value
        _db = SessionLocal()
        try:
            v = get_setting_value(_db, "notifications_closing_date_warning_hours")
            if v is not None:
                db_window = int(v)
        finally:
            _db.close()
    except Exception:
        pass
    window_hours = db_window if db_window else int(settings.notifications_closing_date_warning_hours or 48)

    db: Session = SessionLocal()
    notified = 0
    skipped = 0
    try:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=window_hours)
        tenders = (
            db.query(Tender)
            .filter(
                Tender.closing_date.isnot(None),
                Tender.closing_date >= now,
                Tender.closing_date <= cutoff,
                Tender.assigned_to.isnot(None),
            )
            .all()
        )
        for tender in tenders:
            # Skip terminal workflow states.
            if (tender.workflow_status or "").lower() in (
                "approved", "submitted", "archived", "rejected",
            ):
                continue
            if not _claim_dedupe(tender.id):
                skipped += 1
                continue
            try:
                hours_left = int(
                    (tender.closing_date - now).total_seconds() // 3600
                )
                notification_service.create(
                    db,
                    user_id=tender.assigned_to,
                    kind="tender.closing_soon",
                    title=f"Tender closing in {hours_left}h — {tender.title or tender.tender_id}",
                    body=(
                        f"Tender {tender.portal.upper()}-{tender.tender_id} closes at "
                        f"{tender.closing_date.strftime('%Y-%m-%d %H:%M UTC')}. "
                        f"Submit your proposal before then."
                    ),
                    action_url=f"/tenders/{tender.id}/command-center",
                    tender_id=tender.id,
                )
                db.commit()
                notified += 1
            except Exception as e:
                log.warning(
                    "scheduled_tasks: closing-date notify failed for tender=%s: %s",
                    tender.id, e,
                )
                try: db.rollback()
                except Exception: pass
    finally:
        db.close()

    _reschedule_closing_scan()
    return {"notified": notified, "skipped": skipped}


# ── Discard cleanup ────────────────────────────────────────────────────────


def _run_discard_cleanup(db) -> dict:
    """Archive stale Discarded tenders. Pure body (own-session caller passes db)."""
    from datetime import datetime, timezone, timedelta
    from app.models.tender import Tender
    from app.services.auto_scoring_settings import get_scoring_settings
    scoring = get_scoring_settings(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=scoring["auto_discard_days"])
    rows = (db.query(Tender)
            .filter(Tender.segment == "discarded")
            .filter(Tender.segment_overridden == False)   # noqa: E712
            .filter(Tender.workflow_status == "new")
            .filter(Tender.is_archived == False)           # noqa: E712
            .filter(Tender.created_at < cutoff)
            .all())
    # One clock for the whole batch, so every row in a run shares an archived_at
    # (and therefore the same purge-clock boundary, if one ever applied).
    stamped_at = datetime.now(timezone.utc)
    for t in rows:
        t.is_archived = True
        t.archived_at = stamped_at
        # SAFETY: this reason is what keeps auto-discarded rows OUT of the
        # archive sweep's purge forever — purge_candidates_query only ever
        # touches archive_reason == "past_due".
        t.archive_reason = "auto_discard"
    db.commit()
    log.info("scan_discarded_tenders: archived %d discarded tenders older than %dd",
             len(rows), scoring["auto_discard_days"])
    return {"archived": len(rows)}


def scan_discarded_tenders() -> dict:
    from app.services.auto_scoring_settings import get_scoring_settings
    db = SessionLocal()
    try:
        scoring = get_scoring_settings(db)
        if not scoring["auto_discard_enabled"]:
            return {"archived": 0, "rescheduled": False, "reason": "disabled"}
        out = _run_discard_cleanup(db)
    finally:
        db.close()
    _reschedule_discard_cleanup()
    out["rescheduled"] = True
    return out


def _reschedule_discard_cleanup() -> None:
    q = get_queue()
    if q is None:
        return
    db = SessionLocal()
    try:
        from app.services.auto_scoring_settings import get_scoring_settings
        hours = get_scoring_settings(db)["auto_discard_interval_hours"]
    finally:
        db.close()
    try:
        q.enqueue_in(timedelta(hours=hours), "app.worker.scheduled_tasks.scan_discarded_tenders",
                     job_id="drpl-discard-cleanup", result_ttl=86400)
    except Exception as e:
        log.warning("scheduled_tasks: could not reschedule discard cleanup: %s", e)


# ── Archive sweep (past-due untouched tenders) ─────────────────────────────


def run_archive_sweep() -> dict:
    """Archive past-due untouched tenders, then purge ones archived long enough.

    Ships disabled (``archive_sweep_enabled=False``). The flag is also the kill
    switch — flipping it in Platform Settings stops the sweep with no deploy.

    Returns ``{"archived": int, "purged": int, "rescheduled": True}`` plus:

    * ``reason: "disabled"`` when the flag is off (and nothing was done);
    * ``guard_ok: bool`` when the flag is on — ``False`` means every
      work-artifact probe failed, so the service failed CLOSED and archived
      nothing. Without this, a broken guard is indistinguishable from an idle
      sweep: both report ``archived: 0``.

    NOTHING may escape this function before ``_reschedule_archive_sweep()``.
    Unlike the three sibling jobs above, this one is seeded only at startup and
    has no RQ retry or on_failure hook, so a propagated exception does not just
    fail one tick — it kills the heartbeat until someone redeploys. Any failure
    therefore degrades to the same safe no-op as the disabled path.
    """
    from app.services.auto_scoring_settings import get_scoring_settings
    from app.services import tender_archive_service as tas

    archived = purged = 0
    disabled = True
    guard_ok = None
    db = SessionLocal()
    try:
        try:
            cfg = get_scoring_settings(db)
            # Disabled is a no-op, NOT an early return: the reschedule below has
            # to run either way, or the self-rescheduling loop dies while
            # disabled and flipping the flag on would need a redeploy to restart
            # it.
            disabled = not cfg["archive_sweep_enabled"]
            if not disabled:
                # ONE clock per tick, shared by both passes: if each pass read
                # its own now(), a tender could sit exactly on a day boundary
                # and be evaluated against two different cutoffs in the same run.
                now = datetime.now(timezone.utc)
                batch = cfg["archive_sweep_batch_size"]
                try:
                    # The service fails closed by returning true() from the
                    # work-artifact guard when every probe fails; detect that
                    # here so the job result is honest about a zero it did not
                    # choose. Read-only probes, so this is safe before writes.
                    from sqlalchemy.sql.elements import True_
                    guard_ok = not isinstance(
                        tas._has_work_artifacts_clause(db), True_)
                except Exception as e:  # noqa: BLE001
                    log.warning("archive_sweep: guard health check failed: %s", e)
                    guard_ok = None
                try:
                    archived = tas.run_archive_pass(
                        db, now=now, grace_days=cfg["archive_grace_days"],
                        batch_size=batch)
                except Exception as e:  # noqa: BLE001
                    log.warning("archive_sweep: archive pass failed: %s", e)
                    try: db.rollback()
                    except Exception: pass
                try:
                    purged = tas.run_purge_pass(
                        db, now=now, purge_days=cfg["archive_purge_days"],
                        batch_size=batch)
                except Exception as e:  # noqa: BLE001
                    log.warning("archive_sweep: purge pass failed: %s", e)
                    try: db.rollback()
                    except Exception: pass
        except Exception as e:  # noqa: BLE001
            # Reading the settings (or anything else outside the per-pass
            # handlers) blew up. Degrade to the disabled no-op that `disabled`
            # is pre-initialised to — the reschedule below MUST still run.
            # error, not warning: a sweep that cannot read its own settings is
            # broken, not idle. Same volume as the total-probe-failure case.
            disabled = True
            archived = purged = 0
            log.error("archive_sweep: tick failed before any work (%s) — "
                      "degrading to a no-op and rescheduling anyway", e,
                      exc_info=True)
    finally:
        # A failing close() must not escape the finally and skip the reschedule
        # below. Leaking a session is survivable; a dead heartbeat is not.
        try:
            db.close()
        except Exception as e:  # noqa: BLE001
            log.warning("archive_sweep: session close failed: %s", e)

    # Runs on EVERY tick, including the disabled one — this is the only thing
    # keeping the loop alive so the enabled flag can be flipped without a deploy.
    _reschedule_archive_sweep()

    out = {"archived": archived, "purged": purged, "rescheduled": True}
    if disabled:
        out["reason"] = "disabled"
        log.info("archive_sweep: disabled — no-op, rescheduled next tick")
    else:
        out["guard_ok"] = guard_ok
        log.info("archive_sweep: archived=%d purged=%d guard_ok=%s%s",
                 archived, purged, guard_ok,
                 "" if guard_ok is not False else
                 " (WORK-ARTIFACT GUARD BROKEN — archived 0 by force, not by "
                 "absence of candidates)")
    return out


def _reschedule_archive_sweep() -> None:
    q = get_queue()
    if q is None:
        return
    hours = 6
    db = SessionLocal()
    try:
        from app.services.auto_scoring_settings import get_scoring_settings
        hours = get_scoring_settings(db)["archive_sweep_interval_hours"]
    except Exception as e:  # noqa: BLE001
        log.warning("scheduled_tasks: archive sweep interval lookup failed, "
                    "using %dh: %s", hours, e)
    finally:
        db.close()
    try:
        q.enqueue_in(timedelta(hours=hours), "app.worker.scheduled_tasks.run_archive_sweep",
                     job_id="drpl-archive-sweep", result_ttl=86400)
    except Exception as e:  # noqa: BLE001
        log.warning("scheduled_tasks: could not reschedule archive sweep: %s", e)


# ── Self-rescheduling helpers ──────────────────────────────────────────────


def _next_digest_run() -> datetime:
    settings = get_settings()
    hour = int(settings.notifications_digest_hour_utc or 13) % 24
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _reschedule_daily_digest() -> None:
    q = get_queue()
    if q is None:
        return
    try:
        q.enqueue_at(_next_digest_run(), "app.worker.scheduled_tasks.send_daily_digest",
                     job_id="drpl-daily-digest", result_ttl=86400)
    except Exception as e:
        log.warning("scheduled_tasks: could not reschedule daily digest: %s", e)


def _reschedule_closing_scan() -> None:
    q = get_queue()
    if q is None:
        return
    try:
        q.enqueue_in(timedelta(hours=6), "app.worker.scheduled_tasks.scan_closing_dates",
                     job_id="drpl-closing-date-scan", result_ttl=86400)
    except Exception as e:
        log.warning("scheduled_tasks: could not reschedule closing-date scan: %s", e)


def _claim_dedupe(tender_id: int) -> bool:
    """Set a Redis key `tender:closing_notified:{id}` with TTL so the same
    tender isn't notified twice in one 36h window. Returns True if we got
    the lock (= first claim); False if it was already set."""
    client = get_redis()
    if client is None:
        return True  # no Redis = no dedupe, but still notify
    try:
        return bool(client.set(
            f"drpl:tender:closing_notified:{tender_id}",
            "1",
            ex=_CLOSING_DEDUPE_TTL_HOURS * 3600,
            nx=True,
        ))
    except Exception:
        return True
