# Eager Tender Analysis Implementation Plan (Piece A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically run the existing v2 document analyzer for in-scope tenders that already have documents, via a self-rescheduling sweep with capacity backpressure, a daily cap, idempotency, and a feature flag — all off/conservative by default.

**Architecture:** Mirror the proven auto-scoring reaper (`app/worker/auto_scoring_tasks.py` + `app/services/auto_scoring_service.py` + `app/services/auto_scoring_settings.py`). A self-rescheduling RQ sweep selects candidate tenders (score ≥ 0.70 or eligible-flagged, have documents, not yet analyzed, not in-flight), checks capacity + a daily cap, and enqueues a per-tender job that calls the unchanged `analyze_all_documents`. A per-`Tender` status marker plus a startup orphan-reaper prevent double-work and wedging.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy, RQ (Redis queue) with graceful no-Redis degradation, pytest, Alembic. The analyzer (`tender_analyzer_v2`) is reused verbatim.

## Global Constraints

- Gate: `ai_relevance_score >= eager_analysis_min_score` (default **0.70**, Pursue tier) OR `is_eligible_indicator` true.
- Respect the deliberate `tender_analyzer_max_parallel = 1` sequential constraint: `eager_analysis_max_concurrency` default **1**; the sweep enqueues bounded batches, never parallel fan-out.
- Master flag `eager_analysis_enabled` default **False** — when off, the sweep is a strict no-op and the existing lazy/manual analysis path is untouched.
- Backpressure DEFERS, never drops: skip a tick (log + reschedule) if RQ `queue_depth > eager_analysis_queue_ceiling` (default **20**) OR `llm_429_last_5m > 0` OR DB pool `checked_out / (size + overflow) > 0.80`.
- Daily cap `eager_analysis_daily_cap` default **200**.
- Idempotency: never analyze a tender that already has a `DocumentExtractionResult`; never double-enqueue an in-flight tender.
- Follow repo pattern: new columns on an existing table need an Alembic revision AND self-heal in `app/main.py` (`_add_missing_columns` SQLite, `_apply_schema_drift_fixes` Postgres). New tables self-create via `Base.metadata.create_all`.
- Everything degrades to a no-op when `get_queue()` is None (no Redis), mirroring `auto_scoring_tasks.py`.
- Run backend commands with the venv: `drpl-backend/venv/Scripts/python.exe`. Tests live under `drpl-backend/tests/`.

---

## File Structure

**New files**
- `app/services/eager_analysis_settings.py` — DB-backed live-read settings (mirror `auto_scoring_settings.py`).
- `app/services/eager_analysis_service.py` — candidate selection, capacity gate (pure + gatherer), daily-cap counter.
- `app/worker/eager_analysis_tasks.py` — the self-rescheduling sweep + per-tender job (mirror `auto_scoring_tasks.py`).
- `tests/services/test_eager_analysis_settings.py`, `tests/services/test_eager_analysis_service.py`, `tests/worker/test_eager_analysis_tasks.py`, `tests/test_eager_analysis_e2e.py`.
- `alembic/versions/<rev>_eager_analysis_status.py`.

**Modified files**
- `app/core/config.py` — new `eager_analysis_*` defaults.
- `app/models/tender.py` — `eager_analysis_status`, `eager_analysis_at` columns.
- `app/main.py` — self-heal columns + startup orphan-reaper.
- `app/services/seed_scheduled_jobs.py` — bootstrap first-enqueue of the sweep.

---

## Task 1: Config flags + settings reader

**Files:**
- Modify: `app/core/config.py`
- Create: `app/services/eager_analysis_settings.py`
- Test: `tests/services/test_eager_analysis_settings.py`

**Interfaces:**
- Produces: `get_eager_analysis_settings(db) -> dict` with keys `eager_analysis_enabled: bool, eager_analysis_min_score: float, eager_analysis_interval_seconds: int, eager_analysis_batch_size: int, eager_analysis_max_concurrency: int, eager_analysis_daily_cap: int, eager_analysis_queue_ceiling: int`.

- [ ] **Step 1: Add config defaults**

In `app/core/config.py`, add these fields to the `Settings` class near the `auto_scoring_*` block (search for `auto_scoring_batch_size` to find it):

```python
    # Eager tender analysis (auto-run v2 analyzer for in-scope tenders on first pass)
    eager_analysis_enabled: bool = False
    eager_analysis_min_score: float = 0.70
    eager_analysis_interval_seconds: int = 180
    eager_analysis_batch_size: int = 5
    eager_analysis_max_concurrency: int = 1
    eager_analysis_daily_cap: int = 200
    eager_analysis_queue_ceiling: int = 20
```

- [ ] **Step 2: Write the failing test**

```python
# tests/services/test_eager_analysis_settings.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.platform_setting import PlatformSetting
from app.services.eager_analysis_settings import get_eager_analysis_settings

def _db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e)
    return sessionmaker(bind=e)()

def test_defaults_when_no_overrides():
    s = get_eager_analysis_settings(_db())
    assert s["eager_analysis_enabled"] is False
    assert s["eager_analysis_min_score"] == 0.70
    assert s["eager_analysis_daily_cap"] == 200
    assert s["eager_analysis_queue_ceiling"] == 20

def test_platform_setting_override_and_coercion():
    db = _db()
    db.add(PlatformSetting(key="eager_analysis_enabled", value="true"))
    db.add(PlatformSetting(key="eager_analysis_min_score", value="0.9"))
    db.add(PlatformSetting(key="eager_analysis_daily_cap", value="50"))
    db.commit()
    s = get_eager_analysis_settings(db)
    assert s["eager_analysis_enabled"] is True
    assert s["eager_analysis_min_score"] == 0.9
    assert s["eager_analysis_daily_cap"] == 50
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/test_eager_analysis_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.eager_analysis_settings`.

- [ ] **Step 4: Implement (mirror `auto_scoring_settings.py`)**

```python
# app/services/eager_analysis_settings.py
"""DB-backed, live-read eager-analysis settings with config-default fallback.
Mirrors app/services/auto_scoring_settings.py."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.platform_setting import PlatformSetting

_KEYS = {
    "eager_analysis_enabled": "eager_analysis_enabled",
    "eager_analysis_min_score": "eager_analysis_min_score",
    "eager_analysis_interval_seconds": "eager_analysis_interval_seconds",
    "eager_analysis_batch_size": "eager_analysis_batch_size",
    "eager_analysis_max_concurrency": "eager_analysis_max_concurrency",
    "eager_analysis_daily_cap": "eager_analysis_daily_cap",
    "eager_analysis_queue_ceiling": "eager_analysis_queue_ceiling",
}


def _coerce(raw: str, default):
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return raw


def get_eager_analysis_settings(db: Session) -> dict:
    cfg = get_settings()
    rows = {r.key: r.value for r in db.query(PlatformSetting).filter(
        PlatformSetting.key.in_(list(_KEYS))).all()}
    out = {}
    for key, attr in _KEYS.items():
        default = getattr(cfg, attr)
        out[key] = _coerce(rows[key], default) if key in rows and rows[key] is not None else default
    return out
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/test_eager_analysis_settings.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/core/config.py drpl-backend/app/services/eager_analysis_settings.py drpl-backend/tests/services/test_eager_analysis_settings.py
git commit -m "feat(analysis): eager-analysis config flags + live settings reader"
```

---

## Task 2: Tender status columns + migration + self-heal

**Files:**
- Modify: `app/models/tender.py`
- Create: `alembic/versions/<rev>_eager_analysis_status.py`
- Modify: `app/main.py` (`_add_missing_columns`, `_apply_schema_drift_fixes`)
- Test: `tests/services/test_eager_analysis_columns.py`

**Interfaces:**
- Produces: `Tender.eager_analysis_status: str | None` (values `null`/`queued`/`running`/`done`/`failed`), `Tender.eager_analysis_at: datetime | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_eager_analysis_columns.py
from app.models.tender import Tender

def test_tender_has_eager_analysis_columns():
    t = Tender(portal="ireps", tender_id="X1", title="t",
               eager_analysis_status="queued")
    assert t.eager_analysis_status == "queued"
    assert t.eager_analysis_at is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/test_eager_analysis_columns.py -v`
Expected: FAIL — `TypeError: 'eager_analysis_status' is an invalid keyword argument`.

- [ ] **Step 3: Add the columns**

In `app/models/tender.py`, next to `scoring_attempts` (search for `scoring_attempts = Column`), add:

```python
    eager_analysis_status = Column(String(16), nullable=True, index=True)   # null/queued/running/done/failed
    eager_analysis_at = Column(DateTime(timezone=True), nullable=True)      # last eager-analysis state change
```

(Confirm `String` and `DateTime` are already imported at the top of the file; they are used by existing columns.)

- [ ] **Step 4: Run the model test**

Run: same pytest command. Expected: PASS.

- [ ] **Step 5: Alembic revision + self-heal**

```bash
cd drpl-backend && venv/Scripts/python.exe -m alembic revision -m "eager_analysis_status"
```

Fill `upgrade()`/`downgrade()` (set `down_revision` to the current head — find it via the newest file in `alembic/versions/` or `alembic heads`; the current head is `20260712_cb_reconciliation`):

```python
def upgrade():
    op.add_column("tenders", sa.Column("eager_analysis_status", sa.String(16), nullable=True))
    op.add_column("tenders", sa.Column("eager_analysis_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tenders_eager_analysis_status", "tenders", ["eager_analysis_status"])

def downgrade():
    op.drop_index("ix_tenders_eager_analysis_status", table_name="tenders")
    op.drop_column("tenders", "eager_analysis_at")
    op.drop_column("tenders", "eager_analysis_status")
```

Then add self-heal. In `app/main.py`, find `_add_missing_columns` (SQLite) and add to its column list for the `tenders` table:

```python
        ("tenders", "eager_analysis_status", "VARCHAR(16)"),
        ("tenders", "eager_analysis_at", "TIMESTAMP"),
```

(Match the exact tuple/format the surrounding entries use — read a few existing entries first.) And in `_apply_schema_drift_fixes` (Postgres), add:

```python
        'ALTER TABLE tenders ADD COLUMN IF NOT EXISTS eager_analysis_status VARCHAR(16)',
        'ALTER TABLE tenders ADD COLUMN IF NOT EXISTS eager_analysis_at TIMESTAMPTZ',
```

(Again match the exact surrounding statement style.)

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/models/tender.py drpl-backend/alembic/versions drpl-backend/app/main.py drpl-backend/tests/services/test_eager_analysis_columns.py
git commit -m "feat(analysis): eager_analysis_status/at columns + migration + self-heal"
```

---

## Task 3: Candidate selection

**Files:**
- Create: `app/services/eager_analysis_service.py`
- Test: `tests/services/test_eager_analysis_service.py`

**Interfaces:**
- Consumes: `get_eager_analysis_settings` (Task 1); `Tender.eager_analysis_status` (Task 2).
- Produces: `find_eager_analysis_candidates(db, limit: int | None = None) -> list[int]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_eager_analysis_service.py
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.tender import Tender
from app.models.costing_template import BOQItem  # noqa: F401  (ensure metadata loaded)
from app.models.document_analysis import DocumentExtractionResult
from app.models.tender import TenderDocument
from app.services.eager_analysis_service import find_eager_analysis_candidates

def _db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e)
    return sessionmaker(bind=e)()

_n = 0
def _tender(db, score=0.9, eligible=False, archived=False, dup=False, status=None):
    global _n; _n += 1
    t = Tender(portal="ireps", tender_id=f"T{_n}", title=f"t{_n}",
               ai_relevance_score=score, is_eligible_indicator=eligible,
               is_archived=archived, is_duplicate=dup, eager_analysis_status=status)
    db.add(t); db.commit(); return t

def _doc(db, t):
    d = TenderDocument(tender_id=t.id, file_name=f"d{t.id}.pdf", file_path=f"p/{t.id}")
    db.add(d); db.commit(); return d

def test_selects_only_valid_in_scope_with_docs_unanalyzed():
    db = _db()
    good = _tender(db, score=0.9); _doc(db, good)                 # ✓ candidate
    elig = _tender(db, score=0.1, eligible=True); _doc(db, elig)  # ✓ eligible-flagged
    low = _tender(db, score=0.5); _doc(db, low)                   # ✗ below 0.70, not eligible
    nodocs = _tender(db, score=0.9)                               # ✗ no documents
    arch = _tender(db, score=0.9, archived=True); _doc(db, arch)  # ✗ archived
    inflight = _tender(db, score=0.9, status="running"); _doc(db, inflight)  # ✗ in-flight
    analyzed = _tender(db, score=0.9); _doc(db, analyzed)         # ✗ already analyzed
    db.add(DocumentExtractionResult(tender_id=analyzed.id)); db.commit()

    ids = find_eager_analysis_candidates(db)
    assert set(ids) == {good.id, elig.id}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/test_eager_analysis_service.py -v`
Expected: FAIL — module not found. (If `DocumentExtractionResult` / `TenderDocument` import paths differ, fix the imports first by grepping `class DocumentExtractionResult` / `class TenderDocument` under `app/models/`.)

- [ ] **Step 3: Implement**

```python
# app/services/eager_analysis_service.py
"""Selection + gating helpers for eager tender analysis. Query-only here;
the sweep/job orchestration lives in app/worker/eager_analysis_tasks.py."""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.tender import Tender
from app.models.tender import TenderDocument
from app.models.document_analysis import DocumentExtractionResult
from app.services.eager_analysis_settings import get_eager_analysis_settings


def find_eager_analysis_candidates(db: Session, limit: "int | None" = None) -> list[int]:
    """In-scope tenders that have documents, aren't analyzed yet, and aren't
    in-flight. Ordered oldest-first so a backlog drains fairly."""
    s = get_eager_analysis_settings(db)
    limit = limit or s["eager_analysis_batch_size"]
    min_score = s["eager_analysis_min_score"]

    analyzed = db.query(DocumentExtractionResult.tender_id).distinct().subquery()
    has_doc = db.query(TenderDocument.tender_id).distinct().subquery()

    rows = (
        db.query(Tender.id)
        .filter(or_(Tender.ai_relevance_score >= min_score,
                    Tender.is_eligible_indicator == True))       # noqa: E712
        .filter(Tender.is_archived == False)                     # noqa: E712
        .filter(Tender.is_duplicate == False)                    # noqa: E712
        .filter(Tender.eager_analysis_status.is_(None) | (Tender.eager_analysis_status == "failed"))
        .filter(Tender.id.in_(db.query(has_doc.c.tender_id)))
        .filter(~Tender.id.in_(db.query(analyzed.c.tender_id)))
        .order_by(Tender.created_at.asc())
        .limit(limit)
        .all()
    )
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run to verify it passes**

Run: same pytest command. Expected: PASS. If `Tender.is_duplicate` doesn't exist, drop that filter (grep the model to confirm; the map lists `is_duplicate` on Tender).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/eager_analysis_service.py drpl-backend/tests/services/test_eager_analysis_service.py
git commit -m "feat(analysis): eager-analysis candidate selection query"
```

---

## Task 4: Capacity gate + daily cap

**Files:**
- Modify: `app/services/eager_analysis_service.py`
- Test: add to `tests/services/test_eager_analysis_service.py`

**Interfaces:**
- Produces:
  - `capacity_ok(queue_depth: int, rate_limits_5m: int, pool_util: float, ceiling: int) -> bool` — PURE.
  - `gather_capacity(db) -> tuple[int, int, float]` — reads real signals `(queue_depth, rate_limits_5m, pool_util)`.
  - `today_key() -> str`, `daily_count(db) -> int`, `bump_daily_count(db, n: int) -> None` — cap counter stored in a `PlatformSetting` row `eager_analysis_daily_count` as `"YYYY-MM-DD:N"` (resets when the date changes).

- [ ] **Step 1: Write the failing tests** (append)

```python
from app.services.eager_analysis_service import capacity_ok, daily_count, bump_daily_count

def test_capacity_ok_pure():
    assert capacity_ok(queue_depth=5, rate_limits_5m=0, pool_util=0.3, ceiling=20) is True
    assert capacity_ok(queue_depth=25, rate_limits_5m=0, pool_util=0.3, ceiling=20) is False   # queue too deep
    assert capacity_ok(queue_depth=5, rate_limits_5m=1, pool_util=0.3, ceiling=20) is False     # recent 429
    assert capacity_ok(queue_depth=5, rate_limits_5m=0, pool_util=0.85, ceiling=20) is False    # pool saturated

def test_daily_count_roundtrip():
    db = _db()
    assert daily_count(db) == 0
    bump_daily_count(db, 3)
    assert daily_count(db) == 3
    bump_daily_count(db, 2)
    assert daily_count(db) == 5
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/test_eager_analysis_service.py -v`
Expected: FAIL — names not defined.

- [ ] **Step 3: Implement** (append to `eager_analysis_service.py`)

```python
from datetime import datetime, timezone
from app.models.platform_setting import PlatformSetting

_DAILY_KEY = "eager_analysis_daily_count"


def capacity_ok(queue_depth: int, rate_limits_5m: int, pool_util: float, ceiling: int) -> bool:
    """Pure backpressure gate. Any failing signal → defer this tick."""
    return queue_depth <= ceiling and rate_limits_5m <= 0 and pool_util <= 0.80


def gather_capacity(db: Session) -> tuple[int, int, float]:
    """Read the real capacity signals (mirrors GET /health/capacity)."""
    queue_depth = 0
    try:
        from app.core.redis_client import get_queue
        q = get_queue()
        queue_depth = int(q.count) if q is not None else 0
    except Exception:
        queue_depth = 0
    rate_limits = 0
    try:
        from app.core.llm_metrics import count_recent_rate_limits
        rate_limits = int(count_recent_rate_limits(minutes=5))
    except Exception:
        rate_limits = 0
    pool_util = 0.0
    try:
        from app.core.database import engine
        pool = engine.pool
        cap = (pool.size() + pool.overflow()) or 1
        pool_util = pool.checkedout() / cap
    except Exception:
        pool_util = 0.0
    return queue_depth, rate_limits, pool_util


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def daily_count(db: Session) -> int:
    row = db.query(PlatformSetting).filter(PlatformSetting.key == _DAILY_KEY).first()
    if not row or not row.value or ":" not in row.value:
        return 0
    day, _, n = row.value.partition(":")
    return int(n) if day == today_key() and n.isdigit() else 0


def bump_daily_count(db: Session, n: int) -> None:
    cur = daily_count(db)
    row = db.query(PlatformSetting).filter(PlatformSetting.key == _DAILY_KEY).first()
    val = f"{today_key()}:{cur + n}"
    if row:
        row.value = val
    else:
        db.add(PlatformSetting(key=_DAILY_KEY, value=val))
    db.commit()
```

- [ ] **Step 4: Run to verify it passes**

Run: same pytest command. Expected: PASS (all eager_analysis_service tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/eager_analysis_service.py drpl-backend/tests/services/test_eager_analysis_service.py
git commit -m "feat(analysis): capacity backpressure gate + daily-cap counter"
```

---

## Task 5: Per-tender eager-analysis job

**Files:**
- Create: `app/worker/eager_analysis_tasks.py`
- Test: `tests/worker/test_eager_analysis_tasks.py`

**Interfaces:**
- Consumes: `analyze_all_documents(db, tender_id)` (async, `app/services/tender_analysis_service.py`); `DocumentExtractionResult`.
- Produces: `eager_analyze_tender(tender_id: int) -> dict` — sets status `running`→`done`/`failed`, idempotent.

- [ ] **Step 1: Write the failing test**

```python
# tests/worker/test_eager_analysis_tasks.py
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.tender import Tender
from app.models.document_analysis import DocumentExtractionResult
import app.worker.eager_analysis_tasks as tasks

def _db_factory():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e)
    return sessionmaker(bind=e)

def _mk(SessionLocal, **kw):
    db = SessionLocal(); t = Tender(portal="ireps", tender_id="T1", title="t", **kw)
    db.add(t); db.commit(); tid = t.id; db.close(); return tid

def test_runs_analysis_and_marks_done(monkeypatch):
    SL = _db_factory()
    tid = _mk(SL, ai_relevance_score=0.9)
    monkeypatch.setattr(tasks, "SessionLocal", SL)
    async def _fake(db, tender_id):
        db.add(DocumentExtractionResult(tender_id=tender_id)); db.commit(); return object()
    monkeypatch.setattr(tasks, "analyze_all_documents", _fake)
    out = tasks.eager_analyze_tender(tid)
    assert out["status"] == "done"
    db = SL()
    assert db.query(Tender).get(tid).eager_analysis_status == "done"

def test_idempotent_skip_when_already_analyzed(monkeypatch):
    SL = _db_factory(); tid = _mk(SL, ai_relevance_score=0.9)
    db = SL(); db.add(DocumentExtractionResult(tender_id=tid)); db.commit(); db.close()
    monkeypatch.setattr(tasks, "SessionLocal", SL)
    called = {"n": 0}
    async def _fake(db, tender_id): called["n"] += 1
    monkeypatch.setattr(tasks, "analyze_all_documents", _fake)
    out = tasks.eager_analyze_tender(tid)
    assert out["status"] == "skipped" and called["n"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/worker/test_eager_analysis_tasks.py -v`
Expected: FAIL — module not found. (Create `tests/worker/__init__.py` if the dir is new.)

- [ ] **Step 3: Implement**

```python
# app/worker/eager_analysis_tasks.py
"""RQ entry points for eager tender analysis: per-tender job + self-rescheduling
sweep. Mirrors app/worker/auto_scoring_tasks.py — own SessionLocal per job,
self-reschedule, graceful no-Redis no-op."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.core.database import SessionLocal
from app.core.redis_client import get_queue
from app.core.run_context import run_id_scope
from app.models.tender import Tender
from app.models.document_analysis import DocumentExtractionResult
from app.services.tender_analysis_service import analyze_all_documents

log = logging.getLogger(__name__)


def _already_analyzed(db, tender_id: int) -> bool:
    return db.query(DocumentExtractionResult.id).filter(
        DocumentExtractionResult.tender_id == tender_id).first() is not None


def _set_status(db, tender_id: int, status: "str | None") -> None:
    t = db.query(Tender).get(tender_id)
    if t is not None:
        t.eager_analysis_status = status
        t.eager_analysis_at = datetime.now(timezone.utc)
        db.commit()


def eager_analyze_tender(tender_id: int) -> dict:
    """Run the v2 analyzer for one tender. Idempotent; status-tracked."""
    db = SessionLocal()
    try:
        if _already_analyzed(db, tender_id):
            _set_status(db, tender_id, "done")
            return {"tender_id": tender_id, "status": "skipped"}
        _set_status(db, tender_id, "running")
        try:
            with run_id_scope(f"eageranalyze-{tender_id}"):
                asyncio.run(analyze_all_documents(db, tender_id))
            _set_status(db, tender_id, "done")
            return {"tender_id": tender_id, "status": "done"}
        except Exception as e:  # noqa: BLE001
            log.warning("eager_analyze_tender(%s) failed: %s", tender_id, e)
            _set_status(db, tender_id, "failed")
            return {"tender_id": tender_id, "status": "failed", "error": str(e)}
    finally:
        db.close()
```

- [ ] **Step 4: Run to verify it passes**

Run: same pytest command. Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/worker/eager_analysis_tasks.py drpl-backend/tests/worker
git commit -m "feat(analysis): per-tender eager-analysis job (idempotent, status-tracked)"
```

---

## Task 6: The sweep + self-reschedule

**Files:**
- Modify: `app/worker/eager_analysis_tasks.py`
- Test: add to `tests/worker/test_eager_analysis_tasks.py`

**Interfaces:**
- Consumes: `find_eager_analysis_candidates`, `capacity_ok`, `gather_capacity`, `daily_count`, `bump_daily_count`, `get_eager_analysis_settings`.
- Produces: `enqueue_eager_analysis_sweep() -> dict`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_sweep_noop_when_disabled(monkeypatch):
    SL = _db_factory(); monkeypatch.setattr(tasks, "SessionLocal", SL)
    monkeypatch.setattr(tasks, "get_queue", lambda: object())  # queue "present"
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {"eager_analysis_enabled": False})
    out = tasks.enqueue_eager_analysis_sweep()
    assert out["reason"] == "disabled"

def test_sweep_enqueues_candidates_and_marks_queued(monkeypatch):
    SL = _db_factory()
    tid = _mk(SL, ai_relevance_score=0.9)
    monkeypatch.setattr(tasks, "SessionLocal", SL)
    enqueued = []
    class Q:
        count = 0
        def enqueue(self, path, *a, **k): enqueued.append((path, a)); return object()
        def enqueue_in(self, *a, **k): return object()
    monkeypatch.setattr(tasks, "get_queue", lambda: Q())
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {
        "eager_analysis_enabled": True, "eager_analysis_interval_seconds": 180,
        "eager_analysis_daily_cap": 200, "eager_analysis_queue_ceiling": 20})
    monkeypatch.setattr(tasks, "find_eager_analysis_candidates", lambda db: [tid])
    monkeypatch.setattr(tasks, "gather_capacity", lambda db: (0, 0, 0.1))
    out = tasks.enqueue_eager_analysis_sweep()
    assert out["enqueued"] == 1 and enqueued
    db = SL(); assert db.query(Tender).get(tid).eager_analysis_status == "queued"

def test_sweep_skips_on_backpressure(monkeypatch):
    SL = _db_factory(); monkeypatch.setattr(tasks, "SessionLocal", SL)
    monkeypatch.setattr(tasks, "get_queue", lambda: type("Q", (), {"count": 0, "enqueue_in": lambda *a, **k: None})())
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {
        "eager_analysis_enabled": True, "eager_analysis_interval_seconds": 180,
        "eager_analysis_daily_cap": 200, "eager_analysis_queue_ceiling": 20})
    monkeypatch.setattr(tasks, "gather_capacity", lambda db: (99, 0, 0.1))  # queue too deep
    out = tasks.enqueue_eager_analysis_sweep()
    assert out["reason"] == "backpressure" and out["enqueued"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/worker/test_eager_analysis_tasks.py -v`
Expected: FAIL — `enqueue_eager_analysis_sweep` not defined.

- [ ] **Step 3: Implement** (append; add the imports at the top of the file)

```python
# add to the imports block at the top of app/worker/eager_analysis_tasks.py:
from app.services.eager_analysis_settings import get_eager_analysis_settings
from app.services.eager_analysis_service import (
    find_eager_analysis_candidates, capacity_ok, gather_capacity,
    daily_count, bump_daily_count,
)

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
    the next tick retries."""
    q = get_queue()
    if q is None:
        log.info("eager-analysis sweep: queue unavailable — not running")
        return {"rescheduled": False, "reason": "no_queue", "enqueued": 0}

    db = SessionLocal()
    try:
        s = get_eager_analysis_settings(db)
        interval = s.get("eager_analysis_interval_seconds", 180)
        if not s["eager_analysis_enabled"]:
            _reschedule_sweep(interval)
            return {"rescheduled": True, "reason": "disabled", "enqueued": 0}

        depth, rl, pool = gather_capacity(db)
        if not capacity_ok(depth, rl, pool, s["eager_analysis_queue_ceiling"]):
            _reschedule_sweep(interval)
            return {"rescheduled": True, "reason": "backpressure", "enqueued": 0}

        remaining = s["eager_analysis_daily_cap"] - daily_count(db)
        if remaining <= 0:
            _reschedule_sweep(interval)
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
    finally:
        db.close()

    _reschedule_sweep(interval)
    return {"rescheduled": True, "reason": "ok", "enqueued": n}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/worker/test_eager_analysis_tasks.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/worker/eager_analysis_tasks.py drpl-backend/tests/worker/test_eager_analysis_tasks.py
git commit -m "feat(analysis): self-rescheduling eager-analysis sweep with backpressure + cap"
```

---

## Task 7: Bootstrap + startup orphan reaper

**Files:**
- Modify: `app/services/seed_scheduled_jobs.py`
- Modify: `app/main.py`
- Test: `tests/services/test_eager_analysis_orphan_reaper.py`

**Interfaces:**
- Consumes: `Tender.eager_analysis_status`.
- Produces: `reap_stuck_eager_analysis(db, older_than_minutes: int = 30) -> int` (in `app/services/eager_analysis_service.py`) — flips orphaned `queued`/`running` markers to `null`, returns count.

- [ ] **Step 1: Bootstrap the sweep**

In `app/services/seed_scheduled_jobs.py`, next to the `reap_unscored_tenders` first-enqueue (search for `auto_scoring_tasks.reap_unscored_tenders`), add an analogous first-enqueue:

```python
        try:
            q.enqueue_in(timedelta(minutes=1),
                         "app.worker.eager_analysis_tasks.enqueue_eager_analysis_sweep",
                         job_id="drpl-eager-analysis-sweep", result_ttl=86400)
        except Exception as e:  # noqa: BLE001
            log.warning("seed_scheduled_jobs: could not seed eager-analysis sweep: %s", e)
```

(Match the surrounding style — the exact `log`/guard pattern used by the neighboring seeds.)

- [ ] **Step 2: Write the failing test for the orphan reaper**

```python
# tests/services/test_eager_analysis_orphan_reaper.py
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.tender import Tender
from app.services.eager_analysis_service import reap_stuck_eager_analysis

def _db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e); return sessionmaker(bind=e)()

def test_reaps_stale_running_markers():
    db = _db()
    old = Tender(portal="ireps", tender_id="A", title="a", eager_analysis_status="running",
                 eager_analysis_at=datetime.now(timezone.utc) - timedelta(hours=2))
    fresh = Tender(portal="ireps", tender_id="B", title="b", eager_analysis_status="running",
                   eager_analysis_at=datetime.now(timezone.utc))
    done = Tender(portal="ireps", tender_id="C", title="c", eager_analysis_status="done")
    db.add_all([old, fresh, done]); db.commit()
    n = reap_stuck_eager_analysis(db, older_than_minutes=30)
    assert n == 1
    db.refresh(old); db.refresh(fresh); db.refresh(done)
    assert old.eager_analysis_status is None      # stale → reset
    assert fresh.eager_analysis_status == "running"  # fresh → kept
    assert done.eager_analysis_status == "done"      # terminal → untouched
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/test_eager_analysis_orphan_reaper.py -v`
Expected: FAIL — `reap_stuck_eager_analysis` not defined.

- [ ] **Step 4: Implement the reaper + wire it at startup**

Append to `app/services/eager_analysis_service.py`:

```python
def reap_stuck_eager_analysis(db: Session, older_than_minutes: int = 30) -> int:
    """Reset orphaned queued/running markers (worker killed mid-analysis) so the
    sweep can pick the tender up again. Terminal states (done/failed) untouched."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    rows = (db.query(Tender)
            .filter(Tender.eager_analysis_status.in_(["queued", "running"]))
            .filter((Tender.eager_analysis_at.is_(None)) | (Tender.eager_analysis_at < cutoff))
            .all())
    for t in rows:
        t.eager_analysis_status = None
    if rows:
        db.commit()
    return len(rows)
```

Then call it at backend startup. In `app/main.py`, find `_reap_stuck_executions_on_startup` and add a sibling call in the same startup block (open a `SessionLocal`, call `reap_stuck_eager_analysis(db)`, log the count, close). Match the exact session/try-finally style of the existing reaper.

- [ ] **Step 5: Run to verify it passes**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/test_eager_analysis_orphan_reaper.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/seed_scheduled_jobs.py drpl-backend/app/services/eager_analysis_service.py drpl-backend/app/main.py drpl-backend/tests/services/test_eager_analysis_orphan_reaper.py
git commit -m "feat(analysis): bootstrap sweep + startup orphan reaper for eager analysis"
```

---

## Task 8: End-to-end verification

**Files:**
- Test: `tests/test_eager_analysis_e2e.py`

- [ ] **Step 1: Write the end-to-end test**

Drives the real selection → sweep → per-tender job wiring with the analyzer stubbed (the analyzer itself is exercised elsewhere; here we verify the orchestration).

```python
# tests/test_eager_analysis_e2e.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.tender import Tender
from app.models.tender import TenderDocument
from app.models.document_analysis import DocumentExtractionResult
import app.worker.eager_analysis_tasks as tasks
from app.services.eager_analysis_service import find_eager_analysis_candidates

def _SL():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e); return sessionmaker(bind=e)

def test_in_scope_with_docs_is_selected_then_analyzed(monkeypatch):
    SL = _SL(); db = SL()
    t = Tender(portal="ireps", tender_id="E1", title="e", ai_relevance_score=0.9)
    db.add(t); db.commit(); tid = t.id
    db.add(TenderDocument(tender_id=tid, file_name="d.pdf", file_path="p")); db.commit()

    assert find_eager_analysis_candidates(db) == [tid]      # selected

    monkeypatch.setattr(tasks, "SessionLocal", SL)
    async def _fake(db, tender_id):
        db.add(DocumentExtractionResult(tender_id=tender_id)); db.commit()
    monkeypatch.setattr(tasks, "analyze_all_documents", _fake)
    out = tasks.eager_analyze_tender(tid)
    assert out["status"] == "done"

    db2 = SL()
    assert db2.query(Tender).get(tid).eager_analysis_status == "done"
    assert find_eager_analysis_candidates(db2) == []        # now excluded (analyzed)
```

- [ ] **Step 2: Run to verify it passes** (after Tasks 1–6 are in)

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/test_eager_analysis_e2e.py tests/services/test_eager_analysis*.py tests/worker/test_eager_analysis_tasks.py -v`
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add drpl-backend/tests/test_eager_analysis_e2e.py
git commit -m "test(analysis): end-to-end eager-analysis orchestration"
```

---

## Self-Review

- **Spec coverage:** gate 0.70/eligible → Task 3; sequential/concurrency + backpressure → Tasks 4,6; flag-off no-op → Tasks 1,6; daily cap → Tasks 4,6; idempotency → Tasks 3,5; in-flight marker + orphan recovery → Tasks 2,5,6,7; sweep mirrors reaper → Task 6; bootstrap → Task 7; config → Task 1; verification → Task 8. All spec sections covered.
- **Type consistency:** `get_eager_analysis_settings(db)->dict` (T1) consumed by T3,4,6; `find_eager_analysis_candidates(db,limit)->list[int]` (T3) consumed by T6,8; `capacity_ok/gather_capacity/daily_count/bump_daily_count` (T4) consumed by T6; `eager_analyze_tender(tid)->dict` and `enqueue_eager_analysis_sweep()->dict` (T5,6) referenced by T7 bootstrap + T8; `reap_stuck_eager_analysis(db, older_than_minutes)` (T7). `Tender.eager_analysis_status/at` (T2) used throughout. Consistent.
- **Placeholder note:** model/migration/self-heal (T2) and the two startup wirings (T7: `seed_scheduled_jobs.py` bootstrap, `main.py` startup reaper call) give exact code + exact anchor functions but ask the implementer to match surrounding style because those large files' exact formatting is read at implementation time. All new units (settings, service, tasks) are fully coded and TDD'd. Verify model import paths for `DocumentExtractionResult`/`TenderDocument` at the start of Task 3 (grep given).
