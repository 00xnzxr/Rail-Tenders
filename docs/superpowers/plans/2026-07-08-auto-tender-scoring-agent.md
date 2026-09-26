# Auto Tender-Scoring Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AI relevance scoring of scraped tenders a mandatory, always-on automation: score new tenders on arrival and continuously reap any tender still missing a score, comparing title + scope against a lean prompt-cached digest of the training dataset + scope profile on Haiku 4.5, and flag tenders below ₹50 lakh.

**Architecture:** A single batch-scoring service (`auto_scoring_service`) is fed by two RQ worker entry points in production — an on-upload job and a self-rescheduling periodic reaper (same pattern as the existing `scan_closing_dates`) — and by an inline FastAPI BackgroundTask fallback when Redis is absent (local dev). Scoring uses the existing `call_ai` helper with `model_override="claude-haiku-4-5"`, a compact cached system prompt, and parses a `MATCH: <n>` / `REASON: <text>` reply. Two new `Tender` columns (`below_threshold`, `scoring_attempts`) drive the value filter and retry cap.

**Tech Stack:** FastAPI + SQLAlchemy + RQ (backend), Anthropic Haiku 4.5 via `app/services/ai_service.call_ai`, React + TypeScript (frontend field only), pytest.

## Global Constraints

- Model for scoring: `claude-haiku-4-5` (cheapest current model). Never the Batches API — synchronous Messages API only.
- Reuse `app/services/ai_service.call_ai(system_prompt, user_prompt, db, agent_name, model_override=...)` — it returns a **string**; do NOT hand-roll an Anthropic client. Structured output is by prompt convention (`MATCH:`/`REASON:` lines), parsed defensively.
- Value threshold: `value_threshold_inr = 5_000_000` (₹50 lakh). Flag `below_threshold=True` only when `estimated_value` is a real number **and** `< threshold`. Null/unknown value → never flag, still score.
- "Unscored" predicate: `ai_relevance_score IS NULL AND scoring_attempts < auto_scoring_max_retries AND is_archived == False`.
- New columns need an Alembic revision AND entries in `_apply_schema_drift_fixes()` (Postgres, `app/main.py:162`) AND `_add_missing_columns()` (SQLite, `app/main.py:532`) — the repo's schema-drift self-heal discipline (CLAUDE.md).
- Concurrency cap mirrors the existing `_RELEVANCE_SEMAPHORE = Semaphore(3)` in `tender_service.py:484`.
- Periodic job uses the self-rescheduling RQ pattern of `app/worker/scheduled_tasks.py` + priming in `app/services/seed_scheduled_jobs.py`. If `get_queue()` is None (no `REDIS_URL`), it degrades to a no-op / inline path and logs it — never crash.
- Scoring runs inside `run_id_scope` (`app/core/run_context.py`) so logs carry the `[run=<first8>]` stamp.
- Backend tests: `pytest tests/` from `drpl-backend/`. Two pre-existing failures (`test_extension_config_requires_auth`, `test_tender_upload_requires_auth`, 401 vs 403) are known and unrelated — not regressions.
- Config values live in `app/core/config.py` `Settings` and are env-overridable. Frontend build check: `npm run build` from `drpl-frontend/`.
- Commit after each task; end commit messages with the repo's Co-Authored-By line.

---

## Task 1: Schema — `below_threshold` + `scoring_attempts` columns

Add the two columns to the `Tender` model and wire the self-heal migrations so existing deployments pick them up.

**Files:**
- Modify: `drpl-backend/app/models/tender.py` (add two columns to `Tender`)
- Modify: `drpl-backend/app/main.py:162` (`_apply_schema_drift_fixes` statements list) and `app/main.py:532` (`_add_missing_columns` migrations list)
- Create: `drpl-backend/alembic/versions/20260708_auto_scoring_fields.py`
- Test: `drpl-backend/tests/test_auto_scoring_schema.py`

**Interfaces:**
- Produces: `Tender.below_threshold` (Boolean, default False, not null), `Tender.scoring_attempts` (Integer, default 0, not null).

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_auto_scoring_schema.py`:

```python
"""The Tender model carries the auto-scoring columns."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_defaults_for_new_tender():
    db = _session()
    t = Tender(portal="gem", tender_id="1", title="X")
    db.add(t)
    db.commit()
    db.refresh(t)
    assert t.below_threshold is False
    assert t.scoring_attempts == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_schema.py -v`
Expected: FAIL — `AttributeError: 'Tender' object has no attribute 'below_threshold'` (or the columns are unknown).

- [ ] **Step 3: Add the columns to the model**

In `drpl-backend/app/models/tender.py`, in the `Tender` class alongside the other metadata booleans (near `is_archived`/`workspace_enabled`), add:

```python
    below_threshold = Column(Boolean, default=False, nullable=False)   # value < ₹50L → hidden from default list
    scoring_attempts = Column(Integer, default=0, nullable=False)      # auto-scoring retry counter
```

Ensure `Integer` and `Boolean` are already imported at the top of the file (they are — `Column, Integer, String, Boolean, ...`).

- [ ] **Step 4: Add self-heal migrations**

In `drpl-backend/app/main.py`, append to the `statements` list in `_apply_schema_drift_fixes` (after the `category` line at :174):

```python
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS below_threshold BOOLEAN DEFAULT false",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS scoring_attempts INTEGER DEFAULT 0",
```

And append to the `migrations` list in `_add_missing_columns` (after the `("tenders", "category", ...)` line at :558):

```python
        ("tenders", "below_threshold", "BOOLEAN DEFAULT FALSE"),
        ("tenders", "scoring_attempts", "INTEGER DEFAULT 0"),
```

- [ ] **Step 5: Add the Alembic revision**

Create `drpl-backend/alembic/versions/20260708_auto_scoring_fields.py`. First find the current head: `cd drpl-backend && alembic heads` (or read the latest file in `alembic/versions/` for its `revision = "..."`), and set `down_revision` to that value.

```python
"""auto-scoring fields: below_threshold, scoring_attempts

Revision ID: 20260708_auto_scoring
Revises: <CURRENT_HEAD>
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

revision = "20260708_auto_scoring"
down_revision = "<CURRENT_HEAD>"   # replace with the actual current head
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tenders", sa.Column("below_threshold", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("tenders", sa.Column("scoring_attempts", sa.Integer(), server_default="0", nullable=False))


def downgrade():
    op.drop_column("tenders", "scoring_attempts")
    op.drop_column("tenders", "below_threshold")
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_schema.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/models/tender.py drpl-backend/app/main.py drpl-backend/alembic/versions/20260708_auto_scoring_fields.py drpl-backend/tests/test_auto_scoring_schema.py
git commit -m "feat(scoring): add below_threshold + scoring_attempts columns

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Config — auto-scoring settings

Add the tunables and master switch to `Settings`.

**Files:**
- Modify: `drpl-backend/app/core/config.py` (add fields to the `Settings` class, in the AI Configuration area near :43)
- Test: `drpl-backend/tests/test_auto_scoring_config.py`

**Interfaces:**
- Produces: `settings.auto_scoring_enabled` (bool), `settings.auto_scoring_model` (str), `settings.auto_scoring_interval_seconds` (int), `settings.auto_scoring_batch_size` (int), `settings.auto_scoring_max_concurrency` (int), `settings.auto_scoring_max_retries` (int), `settings.value_threshold_inr` (float).

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_auto_scoring_config.py`:

```python
from app.core.config import get_settings


def test_auto_scoring_defaults():
    s = get_settings()
    assert s.auto_scoring_enabled is True
    assert s.auto_scoring_model == "claude-haiku-4-5"
    assert s.auto_scoring_interval_seconds == 120
    assert s.auto_scoring_batch_size == 25
    assert s.auto_scoring_max_concurrency == 3
    assert s.auto_scoring_max_retries == 3
    assert s.value_threshold_inr == 5_000_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'auto_scoring_enabled'`.

- [ ] **Step 3: Add the config fields**

In `drpl-backend/app/core/config.py`, inside the `Settings` class after the `ai_batch_size` line (:43), add:

```python
    # Auto tender-scoring agent (mandatory automation)
    auto_scoring_enabled: bool = True
    auto_scoring_model: str = "claude-haiku-4-5"
    auto_scoring_interval_seconds: int = 120
    auto_scoring_batch_size: int = 25
    auto_scoring_max_concurrency: int = 3
    auto_scoring_max_retries: int = 3
    value_threshold_inr: float = 5_000_000   # ₹50 lakh — tenders below are flagged below_threshold
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/core/config.py drpl-backend/tests/test_auto_scoring_config.py
git commit -m "feat(scoring): add auto-scoring config settings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Scoring parse + value-filter helpers

Two pure functions that the service will use: parse the LLM reply, and decide the below-threshold flag. Pure functions so they're unit-testable without a DB or LLM.

**Files:**
- Create: `drpl-backend/app/services/auto_scoring_helpers.py`
- Test: `drpl-backend/tests/test_auto_scoring_helpers.py`

**Interfaces:**
- Produces:
  - `parse_score_reply(text: str) -> tuple[float, str]` — parses `MATCH: <0-100>` / `REASON: <text>` from the LLM reply; returns `(relevance_0_1, reasoning)`. Clamps to [0,1]; on unparseable input returns `(0.5, "")` (neutral default, matching the existing `score_relevance` error default).
  - `is_below_threshold(estimated_value: float | None, threshold: float) -> bool` — True iff value is a real number and `< threshold`; False when value is None.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_auto_scoring_helpers.py`:

```python
from app.services.auto_scoring_helpers import parse_score_reply, is_below_threshold


def test_parse_normal_reply():
    score, reason = parse_score_reply("MATCH: 90\nREASON: Core railway electrical AMC")
    assert score == 0.9
    assert reason == "Core railway electrical AMC"


def test_parse_clamps_and_handles_percent_sign():
    score, _ = parse_score_reply("MATCH: 120%")
    assert score == 1.0
    score2, _ = parse_score_reply("MATCH: -5")
    assert score2 == 0.0


def test_parse_unparseable_returns_neutral():
    score, reason = parse_score_reply("I cannot score this.")
    assert score == 0.5
    assert reason == ""


def test_below_threshold_true_when_under():
    assert is_below_threshold(4_000_000, 5_000_000) is True


def test_below_threshold_false_when_over_or_equal():
    assert is_below_threshold(5_000_000, 5_000_000) is False
    assert is_below_threshold(9_000_000, 5_000_000) is False


def test_below_threshold_false_when_none():
    assert is_below_threshold(None, 5_000_000) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.auto_scoring_helpers'`.

- [ ] **Step 3: Write the helpers**

Create `drpl-backend/app/services/auto_scoring_helpers.py`:

```python
"""Pure helpers for the auto tender-scoring agent — no DB, no LLM."""
from __future__ import annotations

import re

_MATCH_RE = re.compile(r"MATCH:\s*(-?\d+(?:\.\d+)?)\s*%?", re.IGNORECASE)
_REASON_RE = re.compile(r"REASON:\s*(.+)", re.IGNORECASE | re.DOTALL)


def parse_score_reply(text: str) -> tuple[float, str]:
    """Parse a `MATCH: <0-100>` / `REASON: <text>` reply into (relevance_0_1, reasoning).

    Neutral 0.5 default on unparseable input, matching score_relevance's error default.
    """
    if not text:
        return 0.5, ""
    m = _MATCH_RE.search(text)
    if not m:
        return 0.5, ""
    pct = float(m.group(1))
    score = max(0.0, min(1.0, pct / 100.0))
    r = _REASON_RE.search(text)
    reason = r.group(1).strip() if r else ""
    return score, reason


def is_below_threshold(estimated_value: "float | None", threshold: float) -> bool:
    """True iff we have a real value and it is under the threshold. None → False (never flag)."""
    if estimated_value is None:
        return False
    return estimated_value < threshold
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_helpers.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/auto_scoring_helpers.py drpl-backend/tests/test_auto_scoring_helpers.py
git commit -m "feat(scoring): parse-reply + below-threshold helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Digest seeder — the lean cached system prompt

Build and store the compact scoring digest (~800 tokens) from the scope profile (+ a training-dataset summary when present), regenerating only when its inputs change (detected via a stored hash). Store it on a `PlatformSetting` row so the service reads it cheaply.

**Files:**
- Create: `drpl-backend/app/services/seed_scoring_agent.py`
- Test: `drpl-backend/tests/test_scoring_digest.py`

**Interfaces:**
- Consumes: `scope_profile_service.get_active_profile`, `scope_profile_service.build_relevance_prompt_context` (both in `app/services/scope_profile_service.py`).
- Produces:
  - `build_digest_inputs(db) -> str` — returns the raw text (scope context + any training-dataset summaries) the digest is derived from. Deterministic (used for hashing).
  - `get_scoring_system_prompt(db) -> str` — returns the stored digest, (re)building it if the inputs hash changed. Stores the digest + hash in `PlatformSetting` keys `auto_scoring_digest` and `auto_scoring_digest_hash`.

- [ ] **Step 1: Inspect the PlatformSetting accessor**

Read `drpl-backend/app/models/` for the `PlatformSetting` model and find the existing get/set helper (grep: `PlatformSetting`). Use the same read/write pattern the codebase already uses (e.g. a `get_setting(db, key)` / `set_setting(db, key, value)` service, or direct query). This step is inspection only — no code.

- [ ] **Step 2: Write the failing test**

Create `drpl-backend/tests/test_scoring_digest.py`:

```python
"""The scoring digest is stable when inputs are unchanged, and rebuilds on change."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
import app.services.seed_scoring_agent as seed


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_digest_static_header_present(monkeypatch):
    db = _session()
    # Stub the distillation LLM call so the test is offline + deterministic.
    monkeypatch.setattr(seed, "_distill", lambda raw: "DIGEST: railway electrical AMC focus")
    monkeypatch.setattr(seed, "build_digest_inputs", lambda _db: "scope keywords: railway, AMC")

    prompt1 = seed.get_scoring_system_prompt(db)
    # Contains the fixed instruction/output-contract header AND the distilled body
    assert "MATCH:" in prompt1
    assert "REASON:" in prompt1
    assert "DIGEST: railway electrical AMC focus" in prompt1

    # Same inputs → no rebuild → identical output (and _distill not called again)
    calls = {"n": 0}
    def _count(raw):
        calls["n"] += 1
        return "DIGEST: should-not-be-called"
    monkeypatch.setattr(seed, "_distill", _count)
    prompt2 = seed.get_scoring_system_prompt(db)
    assert prompt2 == prompt1
    assert calls["n"] == 0
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_scoring_digest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.seed_scoring_agent'`.

- [ ] **Step 4: Write the seeder**

Create `drpl-backend/app/services/seed_scoring_agent.py`. Use the `PlatformSetting` accessor confirmed in Step 1 (shown here as `get_setting`/`set_setting` — adapt to the real names):

```python
"""Build + store the lean, prompt-cached scoring digest for the auto-scoring agent.

The digest is a compact (~800 token) system prompt: a fixed instruction/output
contract header plus a distilled body derived from the admin scope profile and
(when present) training-dataset summaries. It is regenerated only when its
inputs change, detected via a stored SHA-256 hash. Distillation is a single
Haiku call run here at seed time — never per tender.
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Fixed header — the task, output contract, and rubric. Kept short but proper.
_HEADER = """You score how well a government tender fits DRPL's business.
DRPL does mechanical + electrical engineering and AMC work for Indian Railways
(traction, signaling, power systems, locomotive/coach components, spares).

Read the tender title and scope summary the user gives you and reply in EXACTLY
this format, nothing else:
MATCH: <integer 0-100>
REASON: <one short sentence>

Rubric: 90-100 = core railway mechanical/electrical/AMC; 60-89 = adjacent or
partial fit; 40-59 = weak overlap; 0-39 = different domain. Use the relevance
reference below to judge fit."""


def build_digest_inputs(db: Session) -> str:
    """The raw text the digest is derived from — deterministic, used for hashing."""
    from app.services.scope_profile_service import (
        get_active_profile, build_relevance_prompt_context,
    )
    profile = get_active_profile(db)
    scope_ctx = build_relevance_prompt_context(profile) or ""
    # Training-dataset summaries are optional; include their parsed_content if the
    # tables exist and rows are present. Kept compact — names/tags + short excerpts.
    dataset_ctx = ""
    try:
        from app.models.training_dataset import TrainingDataset, TrainingDatasetFile
        datasets = db.query(TrainingDataset).filter(TrainingDataset.status == "active").all()
        parts = []
        for d in datasets:
            files = db.query(TrainingDatasetFile).filter(
                TrainingDatasetFile.dataset_id == d.id
            ).all()
            excerpt = " ".join((f.parsed_content or f.raw_content or "")[:400] for f in files)
            parts.append(f"[{d.name}] tags={d.tags} :: {excerpt}")
        dataset_ctx = "\n".join(parts)
    except Exception:
        dataset_ctx = ""
    return (scope_ctx + "\n\n" + dataset_ctx).strip()


def _distill(raw: str) -> str:
    """Distill the raw inputs into a compact relevance reference (~600 tokens).

    Single synchronous Haiku call. Falls back to a truncated raw block on error
    so scoring is never blocked by distillation.
    """
    import asyncio
    from app.core.config import get_settings
    from app.services.ai_service import call_ai
    settings = get_settings()
    system = ("Condense the following into a compact relevance reference for a "
              "tender-scoring agent: list the recurring buyers, departments, work "
              "categories, keywords, and the value band. Be terse — under 250 words. "
              "No preamble.")
    try:
        out = asyncio.run(call_ai(system, raw, None, "scoring_digest",
                                  model_override=settings.auto_scoring_model))
        return out.strip() or raw[:1500]
    except Exception as e:  # noqa: BLE001
        log.warning("scoring digest distillation failed, using raw excerpt: %s", e)
        return raw[:1500]


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_scoring_system_prompt(db: Session) -> str:
    """Return the cached digest system prompt, rebuilding only if inputs changed."""
    from app.services.platform_settings_service import get_setting, set_setting  # adapt to real accessor

    raw = build_digest_inputs(db)
    current_hash = _hash(raw)
    stored_hash = get_setting(db, "auto_scoring_digest_hash")
    stored_digest = get_setting(db, "auto_scoring_digest")

    if stored_hash == current_hash and stored_digest:
        body = stored_digest
    else:
        body = _distill(raw)
        set_setting(db, "auto_scoring_digest", body)
        set_setting(db, "auto_scoring_digest_hash", current_hash)

    return _HEADER + "\n\nRelevance reference:\n" + body
```

Note: if the real PlatformSetting accessor differs (e.g. no `platform_settings_service`), replace the `get_setting`/`set_setting` import and calls with the codebase's actual mechanism found in Step 1 — the test monkeypatches `_distill` and `build_digest_inputs`, so the settings read/write must work against the in-memory DB (use whatever the app uses; if it's an ORM model, query/insert a `PlatformSetting` row directly).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd drpl-backend && python -m pytest tests/test_scoring_digest.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/seed_scoring_agent.py drpl-backend/tests/test_scoring_digest.py
git commit -m "feat(scoring): lean cached scoring digest seeder

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Batch-scoring service

The core: score one tender, score a batch (bounded concurrency), and the reaper query.

**Files:**
- Create: `drpl-backend/app/services/auto_scoring_service.py`
- Test: `drpl-backend/tests/test_auto_scoring_service.py`

**Interfaces:**
- Consumes: `parse_score_reply`, `is_below_threshold` (Task 3); `get_scoring_system_prompt` (Task 4); `ai_service.call_ai`; config from Task 2.
- Produces:
  - `find_unscored_tender_ids(db, limit: int | None = None) -> list[int]` — ids where `ai_relevance_score IS NULL AND scoring_attempts < max_retries AND is_archived == False`, capped at `limit or auto_scoring_batch_size`.
  - `score_tenders_batch(db, tender_ids: list[int]) -> dict` — scores each (bounded concurrency), writes `ai_relevance_score`, `fit_reasoning`, `below_threshold`, increments `scoring_attempts` on failure; returns `{"scored": int, "flagged_below_threshold": int, "failed": int}`. Wrapped in `run_id_scope`.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_auto_scoring_service.py`:

```python
"""Batch scoring writes results, sets below_threshold correctly, and caps retries."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
import app.services.auto_scoring_service as svc


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _stub_prompt_and_llm(monkeypatch, reply):
    monkeypatch.setattr(svc, "get_scoring_system_prompt", lambda db: "SYS")
    async def _fake_call_ai(system, user, db, agent, model_override=None):
        return reply
    monkeypatch.setattr(svc, "call_ai", _fake_call_ai)


def test_score_writes_relevance_and_flags_below_threshold(monkeypatch):
    db = _session()
    t = Tender(portal="gem", tender_id="1", title="Railway AMC",
               estimated_value=4_000_000.0)   # under ₹50L
    db.add(t); db.commit()
    _stub_prompt_and_llm(monkeypatch, "MATCH: 88\nREASON: strong fit")

    out = svc.score_tenders_batch(db, [t.id])
    db.refresh(t)
    assert round(t.ai_relevance_score, 2) == 0.88
    assert t.fit_reasoning == "strong fit"
    assert t.below_threshold is True
    assert out == {"scored": 1, "flagged_below_threshold": 1, "failed": 0}


def test_unknown_value_not_flagged(monkeypatch):
    db = _session()
    t = Tender(portal="gem", tender_id="2", title="X", estimated_value=None)
    db.add(t); db.commit()
    _stub_prompt_and_llm(monkeypatch, "MATCH: 70\nREASON: ok")
    svc.score_tenders_batch(db, [t.id])
    db.refresh(t)
    assert t.below_threshold is False
    assert round(t.ai_relevance_score, 2) == 0.70


def test_failure_increments_attempts_and_excludes_after_cap(monkeypatch):
    db = _session()
    t = Tender(portal="gem", tender_id="3", title="X")
    db.add(t); db.commit()
    monkeypatch.setattr(svc, "get_scoring_system_prompt", lambda db: "SYS")
    async def _boom(system, user, db, agent, model_override=None):
        raise RuntimeError("api down")
    monkeypatch.setattr(svc, "call_ai", _boom)

    for _ in range(3):   # max_retries default = 3
        svc.score_tenders_batch(db, [t.id])
    db.refresh(t)
    assert t.scoring_attempts == 3
    assert t.ai_relevance_score is None
    assert t.id not in svc.find_unscored_tender_ids(db)   # excluded at cap


def test_find_unscored_excludes_already_scored(monkeypatch):
    db = _session()
    scored = Tender(portal="gem", tender_id="4", title="A", ai_relevance_score=0.5)
    unscored = Tender(portal="gem", tender_id="5", title="B")
    db.add_all([scored, unscored]); db.commit()
    ids = svc.find_unscored_tender_ids(db)
    assert unscored.id in ids
    assert scored.id not in ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.auto_scoring_service'`.

- [ ] **Step 3: Write the service**

Create `drpl-backend/app/services/auto_scoring_service.py`:

```python
"""Batch auto-scoring of tenders on Haiku 4.5 with a cached digest system prompt."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tender import Tender
from app.services.ai_service import call_ai
from app.services.auto_scoring_helpers import parse_score_reply, is_below_threshold
from app.services.seed_scoring_agent import get_scoring_system_prompt

log = logging.getLogger(__name__)


def find_unscored_tender_ids(db: Session, limit: "int | None" = None) -> list[int]:
    settings = get_settings()
    limit = limit or settings.auto_scoring_batch_size
    rows = (
        db.query(Tender.id)
        .filter(Tender.ai_relevance_score.is_(None))
        .filter(Tender.scoring_attempts < settings.auto_scoring_max_retries)
        .filter(Tender.is_archived == False)  # noqa: E712
        .order_by(Tender.created_at.desc())
        .limit(limit)
        .all()
    )
    return [r[0] for r in rows]


def _user_prompt(tender: Tender) -> str:
    scope = tender.ai_summary or (tender.description or tender.full_description or "")
    return f"Title: {tender.title}\n\nScope: {scope[:1500]}"


def score_tenders_batch(db: Session, tender_ids: list[int]) -> dict:
    """Score the given tenders. Bounded concurrency; writes results; retry-safe."""
    if not tender_ids:
        return {"scored": 0, "flagged_below_threshold": 0, "failed": 0}
    try:
        from app.core.run_context import run_id_scope
        ctx = run_id_scope(f"autoscore-{tender_ids[0]}")
    except Exception:  # run_id_scope optional in tests
        from contextlib import nullcontext
        ctx = nullcontext()

    settings = get_settings()
    system_prompt = get_scoring_system_prompt(db)
    sem = asyncio.Semaphore(settings.auto_scoring_max_concurrency)
    tenders = db.query(Tender).filter(Tender.id.in_(tender_ids)).all()

    result = {"scored": 0, "flagged_below_threshold": 0, "failed": 0}

    async def _score_one(t: Tender):
        async with sem:
            reply = await call_ai(system_prompt, _user_prompt(t), db,
                                  "tender_scorer", model_override=settings.auto_scoring_model)
            return parse_score_reply(reply)

    async def _run():
        for t in tenders:
            try:
                score, reason = await _score_one(t)
                t.ai_relevance_score = score
                t.fit_reasoning = reason
                if is_below_threshold(t.estimated_value, settings.value_threshold_inr):
                    t.below_threshold = True
                    result["flagged_below_threshold"] += 1
                result["scored"] += 1
            except Exception as e:  # noqa: BLE001
                t.scoring_attempts = (t.scoring_attempts or 0) + 1
                result["failed"] += 1
                log.warning("auto-score failed for tender %s (attempt %s): %s",
                            t.id, t.scoring_attempts, e)

    with ctx:
        asyncio.run(_run())
        db.commit()

    log.info("auto-scoring batch: found=%d scored=%d flagged_below=%d failed=%d",
             len(tenders), result["scored"], result["flagged_below_threshold"], result["failed"])
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_service.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/auto_scoring_service.py drpl-backend/tests/test_auto_scoring_service.py
git commit -m "feat(scoring): batch scoring service with retry cap + value filter

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: RQ worker tasks + reaper priming

The two RQ entry points and the startup priming for the self-rescheduling reaper.

**Files:**
- Create: `drpl-backend/app/worker/auto_scoring_tasks.py`
- Modify: `drpl-backend/app/services/seed_scheduled_jobs.py` (add reaper priming)
- Test: `drpl-backend/tests/test_auto_scoring_tasks.py`

**Interfaces:**
- Consumes: `find_unscored_tender_ids`, `score_tenders_batch` (Task 5); `get_queue` (`app/core/redis_client.py`).
- Produces:
  - `score_new_tenders(tender_ids: list[int]) -> dict` — opens `SessionLocal`, calls `score_tenders_batch`.
  - `reap_unscored_tenders() -> dict` — opens `SessionLocal`, scores a batch, then self-reschedules `auto_scoring_interval_seconds` later (job_id `drpl-auto-scoring-reaper`); guarded by `auto_scoring_enabled`; no-op if `get_queue()` is None.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_auto_scoring_tasks.py`:

```python
"""Worker entry points delegate to the batch service; reaper self-reschedules."""
import app.worker.auto_scoring_tasks as tasks


def test_score_new_tenders_delegates(monkeypatch):
    called = {}
    monkeypatch.setattr(tasks, "score_tenders_batch", lambda db, ids: called.setdefault("ids", ids) or {"scored": len(ids)})
    # SessionLocal is opened inside; stub it to a dummy
    class _DummyDB:
        def close(self): pass
    monkeypatch.setattr(tasks, "SessionLocal", lambda: _DummyDB())
    out = tasks.score_new_tenders([1, 2, 3])
    assert called["ids"] == [1, 2, 3]
    assert out["scored"] == 3


def test_reaper_noop_when_no_queue(monkeypatch):
    monkeypatch.setattr(tasks, "get_queue", lambda: None)
    # Even with no queue, it should not raise and should return a disabled marker
    out = tasks.reap_unscored_tenders()
    assert out.get("rescheduled") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_tasks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.worker.auto_scoring_tasks'`.

- [ ] **Step 3: Write the worker tasks**

Create `drpl-backend/app/worker/auto_scoring_tasks.py`:

```python
"""RQ entry points for auto tender-scoring: on-upload job + self-rescheduling reaper.

Mirrors app/worker/scheduled_tasks.py: each opens its own SessionLocal, the
reaper re-enqueues its own next run, and everything degrades to a no-op when
Redis (the queue) is unavailable.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.redis_client import get_queue
from app.services.auto_scoring_service import find_unscored_tender_ids, score_tenders_batch

log = logging.getLogger(__name__)

_REAPER_JOB_ID = "drpl-auto-scoring-reaper"


def score_new_tenders(tender_ids: list[int]) -> dict:
    """RQ job enqueued on extension upload — score the freshly-ingested tenders."""
    if not tender_ids:
        return {"scored": 0}
    db = SessionLocal()
    try:
        return score_tenders_batch(db, tender_ids)
    finally:
        db.close()


def _reschedule_reaper() -> None:
    q = get_queue()
    if q is None:
        return
    settings = get_settings()
    try:
        q.enqueue_in(
            timedelta(seconds=settings.auto_scoring_interval_seconds),
            "app.worker.auto_scoring_tasks.reap_unscored_tenders",
            job_id=_REAPER_JOB_ID, result_ttl=86400,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("auto_scoring_tasks: could not reschedule reaper: %s", e)


def reap_unscored_tenders() -> dict:
    """Periodic sweep: score a batch of unscored tenders, then self-reschedule."""
    settings = get_settings()
    if not settings.auto_scoring_enabled:
        log.info("auto-scoring reaper: disabled via auto_scoring_enabled")
        return {"rescheduled": False, "reason": "disabled"}
    if get_queue() is None:
        log.info("auto-scoring reaper: queue unavailable — not running")
        return {"rescheduled": False, "reason": "no_queue"}

    db = SessionLocal()
    try:
        ids = find_unscored_tender_ids(db)
        out = score_tenders_batch(db, ids) if ids else {"scored": 0, "flagged_below_threshold": 0, "failed": 0}
    finally:
        db.close()

    _reschedule_reaper()
    out["rescheduled"] = True
    return out
```

- [ ] **Step 4: Prime the reaper at startup**

In `drpl-backend/app/services/seed_scheduled_jobs.py`, inside `seed_scheduled_jobs()` after the closing-date scan block (after :57, before `return out`), add:

```python
    # Auto-scoring reaper: first run in 1 minute, then every auto_scoring_interval_seconds.
    if settings.auto_scoring_enabled and not _job_already_pending(q, "drpl-auto-scoring-reaper"):
        try:
            q.enqueue_in(timedelta(minutes=1), "app.worker.auto_scoring_tasks.reap_unscored_tenders",
                         job_id="drpl-auto-scoring-reaper", result_ttl=86400)
            out["scheduled"].append({"job": "drpl-auto-scoring-reaper", "in": "1 minute"})
            log.info("seed_scheduled_jobs: queued first auto-scoring reap in 1 minute")
        except Exception as e:
            log.warning("seed_scheduled_jobs: could not seed auto-scoring reaper: %s", e)
```

(`settings` is already in scope at :33; `timedelta` already imported at :15.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd drpl-backend && python -m pytest tests/test_auto_scoring_tasks.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/worker/auto_scoring_tasks.py drpl-backend/app/services/seed_scheduled_jobs.py drpl-backend/tests/test_auto_scoring_tasks.py
git commit -m "feat(scoring): RQ score-new + self-rescheduling reaper tasks

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Wire the upload path — RQ in prod, inline fallback in dev

Replace the fragile in-web-process scoring on extension upload with an RQ enqueue when a queue exists, falling back to inline when it doesn't.

**Files:**
- Modify: `drpl-backend/app/api/routes/extension.py:51-58` (the `upload_tenders` fan-out block)
- Test: `drpl-backend/tests/test_upload_scoring_dispatch.py`

**Interfaces:**
- Consumes: `get_queue` (`app/core/redis_client.py`); `score_new_tenders` (Task 6); `score_tenders_batch` (Task 5).

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_upload_scoring_dispatch.py`:

```python
"""Upload dispatches scoring to RQ when a queue exists, else inline."""
import app.api.routes.extension as ext


def test_dispatch_uses_queue_when_available(monkeypatch):
    enqueued = {}
    class _Q:
        def enqueue(self, fn, *args, **kwargs):
            enqueued["fn"] = fn; enqueued["args"] = args
    monkeypatch.setattr(ext, "get_queue", lambda: _Q())
    added = []
    class _BG:
        def add_task(self, fn, *a): added.append((fn, a))
    ext._dispatch_scoring([10, 11], _BG())
    assert enqueued["args"] == ([10, 11],)
    assert added == []   # not inline when queue present


def test_dispatch_falls_back_inline_without_queue(monkeypatch):
    monkeypatch.setattr(ext, "get_queue", lambda: None)
    added = []
    class _BG:
        def add_task(self, fn, *a): added.append((fn, a))
    ext._dispatch_scoring([12], _BG())
    assert len(added) == 1   # inline BackgroundTask used
    assert added[0][1] == ([12],)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_upload_scoring_dispatch.py -v`
Expected: FAIL — `AttributeError: module 'app.api.routes.extension' has no attribute '_dispatch_scoring'`.

- [ ] **Step 3: Add the dispatch helper + inline runner, and rewire the route**

In `drpl-backend/app/api/routes/extension.py`, add near the top-level helpers (e.g. after the imports / before `upload_tenders`):

```python
from app.core.redis_client import get_queue


def _score_inline(tender_ids: list[int]) -> None:
    """Fallback scoring when no RQ queue (local dev). Opens its own session."""
    if not tender_ids:
        return
    from app.core.database import SessionLocal
    from app.services.auto_scoring_service import score_tenders_batch
    db = SessionLocal()
    try:
        score_tenders_batch(db, tender_ids)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("inline auto-scoring failed: %s", e)
    finally:
        db.close()


def _dispatch_scoring(new_ids: list[int], background_tasks) -> None:
    """Enqueue scoring on the worker when Redis is present; else run inline."""
    if not new_ids:
        return
    q = get_queue()
    if q is not None:
        try:
            q.enqueue("app.worker.auto_scoring_tasks.score_new_tenders", new_ids)
            return
        except Exception:
            pass  # fall through to inline
    if background_tasks is not None:
        background_tasks.add_task(_score_inline, new_ids)
```

Then replace the fan-out block in `upload_tenders` (lines 53-57) — the current `from app.services.tender_service import score_relevance_background` + `background_tasks.add_task(score_relevance_background, new_ids)` — with:

```python
        new_ids = (result.new_ids or []) if hasattr(result, "new_ids") else []
        _dispatch_scoring(new_ids, background_tasks)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd drpl-backend && python -m pytest tests/test_upload_scoring_dispatch.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/api/routes/extension.py drpl-backend/tests/test_upload_scoring_dispatch.py
git commit -m "feat(scoring): dispatch scoring to RQ on upload, inline fallback

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Expose + filter `below_threshold` (API + frontend)

Serialize the flag, filter below-threshold tenders out of the default list, and mirror the field on the frontend type.

**Files:**
- Modify: `drpl-backend/app/schemas/__init__.py` (`TenderResponse` — add `below_threshold`)
- Modify: `drpl-backend/app/services/tender_service.py` (`get_tenders` — filter + include flag) and `drpl-backend/app/api/routes/tenders.py` (`list_tenders` — `include_below_threshold` query param)
- Modify: `drpl-frontend/src/types/tender.ts` (`Tender` — add `below_threshold`)
- Test: `drpl-backend/tests/test_below_threshold_filter.py`

**Interfaces:**
- Consumes: `Tender.below_threshold` (Task 1).
- Produces: `get_tenders(..., include_below_threshold: bool = False)` filters out `below_threshold == True` by default; `TenderResponse.below_threshold: bool`.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_below_threshold_filter.py`:

```python
"""Below-threshold tenders are hidden by default, shown when included."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.services.tender_service import get_tenders


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_below_threshold_hidden_by_default():
    db = _session()
    db.add_all([
        Tender(portal="gem", tender_id="1", title="big", below_threshold=False),
        Tender(portal="gem", tender_id="2", title="small", below_threshold=True),
    ])
    db.commit()
    tids = {r["tender_id"] for r in get_tenders(db)}
    assert "1" in tids
    assert "2" not in tids


def test_below_threshold_shown_when_included():
    db = _session()
    db.add(Tender(portal="gem", tender_id="2", title="small", below_threshold=True))
    db.commit()
    tids = {r["tender_id"] for r in get_tenders(db, include_below_threshold=True)}
    assert "2" in tids
```

(Note: this assumes `get_tenders` returns dicts. If the parallel card-redesign work landed a dict-returning `get_tenders`, this holds; if `get_tenders` returns ORM objects on this branch, adjust the test to read `r.tender_id` and the implementation accordingly — verify by reading the current `get_tenders` return shape in Step 3.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && python -m pytest tests/test_below_threshold_filter.py -v`
Expected: FAIL — below-threshold tender "2" is returned (no filter yet).

- [ ] **Step 3: Add the filter + param + schema field**

First **read** the current `get_tenders` signature and body in `drpl-backend/app/services/tender_service.py` to confirm its return shape and parameter list. Add an `include_below_threshold: bool = False` parameter and, in the query-building section (next to the `if not include_archived:` filter), add:

```python
    if not include_below_threshold:
        query = query.filter(Tender.below_threshold == False)  # noqa: E712
```

If `get_tenders` builds per-row dicts, add `"below_threshold": t.below_threshold` to each dict; if it returns ORM objects, no per-row change is needed (the schema reads the attribute).

In `drpl-backend/app/schemas/__init__.py`, add to `TenderResponse`:

```python
    below_threshold: bool = False
```

In `drpl-backend/app/api/routes/tenders.py`, add a query param to `list_tenders` and pass it through:

```python
    include_below_threshold: bool = Query(False, description="Include tenders flagged below the value threshold"),
```

and add `include_below_threshold=include_below_threshold` to the `get_tenders(...)` call.

- [ ] **Step 4: Mirror the frontend type**

In `drpl-frontend/src/types/tender.ts`, add to the `Tender` interface:

```typescript
  below_threshold: boolean;
```

- [ ] **Step 5: Run tests + build**

Run: `cd drpl-backend && python -m pytest tests/test_below_threshold_filter.py -v`
Expected: PASS.
Run: `cd drpl-frontend && npx tsc -b --noEmit`
Expected: no new errors from `tender.ts`.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/schemas/__init__.py drpl-backend/app/services/tender_service.py drpl-backend/app/api/routes/tenders.py drpl-frontend/src/types/tender.ts drpl-backend/tests/test_below_threshold_filter.py
git commit -m "feat(scoring): expose + filter below_threshold in tenders list

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Final verification

**Files:** none

- [ ] **Step 1: Full backend suite**

Run: `cd drpl-backend && python -m pytest tests/ -q`
Expected: all pass except the two known pre-existing auth failures (`test_extension_config_requires_auth`, `test_tender_upload_requires_auth`). If any OTHER test fails, fix before proceeding.

- [ ] **Step 2: Frontend build**

Run: `cd drpl-frontend && npm run build`
Expected: success (chunk-size warning is pre-existing).

- [ ] **Step 3: Drive the scoring locally (no Redis path)**

Start backend (`cd drpl-backend && python run_local.py` once, then `uvicorn app.main:app --reload --port 8000`). With `REDIS_URL` empty, upload a tender via the extension (or POST to `/api/extension/tenders` with a JWT). Confirm:
  - The tender gets an `ai_relevance_score` shortly after (inline path ran).
  - A tender with `estimated_value < 5_000_000` gets `below_threshold=True` and drops out of `GET /tenders/` unless `include_below_threshold=true`.
  - A tender with null value is still scored and NOT flagged.
  - Logs show the `auto-scoring batch: found=… scored=… flagged_below=… failed=…` line.

- [ ] **Step 4: Confirm the reaper query**

In a Python shell against the local DB: `from app.services.auto_scoring_service import find_unscored_tender_ids` returns only NULL-score, under-retry-cap, non-archived tenders.

---

## Self-review notes

- **Spec coverage:** A.1 unscored predicate → Task 5 `find_unscored_tender_ids`; A.2 scoring call (Haiku, cached system, structured-by-convention) → Tasks 4+5; A.3 distilled digest + regen-on-hash → Task 4; A.4 value filter (null-safe) → Tasks 3+5; A.5 idempotency/retry cap → Tasks 1+5; B.1 service → Task 5; B.2 RQ trigger+reaper+priming → Tasks 6+7; B.3 inline fallback → Task 7; C.1 schema+drift fixes → Task 1; C.2 API/frontend field+filter → Task 8; C.3 config → Task 2; C.4 observability (batch log line, run_id_scope, give-up warning) → Tasks 5+6. Redis batch-claim dedupe (A.5) is noted in the spec; the reaper's `job_id`-based single-scheduling + the `scoring_attempts` cap already prevent runaway/duplicate work in the single-queue setup, so an explicit per-tender claim key is deferred as a hardening follow-up (flagged here, not silently dropped).
- **Placeholder scan:** two spots require the implementer to read current code before writing — Task 4 Step 1 (PlatformSetting accessor name) and Task 8 Step 3 (`get_tenders` return shape). Both are explicit "read then adapt" instructions with the fallback spelled out, not vague TODOs.
- **Type consistency:** `parse_score_reply -> (float, str)` and `is_below_threshold(value, threshold) -> bool` (Task 3) are consumed with those exact signatures in Task 5; `find_unscored_tender_ids` / `score_tenders_batch` (Task 5) are consumed with matching names in Tasks 6–7; `get_scoring_system_prompt(db)` (Task 4) is consumed in Task 5; `below_threshold` column/field/type name is identical across Tasks 1, 8, and the frontend.
```
