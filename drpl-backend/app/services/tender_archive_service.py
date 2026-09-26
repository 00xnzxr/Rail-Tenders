"""Tender archive lifecycle: archive past-due untouched tenders, purge after N days.

See docs/superpowers/specs/2026-07-31-tender-archive-and-purge-design.md.

Every function here is pure w.r.t. time — callers pass ``now`` — so the day
boundaries are testable without sleeping.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import Boolean, bindparam, false, literal_column, not_, or_, text, true
from sqlalchemy.orm import Session

from app.models.tender import Tender, TenderDocument

log = logging.getLogger(__name__)

# Tables whose rows mean a human did work on this tender. A tender with a row in
# ANY of these is never auto-archived.
#
# `tender_documents` is deliberately NOT here: the extension and the backend NIT
# fetcher capture documents automatically, with no human intent, so their
# presence is not evidence of work.
#
# NOTE: `costing_templates` is intentionally excluded — it has no `tender_id`
# column at all (app/models/costing_template.py: it's a zone-scoped template,
# e.g. "CR"/"SECR", shared across tenders, not owned by one).
WORK_ARTIFACT_TABLES = (
    "cost_breakdowns",
    "proposal_sessions",
    "checklist_items",
    "workspace_configs",
    "document_workspaces",
    "document_extraction_results",
    "tender_analysis_summaries",
    "boq_items",
    "generated_documents",
)

# Every table with a tender_id that must be cleaned up on hard delete. These are
# logical FKs with no DB-level cascade, so nothing removes them for us.
#
# INVARIANT: a new table with a tender_id must be added here AND, if it
# represents human work, to WORK_ARTIFACT_TABLES above.
#
# NOTE: `app/models/message_batch.py` defines TWO tables. The parent
# `message_batches` is intentionally excluded — it has no `tender_id` column
# and no structured link back to a tender (any association lives only in a
# free-text `metadata_json` blob). Its child `message_batch_items` DOES have
# a `tender_id` column and IS included below — do not conflate the two.
#
# `extraction_feedback` has no `tender_id` either — it links to a tender only
# indirectly via `extraction_result_id` -> document_extraction_results.tender_id,
# so it is handled with a subquery below rather than a direct filter. The same
# is true of `proposal_messages` / `proposal_documents` / `proposal_reviews`,
# which hang off `proposal_sessions.id` (see app/models/proposal.py) — all
# three are cleared with subqueries before the loop below, since the loop
# deletes their parent `proposal_sessions` row.
CHILD_TABLES = WORK_ARTIFACT_TABLES + (
    "tender_documents",
    "critical_clause_flags",
    "boq_schedule_totals",
    "document_embeddings",
    "notifications",
    "agent_runs",
    "agent_memories",
    "message_batch_items",
)


def delete_tenders_deep(db: Session, tender_ids: list[int]) -> int:
    """Hard-delete tenders plus every child row and stored file. Commits.

    Returns the number of Tender rows deleted.
    """
    if not tender_ids:
        return 0

    _delete_stored_files(db, tender_ids)

    # Grandchild tables: no `tender_id` of their own, only reachable via a
    # parent row that DOES have `tender_id`. Must run BEFORE the CHILD_TABLES
    # loop below, since that loop deletes the parent (cost_breakdowns /
    # document_extraction_results / proposal_sessions) out from under them.
    #
    # NOTE: `expanding=True` is required for `IN :ids` to bind a tuple
    # correctly across dialects (SQLite's pysqlite driver errors on a bare
    # tuple param without it; a literal `:ids` form only happens to work
    # under psycopg2's implicit tuple adaptation on Postgres).
    grandchild_deletes = (
        # cost_breakdown_lines: FK column is `cost_breakdown_id` (see
        # app/models/cost_breakdown.py CostBreakdownLine), not `breakdown_id`.
        ("cost_breakdown_lines",
         "DELETE FROM cost_breakdown_lines WHERE cost_breakdown_id IN "
         "(SELECT id FROM cost_breakdowns WHERE tender_id IN :ids)"),
        # extraction_feedback -> document_extraction_results.tender_id.
        ("extraction_feedback",
         "DELETE FROM extraction_feedback WHERE extraction_result_id IN "
         "(SELECT id FROM document_extraction_results WHERE tender_id IN :ids)"),
        # proposal_messages / proposal_documents / proposal_reviews all hang
        # off proposal_sessions.id via a `session_id` column (see
        # app/models/proposal.py) — proposal_sessions itself has tender_id.
        ("proposal_messages",
         "DELETE FROM proposal_messages WHERE session_id IN "
         "(SELECT id FROM proposal_sessions WHERE tender_id IN :ids)"),
        ("proposal_documents",
         "DELETE FROM proposal_documents WHERE session_id IN "
         "(SELECT id FROM proposal_sessions WHERE tender_id IN :ids)"),
        ("proposal_reviews",
         "DELETE FROM proposal_reviews WHERE session_id IN "
         "(SELECT id FROM proposal_sessions WHERE tender_id IN :ids)"),
    )
    for table, sql in grandchild_deletes:
        # Same per-table SAVEPOINT posture as the CHILD_TABLES loop below: a
        # missing table on an older deploy should skip just that delete, not
        # abort the whole call (and on Postgres, without a SAVEPOINT, one
        # failed statement poisons the transaction for everything after it).
        try:
            with db.begin_nested():
                db.execute(text(sql).bindparams(bindparam("ids", expanding=True)),
                           {"ids": tuple(tender_ids)})
        except Exception as e:  # noqa: BLE001 — table may not exist on older deploys
            log.warning("delete_tenders_deep: skipping %s: %s", table, e)

    for table in CHILD_TABLES:
        # Each table gets its own SAVEPOINT: if one DELETE fails (e.g. a table
        # that doesn't exist yet on an older deploy), only that table's
        # statement is rolled back. Without a savepoint, `db.rollback()` would
        # roll back the *whole* transaction — silently undoing every
        # already-successful DELETE for tables processed earlier in this loop.
        try:
            with db.begin_nested():
                db.execute(text(f"DELETE FROM {table} WHERE tender_id IN :ids")
                           .bindparams(bindparam("ids", expanding=True)),
                           {"ids": tuple(tender_ids)})
        except Exception as e:  # noqa: BLE001 — table may not exist on older deploys
            log.warning("delete_tenders_deep: skipping %s: %s", table, e)

    # synchronize_session="fetch" (not False): callers may still hold references
    # to Tender instances being deleted (as the tests do). With
    # synchronize_session=False the session's identity map is never told these
    # rows are gone, so a later attribute access on a stale instance re-fetches
    # from the DB post-commit and raises ObjectDeletedError instead of just
    # reflecting the deletion.
    deleted = (db.query(Tender)
               .filter(Tender.id.in_(tender_ids))
               .delete(synchronize_session="fetch"))
    db.commit()
    log.info("delete_tenders_deep: removed %d tenders (%s)", deleted, tender_ids[:10])
    return deleted


def _delete_stored_files(db: Session, tender_ids: list[int]) -> None:
    """Best-effort removal of every stored file for these tenders.

    Storage failures must never block the DB delete — an orphaned blob is
    cheaper than a purge that can never complete.
    """
    from app.services.storage_service import get_storage_service

    rows = (db.query(TenderDocument.file_path)
            .filter(TenderDocument.tender_id.in_(tender_ids))
            .all())
    if not rows:
        return
    storage = get_storage_service()
    for (file_path,) in rows:
        if not file_path:
            continue
        try:
            storage.delete_file_sync(file_path)
        except Exception as e:  # noqa: BLE001
            log.warning("delete_tenders_deep: could not delete file %s: %s", file_path, e)


# --------------------------------------------------------------------------
# Sweep passes
# --------------------------------------------------------------------------

# Only `new` tenders are archivable. Anything a human moved forward in the
# workflow (in_progress, checklist_ready, proposal_draft, ...) is off-limits,
# independent of whether it left an artifact row behind.
_ARCHIVABLE_WORKFLOW_STATUS = "new"

# The ONLY archive_reason a purge may ever touch. See purge_candidates_query.
PURGEABLE_ARCHIVE_REASON = "past_due"


def _has_work_artifacts_clause(db: Session):
    """A single OR-of-EXISTS over every work-artifact table.

    Built with raw EXISTS subqueries rather than ORM relationships because
    these are logical FKs — the models declare no relationship to Tender.

    Tables absent on a given deployment are probed once and skipped. The probe
    runs inside a SAVEPOINT (`begin_nested`) rather than being followed by
    ``db.rollback()``: a bare rollback would discard the caller's uncommitted
    work, and on Postgres a failed statement without a SAVEPOINT poisons the
    whole transaction for every statement after it.

    FAILS CLOSED. Two distinct failure modes, deliberately treated differently:

    * SOME probes fail — benign. A table genuinely missing on an older deploy
      cannot contain work artifacts, so the guard is still correct when built
      from the tables that did respond. Returns the partial OR-of-EXISTS.
    * ALL probes fail — the guard is not functioning. This is NOT distinguishable
      from "every table is missing", and the realistic causes (bad search_path,
      revoked permissions, a connection blip mid-loop) all mean we cannot see
      work artifacts that really exist. Returning None here would drop the
      ``not_()`` filter entirely and archive EVERY past-due new/unassigned
      tender — costings, workspaces, BOQs and all — each starting a purge
      clock. So we return ``true()`` instead: read as "assume every tender HAS
      work artifacts", which ``not_()`` turns into zero candidates. Archiving
      nothing is always recoverable; archiving everything is not.
    """
    clauses = []
    failed = []
    for table in WORK_ARTIFACT_TABLES:
        try:
            with db.begin_nested():
                db.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        except Exception as e:  # noqa: BLE001 — table absent on this deployment
            failed.append(table)
            log.warning("archive_sweep: work-artifact table %s unavailable: %s", table, e)
            continue
        # literal_column(..., type_=Boolean) rather than text(): `or_()` of a
        # SINGLE element returns that element unchanged, and a bare TextClause
        # has no working `_negate` — `not_(text(...))` trips an internal
        # assertion in SQLAlchemy. That never fires with all 9 tables present
        # (2+ clauses collapse to a negatable BooleanClauseList) but WOULD
        # crash the sweep on a deploy where exactly one table probes clean.
        # A boolean-typed literal_column negates correctly at any count.
        clauses.append(
            literal_column(
                f"EXISTS (SELECT 1 FROM {table} w WHERE w.tender_id = tenders.id)",
                type_=Boolean(),
            )
        )
    if not clauses:
        # Error, not warning: the sweep is broken, not merely on an old schema.
        log.error(
            "archive_sweep: ALL %d work-artifact probes failed (%s) — failing "
            "CLOSED, archiving nothing this pass. The archive guard cannot see "
            "work artifacts; check DB permissions/search_path/connectivity.",
            len(failed), ", ".join(failed),
        )
        return true()
    return or_(*clauses)


def archive_candidates_query(db: Session, *, now: datetime, grace_days: int):
    """Tenders eligible for archiving: past due, untouched, not already archived.

    Returns a Query (not a list) so callers can `.count()`, `.limit()` or
    `.all()` — the dry-run script (Task 6) reuses it verbatim.
    """
    cutoff = now - timedelta(days=grace_days)
    q = (db.query(Tender)
         .filter(Tender.closing_date.isnot(None))
         .filter(Tender.closing_date < cutoff)
         .filter(or_(Tender.is_archived.is_(False), Tender.is_archived.is_(None)))
         .filter(Tender.workflow_status == _ARCHIVABLE_WORKFLOW_STATUS)
         .filter(Tender.assigned_to.is_(None))
         # A human who touched the segment — including anyone who hit Restore
         # (POST /tenders/{id}/restore sets this) — owns the row. Without this
         # filter, restoring a past-due tender is a lie: the next sweep tick
         # re-archives it and restarts the purge clock.
         #
         # NULL-tolerant on purpose, matching the is_archived filter above.
         # The column is `nullable=False` in the model, but it reaches existing
         # deployments via `ALTER TABLE ... ADD COLUMN ... DEFAULT false`
         # (main.py:180, :615) with no NOT NULL, so legacy rows can hold NULL.
         # SQL three-valued logic makes `segment_overridden = 0` drop those rows
         # silently — they would be exempted from archiving without anyone
         # having touched them. Spelling out IS NULL keeps the default
         # (untouched) behaviour explicit rather than accidental.
         .filter(or_(Tender.segment_overridden.is_(False),
                     Tender.segment_overridden.is_(None))))
    # Always applied — _has_work_artifacts_clause never returns None. On total
    # probe failure it returns true(), so not_(true()) yields zero candidates
    # (fail closed) rather than the filter being silently skipped.
    q = q.filter(not_(_has_work_artifacts_clause(db)))
    return q.order_by(Tender.id)


def purge_candidates_query(db: Session, *, now: datetime, purge_days: int):
    """Archived-as-past-due tenders whose purge clock has expired.

    SAFETY INVARIANT: the ``archive_reason == 'past_due'`` filter is the only
    thing standing between a hard delete and rows this sweep never archived.
    Rows archived by the pre-existing auto-discard job (`auto_discard`),
    archived by hand (`manual`), or predating this feature entirely
    (archived_at IS NULL / archive_reason IS NULL) must survive forever.
    Do not relax this filter, and do not add an `or_` around it.

    ``purge_days <= 0`` means NEVER PURGE — archive-only mode. Taken literally,
    0 would compute a cutoff of `now` and delete everything already archived,
    which is the most destructive possible reading of the number an admin is
    most likely to type meaning "off". Negative values are nonsense input and
    fail the same safe way.
    """
    if purge_days <= 0:
        return (db.query(Tender).filter(false()).order_by(Tender.id))
    cutoff = now - timedelta(days=purge_days)
    return (db.query(Tender)
            .filter(Tender.archive_reason == PURGEABLE_ARCHIVE_REASON)
            .filter(Tender.archived_at.isnot(None))
            .filter(Tender.archived_at < cutoff)
            .order_by(Tender.id))


def run_archive_pass(db: Session, *, now: datetime, grace_days: int,
                     batch_size: int) -> int:
    """Flag one batch of past-due untouched tenders as archived. Commits.

    Batched so a first-run backlog of thousands of rows cannot blow up a
    single transaction; the scheduler calls this repeatedly until it returns 0.
    """
    rows = (archive_candidates_query(db, now=now, grace_days=grace_days)
            .limit(batch_size).all())
    if not rows:
        return 0
    for t in rows:
        t.is_archived = True
        t.archived_at = now
        t.archive_reason = PURGEABLE_ARCHIVE_REASON
    db.commit()
    log.info("archive_sweep: archived %d past-due untouched tenders (grace=%dd)",
             len(rows), grace_days)
    return len(rows)


def run_purge_pass(db: Session, *, now: datetime, purge_days: int,
                   batch_size: int) -> int:
    """Hard-delete one batch of expired past-due archives. Commits.

    Deletion is delegated wholesale to ``delete_tenders_deep`` so child-table
    cleanup and stored-file removal stay in exactly one place.
    """
    ids = [t.id for t in
           purge_candidates_query(db, now=now, purge_days=purge_days)
           .limit(batch_size).all()]
    if not ids:
        return 0
    deleted = delete_tenders_deep(db, ids)
    log.info("archive_sweep: purged %d tenders archived >%dd ago", deleted, purge_days)
    return deleted
