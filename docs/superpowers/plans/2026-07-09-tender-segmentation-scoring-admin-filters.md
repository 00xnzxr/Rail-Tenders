# Tender Segmentation, Scoring Admin, Filters & Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Categorize scored tenders into To Bid / Not Bidable / Discarded (auto with manual override), weekly-archive stale Discarded tenders, expose a scoring-agent admin control panel with DB-backed live settings, add Segment + range/eligibility filters, and switch the tenders list to a `{items, total}` response with 100/page full pagination.

**Architecture:** A pure `compute_segment` helper (score+value → segment) drives a new `Tender.segment` column set during scoring and recomputable in batch; a persistent `segment_overridden` flag protects manual moves. The `auto_scoring_*` knobs move from static `config.py` into `PlatformSetting` rows (read live with config fallback), which a new MasterAdmin routes group + admin page manage. The list API gains a shared filter helper + `count_tenders` and returns `{items, total}`.

**Tech Stack:** FastAPI + SQLAlchemy + RQ (backend), React 18 + TypeScript + Vite + Tailwind + shadcn (frontend), pytest.

## Global Constraints

- Score thresholds are stored as **0–1 fractions** (discard `0.40`, bidable `0.60`) so they compare directly to `Tender.ai_relevance_score` (0–1). The admin UI shows them as percentages.
- Segment boundaries (exact): `score is None`→`None`; `score < discard_below`→`discarded`; `discard_below ≤ score < bidable_at`→`not_bidable`; `score ≥ bidable_at` AND (`value is None` OR `value ≥ value_threshold`)→`to_bid`; `score ≥ bidable_at` AND `value < value_threshold`→`not_bidable`.
- Value threshold default `value_threshold_inr = 5_000_000` (₹50 lakh). Value unknown/null with a high score → `to_bid` (never penalized).
- Auto-discard: only `segment=="discarded"` AND `created_at < now-7d` AND `segment_overridden==False` AND `workflow_status=="new"` AND `is_archived==False` → `is_archived=True` (reversible, nothing deleted).
- New `Tender` columns need an Alembic revision AND entries in `_apply_schema_drift_fixes()` (Postgres, `app/main.py`) AND `_add_missing_columns()` (SQLite, `app/main.py`).
- Settings live in `PlatformSetting` (table via `settings_service.py`), category `"auto_scoring"`. `settings_service.set_setting` RAISES if the key is absent — the new writer must UPSERT (insert row when missing), mirroring the digest seeder's direct-`PlatformSetting` approach.
- Self-rescheduling RQ jobs mirror `app/worker/scheduled_tasks.py` (`scan_closing_dates`/`_reschedule_closing_scan`) + priming in `app/services/seed_scheduled_jobs.py` (idempotent via `_job_already_pending`). No-op when `get_queue()` is None.
- Admin routes/pages are **MasterAdmin-only** (`require_master_admin` backend dep if present, else `require_admin`; `MasterAdminRoute` frontend guard).
- Backend tests: `pytest tests/` from `drpl-backend/`. Two pre-existing failures (`test_extension_config_requires_auth`, `test_tender_upload_requires_auth`, 401 vs 403) are known/unrelated. Frontend build: `npm run build` from `drpl-frontend/`.
- Commit after each task. End commit messages with the repo's Co-Authored-By line.

---

## Task 1: Schema — `segment` + `segment_overridden` columns

**Files:**
- Modify: `drpl-backend/app/models/tender.py` (add 2 columns to `Tender`)
- Modify: `drpl-backend/app/main.py` (`_apply_schema_drift_fixes` list ~line 162, `_add_missing_columns` list ~line 532)
- Create: `drpl-backend/alembic/versions/20260709_tender_segment.py`
- Test: `drpl-backend/tests/test_tender_segment_schema.py`

**Interfaces:**
- Produces: `Tender.segment` (String(20), nullable, default None), `Tender.segment_overridden` (Boolean, default False, not null).

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_tender_segment_schema.py`:

```python
"""Tender carries segment + segment_overridden columns."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_segment_defaults():
    db = _session()
    t = Tender(portal="gem", tender_id="1", title="X")
    db.add(t); db.commit(); db.refresh(t)
    assert t.segment is None
    assert t.segment_overridden is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_tender_segment_schema.py -v`
Expected: FAIL — `AttributeError: 'Tender' object has no attribute 'segment'`.

- [ ] **Step 3: Add columns to the model**

In `drpl-backend/app/models/tender.py`, near the other lifecycle booleans (e.g. after `below_threshold`/`scoring_attempts`), add:

```python
    segment = Column(String(20), nullable=True)                        # to_bid | not_bidable | discarded | None(unscored)
    segment_overridden = Column(Boolean, default=False, nullable=False) # True once a user manually sets the segment
```

(`String`, `Boolean` are already imported in this file.)

- [ ] **Step 4: Add self-heal migrations**

In `drpl-backend/app/main.py`, append to the `_apply_schema_drift_fixes` statements list (after the `scoring_attempts` line):

```python
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS segment VARCHAR(20)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS segment_overridden BOOLEAN DEFAULT false",
```

And append to the `_add_missing_columns` migrations list (after the `scoring_attempts` entry):

```python
        ("tenders", "segment", "VARCHAR(20)"),
        ("tenders", "segment_overridden", "BOOLEAN DEFAULT FALSE"),
```

- [ ] **Step 5: Add the Alembic revision**

Find the current head: `cd drpl-backend && alembic heads` (or read the latest `alembic/versions/` file's `revision = "..."`). Create `drpl-backend/alembic/versions/20260709_tender_segment.py`:

```python
"""tender segment + segment_overridden

Revision ID: 20260709_tender_segment
Revises: <CURRENT_HEAD>
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa

revision = "20260709_tender_segment"
down_revision = "<CURRENT_HEAD>"   # replace with the actual current head id
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tenders", sa.Column("segment", sa.String(length=20), nullable=True))
    op.add_column("tenders", sa.Column("segment_overridden", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column("tenders", "segment_overridden")
    op.drop_column("tenders", "segment")
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd drpl-backend && python -m pytest tests/test_tender_segment_schema.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/models/tender.py drpl-backend/app/main.py drpl-backend/alembic/versions/20260709_tender_segment.py drpl-backend/tests/test_tender_segment_schema.py
git commit -m "feat(tenders): add segment + segment_overridden columns

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Config defaults for segmentation + cleanup

**Files:**
- Modify: `drpl-backend/app/core/config.py` (add fields to `Settings`, near the `auto_scoring_*` block)
- Test: `drpl-backend/tests/test_segment_config.py`

**Interfaces:**
- Produces: `settings.segment_discard_below` (0.40), `settings.segment_bidable_at` (0.60), `settings.auto_discard_enabled` (True), `settings.auto_discard_days` (7), `settings.auto_discard_interval_hours` (24).

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_segment_config.py`:

```python
from app.core.config import get_settings


def test_segment_config_defaults():
    s = get_settings()
    assert s.segment_discard_below == 0.40
    assert s.segment_bidable_at == 0.60
    assert s.auto_discard_enabled is True
    assert s.auto_discard_days == 7
    assert s.auto_discard_interval_hours == 24
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_segment_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'segment_discard_below'`.

- [ ] **Step 3: Add config fields**

In `drpl-backend/app/core/config.py`, after the existing `value_threshold_inr` line, add:

```python
    # Tender segmentation thresholds (score is 0-1)
    segment_discard_below: float = 0.40   # score < this -> discarded
    segment_bidable_at: float = 0.60      # score >= this (and value ok) -> to_bid
    # Weekly auto-discard cleanup
    auto_discard_enabled: bool = True
    auto_discard_days: int = 7
    auto_discard_interval_hours: int = 24
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && python -m pytest tests/test_segment_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/core/config.py drpl-backend/tests/test_segment_config.py
git commit -m "feat(scoring): segmentation + auto-discard config defaults

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `compute_segment` pure helper

**Files:**
- Modify: `drpl-backend/app/services/auto_scoring_helpers.py` (add `compute_segment`)
- Test: `drpl-backend/tests/test_compute_segment.py`

**Interfaces:**
- Produces: `compute_segment(score, estimated_value, *, discard_below, bidable_at, value_threshold) -> str | None`. Returns `"to_bid"` / `"not_bidable"` / `"discarded"` / `None`.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_compute_segment.py`:

```python
from app.services.auto_scoring_helpers import compute_segment

KW = dict(discard_below=0.40, bidable_at=0.60, value_threshold=5_000_000)


def test_unscored_is_none():
    assert compute_segment(None, 9_000_000, **KW) is None


def test_low_score_discarded():
    assert compute_segment(0.30, 9_000_000, **KW) == "discarded"


def test_mid_score_not_bidable():
    assert compute_segment(0.50, 9_000_000, **KW) == "not_bidable"


def test_high_score_high_value_to_bid():
    assert compute_segment(0.80, 9_000_000, **KW) == "to_bid"


def test_high_score_low_value_not_bidable():
    assert compute_segment(0.80, 4_000_000, **KW) == "not_bidable"


def test_high_score_unknown_value_to_bid():
    assert compute_segment(0.80, None, **KW) == "to_bid"


def test_boundaries():
    # exactly discard_below -> not_bidable (>= discard_below, < bidable_at)
    assert compute_segment(0.40, 9_000_000, **KW) == "not_bidable"
    # exactly bidable_at with ok value -> to_bid
    assert compute_segment(0.60, 9_000_000, **KW) == "to_bid"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_compute_segment.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_segment'`.

- [ ] **Step 3: Add the helper**

Append to `drpl-backend/app/services/auto_scoring_helpers.py`:

```python
def compute_segment(
    score: "float | None",
    estimated_value: "float | None",
    *,
    discard_below: float,
    bidable_at: float,
    value_threshold: float,
) -> "str | None":
    """Bucket a tender by AI score + value. score is 0-1; None score -> None."""
    if score is None:
        return None
    if score < discard_below:
        return "discarded"
    if score < bidable_at:
        return "not_bidable"
    # score >= bidable_at
    if estimated_value is None or estimated_value >= value_threshold:
        return "to_bid"
    return "not_bidable"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && python -m pytest tests/test_compute_segment.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/auto_scoring_helpers.py drpl-backend/tests/test_compute_segment.py
git commit -m "feat(scoring): compute_segment helper (score+value -> segment)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: DB-backed scoring settings (`get_scoring_settings` / `set_scoring_setting`)

**Files:**
- Create: `drpl-backend/app/services/auto_scoring_settings.py`
- Test: `drpl-backend/tests/test_auto_scoring_settings.py`

**Interfaces:**
- Consumes: `settings_service.get_setting_value`; the `PlatformSetting` model.
- Produces:
  - `get_scoring_settings(db) -> dict` — every knob, reading `PlatformSetting` with config default fallback. Keys: `auto_scoring_enabled, auto_scoring_model, auto_scoring_interval_seconds, auto_scoring_batch_size, auto_scoring_max_retries, value_threshold_inr, segment_discard_below, segment_bidable_at, auto_discard_enabled, auto_discard_days, auto_discard_interval_hours`.
  - `set_scoring_setting(db, key, value, updated_by=None) -> None` — UPSERT into `PlatformSetting` (insert row if key absent, else update), category `"auto_scoring"`.

- [ ] **Step 1: Inspect PlatformSetting columns**

Read `drpl-backend/app/models/platform_setting.py` to confirm columns (`key`, `value`, and whether `value_type`/`category` are non-nullable). This informs what `set_scoring_setting` must set on insert. Inspection only.

- [ ] **Step 2: Write the failing test**

Create `drpl-backend/tests/test_auto_scoring_settings.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.platform_setting import PlatformSetting  # noqa: F401 (register table)
from app.services.auto_scoring_settings import get_scoring_settings, set_scoring_setting


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_defaults_from_config_when_unset():
    db = _session()
    s = get_scoring_settings(db)
    assert s["auto_scoring_enabled"] is True
    assert s["segment_discard_below"] == 0.40
    assert s["segment_bidable_at"] == 0.60
    assert s["value_threshold_inr"] == 5_000_000
    assert s["auto_discard_days"] == 7


def test_set_then_get_roundtrip_upserts():
    db = _session()
    set_scoring_setting(db, "segment_bidable_at", 0.70)   # key does not pre-exist -> insert
    set_scoring_setting(db, "auto_scoring_enabled", False)
    s = get_scoring_settings(db)
    assert s["segment_bidable_at"] == 0.70
    assert s["auto_scoring_enabled"] is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.auto_scoring_settings'`.

- [ ] **Step 4: Write the module**

Create `drpl-backend/app/services/auto_scoring_settings.py`:

```python
"""DB-backed, live-read scoring/segmentation settings with config-default fallback."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.platform_setting import PlatformSetting

# key -> config attribute providing the default
_KEYS = {
    "auto_scoring_enabled": "auto_scoring_enabled",
    "auto_scoring_model": "auto_scoring_model",
    "auto_scoring_interval_seconds": "auto_scoring_interval_seconds",
    "auto_scoring_batch_size": "auto_scoring_batch_size",
    "auto_scoring_max_retries": "auto_scoring_max_retries",
    "value_threshold_inr": "value_threshold_inr",
    "segment_discard_below": "segment_discard_below",
    "segment_bidable_at": "segment_bidable_at",
    "auto_discard_enabled": "auto_discard_enabled",
    "auto_discard_days": "auto_discard_days",
    "auto_discard_interval_hours": "auto_discard_interval_hours",
}


def _coerce(raw: str, default):
    """Coerce a stored string back to the type of its config default."""
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return raw


def get_scoring_settings(db: Session) -> dict:
    cfg = get_settings()
    rows = {r.key: r.value for r in db.query(PlatformSetting).filter(
        PlatformSetting.key.in_(list(_KEYS))).all()}
    out = {}
    for key, attr in _KEYS.items():
        default = getattr(cfg, attr)
        out[key] = _coerce(rows[key], default) if key in rows and rows[key] is not None else default
    return out


def set_scoring_setting(db: Session, key: str, value, updated_by: "int | None" = None) -> None:
    if key not in _KEYS:
        raise ValueError(f"unknown scoring setting: {key}")
    stored = "true" if value is True else "false" if value is False else str(value)
    row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    if row is None:
        row = PlatformSetting(key=key, value=stored, value_type="string", category="auto_scoring")
        if updated_by is not None and hasattr(row, "updated_by"):
            row.updated_by = updated_by
        db.add(row)
    else:
        row.value = stored
        if updated_by is not None and hasattr(row, "updated_by"):
            row.updated_by = updated_by
    db.commit()
```

Note: if `PlatformSetting` requires other non-null columns (found in Step 1), set them on insert too. The `_coerce` boolean/int/float logic keys off the config default's Python type, so stored strings round-trip correctly.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_settings.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/auto_scoring_settings.py drpl-backend/tests/test_auto_scoring_settings.py
git commit -m "feat(scoring): DB-backed live scoring settings with config fallback

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Set segment during scoring + `resegment_all` + read settings live

**Files:**
- Modify: `drpl-backend/app/services/auto_scoring_service.py` (`_score_and_apply` sets segment; add `resegment_all`; read thresholds via `get_scoring_settings`)
- Test: `drpl-backend/tests/test_segment_apply.py`

**Interfaces:**
- Consumes: `compute_segment` (Task 3), `get_scoring_settings` (Task 4).
- Produces: `resegment_all(db) -> dict` — recomputes `segment` for every scored, non-overridden tender; returns `{"resegmented": int}`. Scoring writes `tender.segment` unless `segment_overridden`.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_segment_apply.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.models.platform_setting import PlatformSetting  # noqa: F401
from app.services.auto_scoring_service import resegment_all


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_resegment_sets_segment_and_skips_overridden():
    db = _session()
    a = Tender(portal="gem", tender_id="1", title="A",
               ai_relevance_score=0.80, estimated_value=9_000_000)   # -> to_bid
    b = Tender(portal="gem", tender_id="2", title="B",
               ai_relevance_score=0.30, estimated_value=9_000_000)   # -> discarded
    c = Tender(portal="gem", tender_id="3", title="C",
               ai_relevance_score=0.30, estimated_value=9_000_000,
               segment="to_bid", segment_overridden=True)           # user override -> untouched
    d = Tender(portal="gem", tender_id="4", title="D")               # unscored -> stays None
    db.add_all([a, b, c, d]); db.commit()

    out = resegment_all(db)
    for t in (a, b, c, d):
        db.refresh(t)
    assert a.segment == "to_bid"
    assert b.segment == "discarded"
    assert c.segment == "to_bid"          # override respected
    assert d.segment is None              # unscored untouched
    assert out["resegmented"] >= 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_segment_apply.py -v`
Expected: FAIL — `ImportError: cannot import name 'resegment_all'`.

- [ ] **Step 3: Implement**

In `drpl-backend/app/services/auto_scoring_service.py`:

Add imports at top (near the existing helper imports):

```python
from app.services.auto_scoring_helpers import parse_score_reply, is_below_threshold, compute_segment
from app.services.auto_scoring_settings import get_scoring_settings
```

In `score_tenders_batch`, the batch already computes `settings = get_settings()`. Replace that with a live read and thread the thresholds:

```python
    scoring = get_scoring_settings(db)
```

Then, inside `_score_and_apply`, after `below_threshold` is set and before/around the counters, set the segment unless overridden:

```python
            if not t.segment_overridden:
                t.segment = compute_segment(
                    t.ai_relevance_score, t.estimated_value,
                    discard_below=scoring["segment_discard_below"],
                    bidable_at=scoring["segment_bidable_at"],
                    value_threshold=scoring["value_threshold_inr"],
                )
```

(Keep the existing `model_override`/`value_threshold_inr` usages working — where the code read `settings.auto_scoring_model` / `settings.value_threshold_inr`, read `scoring["auto_scoring_model"]` / `scoring["value_threshold_inr"]` instead. `_user_prompt` and the semaphore count also read from `scoring[...]`. If any `get_settings()` reference remains needed, keep it, but the scoring knobs come from `scoring`.)

Add the batch resegmenter (module-level function):

```python
def resegment_all(db) -> dict:
    """Recompute `segment` for every scored, non-overridden tender using current thresholds."""
    scoring = get_scoring_settings(db)
    q = (db.query(Tender)
         .filter(Tender.ai_relevance_score.isnot(None))
         .filter(Tender.segment_overridden == False))  # noqa: E712
    n = 0
    for t in q.all():
        t.segment = compute_segment(
            t.ai_relevance_score, t.estimated_value,
            discard_below=scoring["segment_discard_below"],
            bidable_at=scoring["segment_bidable_at"],
            value_threshold=scoring["value_threshold_inr"],
        )
        n += 1
    db.commit()
    return {"resegmented": n}
```

Also: the reaper stamps last-run — in `auto_scoring_tasks.reap_unscored_tenders` (or `score_tenders_batch`), after a successful batch, set a `PlatformSetting` `auto_scoring_last_run` to `datetime.now(timezone.utc).isoformat()` via `set_scoring_setting`-style upsert. (Add a tiny helper `_stamp_last_run(db)` in `auto_scoring_service.py` that upserts key `auto_scoring_last_run`; call it at the end of `score_tenders_batch`. This is read by the admin stats route in Task 7.) `auto_scoring_last_run` is NOT in `_KEYS`, so upsert it directly against `PlatformSetting` in `_stamp_last_run`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd drpl-backend && python -m pytest tests/test_segment_apply.py tests/test_auto_scoring_service.py -v`
Expected: PASS (new test + the existing service tests still green — the existing tests stub `get_scoring_system_prompt`/`call_ai`; ensure the live-settings read has a config fallback so they pass without seeded PlatformSetting rows).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/auto_scoring_service.py drpl-backend/tests/test_segment_apply.py
git commit -m "feat(scoring): set segment during scoring + resegment_all + live settings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Segment override endpoint + weekly auto-discard job

**Files:**
- Modify: `drpl-backend/app/api/routes/tenders.py` (add `POST /tenders/{id}/segment`)
- Modify: `drpl-backend/app/worker/scheduled_tasks.py` (add `scan_discarded_tenders` + `_reschedule_discard_cleanup`)
- Modify: `drpl-backend/app/services/seed_scheduled_jobs.py` (prime `drpl-discard-cleanup`)
- Test: `drpl-backend/tests/test_discard_cleanup.py`, `drpl-backend/tests/test_segment_override.py`

**Interfaces:**
- Consumes: `get_scoring_settings` (Task 4).
- Produces: `scan_discarded_tenders() -> dict` (`{"archived": int}`); `POST /tenders/{id}/segment` body `{segment}` sets `segment` + `segment_overridden=True`.

- [ ] **Step 1: Write the failing tests**

Create `drpl-backend/tests/test_discard_cleanup.py`:

```python
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.models.platform_setting import PlatformSetting  # noqa: F401
import app.worker.scheduled_tasks as st


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _mk(db, tid, **kw):
    t = Tender(portal="gem", tender_id=tid, title=tid, **kw)
    db.add(t); db.commit()
    old = datetime.now(timezone.utc) - timedelta(days=10)
    db.query(Tender).filter(Tender.id == t.id).update({Tender.created_at: old})
    db.commit(); db.refresh(t)
    return t


def test_archives_only_stale_discarded(monkeypatch):
    db = _session()
    monkeypatch.setattr(st, "SessionLocal", lambda: db)
    # eligible: discarded, old, workflow new, not overridden
    a = _mk(db, "a", segment="discarded", workflow_status="new")
    # protected: overridden
    b = _mk(db, "b", segment="discarded", workflow_status="new", segment_overridden=True)
    # protected: workflow moved
    c = _mk(db, "c", segment="discarded", workflow_status="in_progress")
    # not discarded
    d = _mk(db, "d", segment="not_bidable", workflow_status="new")

    out = st._run_discard_cleanup(db)  # pure-ish worker body, no queue
    for t in (a, b, c, d): db.refresh(t)
    assert a.is_archived is True
    assert b.is_archived is False
    assert c.is_archived is False
    assert d.is_archived is False
    assert out["archived"] == 1
```

Create `drpl-backend/tests/test_segment_override.py`:

```python
from fastapi.testclient import TestClient


def test_segment_override_route_shape():
    # Route exists and requires auth (403 without token), proving it's mounted.
    from app.main import app
    client = TestClient(app)
    r = client.post("/api/tenders/1/segment", json={"segment": "to_bid"})
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd drpl-backend && python -m pytest tests/test_discard_cleanup.py tests/test_segment_override.py -v`
Expected: FAIL — `_run_discard_cleanup` missing / route 404.

- [ ] **Step 3: Implement the cleanup job**

In `drpl-backend/app/worker/scheduled_tasks.py`, add a pure body + wrapper mirroring `scan_closing_dates`:

```python
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
    for t in rows:
        t.is_archived = True
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
```

(Uses the file's existing `log`, `SessionLocal`, `get_queue`, `timedelta` — confirm `timedelta` is imported at the top of the file; it is, per `scan_closing_dates`.)

- [ ] **Step 4: Prime it at startup**

In `drpl-backend/app/services/seed_scheduled_jobs.py`, after the auto-scoring-reaper priming block (before `return out`), add:

```python
    # Discard cleanup: first run in 5 minutes, then every auto_discard_interval_hours.
    if settings.auto_discard_enabled and not _job_already_pending(q, "drpl-discard-cleanup"):
        try:
            q.enqueue_in(timedelta(minutes=5), "app.worker.scheduled_tasks.scan_discarded_tenders",
                         job_id="drpl-discard-cleanup", result_ttl=86400)
            out["scheduled"].append({"job": "drpl-discard-cleanup", "in": "5 minutes"})
            log.info("seed_scheduled_jobs: queued first discard cleanup in 5 minutes")
        except Exception as e:
            log.warning("seed_scheduled_jobs: could not seed discard cleanup: %s", e)
```

(`settings` = `get_settings()` already in scope; `timedelta` already imported.)

- [ ] **Step 5: Implement the override route**

In `drpl-backend/app/api/routes/tenders.py`, add (mirroring the existing `update_tender`/assign routes' auth):

```python
from pydantic import BaseModel

class SegmentOverrideInput(BaseModel):
    segment: str

@router.post("/{tender_id}/segment")
def override_segment(
    tender_id: int,
    body: SegmentOverrideInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.segment not in ("to_bid", "not_bidable", "discarded"):
        raise HTTPException(status_code=400, detail="invalid segment")
    t = db.query(Tender).filter(Tender.id == tender_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="tender not found")
    t.segment = body.segment
    t.segment_overridden = True
    db.commit()
    return {"id": t.id, "segment": t.segment, "segment_overridden": True}
```

(Import `HTTPException`, `BaseModel`, `Tender` if not already imported in the file — they are, except confirm `BaseModel`; if absent add `from pydantic import BaseModel`.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd drpl-backend && python -m pytest tests/test_discard_cleanup.py tests/test_segment_override.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/api/routes/tenders.py drpl-backend/app/worker/scheduled_tasks.py drpl-backend/app/services/seed_scheduled_jobs.py drpl-backend/tests/test_discard_cleanup.py drpl-backend/tests/test_segment_override.py
git commit -m "feat(tenders): segment override route + weekly discard-cleanup job

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Scoring-admin backend routes

**Files:**
- Create: `drpl-backend/app/api/routes/tender_scoring_admin.py`
- Modify: `drpl-backend/app/main.py` (mount the router)
- Test: `drpl-backend/tests/test_scoring_admin_routes.py`

**Interfaces:**
- Consumes: `get_scoring_settings`/`set_scoring_setting` (Task 4), `resegment_all` (Task 5), digest seeder.
- Produces: `GET/PUT /admin/tender-scoring/settings`, `GET /admin/tender-scoring/stats`, `GET /admin/tender-scoring/digest`, `POST /admin/tender-scoring/digest/regenerate`.

- [ ] **Step 1: Inspect the admin auth dependency**

Grep for the master-admin FastAPI dependency: `grep -rn "require_master_admin\|require_admin\|master_admin" drpl-backend/app/core/auth.py drpl-backend/app/api/routes/admin*.py | head`. Use `require_master_admin` if it exists; else `require_admin`. Inspection only.

- [ ] **Step 2: Write the failing test**

Create `drpl-backend/tests/test_scoring_admin_routes.py`:

```python
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_settings_route_requires_auth():
    r = client.get("/api/admin/tender-scoring/settings")
    assert r.status_code in (401, 403)


def test_stats_route_requires_auth():
    r = client.get("/api/admin/tender-scoring/stats")
    assert r.status_code in (401, 403)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_scoring_admin_routes.py -v`
Expected: FAIL — 404 (routes not mounted).

- [ ] **Step 4: Write the routes**

Create `drpl-backend/app/api/routes/tender_scoring_admin.py` (use the real admin dependency from Step 1 — shown as `require_admin`):

```python
"""Admin control panel for the auto tender-scoring agent."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_admin  # or require_master_admin per Step 1
from app.models.user import User
from app.models.tender import Tender
from app.models.platform_setting import PlatformSetting
from app.services.auto_scoring_settings import get_scoring_settings, set_scoring_setting, _KEYS
from app.services.auto_scoring_service import resegment_all
from app.services.seed_scoring_agent import get_scoring_system_prompt

router = APIRouter(prefix="/admin/tender-scoring", tags=["tender-scoring-admin"])


@router.get("/settings")
def get_settings_route(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return get_scoring_settings(db)


@router.put("/settings")
def put_settings_route(
    body: dict,
    resegment: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    for key, value in body.items():
        if key in _KEYS:
            set_scoring_setting(db, key, value, updated_by=getattr(user, "id", None))
    result = get_scoring_settings(db)
    if resegment:
        result["_resegmented"] = resegment_all(db)["resegmented"]
    return result


@router.get("/stats")
def stats_route(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = dict(db.query(Tender.segment, func.count(Tender.id)).group_by(Tender.segment).all())
    total_scored = db.query(func.count(Tender.id)).filter(Tender.ai_relevance_score.isnot(None)).scalar()
    backlog = db.query(func.count(Tender.id)).filter(Tender.ai_relevance_score.is_(None)).scalar()
    last = db.query(PlatformSetting).filter(PlatformSetting.key == "auto_scoring_last_run").first()
    return {
        "total_scored": total_scored,
        "unscored_backlog": backlog,
        "last_reaper_run": last.value if last else None,
        "counts": {
            "to_bid": rows.get("to_bid", 0),
            "not_bidable": rows.get("not_bidable", 0),
            "discarded": rows.get("discarded", 0),
            "unscored": rows.get(None, 0),
        },
    }


@router.get("/digest")
def digest_route(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    row = db.query(PlatformSetting).filter(PlatformSetting.key == "auto_scoring_digest").first()
    return {"digest": row.value if row else None}


@router.post("/digest/regenerate")
def regenerate_digest_route(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    # Clear the stored hash so the next build re-distills, then rebuild now.
    row = db.query(PlatformSetting).filter(PlatformSetting.key == "auto_scoring_digest_hash").first()
    if row is not None:
        db.delete(row)
        db.commit()
    prompt = get_scoring_system_prompt(db)
    return {"digest": prompt}
```

- [ ] **Step 5: Mount the router**

In `drpl-backend/app/main.py`, where other routers are `include_router`-ed, add:

```python
from app.api.routes import tender_scoring_admin
app.include_router(tender_scoring_admin.router, prefix="/api")
```

(Match the existing prefix convention — other routes are mounted under `/api`.)

- [ ] **Step 6: Run test to verify it passes**

Run: `cd drpl-backend && python -m pytest tests/test_scoring_admin_routes.py -v`
Expected: PASS (both return 401/403, proving mounted + guarded).

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/api/routes/tender_scoring_admin.py drpl-backend/app/main.py drpl-backend/tests/test_scoring_admin_routes.py
git commit -m "feat(scoring): admin routes for settings/stats/digest

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Backend filters + `{items, total}` pagination

**Files:**
- Modify: `drpl-backend/app/services/tender_service.py` (extract `_apply_tender_filters`, add filter params, add `count_tenders`)
- Modify: `drpl-backend/app/schemas/__init__.py` (add `TenderListResponse`)
- Modify: `drpl-backend/app/api/routes/tenders.py` (`list_tenders` returns `{items, total}`, new query params)
- Test: `drpl-backend/tests/test_tender_filters_count.py`

**Interfaces:**
- Produces: `count_tenders(db, **filters) -> int`; `get_tenders` gains `segment, score_min, score_max, value_min, value_max, emd_min, emd_max, closing_after, closing_before, eligibility_status`; `list_tenders` returns `TenderListResponse {items, total}`.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_tender_filters_count.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.services.tender_service import get_tenders, count_tenders


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    db.add_all([
        Tender(portal="gem", tender_id="1", title="A", segment="to_bid",
               ai_relevance_score=0.9, estimated_value=9_000_000, eligibility_status="eligible"),
        Tender(portal="gem", tender_id="2", title="B", segment="not_bidable",
               ai_relevance_score=0.5, estimated_value=1_000_000, eligibility_status="unknown"),
        Tender(portal="gem", tender_id="3", title="C", segment="discarded",
               ai_relevance_score=0.2, estimated_value=9_000_000, eligibility_status="not_eligible"),
    ])
    db.commit()


def test_segment_filter_and_count_match():
    db = _session(); _seed(db)
    items = get_tenders(db, segment="to_bid")
    assert {t.tender_id for t in items} == {"1"}
    assert count_tenders(db, segment="to_bid") == 1


def test_score_range_filter():
    db = _session(); _seed(db)
    items = get_tenders(db, score_min=0.6)
    assert {t.tender_id for t in items} == {"1"}
    assert count_tenders(db, score_min=0.6) == 1


def test_eligibility_and_value_filters_count_matches_items():
    db = _session(); _seed(db)
    for kw in ({"eligibility_status": "eligible"}, {"value_min": 5_000_000}, {"segment": "discarded"}):
        assert count_tenders(db, **kw) == len(get_tenders(db, **kw))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_tender_filters_count.py -v`
Expected: FAIL — `count_tenders` missing / `get_tenders` rejects `segment` kwarg.

- [ ] **Step 3: Implement shared filters + count**

In `drpl-backend/app/services/tender_service.py`, extract the filter-building from `get_tenders` into a shared helper and add the new params. Replace the body of `get_tenders`'s filter section with a call to `_apply_tender_filters`, and add `count_tenders`:

```python
def _apply_tender_filters(query, *, portal=None, department=None, status=None, priority=None,
                          workflow_status=None, assigned_to=None, bid_type=None, location=None,
                          include_archived=False, include_below_threshold=False,
                          segment=None, score_min=None, score_max=None,
                          value_min=None, value_max=None, emd_min=None, emd_max=None,
                          closing_after=None, closing_before=None, eligibility_status=None):
    if not include_archived:
        query = query.filter(Tender.is_archived == False)          # noqa: E712
    if not include_below_threshold:
        query = query.filter(Tender.below_threshold == False)      # noqa: E712
    if portal:
        query = query.filter(Tender.portal == portal)
    if department:
        query = query.filter(Tender.department.ilike(f"%{department}%"))
    if status:
        query = query.filter(Tender.status == status.lower())
    if priority:
        query = query.filter(Tender.priority == priority.lower())
    if workflow_status:
        query = query.filter(Tender.workflow_status == workflow_status)
    if assigned_to is not None:
        query = query.filter(Tender.assigned_to == assigned_to)
    if bid_type:
        query = query.filter(Tender.bid_type == bid_type)
    if location:
        query = query.filter(Tender.location.ilike(f"%{location}%"))
    if segment:
        query = query.filter(Tender.segment == segment)
    if score_min is not None:
        query = query.filter(Tender.ai_relevance_score >= score_min)
    if score_max is not None:
        query = query.filter(Tender.ai_relevance_score <= score_max)
    if value_min is not None:
        query = query.filter(Tender.estimated_value >= value_min)
    if value_max is not None:
        query = query.filter(Tender.estimated_value <= value_max)
    if emd_min is not None:
        query = query.filter(Tender.emd_amount >= emd_min)
    if emd_max is not None:
        query = query.filter(Tender.emd_amount <= emd_max)
    if closing_after is not None:
        query = query.filter(Tender.closing_date >= closing_after)
    if closing_before is not None:
        query = query.filter(Tender.closing_date <= closing_before)
    if eligibility_status:
        query = query.filter(Tender.eligibility_status == eligibility_status)
    return query


def count_tenders(db, **filters) -> int:
    from sqlalchemy import func
    # count uses the same filters; drop sort/limit/offset kwargs if present
    for k in ("sort_by", "limit", "offset"):
        filters.pop(k, None)
    q = _apply_tender_filters(db.query(Tender), **filters)
    return q.with_entities(func.count(Tender.id)).scalar() or 0
```

Update `get_tenders(...)` to accept the new keyword params (add them to its signature with `None` defaults) and to build its query via `_apply_tender_filters(db.query(Tender), portal=portal, ..., segment=segment, score_min=score_min, ...)`, keeping the existing sort + `.offset(offset).limit(limit).all()`.

- [ ] **Step 4: Add the list-response schema**

In `drpl-backend/app/schemas/__init__.py`, after `TenderResponse`, add:

```python
class TenderListResponse(BaseModel):
    items: list[TenderResponse]
    total: int
```

- [ ] **Step 5: Update the route to return `{items, total}` + new params**

In `drpl-backend/app/api/routes/tenders.py`, change the `list_tenders` decorator to `response_model=TenderListResponse`, add the new Query params (`segment, score_min, score_max, value_min, value_max, emd_min, emd_max, closing_after, closing_before, eligibility_status`), and return:

```python
    common = dict(
        portal=portal, department=department, status=status, priority=priority,
        workflow_status=workflow_status, assigned_to=assigned_to, bid_type=bid_type,
        location=location, include_archived=include_archived,
        include_below_threshold=include_below_threshold, segment=segment,
        score_min=score_min, score_max=score_max, value_min=value_min, value_max=value_max,
        emd_min=emd_min, emd_max=emd_max, closing_after=closing_after,
        closing_before=closing_before, eligibility_status=eligibility_status,
    )
    items = get_tenders(db, sort_by=sort_by, limit=limit, offset=offset, **common)
    total = count_tenders(db, **common)
    return {"items": items, "total": total}
```

Import `count_tenders`, `TenderListResponse`. Query param declarations use `Optional[...]` with `Query(None)`; `closing_after`/`closing_before` are `Optional[str]` (ISO date) — SQLAlchemy compares them against the `DateTime` column; if the DB rejects string comparison, parse with `datetime.fromisoformat` in the route before passing. Add that parse defensively.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd drpl-backend && python -m pytest tests/test_tender_filters_count.py tests/test_below_threshold_filter.py -v`
Expected: PASS (new tests + the existing below-threshold test, which still calls `get_tenders`, stays green).

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/services/tender_service.py drpl-backend/app/schemas/__init__.py drpl-backend/app/api/routes/tenders.py drpl-backend/tests/test_tender_filters_count.py
git commit -m "feat(tenders): segment+range+eligibility filters, count, {items,total} response

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Frontend — `{items,total}` hook + 100/page Pagination

**Files:**
- Modify: `drpl-frontend/src/lib/api.ts` (`getTenders` returns `{items,total}`)
- Modify: `drpl-frontend/src/types/tender.ts` (`TenderFilters` new fields; `TenderListResult` type)
- Modify: `drpl-frontend/src/hooks/useTenders.ts` (consume `{items,total}`)
- Modify: `drpl-frontend/src/pages/TendersPage.tsx` (`PAGE_SIZE=100`; pass `total`)
- Modify: `drpl-frontend/src/components/ui/Pagination.tsx` (add `total`, full nav)
- (No new test; verified via `npm run build` + Task 11 drive.)

**Interfaces:**
- Produces: `getTenders(filters) -> Promise<{items: Tender[], total: number}>`; `useTenders` returns `{tenders, total, loading, error, refetch}`; `Pagination` gains `total: number`.

- [ ] **Step 1: Update `getTenders` return shape**

In `drpl-frontend/src/lib/api.ts`, change `getTenders` to type the response as `{items: Tender[]; total: number}` and return `data`:

```typescript
export async function getTenders(filters: TenderFilters): Promise<{ items: Tender[]; total: number }> {
  const params: Record<string, string | number> = { limit: filters.limit, offset: filters.offset };
  if (filters.portal) params.portal = filters.portal;
  if (filters.department) params.department = filters.department;
  if (filters.status) params.status = filters.status;
  if (filters.priority) params.priority = filters.priority;
  if (filters.workflow_status) params.workflow_status = filters.workflow_status;
  if (filters.assigned_to) params.assigned_to = filters.assigned_to;
  if (filters.sort_by) params.sort_by = filters.sort_by;
  if (filters.bid_type) params.bid_type = filters.bid_type;
  if (filters.location) params.location = filters.location;
  if (filters.segment) params.segment = filters.segment;
  if (filters.score_min != null) params.score_min = filters.score_min;
  if (filters.score_max != null) params.score_max = filters.score_max;
  if (filters.value_min != null) params.value_min = filters.value_min;
  if (filters.value_max != null) params.value_max = filters.value_max;
  if (filters.emd_min != null) params.emd_min = filters.emd_min;
  if (filters.emd_max != null) params.emd_max = filters.emd_max;
  if (filters.closing_after) params.closing_after = filters.closing_after;
  if (filters.closing_before) params.closing_before = filters.closing_before;
  if (filters.eligibility_status) params.eligibility_status = filters.eligibility_status;

  const { data } = await api.get<{ items: Tender[]; total: number }>('/api/tenders/', { params });
  return data;
}
```

- [ ] **Step 2: Extend `TenderFilters` type**

In `drpl-frontend/src/types/tender.ts`, add optional fields to `TenderFilters`:

```typescript
  segment?: string;
  score_min?: number;
  score_max?: number;
  value_min?: number;
  value_max?: number;
  emd_min?: number;
  emd_max?: number;
  closing_after?: string;
  closing_before?: string;
  eligibility_status?: string;
```

- [ ] **Step 3: Update the hook**

Replace `drpl-frontend/src/hooks/useTenders.ts` body so it consumes `{items,total}`:

```typescript
import { useState, useEffect, useCallback } from 'react';
import { getTenders } from '../lib/api';
import type { Tender, TenderFilters } from '../types/tender';

export function useTenders(filters: TenderFilters) {
  const [tenders, setTenders] = useState<Tender[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetch = useCallback(() => {
    setLoading(true);
    setError(null);
    getTenders(filters)
      .then((res) => { setTenders(res.items); setTotal(res.total); })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [filters.portal, filters.department, filters.status, filters.priority, filters.workflow_status,
      filters.assigned_to, filters.sort_by, filters.bid_type, filters.location, filters.segment,
      filters.score_min, filters.score_max, filters.value_min, filters.value_max, filters.emd_min,
      filters.emd_max, filters.closing_after, filters.closing_before, filters.eligibility_status,
      filters.limit, filters.offset]);

  useEffect(() => { fetch(); }, [fetch]);

  return { tenders, total, loading, error, refetch: fetch };
}
```

- [ ] **Step 4: Update `TendersPage` (page size + total)**

In `drpl-frontend/src/pages/TendersPage.tsx`: change `const PAGE_SIZE = 25;` → `const PAGE_SIZE = 100;`. Destructure `total` from the hook (`const { tenders, total, loading, refetch } = useTenders(filters);`). Pass `total={total}` to `<Pagination .../>` (alongside the existing `offset`/`limit`; you may drop the `count={tenders.length}` prop or keep it — see Step 5).

- [ ] **Step 5: Upgrade `Pagination` to full nav**

Replace `drpl-frontend/src/components/ui/Pagination.tsx`:

```tsx
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';
import clsx from 'clsx';

interface PaginationProps {
  offset: number;
  limit: number;
  total: number;
  onChange: (newOffset: number) => void;
}

export default function Pagination({ offset, limit, total, onChange }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.floor(offset / limit) + 1;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);

  // windowed page numbers around current
  const pages: number[] = [];
  const start = Math.max(1, currentPage - 2);
  const end = Math.min(totalPages, start + 4);
  for (let p = start; p <= end; p++) pages.push(p);

  const go = (page: number) => onChange((page - 1) * limit);
  const btn = 'flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors';
  const disabled = 'border-border text-muted-foreground/50 cursor-not-allowed';
  const active = 'border-border text-foreground hover:bg-muted';

  return (
    <div className="flex items-center justify-between pt-4 flex-wrap gap-2">
      <p className="text-sm text-muted-foreground">
        Showing {from}-{to} of {total.toLocaleString()} · Page {currentPage} of {totalPages}
      </p>
      <div className="flex items-center gap-1">
        <button onClick={() => go(1)} disabled={currentPage === 1} className={clsx(btn, currentPage === 1 ? disabled : active)} aria-label="First page">
          <ChevronsLeft size={16} />
        </button>
        <button onClick={() => go(currentPage - 1)} disabled={currentPage === 1} className={clsx(btn, currentPage === 1 ? disabled : active)}>
          <ChevronLeft size={16} /> Prev
        </button>
        {pages.map((p) => (
          <button key={p} onClick={() => go(p)}
            className={clsx('px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors',
              p === currentPage ? 'bg-accent text-accent-foreground border-accent' : active)}>
            {p}
          </button>
        ))}
        <button onClick={() => go(currentPage + 1)} disabled={currentPage >= totalPages} className={clsx(btn, currentPage >= totalPages ? disabled : active)}>
          Next <ChevronRight size={16} />
        </button>
        <button onClick={() => go(totalPages)} disabled={currentPage >= totalPages} className={clsx(btn, currentPage >= totalPages ? disabled : active)} aria-label="Last page">
          <ChevronsRight size={16} />
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Verify build**

Run: `cd drpl-frontend && npx tsc -b --noEmit`
Expected: no errors. (If any other file imports `getTenders` expecting an array, update it — grep `getTenders` in `src/`. The `useTenders` hook is the primary consumer.)

- [ ] **Step 7: Commit**

```bash
git add drpl-frontend/src/lib/api.ts drpl-frontend/src/types/tender.ts drpl-frontend/src/hooks/useTenders.ts drpl-frontend/src/pages/TendersPage.tsx drpl-frontend/src/components/ui/Pagination.tsx
git commit -m "feat(tenders): {items,total} hook + 100/page full pagination

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Frontend — Segment + new filters in `TenderFilters`

**Files:**
- Modify: `drpl-frontend/src/components/tenders/TenderFilters.tsx`
- (Verified via `npm run build` + Task 11 drive.)

**Interfaces:**
- Consumes: the extended `TenderFilters` type (Task 9). `onApply` now carries the new fields.

- [ ] **Step 1: Read the current component**

Read `drpl-frontend/src/components/tenders/TenderFilters.tsx` fully to match its existing state/props/`onApply` shape and the main-row-vs-Advanced structure (it already has an Advanced toggle in `TendersPage`).

- [ ] **Step 2: Add the Segment + Eligibility dropdowns (main row)**

Add to the main filter row, matching the existing `<select>` styling:

```tsx
{/* Segment */}
<select value={segment} onChange={(e) => setSegment(e.target.value)} className="<same classes as other selects>">
  <option value="">All Segments</option>
  <option value="to_bid">To Bid</option>
  <option value="not_bidable">Not Bidable</option>
  <option value="discarded">Discarded</option>
</select>

{/* Eligibility */}
<select value={eligibility} onChange={(e) => setEligibility(e.target.value)} className="<same classes>">
  <option value="">All Eligibility</option>
  <option value="eligible">Eligible</option>
  <option value="not_eligible">Not Eligible</option>
  <option value="unknown">Unknown</option>
</select>
```

Add `segment`/`eligibility` to the component's `useState` and include them in the object passed to `onApply`.

- [ ] **Step 3: Add the range filters (AI score / value / EMD / closing date)**

Add these below the main row (rendered when Advanced is on — the component receives/knows the advanced flag; if it doesn't, add a local collapsible "More filters" section):

- **AI score** preset buttons mapping to `score_min`/`score_max`: ≥90 → `{score_min:0.9}`; 70–90 → `{score_min:0.7, score_max:0.9}`; 40–70 → `{score_min:0.4, score_max:0.7}`; <40 → `{score_max:0.4}`; All → clear both.
- **Value** preset buttons: `<50L` → `{value_max:5_000_000}`; `50L–1Cr` → `{value_min:5_000_000, value_max:10_000_000}`; `≥1Cr` → `{value_min:10_000_000}`.
- **EMD** two number inputs → `emd_min`/`emd_max`.
- **Closing date** presets: This week → `closing_before = <now+7d ISO>`; ≤30 days → `closing_before = <now+30d ISO>`; Custom → two `<input type="date">` → `closing_after`/`closing_before`.

Wire all into `useState` + `onApply`. Compute the date presets with `date-fns` (already a dependency): e.g. `format(addDays(new Date(), 7), 'yyyy-MM-dd')`.

- [ ] **Step 4: Include the new fields in `onApply`**

Ensure the object handed to `onApply` (consumed by `TendersPage.handleApplyFilters`) now includes `segment, eligibility_status, score_min, score_max, value_min, value_max, emd_min, emd_max, closing_after, closing_before`. Confirm `TendersPage.handleApplyFilters` spreads `f` into `setFilters` so the new keys flow through (it does: `setFilters({ ...f, limit: PAGE_SIZE, offset: 0 })`).

- [ ] **Step 5: Verify build**

Run: `cd drpl-frontend && npx tsc -b --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add drpl-frontend/src/components/tenders/TenderFilters.tsx
git commit -m "feat(tenders): segment + score/value/emd/date/eligibility filters

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Frontend — Scoring-agent admin page

**Files:**
- Create: `drpl-frontend/src/pages/admin/AdminTenderScoringPage.tsx`
- Modify: `drpl-frontend/src/lib/api.ts` (5 API client fns)
- Modify: `drpl-frontend/src/router.tsx` (route)
- Modify: `drpl-frontend/src/components/layout/SidebarNav.tsx` (masterAdminNav entry)

**Interfaces:**
- Consumes: the Task 7 admin routes.

- [ ] **Step 1: Add API client functions**

In `drpl-frontend/src/lib/api.ts`:

```typescript
export async function getScoringSettings() {
  const { data } = await api.get('/api/admin/tender-scoring/settings');
  return data as Record<string, any>;
}
export async function updateScoringSettings(body: Record<string, any>, resegment = false) {
  const { data } = await api.put('/api/admin/tender-scoring/settings', body, { params: { resegment } });
  return data as Record<string, any>;
}
export async function getScoringStats() {
  const { data } = await api.get('/api/admin/tender-scoring/stats');
  return data as { total_scored: number; unscored_backlog: number; last_reaper_run: string | null;
    counts: { to_bid: number; not_bidable: number; discarded: number; unscored: number } };
}
export async function getScoringDigest() {
  const { data } = await api.get('/api/admin/tender-scoring/digest');
  return data as { digest: string | null };
}
export async function regenerateScoringDigest() {
  const { data } = await api.post('/api/admin/tender-scoring/digest/regenerate');
  return data as { digest: string };
}
```

- [ ] **Step 2: Build the page**

Create `drpl-frontend/src/pages/admin/AdminTenderScoringPage.tsx` — a page with `Header title="Tender Scoring Agent"`, that on mount loads `getScoringSettings` + `getScoringStats` + `getScoringDigest`, and renders:
- **Controls card:** an Enabled toggle (`auto_scoring_enabled`), text/number inputs for `auto_scoring_model`, `auto_scoring_interval_seconds`, `auto_scoring_batch_size`, `auto_scoring_max_retries`, and percentage inputs for the thresholds (show `segment_discard_below*100` and `segment_bidable_at*100` as %, convert back to 0–1 on save), a `value_threshold_inr` input, and cleanup controls (`auto_discard_enabled` toggle, `auto_discard_days`). A **Save** button → `updateScoringSettings(body)`; an **Apply & re-segment** button → `updateScoringSettings(body, true)` then refresh stats.
- **Stats card:** `total_scored`, `unscored_backlog`, `last_reaper_run` (formatted), and a per-segment count row (To Bid / Not Bidable / Discarded / Unscored) — use existing card/badge styling and the section-accent tokens.
- **Digest card:** a read-only `<pre>` of `digest` + a **Regenerate** button → `regenerateScoringDigest()` then update the shown digest.

Use existing UI primitives (buttons, cards, toggle) consistent with other admin pages (read `AgentBuilderPage.tsx` or `AdminSettingsPage`-style pages for the house style). Keep it a single focused file.

- [ ] **Step 3: Add the route (MasterAdmin-only)**

In `drpl-frontend/src/router.tsx`, add a route `/admin/tender-scoring` rendering `AdminTenderScoringPage`, wrapped in the same `MasterAdminRoute` guard used by the other `/admin/*` routes.

- [ ] **Step 4: Add the nav entry**

In `drpl-frontend/src/components/layout/SidebarNav.tsx`, add to `masterAdminNav` (after the `ratecards`/`mcp-servers` entries):

```tsx
  { to: '/admin/tender-scoring', icon: Gauge, label: 'Tender Scoring' },
```

(Reuse an already-imported lucide icon, e.g. `Gauge` or `Target`; pick one already in the import list.)

- [ ] **Step 5: Verify build**

Run: `cd drpl-frontend && npm run build`
Expected: `tsc -b` + `vite build` succeed.

- [ ] **Step 6: Commit**

```bash
git add drpl-frontend/src/pages/admin/AdminTenderScoringPage.tsx drpl-frontend/src/lib/api.ts drpl-frontend/src/router.tsx drpl-frontend/src/components/layout/SidebarNav.tsx
git commit -m "feat(scoring): admin control-panel page for the scoring agent

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Final verification

**Files:** none

- [ ] **Step 1: Backend suite**

Run: `cd drpl-backend && python -m pytest tests/ -q`
Expected: all pass except the two known pre-existing auth failures (`test_extension_config_requires_auth`, `test_tender_upload_requires_auth`). Any OTHER failure must be fixed before proceeding.

- [ ] **Step 2: Frontend build**

Run: `cd drpl-frontend && npm run build`
Expected: success (chunk-size warning is pre-existing).

- [ ] **Step 3: Drive the app locally**

Start backend + frontend. Confirm:
  - `/tenders` shows 100/page and "Page X of Y" with First/Prev/numbered/Next/Last.
  - The **Segment** dropdown filters To Bid / Not Bidable / Discarded; the score/value/EMD/date/eligibility filters narrow results (and the count updates).
  - Overriding a tender's segment (via the new endpoint, or a UI affordance if added) sticks after re-scoring.
  - `/admin/tender-scoring` loads: shows stats + per-segment counts, lets you toggle Enabled, change a threshold and **Apply & re-segment** (counts shift), and **Regenerate** the digest.
  - With `auto_scoring_enabled` off, no new scoring occurs.

- [ ] **Step 4: Confirm the cleanup query (no destructive surprise)**

In a Python shell: import `app.worker.scheduled_tasks._run_discard_cleanup`, seed a stale discarded + an overridden + an in-progress tender in a scratch session, run it, confirm only the plain stale-discarded one gets `is_archived=True`.

---

## Self-review notes

- **Spec coverage:** A.1 columns → Task 1; A.2 compute + resegment → Tasks 3,5; A.3 override → Task 6; B cleanup → Task 6 (+ prime); C.1 DB-backed settings → Task 4 (+ live read in Task 5); C.2 admin routes → Task 7; C.3 admin page → Task 11; D.1 filters → Tasks 8 (backend) + 10 (frontend); D.2 pagination `{items,total}` → Tasks 8 (backend) + 9 (frontend). Config defaults → Task 2. Segment stats last-run stamp → Task 5 (`_stamp_last_run`) read in Task 7.
- **Placeholder scan:** two explicit "read current code first" steps remain by design — Task 4 Step 1 (PlatformSetting columns), Task 7 Step 1 (admin auth dep name), Task 10 Step 1 / Task 11 Step 2 (match existing component/house style). Each states the fallback (direct upsert; `require_admin`; reuse existing select classes). No vague TODOs.
- **Type consistency:** `compute_segment(score, value, *, discard_below, bidable_at, value_threshold)` (Task 3) is called with those exact kwargs in Tasks 5 and (via settings) 5's `resegment_all`; `get_scoring_settings` keys (Task 4) are the same strings read in Tasks 5/6/7 and written by `set_scoring_setting`; `count_tenders`/`_apply_tender_filters` param names (Task 8) match the route's `common` dict; `{items,total}` shape (Task 8) matches `getTenders`→`useTenders`→`Pagination.total` (Task 9). Segment string values `to_bid`/`not_bidable`/`discarded` are identical across model, compute, override route, filters, and UI.
- **Known cross-task note:** existing `test_auto_scoring_service.py` stubs `call_ai`/`get_scoring_system_prompt` but NOT `get_scoring_settings`; Task 5 must ensure `get_scoring_settings` works against those tests' in-memory DB (it does — pure config fallback when no PlatformSetting rows). Flagged in Task 5 Step 4.
```
