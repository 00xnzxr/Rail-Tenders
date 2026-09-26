# Pending Backlog Phases 0–2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a clean, green test baseline, populate the stored tender `segment`, and close two NIT-costing code minors — Phases 0–2 of the [pending-backlog roadmap](../specs/2026-07-13-pending-backlog-roadmap-design.md).

**Architecture:** Three sequenced, independently-testable phases in `drpl-backend/`. Phase 0 adds real test isolation (the root cause of all 4 failing tests) plus corrects two auth assertions. Phase 1 runs the existing `resegment_all()` backfill safely and pins its behavior with a test. Phase 2 fixes an escalation-free amount fallback and generalizes the NIT footer-artifact filter.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0, pytest, Pydantic v2.

## Global Constraints

- **Backend venv:** run all Python via `venv/Scripts/python.exe` (from `drpl-backend/`).
- **Never point tests at prod.** Local `.env` `DATABASE_URL` is the **live Neon prod DB**. Tests must run against a disposable SQLite DB. No task may mutate prod without an explicit, separate human go-ahead.
- **Every code change ships with a test.** `venv/Scripts/python.exe -m pytest tests/` must stay green phase-to-phase.
- **verification-before-completion:** no task is "done" without the pytest output confirming it.
- **Do not touch** `tender_analyzer_max_parallel=1` (deliberately sequential, documented in `config.py`).
- **Schema-drift discipline:** any new column needs an Alembic revision **and** an entry in `_apply_schema_drift_fixes()` / `_add_missing_columns()`. (Phases 0–2 add no columns.)
- **Branch:** all work on `feat/pending-backlog-phases-0-2` (created off `main`), never commit code to `main`.
- **Commit messages** end with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `drpl-backend/tests/conftest.py` | Bind the whole test session to a disposable SQLite DB **before** app import; expose a clean-DB fixture | Create |
| `drpl-backend/tests/test_api.py` | Correct two auth assertions 403→401 | Modify (`:27`, `:33`) |
| `drpl-backend/tests/test_segment_backfill.py` | Pin `resegment_all()` behavior | Create |
| `drpl-backend/tests/test_normalize_copied_rates_escalation.py` | Pin escalation-inclusive fallback amount | Create |
| `drpl-backend/tests/test_nit_boilerplate_filter.py` | Pin generalized footer-artifact filtering | Create |
| `drpl-backend/app/services/cost_breakdown_service.py` | Route escl-free fallback through the escalation formula | Modify (`:1025-1026`) |
| `drpl-backend/app/services/costing/nit_schedule_parser.py` | Generalize `_BOILERPLATE_RE` toward a page-artifact heuristic | Modify (`:32-40`) |
| `docs/RUNBOOK-segment-backfill.md` | Safe prod-backfill runbook (Phase 1 execution against prod is human-gated) | Create |

---

## Task 0: Branch setup

- [ ] **Step 1: Create the feature branch off up-to-date main**

```bash
cd /c/Project/drpl-platform
git checkout main && git pull --ff-only
git checkout -b feat/pending-backlog-phases-0-2
```

- [ ] **Step 2: Confirm the 4 failures reproduce (baseline)**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest \
  tests/test_api.py::test_extension_config_requires_auth \
  tests/test_api.py::test_tender_upload_requires_auth \
  tests/test_tender_card_fields.py::test_get_tenders_relevance_sort_puts_nulls_last \
  tests/test_tender_card_fields.py::test_relevance_sort_secondary_created_at -q
```
Expected: `4 failed`. (Confirms the starting state before we fix anything.)

---

## Task 1: Test isolation via conftest.py (Phase 0b)

**Files:**
- Create: `drpl-backend/tests/conftest.py`

**Interfaces:**
- Produces: a disposable SQLite test DB bound at import time via `DATABASE_URL`; a `db` fixture yielding a `SessionLocal()` session on clean tables.

**Why:** `app/core/database.py:12,27,62` builds the engine from `settings.database_url` at import, and `get_settings()` is `lru_cache`d reading env on first call. Setting `os.environ["DATABASE_URL"]` at the **top of conftest.py** (pytest loads conftest before test modules, so before `app` is ever imported) makes the entire app + `SessionLocal` bind to SQLite with no rebinding.

- [ ] **Step 1: Write conftest.py**

```python
"""Pytest session setup: bind the app to a disposable SQLite DB.

CRITICAL: local .env DATABASE_URL is the LIVE Neon prod DB. This file forces
a throwaway SQLite file for the whole test session. It MUST set the env var
before any `app.*` import (pytest imports conftest before test modules, so
this top-level assignment runs first).
"""
import os
import tempfile

# Disposable on-disk SQLite (on-disk, not :memory:, so the app's create_all
# and the request-scoped sessions share one DB across connections/threads).
_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "drpl_test.db")
if os.path.exists(_TEST_DB_PATH):
    os.remove(_TEST_DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
# Neutralize any prod-oriented pool env that could leak in.
os.environ.pop("DB_POOL_SIZE", None)
os.environ.pop("DB_MAX_OVERFLOW", None)

import pytest  # noqa: E402
from app.core.database import Base, engine, SessionLocal  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """Create all tables once on the SQLite test DB."""
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db():
    """A session on the isolated test DB. Rolls back at teardown."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
```

- [ ] **Step 2: Verify the two sort tests now pass (code was already correct)**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest \
  tests/test_tender_card_fields.py::test_get_tenders_relevance_sort_puts_nulls_last \
  tests/test_tender_card_fields.py::test_relevance_sort_secondary_created_at -q
```
Expected: `2 passed`. (The rows now live in a small isolated DB, so `limit=200` includes them and `ids.index(...)` succeeds.)

- [ ] **Step 3: Sanity-check the DB is SQLite, not Neon**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -c "import tests.conftest; from app.core.database import engine; print(engine.url)"
```
Expected: a `sqlite:///.../drpl_test.db` URL — NOT a `neon.tech` host.

- [ ] **Step 4: Commit**

```bash
git add drpl-backend/tests/conftest.py
git commit -m "test: isolate suite on disposable SQLite; fix relevance-sort tests

Root cause of the relevance-sort failures was no test isolation: tests bound
to the live Neon prod DB, so injected rows fell outside limit=200. The
get_tenders order_by was already correct.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Correct auth assertions 403→401 (Phase 0a)

**Files:**
- Modify: `drpl-backend/tests/test_api.py:27,33`

**Interfaces:**
- Consumes: nothing new. Relies on Task 1's isolation only for a clean run.

**Why:** `app/core/auth.py` uses 401 for missing/invalid token (`:46,108,116,120`) and 403 for role/ownership (`:128,135`). The endpoints correctly return 401 for a missing token; the two assertions are the outliers. This is the codebase-consistent semantic.

- [ ] **Step 1: Verify the endpoints return 401 (evidence before editing)**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -c "
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
print('config:', c.get('/api/extension/config').status_code)
print('upload:', c.post('/api/extension/tenders', json={'tenders': []}).status_code)
"
```
Expected: `config: 401` and `upload: 401`. If either prints `403`, STOP — the code path differs from the assumption; re-open the decision instead of editing the test.

- [ ] **Step 2: Fix the two assertions**

In `tests/test_api.py`, change line 27:
```python
    assert response.status_code == 401  # No token provided
```
and line 33:
```python
    assert response.status_code == 401
```

- [ ] **Step 3: Verify the two auth tests pass**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest \
  tests/test_api.py::test_extension_config_requires_auth \
  tests/test_api.py::test_tender_upload_requires_auth -q
```
Expected: `2 passed`.

- [ ] **Step 4: Run the FULL suite — confirm the 4 originals are green and nothing regressed**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest tests/ -q
```
Expected: `0 failed`. (Record the pass count; isolation may reveal previously prod-masked behavior — investigate any *new* failure before proceeding.)

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/tests/test_api.py
git commit -m "test: assert 401 (not 403) for missing token, matching auth house style

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Pin segment-backfill behavior with a test (Phase 1)

**Files:**
- Create: `drpl-backend/tests/test_segment_backfill.py`

**Interfaces:**
- Consumes: `app.services.auto_scoring_service.resegment_all(db) -> dict` (returns `{"resegmented": int}`); `compute_segment(...)`; `Tender` model fields `ai_relevance_score`, `estimated_value`, `segment`, `segment_overridden`.

**Why:** `resegment_all()` and its admin endpoint (`tender_scoring_admin.py:37`, `?resegment=true`) already exist; the only gap is that no test pins the contract and the prod data was never backfilled. This test guarantees: scored non-overridden tenders get a non-null `segment`; overridden ones are left untouched.

- [ ] **Step 1: Write the failing test**

```python
from app.models.tender import Tender
from app.services.auto_scoring_service import resegment_all


def _seed(db, **kw):
    t = Tender(portal="gem", **kw)
    db.add(t); db.commit(); db.refresh(t)
    return t


def test_resegment_populates_scored_non_overridden(db):
    scored = _seed(db, tender_id="SEG-SCORED", title="scored",
                   ai_relevance_score=0.9, estimated_value=1000000.0,
                   segment=None, segment_overridden=False)
    overridden = _seed(db, tender_id="SEG-OVR", title="ovr",
                       ai_relevance_score=0.1, estimated_value=1000000.0,
                       segment="to_bid", segment_overridden=True)
    unscored = _seed(db, tender_id="SEG-UNSCORED", title="unscored",
                     ai_relevance_score=None, segment=None,
                     segment_overridden=False)

    result = resegment_all(db)

    db.refresh(scored); db.refresh(overridden); db.refresh(unscored)
    assert result["resegmented"] >= 1
    assert scored.segment is not None            # scored -> segment populated
    assert overridden.segment == "to_bid"        # override respected
    assert unscored.segment is None              # unscored left alone
```

- [ ] **Step 2: Run test to verify it passes (behavior already implemented)**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest tests/test_segment_backfill.py -q
```
Expected: `1 passed`. (This is a characterization test — `resegment_all` already implements the behavior. If it FAILS, the segment/override contract is broken and must be fixed before proceeding.)

- [ ] **Step 3: Commit**

```bash
git add drpl-backend/tests/test_segment_backfill.py
git commit -m "test: pin resegment_all backfill contract (scored populated, override respected)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Segment-backfill runbook (Phase 1 prod execution — human-gated)

**Files:**
- Create: `docs/RUNBOOK-segment-backfill.md`

**Why:** the actual prod backfill mutates the live Neon DB. Per Global Constraints it must not run without explicit human go-ahead. Deliver the exact, reversible procedure as a runbook rather than executing it.

- [ ] **Step 1: Write the runbook**

```markdown
# Runbook — Populate stored `segment` (Phase 1 backfill)

`resegment_all()` recomputes `segment` for every scored, non-overridden tender
using current thresholds. It is idempotent and respects `segment_overridden`.
The admin endpoint already exposes it.

## Safety
- Mutates the LIVE Neon prod DB. Requires explicit human go-ahead.
- Idempotent: re-running yields the same result for unchanged thresholds/scores.
- Only touches rows where `segment_overridden = False`.

## Pre-check (read-only) — how many rows will change
    SELECT count(*) FROM tenders
    WHERE ai_relevance_score IS NOT NULL AND segment_overridden = false;

## Execute (choose ONE)
### Option A — admin endpoint (preferred; goes through app auth)
    POST /api/admin/tender-scoring/settings?resegment=true
    (as master_admin; see tender_scoring_admin.py)

### Option B — one-off script (only if the endpoint is unavailable)
    cd drpl-backend
    venv/Scripts/python.exe -c "from app.core.database import SessionLocal; \
    from app.services.auto_scoring_service import resegment_all; \
    db=SessionLocal(); print(resegment_all(db)); db.close()"

## Verify
    SELECT segment, count(*) FROM tenders
    WHERE ai_relevance_score IS NOT NULL GROUP BY segment;
(expect no NULL segment among scored, non-overridden rows)

## Rollback
No destructive change — segment is derived. Re-running with prior thresholds
restores prior values; manual overrides are never touched.
```

- [ ] **Step 2: Commit**

```bash
git add docs/RUNBOOK-segment-backfill.md
git commit -m "docs: runbook for Phase 1 segment backfill (human-gated prod mutation)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Escalation-inclusive fallback amount (Phase 2b)

**Files:**
- Modify: `drpl-backend/app/services/cost_breakdown_service.py:1025-1026`
- Create: `drpl-backend/tests/test_normalize_copied_rates_escalation.py`

**Interfaces:**
- Consumes: `CostBreakdownLine` fields `tender_rate`, `quantity`, `tender_amount`, `escalation_pct`, `rate_source`, `is_tax_line`, `rate`, `needs_input`. The canonical escalation formula already used at `:284` is `tender_rate * qty * (1 + escl/100)`.

**Why:** at `:1026`, the escl-free fallback computes `ln.tender_amount = round(tr * q, 2)` — omitting escalation, inconsistent with the authoritative formula at `:284`. Route it through the escalation-inclusive formula.

- [ ] **Step 1: Write the failing test**

```python
from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine
from app.services.cost_breakdown_service import normalize_copied_rates


def _mk_breakdown(db):
    bd = CostBreakdown(tender_id=1)
    db.add(bd); db.commit(); db.refresh(bd)
    return bd


def test_fallback_tender_amount_includes_escalation(db):
    bd = _mk_breakdown(db)
    # A non-tax line that copies the published rate, qty set, escalation 10%,
    # tender_amount unset so the fallback path at :1025 fires.
    ln = CostBreakdownLine(
        cost_breakdown_id=bd.id, is_tax_line=False,
        tender_rate=100.0, quantity=2.0, rate=100.0,
        rate_source="tender_estimate", escalation_pct=10.0,
        tender_amount=None,
    )
    db.add(ln); db.commit()

    normalize_copied_rates(db, bd.id, overhead_pct=10.0, margin_pct=15.0)

    db.refresh(ln)
    # 100 * 2 * (1 + 10/100) = 220.0, NOT 200.0
    assert ln.tender_amount == 220.0
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest tests/test_normalize_copied_rates_escalation.py -q
```
Expected: FAIL — asserts `220.0 == 200.0` (current escl-free code yields 200.0).

- [ ] **Step 3: Implement the fix**

Replace `cost_breakdown_service.py:1025-1026`:
```python
        if ln.tender_amount is None:
            ln.tender_amount = round(tr * q, 2)
```
with:
```python
        if ln.tender_amount is None:
            # Escalation-inclusive, matching the authoritative formula at ~:284
            # (tender_rate * qty * (1 + escl/100)); an escl-free amount would
            # understate the NIT-authoritative tender value.
            _escl = 0.0
            try:
                _escl = float(ln.escalation_pct or 0.0)
            except (TypeError, ValueError):
                _escl = 0.0
            ln.tender_amount = round(tr * q * (1 + _escl / 100.0), 2)
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest tests/test_normalize_copied_rates_escalation.py -q
```
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/cost_breakdown_service.py drpl-backend/tests/test_normalize_copied_rates_escalation.py
git commit -m "fix(costing): escl-inclusive fallback tender_amount in normalize_copied_rates

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Generalize NIT footer-artifact filter (Phase 2a)

**Files:**
- Modify: `drpl-backend/app/services/costing/nit_schedule_parser.py:32-40`
- Create: `drpl-backend/tests/test_nit_boilerplate_filter.py`

**Interfaces:**
- Consumes: `nit_schedule_parser._BOILERPLATE_RE` (a compiled `re.Pattern`; used at `:192` via `.match(t)`).

**Why:** `_BOILERPLATE_RE` (`:32-40`) enumerates fixed IREPS strings. A differently-worded footer could leak into a description. Add a general page-artifact alternative (e.g. bare `Page N`, `Page N of M`, `N | Page`, dotted continuation leaders) while keeping the existing specific strings. The reconciliation gate remains the safety net; this just reduces leakage.

- [ ] **Step 1: Write the failing test**

```python
from app.services.costing.nit_schedule_parser import _BOILERPLATE_RE


def test_matches_existing_specific_footers():
    for s in ["Page 3 of 12", "Run Date/Time: 01/02/2026",
              "TENDER DOCUMENT", "Tender No: 12345",
              "Closing Date/Time: 10:00", "LOCO-SHOP-NR RLY"]:
        assert _BOILERPLATE_RE.match(s), s


def test_matches_generalized_page_artifacts():
    # Differently-worded page artifacts that the fixed enumeration missed.
    for s in ["Page 4", "4 | Page", "Page | 4", "- 7 -"]:
        assert _BOILERPLATE_RE.match(s), s


def test_does_not_match_real_description_lines():
    for s in ["Supply of traction motor bearings",
              "Page mounting bracket assembly",   # 'Page' as a real word
              "Item 5: brake shoe"]:
        assert not _BOILERPLATE_RE.match(s), s
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest tests/test_nit_boilerplate_filter.py -q
```
Expected: `test_matches_generalized_page_artifacts` FAILS (current regex doesn't match `Page 4`, `4 | Page`, `- 7 -`).

- [ ] **Step 3: Generalize the regex**

Replace `nit_schedule_parser.py:32-40`:
```python
_BOILERPLATE_RE = re.compile(
    r"^Page\s+\d+\s+of\s+\d+$|"
    r"^Run Date/Time:|"
    r"^TENDER DOCUMENT$|"
    r"^Tender No:|"
    r"^Closing Date/Time:|"
    r"^LOCO-SHOP-.*RLY$",
    re.IGNORECASE,
)
```
with:
```python
_BOILERPLATE_RE = re.compile(
    # Specific IREPS strings (kept verbatim)…
    r"^Run Date/Time:|"
    r"^TENDER DOCUMENT$|"
    r"^Tender No:|"
    r"^Closing Date/Time:|"
    r"^LOCO-SHOP-.*RLY$|"
    # …plus generalized page-number artifacts. Each alternative is fully
    # anchored so a real description that merely contains 'Page' is NOT matched.
    r"^Page\s+\d+(\s+of\s+\d+)?$|"   # "Page 4", "Page 4 of 12"
    r"^\d+\s*\|\s*Page$|"            # "4 | Page"
    r"^Page\s*\|\s*\d+$|"           # "Page | 4"
    r"^-\s*\d+\s*-$",               # "- 7 -" centered page number
    re.IGNORECASE,
)
```

- [ ] **Step 4: Run the filter test to verify it passes**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest tests/test_nit_boilerplate_filter.py -q
```
Expected: `3 passed`.

- [ ] **Step 5: Run the existing NIT parser tests — confirm no regression**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest tests/ -k "nit or schedule or costing or breakdown" -q
```
Expected: all pass (no previously-green NIT/costing test regresses).

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/costing/nit_schedule_parser.py drpl-backend/tests/test_nit_boilerplate_filter.py
git commit -m "fix(nit): generalize footer-artifact filter to catch page-number variants

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Full-suite green gate + Phase 3 runbook stub

**Files:**
- Create: `docs/RUNBOOK-phase3-verification.md`

- [ ] **Step 1: Run the entire suite**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -m pytest tests/ -q
```
Expected: `0 failed`. Record the pass count. If any test regressed vs Task 2 Step 4's count, fix before continuing.

- [ ] **Step 2: Write the Phase 3 verification runbook (infra-bound, handed off)**

```markdown
# Runbook — Phase 3 verification gates

## 3a. Eager-analysis live smoke (needs RQ worker + Redis)
1. Set REDIS_URL; start worker: `cd drpl-backend && venv/Scripts/python.exe worker.py`
2. Set `EAGER_ANALYSIS_ENABLED=true` in the worker env; restart worker.
3. Ensure an in-scope tender (score >= 0.70 or eligible-flagged) with >=1 uploaded doc exists.
4. Observe: per-tender status marker goes queued -> running -> done; a
   DocumentExtractionResult row is created. Confirm daily-cap + backpressure
   behave (watch logs/costing_run.log and the worker log).
5. Revert `EAGER_ANALYSIS_ENABLED` to false when done unless enabling for real.

## 3b. NIT full UI costing run (needs prod NIT file in R2)
1. From the UI, run a full costing on a reference tender (e.g. #2531).
2. Download the generated XLSX; eyeball the reconciliation block and that the
   locked-NIT number-world matches the known-good total (60,879,392.16 for #2531).
3. Note any fabrication/needs_review flags.

Both are infra-bound and must be executed by a human on the deploy host.
```

- [ ] **Step 3: Commit**

```bash
git add docs/RUNBOOK-phase3-verification.md
git commit -m "docs: Phase 3 verification runbook (eager-analysis smoke + NIT full run)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 4: Update PENDING.md to reflect what shipped**

Strike the now-closed items (4 failing tests, segment population wiring/backfill test, NIT footer filter, escl-free fallback) and note the two backfill/verification steps are runbook-gated. Commit:
```bash
git add docs/PENDING.md
git commit -m "docs: mark Phases 0-2 pending items closed; link runbooks

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Phase 0a (auth 401) → Task 2. ✓
- Phase 0b (sort tests) → Task 1 (root cause = isolation). ✓
- Phase 0c (Pydantic v2) → **deferred**: see note below.
- Phase 1 (segment backfill + hybrid/override decision) → Tasks 3 (test), 4 (runbook). Hybrid *decision* documented in spec §Phase 1; UI deferred per spec boundary. ✓
- Phase 2a (footer filter) → Task 6. ✓
- Phase 2b (escl fallback) → Task 5. ✓
- Phase 3 (verification gates) → Task 7 runbook. ✓

**Deviation from spec — Pydantic v2 migration (0c):** intentionally NOT in this plan. Reason: it's a repo-wide mechanical sweep (~20 files) with zero behavior change and no bearing on the roadmap's functional goals; bundling it risks noise in the same branch as real fixes and can regress imports subtly. Recommend a separate dedicated branch/PR. Flagged here so it isn't silently dropped. If you want it in-scope, it becomes its own task set.

**Placeholder scan:** no TBD/TODO; every code step shows exact code; every test step shows the command + expected result. ✓

**Type consistency:** `resegment_all` returns `{"resegmented": int}` (Task 3 asserts `result["resegmented"]`). `_BOILERPLATE_RE` stays a compiled pattern used via `.match` (Task 6). `normalize_copied_rates(db, breakdown_id, overhead_pct, margin_pct)` signature matches source (Task 5). ✓
