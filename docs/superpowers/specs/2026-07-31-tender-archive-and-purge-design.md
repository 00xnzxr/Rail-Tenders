# Tender Archive & Purge — Design

**Date:** 2026-07-31
**Status:** Approved (design), pending implementation plan

## Problem

Tenders whose closing date has passed and that nobody worked on stay in the
active list forever. They inflate every count on the dashboard and the tender
funnel, so the numbers stop meaning "work available to me".

## Goal

Past-due, untouched tenders move automatically to an **Archive**, disappear from
all active counts, and are hard-deleted 7 days later. Tenders anyone actually
worked on are never auto-archived.

## Decisions (agreed with user)

| Question | Decision |
|---|---|
| What is "not filled"? | **Untouched only** — no work artifacts of any kind |
| After 7 days in archive | **Hard delete** row + children + files |
| Archive UI | **Separate `/archive` page + sidebar link** |
| Grace after closing | **3 days**, then archive; 7 more in archive |
| Existing `is_archived` rows | **Never auto-purged** — distinguished by `archive_reason` |
| Counts | **Exclude archived everywhere**, including admin dashboard |

## Non-goals

- Restoring a purged tender. Hard delete is final; a re-scrape may re-import it
  as a new row. Accepted, given the 10-day total window.
- Archiving tenders with no `closing_date`. They are never touched by this job.
- Fixing `workflow_status` never being set to `submitted` programmatically.

---

## 1. Data model

Two new nullable columns on `Tender` (`app/models/tender.py`):

| Column | Type | Purpose |
|---|---|---|
| `archived_at` | `DateTime(timezone=True)`, indexed | When it entered the archive. The purge clock. |
| `archive_reason` | `String(30)` | `past_due` \| `auto_discard` \| `manual` |

`is_archived` keeps its current meaning: the boolean that hides a row from
active lists. No change to `_apply_tender_filters`.

**The safety property:** only `archive_reason == 'past_due'` is ever
auto-purged. Rows archived before this feature shipped have
`archived_at IS NULL` and `archive_reason IS NULL`, so the purge query cannot
match them. The existing `drpl-discard-cleanup` job and `bulk-archive` endpoint
are updated to stamp `archived_at` + their own reason, so future data is honest,
but neither reason is purgeable.

Per repo convention (CLAUDE.md, "schema drift discipline"): an Alembic revision
on head `7f6ae5991d3c` **and** matching entries in `_apply_schema_drift_fixes()`
(Postgres) and `_add_missing_columns()` (SQLite) so existing deployments
self-heal.

---

## 2. The sweep job

New self-rescheduling RQ job `drpl-archive-sweep` in
`app/worker/scheduled_tasks.py`, following the existing pattern exactly:
a pure `_run_*(db, now)` body, a session-owning wrapper that checks the enabled
flag, and a `_reschedule_*()` helper. Seeded in `seed_scheduled_jobs.py`,
primed at startup from `main.py`. Default interval 6 hours.

### Pass A — archive

Archive a tender when **all** hold:

```
closing_date IS NOT NULL
AND closing_date < now - archive_grace_days       # default 3
AND is_archived = False
AND workflow_status = 'new'
AND assigned_to IS NULL
AND NOT EXISTS any work artifact (below)
```

Sets `is_archived=True`, `archived_at=now`, `archive_reason='past_due'`.

**Work-artifact tables** — a row in any of these means the tender was worked on
and must not be auto-archived. These are logical FKs (`tender_id` integer
columns, no DB-level constraint), checked as `NOT EXISTS` subqueries, never
per-row loops:

- `cost_breakdowns`
- `proposal_sessions`
- `checklist_items`
- `workspace_configs`, `document_workspaces`
- `document_extraction_results`, `tender_analysis_summaries`
- `costing_templates`, `boq_items`
- `generated_documents`

Rationale for the breadth: `workflow_status` alone is unreliable — nothing in
the codebase advances it past `new` except a manual PATCH, so a tender with a
full analyzed document set can still read `new`. `tender_documents` is
deliberately **excluded** from this list: documents are auto-captured by the
extension and the backend NIT fetcher without any human intent, so their
presence is not evidence of work.

### Pass B — purge

Hard-delete tenders where:

```
archive_reason = 'past_due'
AND archived_at IS NOT NULL
AND archived_at < now - archive_purge_days        # default 7
```

For each batch, in one transaction:

1. Delete stored files for its `TenderDocument` rows via
   `app/services/storage_service.py` (never touch `uploads/` directly).
2. Delete child rows in every table carrying a `tender_id` for this tender:
   the work-artifact tables above plus `tender_documents`,
   `critical_clause_flags`, `extraction_feedback`, `boq_schedule_totals`,
   `document_embeddings`, `notifications`, `agent_runs`, `agent_memories`,
   `message_batches`, `cost_breakdown_lines` (via their breakdown).
3. Delete the `Tender` row.

This child-deletion helper is extracted as
`tender_service.delete_tenders_deep(db, ids)` and **`POST /tenders/bulk-delete`
is switched to use it** — that endpoint currently deletes only
`TenderDocument` rows (`routes/tenders.py:137`) and orphans the other 12
tables. Fixing it here is in scope because the purge would otherwise duplicate
the same bug.

`notifications.tender_id` is the one real DB-level FK
(`app/models/notification.py:34`); without deleting those rows first the tender
delete raises an IntegrityError on Postgres.

### Batching and safety

Both passes process at most 500 tenders per run, ordered deterministically, so
a first-run backlog cannot blow up a single transaction. Each pass logs its
count. Failures roll back that batch and are retried on the next sweep.

### Settings

Three new keys in `auto_scoring_settings.py`'s `_KEYS` map (DB-overridable
`PlatformSetting` with a `config.py` default), alongside the existing
`auto_discard_*` keys:

| Key | Default |
|---|---|
| `archive_sweep_enabled` | `False` — ships **off** |
| `archive_grace_days` | `3` |
| `archive_purge_days` | `7` |

Shipping off is deliberate: run the dry-run script first (below), confirm the
candidate count, then enable. The flag is also the kill switch if the sweep
misbehaves in production, with no deploy needed.

### Dry-run script

`scripts/archive_sweep_dryrun.py` reports how many tenders Pass A and Pass B
would touch, with a sample of 20 rows each, and writes nothing.

---

## 3. Counts

`_apply_tender_filters` (`tender_service.py:301-304`) already excludes archived
rows by default, so `GET /tenders/` `total` and `GET /tenders/view-counts` are
correct with no change once rows are flagged.

`GET /tenders/stats` (`routes/tenders.py:144-195`) is the broken surface: all
ten counts query `Tender` directly with no archive filter. Each gains
`.filter(Tender.is_archived == False)`. `with_costing_count` joins
`CostBreakdown` to `Tender` so archived tenders' breakdowns drop out.

`admin_dashboard.py:54-67` gets the same treatment.

**Expected effect:** dashboard totals drop once, by the number of archived rows
(including the pre-existing auto-discard backlog, which is already hidden from
lists but has been counted in stats until now), then stay honest.

---

## 4. Archive page + API

### Backend

| Endpoint | Auth | Behaviour |
|---|---|---|
| `GET /tenders/archive` | any user | Paginated `is_archived=True`, sorted `archived_at desc NULLS LAST`. Each item carries `archived_at`, `archive_reason`, and computed `purge_at` (null unless reason is `past_due`). Optional `reason` filter. |
| `POST /tenders/{id}/restore` | admin | Clears `is_archived`, `archived_at`, `archive_reason`. Back in the active pipeline. |
| `POST /tenders/archive/purge-now` | admin | Immediate deep-delete of given IDs (max 50), reusing `delete_tenders_deep`. |

A restored tender past its grace window would be re-archived by the next sweep.
Restore therefore also sets `segment_overridden = True`, reusing the existing
"a human touched this" marker that already exempts rows from
`drpl-discard-cleanup`. Documented as the exemption mechanism.

### Frontend

- Route `/archive` in `src/router.tsx` under `ProtectedRoute`.
- `SidebarNav.tsx` entry below Tenders, showing the archive count.
- New `src/pages/ArchivePage.tsx` reusing `TenderCardList`.
- Each card: a "Deletes in N days" chip (`past_due` only) and a Restore button.
  `auto_discard` rows render with a neutral "Archived — low score" chip and no
  countdown, so the two populations are never confused.
- `src/lib/api.ts` gains `getArchivedTenders`, `restoreTender`, `purgeTenders`.
- `types/tender.ts` gains the three new fields.

---

## 5. Testing

Backend pytest, with sweep functions taking injected `db` and `now` so time
boundaries are testable without sleeping:

- Past-due untouched tender **is** archived.
- Past-due tender with a `cost_breakdowns` row **is not**.
- Past-due tender with a `document_workspaces` row **is not**.
- Past-due tender with an assignee **is not**.
- Past-due tender with only `tender_documents` **is** archived (documents are
  not evidence of work).
- Grace boundary: `closing_date` 2 days ago not archived; 4 days ago archived.
- Purge boundary: `archived_at` 6 days ago survives; 8 days ago deleted.
- A row with `archive_reason='auto_discard'` and an old `archived_at` **is not**
  purged.
- A legacy row with `is_archived=True, archived_at=NULL` **is not** purged.
- `delete_tenders_deep` removes rows from every child table.
- `/tenders/stats` and admin dashboard exclude archived (extends
  `test_dashboard_stats.py`).
- `/tenders/view-counts` unchanged behaviour (`test_view_counts_route.py`).
- Restore clears the fields and sets `segment_overridden`.

---

## Risks

1. **Hard delete is irreversible.** A purged tender re-scraped later returns as
   a new row with no history. Mitigated by the 10-day window and the
   ships-off-by-default flag.
2. **Tenders with no `closing_date` accumulate.** Never archived by this job.
   Out of scope; worth a follow-up if the population grows.
3. **First run archives a backlog.** Counts drop visibly. Mitigated by the
   dry-run script and batching.
4. **The work-artifact list can go stale.** A future feature adding a new
   `tender_id` table must be added to both the Pass-A guard and
   `delete_tenders_deep`. Noted in the backend CONSTITUTION.
