# Pending Backlog Roadmap — Design

**Date:** 2026-07-13
**Status:** Design (awaiting user review)
**Source backlog:** [`docs/PENDING.md`](../../PENDING.md)
**Approach:** A — Phase-gated roadmap (one spec, phases as independent shippable units)

---

## Purpose

`docs/PENDING.md` accumulated deferred items across four independently-shipped
features. They range from trivial mechanical fixes to multi-week net-new
features. This roadmap decomposes that backlog into **ordered, independently
shippable phases**, each with explicit entry criteria, scope boundary, and
verification, so the work lands on a stable base and the large features get
their own future spec→plan→build cycles instead of being guessed at now.

This document is a **roadmap spec**: Phases 0–3 are specified to
implementation-ready detail; Phases 4+ are named, ordered, and given entry
criteria only (they spin out into their own briefs).

---

## Roadmap at a glance

| Phase | Content | Executable now | Exit / verification |
|---|---|---|---|
| 0 — Clean baseline | Fix 4 failing tests; Pydantic v2 `class Config`→`ConfigDict` | Yes | `pytest tests/` green; no `class Config` in schemas/routes |
| 1 — Segment gap | Populate stored `segment` via existing `resegment_all()`; decide hybrid + manual override | Yes | Backfill run; test asserts non-null segment for scored, non-overridden tenders |
| 2 — NIT costing minors | Generalize footer-artifact filter; route escl-free fallback through escalation formula | Yes | Unit tests on parser + fallback |
| 3 — Verification gates | Eager-analysis live smoke; NIT full UI costing run | Partial (needs worker/Redis/prod R2) | Automate what's possible + manual runbook |
| 4+ — Net-new features | NIT Piece 2 (labour wages), Piece 3 (source links) | No — own spec each | N/A here (placeholders w/ entry criteria) |

**Ordering principle:** each phase lands on the previous phase's green baseline.
Phase 3 is a *gate* — shipped-but-unverified work must be proven before Phase 4+
builds atop eager-analysis / costing.

---

## Phase 0 — Clean baseline

**Goal:** a green `pytest tests/` and no Pydantic v2 deprecation noise, so every
later change lands on a passing suite.

### 0a. Auth test failures (401 vs 403)

- **Symptom:** `test_extension_config_requires_auth` and
  `test_tender_upload_requires_auth` assert `403`; endpoints return `401`.
- **Investigation (done):** `app/core/auth.py:22` uses `HTTPBearer()`
  (`auto_error=True`). The codebase's house convention is **401 for
  missing/invalid token** (`auth.py:46,108,116,120`) and **403 for
  role/ownership** (`auth.py:128,135`). The tests contradict both HTTP semantics
  and the house style.
- **Decision:** fix the two test assertions to expect **401**.
- **Guard:** during implementation, confirm the failing path actually returns
  401 (not an `HTTPBearer` `auto_error` 403 for a fully-absent header). If the
  endpoint genuinely returns 403 via `auto_error`, stop and flag rather than
  flip — do not paper over a real inconsistency.

### 0b. Relevance-sort test failures — ROOT CAUSE: no test isolation

- **Tests:** `test_get_tenders_relevance_sort_puts_nulls_last`,
  `test_relevance_sort_secondary_created_at` (`tests/test_tender_card_fields.py`).
- **Investigation (done):** the `get_tenders` `order_by`
  (`tender_service.py:400`) **already** applies
  `ai_relevance_score.desc().nullslast(), created_at.desc()` — the production
  code is correct. The tests fail because there is **no `conftest.py` and no
  test DB isolation**: tests import `SessionLocal`, which binds to the live Neon
  prod DB (`.env`). Each test inserts 2 rows then queries `limit=200`; prod has
  thousands of rows, so the injected rows fall outside the window and
  `ids.index(...)` raises `ValueError`.
- **Decision:** the fix is **test isolation**, not a code change. Add a
  `tests/conftest.py` that binds the app + `SessionLocal` to a clean, disposable
  SQLite DB for the test session. This fixes both sort tests (and hardens the
  whole suite against prod pollution). The auth tests (0a) become independent of
  DB state once isolated, but their assertion values are still corrected in 0a.

### 0c. Pydantic v2 migration

- **Scope:** mechanical `class Config:` → `model_config = ConfigDict(...)` across
  the ~20 schemas/routes flagged (e.g. `app/core/config.py:10`,
  `app/schemas/__init__.py`, `app/api/routes/admin_*.py`).
- **Constraint:** no behavior change; one focused commit; verify warnings gone in
  test output.

**Exit:** `pytest tests/` passes; grep confirms no remaining `class Config` in
app schemas/routes.

---

## Phase 1 — Segment gap

**Goal:** the stored `segment` field (`to_bid`/`not_bidable`/`discarded`) is
populated for scored tenders, and a manual override path is designed.

**Current state (verified):** `compute_segment()` and `resegment_all()` already
exist in `app/services/auto_scoring_service.py`; `resegment_all()` recomputes
`segment` for every scored, non-`segment_overridden` tender using live
thresholds. The gap is that it was never run as a backfill, and there's no UI to
pull a low-score tender into Pursue.

**Scope:**
- **1a. Backfill:** invoke `resegment_all()` as a one-time backfill (idempotent;
  respects `segment_overridden`). Add a test asserting scored, non-overridden
  tenders get a non-null `segment`.
- **1b. Hybrid decision:** the funnel piles currently key off the live AI score.
  Decide and document the hybrid rule: **score-based piles that respect a manual
  `segment` override.** The *decision + data model* live here; if the override
  **UI** grows beyond a small change, it spins into its own slice.

**Boundary:** does not re-run the AI scorer; does not change threshold config.

**Exit:** backfill executed against the target DB (scoped — see risks), test
green, hybrid rule written down.

---

## Phase 2 — NIT costing minors

**Goal:** close the two deferred NIT-faithful-costing code minors.

- **2a. Footer-artifact filter:** `nit_schedule_parser.py` currently filters a
  fixed IREPS-string enumeration. Generalize toward a page-artifact heuristic so
  a differently-worded footer can't leak into a description. The reconciliation
  gate (sum-mismatch → FLAG) remains the safety net. Add parser unit tests
  covering a novel footer string.
- **2b. Escl-free fallback:** `normalize_copied_rates` (~`cost_breakdown_service.py:1025`)
  computes `tender_amount = tender_rate × qty` without escalation. Route it
  through the escalation-inclusive formula for consistency. Add a unit test for
  the `qty` 0/None + `tender_rate` set edge case.

**Boundary:** does not touch the locked-NIT number-world or the fabrication
lockdown; behavior-preserving except the fallback consistency fix.

**Exit:** both changes covered by unit tests; `pytest tests/` green.

---

## Phase 3 — Verification gates

**Goal:** prove shipped-but-unverified work before anything builds atop it.

- **3a. Eager-analysis live smoke:** needs a running RQ worker + Redis. Enable
  `eager_analysis_enabled=True` on a worker and confirm a real in-scope tender
  with documents auto-analyzes (`queued`→`running`→`done`, a
  `DocumentExtractionResult` created) and that backpressure + daily-cap behave.
- **3b. NIT full UI costing run:** a full LLM costing run → downloaded XLSX for
  a reference tender (e.g. #2531), eyeballing the generated sheet +
  reconciliation block. Needs the prod NIT file in R2 the local env can't reach.

**Reality:** both are infra/ops-bound and cannot be fully completed from the dev
host. Deliverable = automate/script whatever is automatable **plus a manual
runbook** with exact steps and expected observations. No false "done" claims —
verification-before-completion applies.

**Exit:** runbook written; any automatable portion executed with captured
output; manual steps clearly handed off.

---

## Phase 4+ — Net-new features (placeholders)

Named and ordered here; **each gets its own brainstorm → spec → plan → build.**
Not detailed now because both carry open product questions that would make a
spec-now stale.

- **Piece 2 — Labour wage sourcing:** cost labour from Indian government
  minimum-wage schedules (railway/construction/maintenance) by region, surfacing
  which wage / state / parameter was used.
  **Entry criteria:** Phase 2 shipped; open questions resolved (which schedules,
  which regions, data source/refresh).
- **Piece 3 — Source links:** capture and show the OEM / IndiaMART / website URL
  behind each costed part.
  **Entry criteria:** Piece 2 shipped; open questions resolved (which sources,
  capture point in the pipeline, UI surface).

---

## Cross-cutting constraints (apply to every phase)

- **Every code change ships with a test.** `pytest tests/` stays green
  phase-to-phase.
- **verification-before-completion:** no "done/fixed/passing" without command
  output confirming it.
- **Do not touch** the deliberately-sequential `tender_analyzer_max_parallel=1`
  constraint (documented rationale in `config.py`).
- **Schema drift discipline:** any new column gets an Alembic revision **and** an
  entry in `_apply_schema_drift_fixes()` / `_add_missing_columns()`.
- **No unrelated refactoring.** Improvements limited to code the phase touches.

---

## Risks & open decisions

- **Local `.env` targets the live Neon prod DB.** Phase 1's backfill mutates
  data — it must be scoped tightly and confirmed before running against prod, or
  run against a local SQLite DB first. This is a hard gate, not a footnote.
- **Auth 401/403** — decided (fix tests to 401) but guarded: flag if the code
  path genuinely differs.
- **Segment override UI size** — if 1b's UI grows beyond a small change, it
  splits into its own slice rather than bloating Phase 1.
- **Phase 3 is partially un-completable here** — explicitly delivered as
  runbook + automated portion, not a completion claim.

---

## Execution intent for this session

Execute **Phases 0 → 1 → 2** end-to-end (each with tests, each landing green),
then deliver **Phase 3** as automated-portion + runbook. Phases 4+ remain
roadmap placeholders for future dedicated brainstorms.
