# Tender Archive & Purge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Past-due, untouched tenders auto-move to an Archive, drop out of every count, and are hard-deleted 7 days later.

**Architecture:** Two new nullable columns on `tenders` (`archived_at`, `archive_reason`) let us distinguish *why* a row was archived, so the existing auto-discard backlog is never purged. A self-rescheduling RQ job runs two passes (archive, then purge). A shared `delete_tenders_deep()` helper removes every child row across 13 logical-FK tables and is reused by the existing `bulk-delete` endpoint. The frontend gains an `/archive` page.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, RQ, pytest — React 18 + Vite + TypeScript + TanStack Query on the frontend.

**Spec:** `docs/superpowers/specs/2026-07-31-tender-archive-and-purge-design.md`

## Global Constraints

- **The live DB is production.** `drpl-backend/.env` `DATABASE_URL` points at the live Neon prod database. Never run the sweep, the purge, or any `DELETE` against it during development. Tests run against a throwaway SQLite file forced by `tests/conftest.py`.
- **Schema drift discipline (CLAUDE.md):** every new column needs an Alembic revision **and** an entry in both `_apply_schema_drift_fixes()` (Postgres, `app/main.py:161`) and `_add_missing_columns()` (SQLite, `app/main.py:578`).
- **Ships off:** `archive_sweep_enabled` defaults to `False`. No task may change that default.
- **Only `archive_reason == 'past_due'` is ever auto-purged.** Rows with any other reason, or `NULL`, must survive the purge forever. This invariant has a dedicated test in Task 4.
- **All file IO goes through `app/services/storage_service.py`** — never touch `uploads/` directly.
- **Never loop per-row for existence checks.** Use `NOT EXISTS` subqueries or a single `IN` query.
- Backend tests: `cd drpl-backend && pytest tests/<file> -v`.
- Commit after every task. Branch: `feat/tender-archive-purge`.

---

## File Structure

**Backend — create:**
- `alembic/versions/20260731_tender_archive_fields.py` — the migration
- `app/services/tender_archive_service.py` — archive predicate, sweep passes, deep delete. All pure functions taking `db` and `now`.
- `scripts/archive_sweep_dryrun.py` — read-only candidate report
- `tests/test_tender_archive_service.py` — predicate + boundary + purge-safety tests
- `tests/test_archive_routes.py` — the three new endpoints

**Backend — modify:**
- `app/models/tender.py:96` — two new columns
- `app/main.py:228` + `:661` — drift-fix entries
- `app/core/config.py:60` — three new settings defaults
- `app/services/auto_scoring_settings.py:22` — three new `_KEYS` entries
- `app/worker/scheduled_tasks.py:196` — the sweep job + reschedule helper; stamp `archived_at` in `_run_discard_cleanup`
- `app/services/seed_scheduled_jobs.py:90` — seed `drpl-archive-sweep`
- `app/api/routes/tenders.py` — 3 new endpoints, stats archive filter, `bulk-archive`/`bulk-delete` fixes
- `app/api/routes/admin_dashboard.py:54-67` — archive filter
- `app/schemas/__init__.py:128` — archive fields on `TenderResponse`
- `tests/test_dashboard_stats.py` — archived-exclusion cases

**Frontend — create:**
- `src/pages/ArchivePage.tsx`

**Frontend — modify:**
- `src/router.tsx:95`, `src/components/layout/SidebarNav.tsx:17`, `src/lib/api.ts`, `src/types/tender.ts`

---

## Task 1: Schema — archive columns

**Files:**
- Modify: `drpl-backend/app/models/tender.py:96`
- Create: `drpl-backend/alembic/versions/20260731_tender_archive_fields.py`
- Modify: `drpl-backend/app/main.py` (two lists)
- Test: `drpl-backend/tests/test_tender_archive_service.py`

**Interfaces:**
- Produces: `Tender.archived_at` (`datetime | None`), `Tender.archive_reason` (`str | None`, one of `past_due` / `auto_discard` / `manual`). Every later task depends on these.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_tender_archive_service.py`:

```python
"""Archive columns, sweep passes, and deep delete."""
from datetime import datetime, timezone, timedelta

from app.models.tender import Tender

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)

_n = 0


def _mk(db, *, closing_days_ago=None, workflow_status="new", assigned_to=None,
        is_archived=False, archived_at=None, archive_reason=None):
    """Insert a tender. closing_days_ago=4 means closing_date was 4 days before NOW."""
    global _n
    _n += 1
    closing = None if closing_days_ago is None else NOW - timedelta(days=closing_days_ago)
    t = Tender(portal="ireps", tender_id=f"ARC{_n}", title=f"archive test {_n}",
               closing_date=closing, workflow_status=workflow_status,
               assigned_to=assigned_to, is_archived=is_archived,
               archived_at=archived_at, archive_reason=archive_reason)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_archive_columns_exist_and_default_to_none(db):
    t = _mk(db)
    assert t.archived_at is None
    assert t.archive_reason is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -v`
Expected: FAIL — `TypeError: 'archived_at' is an invalid keyword argument for Tender`

- [ ] **Step 3: Add the columns to the model**

In `drpl-backend/app/models/tender.py`, directly after the `is_archived` line (`:96`):

```python
    is_archived = Column(Boolean, default=False)
    # Archive lifecycle — see docs/superpowers/specs/2026-07-31-tender-archive-and-purge-design.md
    archived_at = Column(DateTime(timezone=True), nullable=True, index=True)  # purge clock
    archive_reason = Column(String(30), nullable=True)   # past_due | auto_discard | manual
```

Only `past_due` rows are ever auto-purged.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -v`
Expected: PASS

- [ ] **Step 5: Write the Alembic migration**

Create `drpl-backend/alembic/versions/20260731_tender_archive_fields.py`:

```python
"""tender archive fields

Revision ID: 20260731_tender_archive
Revises: 7f6ae5991d3c
Create Date: 2026-07-31
"""
from alembic import op
import sqlalchemy as sa

revision = "20260731_tender_archive"
down_revision = "7f6ae5991d3c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tenders", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenders", sa.Column("archive_reason", sa.String(30), nullable=True))
    op.create_index("ix_tenders_archived_at", "tenders", ["archived_at"])


def downgrade():
    op.drop_index("ix_tenders_archived_at", table_name="tenders")
    op.drop_column("tenders", "archive_reason")
    op.drop_column("tenders", "archived_at")
```

- [ ] **Step 6: Add the Postgres drift fix**

In `drpl-backend/app/main.py`, in the `_apply_schema_drift_fixes()` statement list, after the `eager_analysis_at` line (`:229`):

```python
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS archive_reason VARCHAR(30)",
```

And in the index list, after the `ix_tenders_eager_analysis_status` line (`:235`):

```python
        "CREATE INDEX IF NOT EXISTS ix_tenders_archived_at ON tenders(archived_at)",
```

- [ ] **Step 7: Add the SQLite drift fix**

In `drpl-backend/app/main.py`, in the `_add_missing_columns()` tuple list, after the `("tenders", "eager_analysis_at", "TIMESTAMP")` line (`:662`):

```python
        ("tenders", "archived_at", "TIMESTAMP"),
        ("tenders", "archive_reason", "VARCHAR(30)"),
```

- [ ] **Step 8: Verify the migration chain is linear**

Run: `cd drpl-backend && alembic heads`
Expected: exactly one head, `20260731_tender_archive`. If two heads print, the `down_revision` is wrong — fix it before continuing.

- [ ] **Step 9: Commit**

```bash
git add drpl-backend/app/models/tender.py drpl-backend/alembic/versions/20260731_tender_archive_fields.py drpl-backend/app/main.py drpl-backend/tests/test_tender_archive_service.py
git commit -m "feat(archive): add archived_at + archive_reason to tenders"
```

---

## Task 2: Settings

**Files:**
- Modify: `drpl-backend/app/core/config.py:60`
- Modify: `drpl-backend/app/services/auto_scoring_settings.py:22`
- Test: `drpl-backend/tests/test_tender_archive_service.py`

**Interfaces:**
- Produces: `get_scoring_settings(db)` returns three new keys — `archive_sweep_enabled: bool`, `archive_grace_days: int`, `archive_purge_days: int`. Tasks 4 and 5 read these.

- [ ] **Step 1: Write the failing test**

Append to `drpl-backend/tests/test_tender_archive_service.py`:

```python
def test_archive_settings_defaults(db):
    from app.services.auto_scoring_settings import get_scoring_settings
    s = get_scoring_settings(db)
    assert s["archive_sweep_enabled"] is False   # ships OFF — deliberate
    assert s["archive_grace_days"] == 3
    assert s["archive_purge_days"] == 7


def test_archive_settings_are_db_overridable(db):
    from app.services.auto_scoring_settings import get_scoring_settings, set_scoring_setting
    set_scoring_setting(db, "archive_grace_days", 5)
    assert get_scoring_settings(db)["archive_grace_days"] == 5
    set_scoring_setting(db, "archive_grace_days", 3)   # restore
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -k settings -v`
Expected: FAIL with `KeyError: 'archive_sweep_enabled'`

- [ ] **Step 3: Add config defaults**

In `drpl-backend/app/core/config.py`, after `auto_discard_interval_hours` (`:60`):

```python
    # Archive sweep — past-due untouched tenders. Ships OFF; enable from admin
    # after checking scripts/archive_sweep_dryrun.py output.
    archive_sweep_enabled: bool = False
    archive_grace_days: int = 3         # days past closing_date before archiving
    archive_purge_days: int = 7         # days in archive before hard delete
    archive_sweep_interval_hours: int = 6
    archive_sweep_batch_size: int = 500
```

- [ ] **Step 4: Register them as DB-overridable**

In `drpl-backend/app/services/auto_scoring_settings.py`, add to the `_KEYS` dict after `auto_discard_interval_hours` (`:22`):

```python
    "archive_sweep_enabled": "archive_sweep_enabled",
    "archive_grace_days": "archive_grace_days",
    "archive_purge_days": "archive_purge_days",
    "archive_sweep_interval_hours": "archive_sweep_interval_hours",
    "archive_sweep_batch_size": "archive_sweep_batch_size",
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/core/config.py drpl-backend/app/services/auto_scoring_settings.py drpl-backend/tests/test_tender_archive_service.py
git commit -m "feat(archive): add archive sweep settings, default off"
```

---

## Task 3: Deep delete helper

Removes a tender and every child row. 13 tables carry a `tender_id` with **no DB-level cascade**, so a bare `DELETE FROM tenders` orphans all of them. `notifications.tender_id` is the one real FK (`app/models/notification.py:34`) — without deleting those first, Postgres raises IntegrityError.

**Files:**
- Create: `drpl-backend/app/services/tender_archive_service.py`
- Modify: `drpl-backend/app/api/routes/tenders.py:123-141`
- Test: `drpl-backend/tests/test_tender_archive_service.py`

**Interfaces:**
- Produces: `delete_tenders_deep(db, tender_ids: list[int]) -> int` — returns the number of `Tender` rows deleted. Commits. Used by Task 4's purge pass and by `bulk-delete`.

- [ ] **Step 1: Write the failing test**

Append to `drpl-backend/tests/test_tender_archive_service.py`:

```python
def test_delete_tenders_deep_removes_children(db):
    from app.models.tender import TenderDocument
    from app.models.checklist import ChecklistItem
    from app.models.cost_breakdown import CostBreakdown
    from app.services.tender_archive_service import delete_tenders_deep

    t = _mk(db)
    db.add(TenderDocument(tender_id=t.id, file_name="a.pdf", file_path="tenders/1/a.pdf"))
    db.add(ChecklistItem(tender_id=t.id, item_name="Reg cert"))
    db.add(CostBreakdown(tender_id=t.id))
    db.commit()

    assert delete_tenders_deep(db, [t.id]) == 1

    assert db.query(Tender).filter(Tender.id == t.id).first() is None
    assert db.query(TenderDocument).filter(TenderDocument.tender_id == t.id).count() == 0
    assert db.query(ChecklistItem).filter(ChecklistItem.tender_id == t.id).count() == 0
    assert db.query(CostBreakdown).filter(CostBreakdown.tender_id == t.id).count() == 0


def test_delete_tenders_deep_empty_list_is_noop(db):
    from app.services.tender_archive_service import delete_tenders_deep
    assert delete_tenders_deep(db, []) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -k deep -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.tender_archive_service'`

- [ ] **Step 3: Create the service with the deep delete**

Create `drpl-backend/app/services/tender_archive_service.py`:

```python
"""Tender archive lifecycle: archive past-due untouched tenders, purge after N days.

See docs/superpowers/specs/2026-07-31-tender-archive-and-purge-design.md.

Every function here is pure w.r.t. time — callers pass ``now`` — so the day
boundaries are testable without sleeping.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.tender import Tender, TenderDocument

log = logging.getLogger(__name__)

# Tables whose rows mean a human did work on this tender. A tender with a row in
# ANY of these is never auto-archived.
#
# `tender_documents` is deliberately NOT here: the extension and the backend NIT
# fetcher capture documents automatically, with no human intent, so their
# presence is not evidence of work.
WORK_ARTIFACT_TABLES = (
    "cost_breakdowns",
    "proposal_sessions",
    "checklist_items",
    "workspace_configs",
    "document_workspaces",
    "document_extraction_results",
    "tender_analysis_summaries",
    "costing_templates",
    "boq_items",
    "generated_documents",
)

# Every table with a tender_id that must be cleaned up on hard delete. These are
# logical FKs with no DB-level cascade, so nothing removes them for us.
#
# INVARIANT: a new table with a tender_id must be added here AND, if it
# represents human work, to WORK_ARTIFACT_TABLES above.
CHILD_TABLES = WORK_ARTIFACT_TABLES + (
    "tender_documents",
    "critical_clause_flags",
    "extraction_feedback",
    "boq_schedule_totals",
    "document_embeddings",
    "notifications",
    "agent_runs",
    "agent_memories",
    "message_batches",
)


def delete_tenders_deep(db: Session, tender_ids: list[int]) -> int:
    """Hard-delete tenders plus every child row and stored file. Commits.

    Returns the number of Tender rows deleted.
    """
    if not tender_ids:
        return 0

    _delete_stored_files(db, tender_ids)

    # cost_breakdown_lines hangs off cost_breakdowns, not tenders — clear it first.
    db.execute(
        text("DELETE FROM cost_breakdown_lines WHERE breakdown_id IN "
             "(SELECT id FROM cost_breakdowns WHERE tender_id IN :ids)"),
        {"ids": tuple(tender_ids)},
    )

    for table in CHILD_TABLES:
        try:
            db.execute(text(f"DELETE FROM {table} WHERE tender_id IN :ids"),
                       {"ids": tuple(tender_ids)})
        except Exception as e:  # noqa: BLE001 — table may not exist on older deploys
            log.warning("delete_tenders_deep: skipping %s: %s", table, e)
            db.rollback()

    deleted = (db.query(Tender)
               .filter(Tender.id.in_(tender_ids))
               .delete(synchronize_session=False))
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -k deep -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Fix the pre-existing bulk-delete orphan bug**

`POST /tenders/bulk-delete` currently deletes only `TenderDocument` rows, orphaning the other 12 tables. Replace the body at `drpl-backend/app/api/routes/tenders.py:136-141`:

```python
    from app.services.tender_archive_service import delete_tenders_deep
    deleted = delete_tenders_deep(db, tender_ids)
    return {"deleted": deleted}
```

Remove the two now-dead lines that deleted `TenderDocument` and `Tender` directly.

- [ ] **Step 6: Run the full backend suite for regressions**

Run: `cd drpl-backend && pytest tests/ -q`
Expected: no new failures versus the pre-task baseline. If `bulk-delete` tests exist and fail, read them — the return shape `{"deleted": n}` is unchanged, so a failure means a real behaviour difference worth understanding before proceeding.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/services/tender_archive_service.py drpl-backend/app/api/routes/tenders.py drpl-backend/tests/test_tender_archive_service.py
git commit -m "feat(archive): deep-delete helper; fix bulk-delete orphaning 12 child tables"
```

---

## Task 4: Sweep passes

**Files:**
- Modify: `drpl-backend/app/services/tender_archive_service.py`
- Test: `drpl-backend/tests/test_tender_archive_service.py`

**Interfaces:**
- Produces:
  - `archive_candidates_query(db, now, grace_days)` → a SQLAlchemy `Query` of `Tender` rows eligible for archiving. Reused by the dry-run script (Task 6).
  - `purge_candidates_query(db, now, purge_days)` → a `Query` of rows eligible for hard delete.
  - `run_archive_pass(db, now, grace_days, batch_size) -> int` — flags rows, commits, returns count.
  - `run_purge_pass(db, now, purge_days, batch_size) -> int` — deep-deletes, returns count.

- [ ] **Step 1: Write the failing tests**

Append to `drpl-backend/tests/test_tender_archive_service.py`:

```python
def test_past_due_untouched_tender_is_archived(db):
    from app.services.tender_archive_service import run_archive_pass
    t = _mk(db, closing_days_ago=4)
    assert run_archive_pass(db, now=NOW, grace_days=3, batch_size=500) >= 1
    db.refresh(t)
    assert t.is_archived is True
    assert t.archive_reason == "past_due"
    assert t.archived_at is not None


def test_grace_boundary(db):
    """2 days past closing survives a 3-day grace; 4 days does not."""
    from app.services.tender_archive_service import archive_candidates_query
    young = _mk(db, closing_days_ago=2)
    old = _mk(db, closing_days_ago=4)
    ids = {t.id for t in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert young.id not in ids
    assert old.id in ids


def test_tender_with_no_closing_date_is_never_archived(db):
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=None)
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_assigned_tender_is_not_archived(db):
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10, assigned_to=1)
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_advanced_workflow_status_is_not_archived(db):
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10, workflow_status="proposal_draft")
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_tender_with_costing_is_not_archived(db):
    from app.models.cost_breakdown import CostBreakdown
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10)
    db.add(CostBreakdown(tender_id=t.id))
    db.commit()
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_tender_with_workspace_is_not_archived(db):
    from app.models.workspace import WorkspaceConfig
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10)
    db.add(WorkspaceConfig(tender_id=t.id))
    db.commit()
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_tender_with_only_documents_IS_archived(db):
    """Documents are auto-captured, not evidence of human work."""
    from app.models.tender import TenderDocument
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10)
    db.add(TenderDocument(tender_id=t.id, file_name="nit.pdf", file_path="tenders/x/nit.pdf"))
    db.commit()
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id in ids


def test_purge_boundary(db):
    """6 days in archive survives a 7-day purge; 8 days does not."""
    from app.services.tender_archive_service import purge_candidates_query
    young = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=6), archive_reason="past_due")
    old = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=8), archive_reason="past_due")
    ids = {t.id for t in purge_candidates_query(db, now=NOW, purge_days=7).all()}
    assert young.id not in ids
    assert old.id in ids


def test_auto_discard_rows_are_never_purged(db):
    from app.services.tender_archive_service import purge_candidates_query
    t = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=90), archive_reason="auto_discard")
    ids = {x.id for x in purge_candidates_query(db, now=NOW, purge_days=7).all()}
    assert t.id not in ids


def test_legacy_archived_rows_are_never_purged(db):
    """Rows archived before this feature have archived_at=NULL, reason=NULL."""
    from app.services.tender_archive_service import purge_candidates_query
    t = _mk(db, is_archived=True, archived_at=None, archive_reason=None)
    ids = {x.id for x in purge_candidates_query(db, now=NOW, purge_days=7).all()}
    assert t.id not in ids


def test_run_purge_pass_deletes_the_row(db):
    from app.services.tender_archive_service import run_purge_pass
    t = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=8), archive_reason="past_due")
    tid = t.id
    assert run_purge_pass(db, now=NOW, purge_days=7, batch_size=500) >= 1
    assert db.query(Tender).filter(Tender.id == tid).first() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'archive_candidates_query'`

- [ ] **Step 3: Implement the two passes**

Append to `drpl-backend/app/services/tender_archive_service.py`:

```python
from datetime import timedelta

from sqlalchemy import and_, not_, or_


def _has_work_artifacts_clause(db: Session):
    """A single OR-of-EXISTS over every work-artifact table.

    Built with raw EXISTS subqueries rather than ORM relationships because these
    are logical FKs — the models declare no relationship to Tender.
    """
    clauses = []
    for table in WORK_ARTIFACT_TABLES:
        try:
            db.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        except Exception:  # noqa: BLE001 — table absent on this deployment
            db.rollback()
            continue
        clauses.append(
            text(f"EXISTS (SELECT 1 FROM {table} w WHERE w.tender_id = tenders.id)")
        )
    if not clauses:
        return None
    return or_(*clauses)


def archive_candidates_query(db: Session, *, now: datetime, grace_days: int):
    """Tenders eligible for archiving: past due, untouched, not already archived."""
    cutoff = now - timedelta(days=grace_days)
    q = (db.query(Tender)
         .filter(Tender.closing_date.isnot(None))
         .filter(Tender.closing_date < cutoff)
         .filter(or_(Tender.is_archived == False, Tender.is_archived.is_(None)))  # noqa: E712
         .filter(Tender.workflow_status == "new")
         .filter(Tender.assigned_to.is_(None)))
    work = _has_work_artifacts_clause(db)
    if work is not None:
        q = q.filter(not_(work))
    return q.order_by(Tender.id)


def purge_candidates_query(db: Session, *, now: datetime, purge_days: int):
    """Archived-as-past-due tenders whose purge clock has expired.

    INVARIANT: the archive_reason filter is what protects auto-discarded and
    legacy (NULL-reason) archived rows from ever being deleted.
    """
    cutoff = now - timedelta(days=purge_days)
    return (db.query(Tender)
            .filter(Tender.archive_reason == "past_due")
            .filter(Tender.archived_at.isnot(None))
            .filter(Tender.archived_at < cutoff)
            .order_by(Tender.id))


def run_archive_pass(db: Session, *, now: datetime, grace_days: int, batch_size: int) -> int:
    rows = archive_candidates_query(db, now=now, grace_days=grace_days).limit(batch_size).all()
    for t in rows:
        t.is_archived = True
        t.archived_at = now
        t.archive_reason = "past_due"
    db.commit()
    log.info("archive_sweep: archived %d past-due untouched tenders (grace=%dd)",
             len(rows), grace_days)
    return len(rows)


def run_purge_pass(db: Session, *, now: datetime, purge_days: int, batch_size: int) -> int:
    ids = [t.id for t in
           purge_candidates_query(db, now=now, purge_days=purge_days).limit(batch_size).all()]
    if not ids:
        return 0
    deleted = delete_tenders_deep(db, ids)
    log.info("archive_sweep: purged %d tenders archived >%dd ago", deleted, purge_days)
    return deleted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/tender_archive_service.py drpl-backend/tests/test_tender_archive_service.py
git commit -m "feat(archive): archive + purge passes with work-artifact guard"
```

---

## Task 5: The scheduled job

**Files:**
- Modify: `drpl-backend/app/worker/scheduled_tasks.py:196`
- Modify: `drpl-backend/app/services/seed_scheduled_jobs.py:90`
- Test: `drpl-backend/tests/test_tender_archive_service.py`

**Interfaces:**
- Produces: `app.worker.scheduled_tasks.run_archive_sweep()` → `dict` with keys `archived`, `purged`, `rescheduled`, and optionally `reason`. RQ job id `drpl-archive-sweep`.

- [ ] **Step 1: Write the failing test**

Append to `drpl-backend/tests/test_tender_archive_service.py`:

```python
def test_sweep_is_a_noop_when_disabled(db, monkeypatch):
    """Ships off — the job must do nothing until an admin enables it."""
    import app.worker.scheduled_tasks as st
    monkeypatch.setattr(st, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    # No Redis in tests: _reschedule_archive_sweep() returns immediately.
    monkeypatch.setattr(st, "get_queue", lambda: None)

    t = _mk(db, closing_days_ago=30)
    out = st.run_archive_sweep()

    assert out["archived"] == 0
    assert out["purged"] == 0
    assert out["reason"] == "disabled"
    db.refresh(t)
    assert t.is_archived in (False, None)


def test_sweep_archives_when_enabled(db, monkeypatch):
    import app.worker.scheduled_tasks as st
    from app.services.auto_scoring_settings import set_scoring_setting
    monkeypatch.setattr(st, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(st, "get_queue", lambda: None)
    set_scoring_setting(db, "archive_sweep_enabled", True)
    try:
        t = _mk(db, closing_days_ago=30)
        out = st.run_archive_sweep()
        assert out["archived"] >= 1
        assert "reason" not in out
        db.refresh(t)
        assert t.is_archived is True
        assert t.archive_reason == "past_due"
    finally:
        set_scoring_setting(db, "archive_sweep_enabled", False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -k sweep -v`
Expected: FAIL — `AttributeError: module 'app.worker.scheduled_tasks' has no attribute 'run_archive_sweep'`

- [ ] **Step 3: Add the job**

Append to `drpl-backend/app/worker/scheduled_tasks.py`, after `_reschedule_discard_cleanup()` (`:228`):

```python
# ── Archive sweep (past-due untouched tenders) ─────────────────────────────


def run_archive_sweep() -> dict:
    """Archive past-due untouched tenders, then purge ones archived long enough.

    Ships disabled (``archive_sweep_enabled=False``). The flag is also the kill
    switch — flipping it in Platform Settings stops the sweep with no deploy.
    """
    from app.services.auto_scoring_settings import get_scoring_settings
    from app.services import tender_archive_service as tas

    archived = purged = 0
    db = SessionLocal()
    try:
        cfg = get_scoring_settings(db)
        # Disabled is a no-op, NOT an early return: the reschedule below has to
        # run either way, or the self-rescheduling loop dies while disabled and
        # flipping the flag on would need a redeploy to restart it.
        disabled = not cfg["archive_sweep_enabled"]
        if not disabled:
            now = datetime.now(timezone.utc)
            batch = cfg["archive_sweep_batch_size"]
            try:
                archived = tas.run_archive_pass(
                    db, now=now, grace_days=cfg["archive_grace_days"], batch_size=batch)
            except Exception as e:  # noqa: BLE001
                log.warning("archive_sweep: archive pass failed: %s", e)
                db.rollback()
            try:
                purged = tas.run_purge_pass(
                    db, now=now, purge_days=cfg["archive_purge_days"], batch_size=batch)
            except Exception as e:  # noqa: BLE001
                log.warning("archive_sweep: purge pass failed: %s", e)
                db.rollback()
    finally:
        db.close()

    _reschedule_archive_sweep()
    out = {"archived": archived, "purged": purged, "rescheduled": True}
    if disabled:
        out["reason"] = "disabled"
    return out


def _reschedule_archive_sweep() -> None:
    q = get_queue()
    if q is None:
        return
    db = SessionLocal()
    try:
        from app.services.auto_scoring_settings import get_scoring_settings
        hours = get_scoring_settings(db)["archive_sweep_interval_hours"]
    finally:
        db.close()
    try:
        q.enqueue_in(timedelta(hours=hours), "app.worker.scheduled_tasks.run_archive_sweep",
                     job_id="drpl-archive-sweep", result_ttl=86400)
    except Exception as e:  # noqa: BLE001
        log.warning("scheduled_tasks: could not reschedule archive sweep: %s", e)
```

Note the sweep reschedules itself **even when disabled is later flipped on** — but when disabled it returns before rescheduling, so the loop stops. Seeding (Step 5) restarts it.

- [ ] **Step 4: Stamp archived_at in the existing discard cleanup**

So future auto-discard rows are distinguishable. In `_run_discard_cleanup` (`drpl-backend/app/worker/scheduled_tasks.py:191-192`), replace the loop:

```python
    now = datetime.now(timezone.utc)
    for t in rows:
        t.is_archived = True
        t.archived_at = now
        t.archive_reason = "auto_discard"   # never purged by the archive sweep
```

- [ ] **Step 5: Seed the job**

In `drpl-backend/app/services/seed_scheduled_jobs.py`, before the final `return out` (`:90`):

```python
    # Archive sweep: first run in 10 minutes, then every archive_sweep_interval_hours.
    # Seeded regardless of the enabled flag — the job itself checks it and exits
    # early, so flipping the setting on takes effect without a redeploy.
    if not _job_already_pending(q, "drpl-archive-sweep"):
        try:
            q.enqueue_in(timedelta(minutes=10), "app.worker.scheduled_tasks.run_archive_sweep",
                         job_id="drpl-archive-sweep", result_ttl=86400)
            out["scheduled"].append({"job": "drpl-archive-sweep", "in": "10 minutes"})
            log.info("seed_scheduled_jobs: queued first archive sweep in 10 minutes")
        except Exception as e:  # noqa: BLE001
            log.warning("seed_scheduled_jobs: could not seed archive sweep: %s", e)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd drpl-backend && pytest tests/test_tender_archive_service.py -v`
Expected: PASS (all tests, including the disabled no-op)

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/worker/scheduled_tasks.py drpl-backend/app/services/seed_scheduled_jobs.py drpl-backend/tests/test_tender_archive_service.py
git commit -m "feat(archive): self-rescheduling archive sweep job, ships disabled"
```

---

## Task 6: Dry-run script

Run this against production **before** enabling the flag.

**Files:**
- Create: `drpl-backend/scripts/archive_sweep_dryrun.py`

**Interfaces:**
- Consumes: `archive_candidates_query`, `purge_candidates_query` from Task 4.
- Produces: nothing importable. Read-only CLI.

- [ ] **Step 1: Write the script**

Create `drpl-backend/scripts/archive_sweep_dryrun.py`:

```python
"""Report what the archive sweep WOULD do. Writes nothing.

Run this against production before enabling archive_sweep_enabled:

    cd drpl-backend && python scripts/archive_sweep_dryrun.py
"""
from datetime import datetime, timezone

from app.core.database import SessionLocal
from app.services.auto_scoring_settings import get_scoring_settings
from app.services.tender_archive_service import (
    archive_candidates_query,
    purge_candidates_query,
)


def main() -> None:
    db = SessionLocal()
    try:
        cfg = get_scoring_settings(db)
        now = datetime.now(timezone.utc)
        grace, purge = cfg["archive_grace_days"], cfg["archive_purge_days"]

        arch_q = archive_candidates_query(db, now=now, grace_days=grace)
        purge_q = purge_candidates_query(db, now=now, purge_days=purge)
        arch_n, purge_n = arch_q.count(), purge_q.count()

        print(f"enabled          : {cfg['archive_sweep_enabled']}")
        print(f"grace / purge    : {grace}d / {purge}d")
        print(f"WOULD ARCHIVE    : {arch_n}")
        for t in arch_q.limit(20).all():
            print(f"   #{t.id:<7} closed={t.closing_date}  {(t.title or '')[:60]}")
        print(f"WOULD DELETE     : {purge_n}")
        for t in purge_q.limit(20).all():
            print(f"   #{t.id:<7} archived={t.archived_at}  {(t.title or '')[:60]}")
        if arch_n > 20 or purge_n > 20:
            print("(showing first 20 of each)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it runs read-only against the test DB**

Run: `cd drpl-backend && DATABASE_URL="sqlite:///./drpl_local.db" python scripts/archive_sweep_dryrun.py`
Expected: prints the six report lines with counts (likely 0) and exits 0. **Do not** run it without the `DATABASE_URL` override during development — the default `.env` points at live Neon.

- [ ] **Step 3: Commit**

```bash
git add drpl-backend/scripts/archive_sweep_dryrun.py
git commit -m "feat(archive): read-only dry-run script for the sweep"
```

---

## Task 7: Counts exclude archived

**Files:**
- Modify: `drpl-backend/app/api/routes/tenders.py:144-195`
- Modify: `drpl-backend/app/api/routes/admin_dashboard.py:54-67`
- Test: `drpl-backend/tests/test_dashboard_stats.py`

**Interfaces:**
- Produces: no new symbols. `GET /tenders/stats` and the admin overview stop counting archived rows.

- [ ] **Step 1: Write the failing test**

Append to `drpl-backend/tests/test_dashboard_stats.py`, matching that file's existing `_mk` / `_client` helpers:

```python
def test_stats_exclude_archived_tenders():
    db = _session()
    client = _client(db)
    _mk(db, score=0.9, status="open", segment="to_bid")
    archived = _mk(db, score=0.9, status="open", segment="to_bid")
    archived.is_archived = True
    archived.archive_reason = "past_due"
    db.commit()

    body = client.get("/api/tenders/stats").json()
    assert body["total_tenders"] == 1
    assert body["open_tenders"] == 1
    assert body["promising_count"] == 1
    assert body["to_bid_count"] == 1
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_dashboard_stats.py -k archived -v`
Expected: FAIL — `assert 2 == 1`

- [ ] **Step 3: Add the filter to every stats count**

In `drpl-backend/app/api/routes/tenders.py`, `tender_stats()`. Add a shared predicate right after the docstring (`:149`) and apply it to all ten counts:

```python
    # Archived tenders are out of the pipeline — they must not inflate any count.
    ACTIVE = Tender.is_archived == False  # noqa: E712

    total = db.query(Tender).filter(ACTIVE).count()
    by_portal = {}
    for portal in ["ireps", "gem", "tendertiger", "bidassist"]:
        by_portal[portal] = db.query(Tender).filter(ACTIVE, Tender.portal == portal).count()

    open_count = db.query(Tender).filter(ACTIVE, Tender.status == "open").count()
    analyzed_count = db.query(Tender).filter(ACTIVE, Tender.ai_category != None).count()  # noqa: E711

    avg_relevance = db.query(func.avg(Tender.ai_relevance_score)).filter(
        ACTIVE, Tender.ai_relevance_score != None,  # noqa: E711
    ).scalar()
```

Then the four remaining counts:

```python
    promising_count = db.query(Tender).filter(
        ACTIVE, Tender.ai_relevance_score >= DASHBOARD_PROMISING_THRESHOLD,
    ).count()
    promising_open_count = db.query(Tender).filter(
        ACTIVE,
        Tender.ai_relevance_score >= DASHBOARD_PROMISING_THRESHOLD,
        Tender.status == "open",
    ).count()
    closing_soon_count = db.query(Tender).filter(
        ACTIVE,
        Tender.ai_relevance_score >= DASHBOARD_PROMISING_THRESHOLD,
        Tender.status == "open",
        Tender.closing_date != None,  # noqa: E711
        Tender.closing_date >= now,
        Tender.closing_date <= soon,
    ).count()
    to_bid_count = db.query(Tender).filter(ACTIVE, Tender.segment == "to_bid").count()
    with_costing_count = (db.query(CostBreakdown.tender_id)
                          .join(Tender, Tender.id == CostBreakdown.tender_id)
                          .filter(ACTIVE)
                          .distinct().count())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && pytest tests/test_dashboard_stats.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Apply the same filter to the admin dashboard**

In `drpl-backend/app/api/routes/admin_dashboard.py:54-67`, add `.filter(Tender.is_archived == False)  # noqa: E712` to both the `total` and `open` tender counts.

- [ ] **Step 6: Confirm view-counts still passes untouched**

Run: `cd drpl-backend && pytest tests/test_view_counts_route.py -v`
Expected: PASS — `_apply_tender_filters` already excluded archived, so this endpoint needed no change.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/api/routes/tenders.py drpl-backend/app/api/routes/admin_dashboard.py drpl-backend/tests/test_dashboard_stats.py
git commit -m "fix(archive): exclude archived tenders from all dashboard counts"
```

---

## Task 8: Archive API

**Files:**
- Modify: `drpl-backend/app/api/routes/tenders.py`
- Modify: `drpl-backend/app/schemas/__init__.py:128`
- Test: `drpl-backend/tests/test_archive_routes.py`

**Interfaces:**
- Produces:
  - `GET /api/tenders/archive?limit&offset&reason` → `{"items": [...], "total": n}`, each item a `TenderResponse` plus `archived_at`, `archive_reason`, `purge_at`.
  - `POST /api/tenders/{id}/restore` → `{"id": n, "restored": true}`
  - `POST /api/tenders/archive/purge-now` body `{"tender_ids": [...]}` → `{"deleted": n}`

- [ ] **Step 1: Write the failing tests**

Create `drpl-backend/tests/test_archive_routes.py`:

```python
"""Archive listing, restore, and manual purge endpoints."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.core.auth import get_current_user, require_admin
from app.main import app
from app.models.tender import Tender
from app.models import platform_setting as _ps  # noqa: F401

NOW = datetime.now(timezone.utc)
_n = 0


def _session():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: object()
    app.dependency_overrides[require_admin] = lambda: object()
    return TestClient(app)


def _mk(db, **kw):
    global _n
    _n += 1
    t = Tender(portal="ireps", tender_id=f"AR{_n}", title=f"arch {_n}", **kw)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_archive_list_returns_archived_with_purge_at():
    db = _session()
    client = _client(db)
    archived_at = NOW - timedelta(days=2)
    _mk(db, is_archived=True, archived_at=archived_at, archive_reason="past_due")
    _mk(db, is_archived=False)   # active — must not appear

    body = client.get("/api/tenders/archive").json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["archive_reason"] == "past_due"
    assert item["purge_at"] is not None
    app.dependency_overrides.clear()


def test_archive_list_purge_at_is_null_for_non_past_due():
    db = _session()
    client = _client(db)
    _mk(db, is_archived=True, archived_at=NOW, archive_reason="auto_discard")
    item = client.get("/api/tenders/archive").json()["items"][0]
    assert item["purge_at"] is None
    app.dependency_overrides.clear()


def test_restore_clears_archive_fields_and_marks_overridden():
    db = _session()
    client = _client(db)
    t = _mk(db, is_archived=True, archived_at=NOW, archive_reason="past_due")

    assert client.post(f"/api/tenders/{t.id}/restore").status_code == 200
    db.refresh(t)
    assert t.is_archived is False
    assert t.archived_at is None
    assert t.archive_reason is None
    assert t.segment_overridden is True   # exempts it from being re-archived
    app.dependency_overrides.clear()


def test_restore_404s_for_unknown_tender():
    db = _session()
    client = _client(db)
    assert client.post("/api/tenders/999999/restore").status_code == 404
    app.dependency_overrides.clear()


def test_purge_now_deletes():
    db = _session()
    client = _client(db)
    t = _mk(db, is_archived=True, archived_at=NOW, archive_reason="past_due")
    r = client.post("/api/tenders/archive/purge-now", json={"tender_ids": [t.id]})
    assert r.status_code == 200
    assert r.json()["deleted"] == 1
    assert db.query(Tender).filter(Tender.id == t.id).first() is None
    app.dependency_overrides.clear()


def test_purge_now_rejects_oversized_batch():
    db = _session()
    client = _client(db)
    r = client.post("/api/tenders/archive/purge-now",
                    json={"tender_ids": list(range(1, 60))})
    assert r.status_code == 400
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd drpl-backend && pytest tests/test_archive_routes.py -v`
Expected: FAIL with 404s — the routes don't exist.

- [ ] **Step 3: Add the archive fields to the response schema**

In `drpl-backend/app/schemas/__init__.py`, in `TenderResponse` after `below_threshold` (`:148`):

```python
    is_archived: bool = False
    archived_at: Optional[datetime] = None
    archive_reason: Optional[str] = None
```

- [ ] **Step 4: Add the three endpoints**

In `drpl-backend/app/api/routes/tenders.py`, after `tender_view_counts` (`:220`).

**Route ordering matters:** these must be declared before any `@router.get("/{tender_id}")` route, otherwise FastAPI matches `archive` as a tender id.

```python
@router.get("/archive")
def list_archived_tenders(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    reason: Optional[str] = Query(None, description="Filter by archive_reason"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Archived tenders, newest first, with a purge countdown for past-due rows."""
    from app.services.auto_scoring_settings import get_scoring_settings

    q = db.query(Tender).filter(Tender.is_archived == True)  # noqa: E712
    if reason:
        q = q.filter(Tender.archive_reason == reason)
    total = q.count()
    rows = (q.order_by(Tender.archived_at.desc().nullslast(), Tender.id.desc())
            .offset(offset).limit(limit).all())

    purge_days = get_scoring_settings(db)["archive_purge_days"]
    items = []
    for t in rows:
        # Only past-due rows carry a purge clock; everything else lives forever.
        purge_at = None
        if t.archive_reason == "past_due" and t.archived_at is not None:
            purge_at = (t.archived_at + timedelta(days=purge_days)).isoformat()
        items.append({
            "id": t.id, "portal": t.portal, "tender_id": t.tender_id,
            "title": t.title, "department": t.department,
            "estimated_value": t.estimated_value,
            "closing_date": t.closing_date.isoformat() if t.closing_date else None,
            "ai_relevance_score": t.ai_relevance_score,
            "archived_at": t.archived_at.isoformat() if t.archived_at else None,
            "archive_reason": t.archive_reason,
            "purge_at": purge_at,
        })
    return {"items": items, "total": total}


@router.post("/{tender_id}/restore")
def restore_tender(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Pull a tender back out of the archive.

    Sets segment_overridden so the sweep does not immediately re-archive it —
    reusing the existing "a human touched this" marker.
    """
    t = db.query(Tender).filter(Tender.id == tender_id).first()
    if t is None:
        raise HTTPException(status_code=404, detail="Tender not found")
    t.is_archived = False
    t.archived_at = None
    t.archive_reason = None
    t.segment_overridden = True
    db.commit()
    return {"id": t.id, "restored": True}


@router.post("/archive/purge-now")
def purge_archived_now(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Permanently delete archived tenders immediately. Body: { tender_ids: [...] }"""
    from app.services.tender_archive_service import delete_tenders_deep

    tender_ids = body.get("tender_ids", [])
    if not tender_ids or not isinstance(tender_ids, list):
        raise HTTPException(status_code=400, detail="tender_ids is required and must be a list")
    if len(tender_ids) > 50:
        raise HTTPException(status_code=400, detail="Cannot purge more than 50 tenders at once")
    return {"deleted": delete_tenders_deep(db, tender_ids)}
```

Confirm `require_admin` and `timedelta` are already imported at the top of the file; `require_admin` is used by `bulk_archive_tenders` (`:103`) and `timedelta` by `tender_stats` (`:165`), so both should be present.

- [ ] **Step 5: Stamp the reason in bulk-archive**

In `bulk_archive_tenders` (`drpl-backend/app/api/routes/tenders.py:117-118`):

```python
    now = datetime.now(timezone.utc)
    for t in updated:
        t.is_archived = True
        t.archived_at = now
        t.archive_reason = "manual"   # never auto-purged
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd drpl-backend && pytest tests/test_archive_routes.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Run the whole backend suite**

Run: `cd drpl-backend && pytest tests/ -q`
Expected: no new failures. Route-ordering mistakes surface here as unrelated tender-detail tests failing.

- [ ] **Step 8: Commit**

```bash
git add drpl-backend/app/api/routes/tenders.py drpl-backend/app/schemas/__init__.py drpl-backend/tests/test_archive_routes.py
git commit -m "feat(archive): archive list, restore, and purge-now endpoints"
```

---

## Task 9: Frontend archive page

**Files:**
- Modify: `drpl-frontend/src/types/tender.ts`
- Modify: `drpl-frontend/src/lib/api.ts`
- Create: `drpl-frontend/src/pages/ArchivePage.tsx`
- Modify: `drpl-frontend/src/router.tsx:95`
- Modify: `drpl-frontend/src/components/layout/SidebarNav.tsx:17`

**Interfaces:**
- Consumes: the three endpoints from Task 8.
- Produces: route `/archive`; `getArchivedTenders`, `restoreTender`, `purgeTenders` in `api.ts`.

- [ ] **Step 1: Add the type**

In `drpl-frontend/src/types/tender.ts`, after the `Tender` interface:

```typescript
export interface ArchivedTender {
  id: number;
  portal: string;
  tender_id: string;
  title: string;
  department: string | null;
  estimated_value: number | null;
  closing_date: string | null;
  ai_relevance_score: number | null;
  archived_at: string | null;
  archive_reason: 'past_due' | 'auto_discard' | 'manual' | null;
  /** When this row will be hard-deleted. Null unless archive_reason is past_due. */
  purge_at: string | null;
}
```

- [ ] **Step 2: Add the API helpers**

In `drpl-frontend/src/lib/api.ts`, after `getTenderViewCounts`:

```typescript
export async function getArchivedTenders(
  params: { limit?: number; offset?: number; reason?: string } = {},
): Promise<{ items: ArchivedTender[]; total: number }> {
  const { data } = await api.get('/api/tenders/archive', { params });
  return data;
}

export async function restoreTender(id: number): Promise<void> {
  await api.post(`/api/tenders/${id}/restore`);
}

export async function purgeTenders(tenderIds: number[]): Promise<{ deleted: number }> {
  const { data } = await api.post('/api/tenders/archive/purge-now', { tender_ids: tenderIds });
  return data;
}
```

Add `ArchivedTender` to the existing `@/types/tender` import at the top of the file.

- [ ] **Step 3: Create the page**

Create `drpl-frontend/src/pages/ArchivePage.tsx`:

```tsx
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Archive, RotateCcw, Trash2 } from 'lucide-react'
import { getArchivedTenders, restoreTender, purgeTenders } from '@/lib/api'

const PAGE_SIZE = 50

function daysUntil(iso: string | null): number | null {
  if (!iso) return null
  const ms = new Date(iso).getTime() - Date.now()
  return Math.max(0, Math.ceil(ms / 86_400_000))
}

export default function ArchivePage() {
  const [offset, setOffset] = useState(0)
  const qc = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ['archived-tenders', offset],
    queryFn: () => getArchivedTenders({ limit: PAGE_SIZE, offset }),
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['archived-tenders'] })
    qc.invalidateQueries({ queryKey: ['tender-stats'] })
    qc.invalidateQueries({ queryKey: ['tender-view-counts'] })
  }
  const restore = useMutation({ mutationFn: restoreTender, onSuccess: invalidate })
  const purge = useMutation({
    mutationFn: (id: number) => purgeTenders([id]),
    onSuccess: invalidate,
  })

  const items = data?.items ?? []

  return (
    <div className="p-6 space-y-4">
      <header className="flex items-center gap-3">
        <Archive className="h-5 w-5 text-muted-foreground" />
        <div>
          <h1 className="text-xl font-semibold">Archive</h1>
          <p className="text-sm text-muted-foreground">
            {data?.total ?? 0} archived. Past-due tenders are deleted permanently
            once their countdown ends; restore one to keep it.
          </p>
        </div>
      </header>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {!isLoading && items.length === 0 && (
        <p className="text-sm text-muted-foreground">Nothing archived.</p>
      )}

      <ul className="space-y-2">
        {items.map((t) => {
          const days = daysUntil(t.purge_at)
          return (
            <li key={t.id} className="rounded-lg border p-3 flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <Link to={`/tenders/${t.id}`} className="font-medium hover:underline">
                  {t.title}
                </Link>
                <p className="text-xs text-muted-foreground">
                  {t.portal.toUpperCase()} · {t.tender_id}
                  {t.closing_date && ` · closed ${new Date(t.closing_date).toLocaleDateString()}`}
                </p>
                {t.archive_reason === 'past_due' ? (
                  <span className="mt-1 inline-block rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800">
                    {days === 0 ? 'Deletes today' : `Deletes in ${days} day${days === 1 ? '' : 's'}`}
                  </span>
                ) : (
                  <span className="mt-1 inline-block rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                    {t.archive_reason === 'auto_discard' ? 'Archived — low score' : 'Archived manually'}
                  </span>
                )}
              </div>
              <div className="flex shrink-0 gap-2">
                <button
                  onClick={() => restore.mutate(t.id)}
                  disabled={restore.isPending}
                  className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-muted"
                >
                  <RotateCcw className="h-3 w-3" /> Restore
                </button>
                <button
                  onClick={() => purge.mutate(t.id)}
                  disabled={purge.isPending}
                  className="inline-flex items-center gap-1 rounded border border-destructive/40 px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
                >
                  <Trash2 className="h-3 w-3" /> Delete now
                </button>
              </div>
            </li>
          )
        })}
      </ul>

      {(data?.total ?? 0) > PAGE_SIZE && (
        <div className="flex items-center gap-2 text-sm">
          <button
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={offset === 0}
            className="rounded border px-2 py-1 disabled:opacity-40"
          >
            Previous
          </button>
          <span className="text-muted-foreground">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, data!.total)} of {data!.total}
          </span>
          <button
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={offset + PAGE_SIZE >= (data?.total ?? 0)}
            className="rounded border px-2 py-1 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      )}
    </div>
  )
}
```

The "Delete now" button calls the API directly with no `window.confirm` — a browser modal would block the page. If a confirmation step is wanted, use an in-app dialog component in a follow-up.

- [ ] **Step 4: Register the route**

In `drpl-frontend/src/router.tsx`, add the lazy import alongside the other page imports, then add this child route after the `tenders/view/:view` line (`:95`):

```tsx
      { path: 'archive', element: <ArchivePage /> },
```

Match the file's existing import style (direct import or `lazy()`) — check the top of `router.tsx` and follow it.

- [ ] **Step 5: Add the sidebar link**

In `drpl-frontend/src/components/layout/SidebarNav.tsx`, add `Archive` to the `lucide-react` import list, then add to `mainNavTop` after the Tenders entry (`:17`):

```tsx
  { to: '/archive', icon: Archive, label: 'Archive' },
```

- [ ] **Step 6: Verify the build**

Run: `cd drpl-frontend && npm run build`
Expected: `tsc -b && vite build` completes with no type errors. A missing `ArchivedTender` import in `api.ts` surfaces here.

- [ ] **Step 7: Commit**

```bash
git add drpl-frontend/src/types/tender.ts drpl-frontend/src/lib/api.ts drpl-frontend/src/pages/ArchivePage.tsx drpl-frontend/src/router.tsx drpl-frontend/src/components/layout/SidebarNav.tsx
git commit -m "feat(archive): archive page with restore and purge-now"
```

---

## Task 10: Documentation + final verification

**Files:**
- Modify: `drpl-backend/CONSTITUTION.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Record the child-table invariant**

Add to `drpl-backend/CONSTITUTION.md`, in the schema-discipline section:

```markdown
### Tender child tables

Adding a table with a `tender_id` column requires adding it to `CHILD_TABLES`
in `app/services/tender_archive_service.py`, or the archive purge will orphan
its rows. If the table represents human work on a tender, also add it to
`WORK_ARTIFACT_TABLES` — otherwise the sweep will archive tenders someone was
actively working on.
```

- [ ] **Step 2: Document the sweep in CLAUDE.md**

Add to the "Architecture notes" section of `CLAUDE.md`, after the storage subsection:

```markdown
### Tender archive sweep

`drpl-archive-sweep` (`app/worker/scheduled_tasks.py`) archives past-due
untouched tenders after a 3-day grace, then hard-deletes them 7 days later via
`tender_archive_service.delete_tenders_deep()`. It ships **disabled**
(`archive_sweep_enabled=False`); run `scripts/archive_sweep_dryrun.py` before
enabling. Only `archive_reason='past_due'` rows are ever purged — auto-discard
and manually archived rows live indefinitely.
```

- [ ] **Step 3: Run the full backend suite**

Run: `cd drpl-backend && pytest tests/ -q`
Expected: all pass. Record the actual pass/fail counts — do not claim success without reading the output.

- [ ] **Step 4: Run the frontend build**

Run: `cd drpl-frontend && npm run build`
Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/CONSTITUTION.md CLAUDE.md
git commit -m "docs(archive): document the sweep and the child-table invariant"
```

---

## Post-implementation: enabling in production

Not part of the plan's tasks — this is the operator runbook, to be done deliberately after review.

**The authoritative runbook now lives in [CLAUDE.md](../../../CLAUDE.md), under "Tender archive sweep" → "Operator runbook: enabling the sweep in production".** It was moved there because an operator reads CLAUDE.md, not a completed implementation plan, and because the enable switch, the `segment_overridden` gap, the drain rate, and the `guard_ok` health signal all need to be found together. Follow that version; the outline below is kept only for historical context.

1. Deploy. The sweep is seeded but disabled, so nothing moves.
2. Run `python scripts/archive_sweep_dryrun.py` against production. Read the
   "WOULD ARCHIVE" and "WOULD DELETE" counts and the sample rows.
3. If the counts look right, enable the sweep in **Admin → Tender Scoring Agent
   → Archive & Purge Sweep**. The next sweep (within 6 hours) archives the
   first batch of 500.
4. Watch counts on the dashboard drop, and the Archive page fill.
5. The first purge happens 7 days after the first archive run — there is a
   week's window to reconsider before anything is deleted.
