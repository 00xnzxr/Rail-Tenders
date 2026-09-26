# Tender Scoring Backlog Awareness + Low-Cost Drain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably score (relevance) every new/unanalyzed tender at low token cost, and surface the backlog through an admin page, a dashboard one-click drain, and a chat-agent tool.

**Architecture:** One new backend service `scoring_backlog_service.py` is the single source of truth (backlog stats + drain). Large backlogs drain via the Anthropic Batch API (50% off, async) with content-hash dedupe so duplicate tenders cost one score; new/urgent tenders drain live via the existing `auto_scoring_service`. Three consumers read the service: the existing admin scoring page, the dashboard AI panel, and a lean LangChain tool.

**Tech Stack:** FastAPI + SQLAlchemy + RQ (backend), React 18 + Vite + axios (frontend), Anthropic Message Batches API, LangChain `BaseTool`.

## Global Constraints

- Scoring only (`Tender.ai_relevance_score` + `segment`). **Never** touch the deep-analysis path (`ai_category`, classifier/risk/summary).
- No new DB columns, no new infra, no extension changes.
- Reuse `MessageBatch` / `MessageBatchItem` with a new `batch_type="tender_scoring"`; store the content-hash fan-out map in the existing `metadata_json`.
- "Pending" always = `ai_relevance_score IS NULL AND is_archived == False` (the reaper's exact filter). "Drainable" additionally requires `scoring_attempts < auto_scoring_max_retries`.
- Reuse existing helpers verbatim: `auto_scoring_service._user_prompt`, `auto_scoring_helpers.parse_score_reply` / `compute_segment` / `is_below_threshold`, `seed_scoring_agent.get_scoring_system_prompt`, `auto_scoring_settings.get_scoring_settings`.
- Backend tests use pytest with the existing in-memory SQLite fixtures (see `tests/test_auto_scoring_service.py` for the `db` fixture pattern). Run from `drpl-backend/` with the venv active.
- The scoring admin router is mounted at `/api` and the router itself has `prefix="/admin/tender-scoring"`, so full paths are `/api/admin/tender-scoring/...`.
- Frontend admin routes are master-admin only (`MasterAdminRoute` in `router.tsx`). The scoring admin page already exists at `/admin/tender-scoring` (`AdminTenderScoringPage.tsx`) — **extend it, do not create a new page.**

---

### Task 1: `scoring_backlog_service.backlog_stats` + content-hash dedupe helper

**Files:**
- Create: `drpl-backend/app/services/scoring_backlog_service.py`
- Test: `drpl-backend/tests/test_scoring_backlog_service.py`

**Interfaces:**
- Consumes: `Tender` model; `auto_scoring_settings.get_scoring_settings`; `auto_scoring_service._user_prompt`; `PlatformSetting`; `MessageBatch`.
- Produces:
  - `backlog_stats(db: Session) -> dict` with keys `total, scored, pending, drainable, stuck_at_cap, in_flight_batches, last_reaper_run, enabled, est_cost_inr`.
  - `content_hash(title: str, scope: str) -> str` — sha256 of normalized `title + "\n" + scope[:1500]`.
  - `dedupe_by_content_hash(tenders: list[Tender]) -> tuple[list[Tender], dict[str, list[int]]]` — `(unique_representatives, {hash: [tender_id, ...]})`.

- [ ] **Step 1: Write the failing test**

```python
# drpl-backend/tests/test_scoring_backlog_service.py
from app.models.tender import Tender
from app.services import scoring_backlog_service as svc


def _mk(db, title, score=None, archived=False, attempts=0, scope="rail traction spares"):
    t = Tender(title=title, portal="ireps", tender_id=title,
               description=scope, ai_relevance_score=score,
               is_archived=archived, scoring_attempts=attempts)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_backlog_stats_counts_match_reaper_filter(db):
    _mk(db, "scored", score=0.8)
    _mk(db, "pending-a")
    _mk(db, "pending-b")
    _mk(db, "archived-pending", archived=True)
    _mk(db, "retry-capped", attempts=99)  # above default max_retries

    stats = svc.backlog_stats(db)
    assert stats["scored"] == 1
    # pending excludes the archived one, includes the retry-capped one
    assert stats["pending"] == 3
    # drainable excludes retry-capped
    assert stats["drainable"] == 2
    assert stats["stuck_at_cap"] == 1
    assert "est_cost_inr" in stats
    assert isinstance(stats["enabled"], bool)


def test_dedupe_by_content_hash_collapses_identical_scope(db):
    a = _mk(db, "Dup tender", scope="supply of traction motors")
    b = _mk(db, "Dup tender", scope="supply of traction motors")
    c = _mk(db, "Different", scope="civil bridge works")
    unique, fanout = svc.dedupe_by_content_hash([a, b, c])
    assert len(unique) == 2
    # the two identical ones share a hash pointing at both ids
    groups = [ids for ids in fanout.values() if len(ids) == 2]
    assert groups and sorted(groups[0]) == sorted([a.id, b.id])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_scoring_backlog_service.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.scoring_backlog_service`.

- [ ] **Step 3: Write the implementation**

```python
# drpl-backend/app/services/scoring_backlog_service.py
"""Single source of truth for tender-scoring backlog stats + draining.

Scoring only (ai_relevance_score). Never touches the deep-analysis path.
"""
from __future__ import annotations

import hashlib
import logging
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.tender import Tender
from app.models.platform_setting import PlatformSetting
from app.services.auto_scoring_settings import get_scoring_settings

log = logging.getLogger(__name__)

# Rough per-score cost estimate (Haiku 4.5, ~800 cached-in + ~1500-char scope + 2-line out).
# Deliberately approximate — labelled an estimate in the UI. INR per scored tender, live price.
_EST_INR_PER_SCORE_LIVE = 0.15
_WS_RE = re.compile(r"\s+")


def _pending_query(db: Session):
    return db.query(Tender).filter(
        Tender.ai_relevance_score.is_(None),
        Tender.is_archived == False,  # noqa: E712
    )


def content_hash(title: str, scope: str) -> str:
    norm = _WS_RE.sub(" ", f"{title or ''}\n{(scope or '')[:1500]}").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _tender_scope(t: Tender) -> str:
    return t.ai_summary or (t.description or t.full_description or "")


def dedupe_by_content_hash(tenders: list[Tender]) -> tuple[list[Tender], dict[str, list[int]]]:
    """Return (one representative per hash, {hash: [all tender ids sharing it]})."""
    fanout: dict[str, list[int]] = {}
    unique: list[Tender] = []
    for t in tenders:
        h = content_hash(t.title, _tender_scope(t))
        if h not in fanout:
            fanout[h] = []
            unique.append(t)
        fanout[h].append(t.id)
    return unique, fanout


def backlog_stats(db: Session) -> dict:
    scoring = get_scoring_settings(db)
    max_retries = scoring["auto_scoring_max_retries"]

    total = db.query(func.count(Tender.id)).filter(Tender.is_archived == False).scalar()  # noqa: E712
    scored = db.query(func.count(Tender.id)).filter(Tender.ai_relevance_score.isnot(None)).scalar()
    pending = _pending_query(db).count()
    drainable = _pending_query(db).filter(Tender.scoring_attempts < max_retries).count()

    in_flight = db.query(func.count(MessageBatchCount.c)).scalar() if False else _count_in_flight(db)

    last = db.query(PlatformSetting).filter(
        PlatformSetting.key == "auto_scoring_last_run").first()

    return {
        "total": total,
        "scored": scored,
        "pending": pending,
        "drainable": drainable,
        "stuck_at_cap": pending - drainable,
        "in_flight_batches": in_flight,
        "last_reaper_run": last.value if last else None,
        "enabled": bool(scoring["auto_scoring_enabled"]),
        "est_cost_inr": round(drainable * _EST_INR_PER_SCORE_LIVE / 2, 2),  # batch path = 50% off
    }


def _count_in_flight(db: Session) -> int:
    from app.models.message_batch import MessageBatch
    return db.query(func.count(MessageBatch.id)).filter(
        MessageBatch.batch_type == "tender_scoring",
        MessageBatch.status.notin_(("ended", "canceled", "expired")),
    ).scalar() or 0
```

Remove the dead `MessageBatchCount` placeholder line — write `_count_in_flight(db)` directly:

```python
    in_flight = _count_in_flight(db)
```

Note: confirm the batch model import path. Check with `grep -rn "class MessageBatch" drpl-backend/app/models/` and use that module in `_count_in_flight`.

- [ ] **Step 4: Verify the model import path, then run the test**

Run: `cd drpl-backend && grep -rn "class MessageBatch\b" app/models/`
Then fix the import in `_count_in_flight` to match.
Run: `pytest tests/test_scoring_backlog_service.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/scoring_backlog_service.py drpl-backend/tests/test_scoring_backlog_service.py
git commit -m "feat(scoring): backlog_stats + content-hash dedupe service"
```

---

### Task 2: `drain_backlog(mode="live")` + drain/backlog admin endpoints

**Files:**
- Modify: `drpl-backend/app/services/scoring_backlog_service.py`
- Modify: `drpl-backend/app/api/routes/tender_scoring_admin.py`
- Test: `drpl-backend/tests/test_scoring_backlog_service.py`, `drpl-backend/tests/test_scoring_backlog_routes.py`

**Interfaces:**
- Consumes: `auto_scoring_service.find_unscored_tender_ids`, `auto_scoring_service.score_tenders_batch`; `backlog_stats` (Task 1).
- Produces:
  - `drain_backlog(db: Session, mode: str = "live", limit: int | None = None) -> dict`. `mode="live"` returns `{"mode": "live", "scored": int, "failed": int}`.
  - `GET /api/admin/tender-scoring/backlog` → `backlog_stats(db)`.
  - `POST /api/admin/tender-scoring/drain` body `{"mode": "live"|"batch", "limit": int|None}` → drain result dict.

- [ ] **Step 1: Write the failing test (service)**

```python
# append to tests/test_scoring_backlog_service.py
def test_drain_live_scores_all_pending(db, monkeypatch):
    from app.services import auto_scoring_service as ass
    import app.services.scoring_backlog_service as svc

    _mk(db, "p1")
    _mk(db, "p2")

    # stub the LLM call: mark each scored tender with a fixed score
    def _fake_score_batch(db_, ids):
        for t in db_.query(Tender).filter(Tender.id.in_(ids)).all():
            t.ai_relevance_score = 0.7
        db_.commit()
        return {"scored": len(ids), "flagged_below_threshold": 0, "failed": 0}

    monkeypatch.setattr(svc, "score_tenders_batch", _fake_score_batch)

    out = svc.drain_backlog(db, mode="live")
    assert out["mode"] == "live"
    assert out["scored"] == 2
    # idempotent: nothing left
    assert svc.backlog_stats(db)["pending"] == 0
    assert svc.drain_backlog(db, mode="live")["scored"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && pytest tests/test_scoring_backlog_service.py::test_drain_live_scores_all_pending -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'drain_backlog'`.

- [ ] **Step 3: Implement `drain_backlog` (live mode) in the service**

Add these imports at the top of `scoring_backlog_service.py`:

```python
from app.services.auto_scoring_service import find_unscored_tender_ids, score_tenders_batch
```

Add:

```python
def drain_backlog(db: Session, mode: str = "live", limit: int | None = None) -> dict:
    """Drain the unscored backlog.

    mode="live"  -> loop the existing reaper batch scorer until empty (full price, fast).
    mode="batch" -> submit an Anthropic Batch API job (50% off, async). Added in Task 4.
    """
    if mode == "batch":
        raise NotImplementedError("batch drain is added in Task 4")

    scoring = get_scoring_settings(db)
    batch = scoring["auto_scoring_batch_size"]
    scored = failed = 0
    remaining = limit
    while True:
        take = batch if remaining is None else min(batch, remaining)
        if take <= 0:
            break
        ids = find_unscored_tender_ids(db, limit=take)
        if not ids:
            break
        out = score_tenders_batch(db, ids)
        scored += out.get("scored", 0)
        failed += out.get("failed", 0)
        if remaining is not None:
            remaining -= len(ids)
        # guard against a batch that scores nothing (all failed at retry cap) — avoid infinite loop
        if out.get("scored", 0) == 0 and out.get("failed", 0) == 0:
            break
    return {"mode": "live", "scored": scored, "failed": failed}
```

- [ ] **Step 4: Run the service test**

Run: `cd drpl-backend && pytest tests/test_scoring_backlog_service.py -v`
Expected: PASS (all).

- [ ] **Step 5: Write the failing route test**

```python
# drpl-backend/tests/test_scoring_backlog_routes.py
"""Backlog + drain endpoints are master-admin gated and return the service shapes."""
from app.services import scoring_backlog_service as svc


def test_backlog_route_returns_stats(client_master_admin):
    r = client_master_admin.get("/api/admin/tender-scoring/backlog")
    assert r.status_code == 200
    body = r.json()
    for k in ("total", "scored", "pending", "drainable", "in_flight_batches", "enabled"):
        assert k in body


def test_drain_route_live(client_master_admin, monkeypatch):
    monkeypatch.setattr(svc, "drain_backlog",
                        lambda db, mode, limit=None: {"mode": mode, "scored": 3, "failed": 0})
    r = client_master_admin.post("/api/admin/tender-scoring/drain",
                                 json={"mode": "live"})
    assert r.status_code == 200
    assert r.json()["scored"] == 3
```

Note: reuse the existing master-admin test client fixture. Find it: `grep -rn "master_admin" drpl-backend/tests/conftest.py drpl-backend/tests/test_tender_scoring*.py`. If a `client_master_admin` fixture does not exist, model one on the existing authenticated-client fixture used by other admin-route tests (look at how `tests/` builds a `TestClient` with a master-admin JWT) and add it to `conftest.py`.

- [ ] **Step 6: Run to verify it fails**

Run: `cd drpl-backend && pytest tests/test_scoring_backlog_routes.py -v`
Expected: FAIL — 404 (routes not added yet).

- [ ] **Step 7: Add the routes to `tender_scoring_admin.py`**

Add imports near the top:

```python
from pydantic import BaseModel
from app.services.scoring_backlog_service import backlog_stats, drain_backlog
```

Add a request model + two routes (after the existing `stats_route`):

```python
class DrainRequest(BaseModel):
    mode: str = "live"          # "live" | "batch"
    limit: "int | None" = None


@router.get("/backlog")
def backlog_route(db: Session = Depends(get_db), _: User = Depends(require_master_admin)):
    return backlog_stats(db)


@router.post("/drain")
def drain_route(
    body: DrainRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_master_admin),
):
    return drain_backlog(db, mode=body.mode, limit=body.limit)
```

- [ ] **Step 8: Run the route test**

Run: `cd drpl-backend && pytest tests/test_scoring_backlog_routes.py -v`
Expected: PASS (2 tests).

- [ ] **Step 9: Commit**

```bash
git add drpl-backend/app/services/scoring_backlog_service.py drpl-backend/app/api/routes/tender_scoring_admin.py drpl-backend/tests/test_scoring_backlog_service.py drpl-backend/tests/test_scoring_backlog_routes.py drpl-backend/tests/conftest.py
git commit -m "feat(scoring): live drain + backlog/drain admin endpoints"
```

---

### Task 3: Dashboard — read scoring source of truth + one-click live drain

**Files:**
- Modify: `drpl-frontend/src/lib/api.ts`
- Modify: `drpl-frontend/src/pages/DashboardPage.tsx:14,22-36,87-126`

**Interfaces:**
- Consumes: `GET /api/admin/tender-scoring/backlog`, `POST /api/admin/tender-scoring/drain` (Task 2).
- Produces: `getScoringBacklog()`, `drainScoring(mode, limit?)` in `api.ts`; the dashboard AI panel reads `pending`/`scored` from the backlog endpoint instead of `stats.analyzed_tenders`.

- [ ] **Step 1: Add the two API helpers**

In `drpl-frontend/src/lib/api.ts`, after the existing scoring helpers block (around the `regenerateScoringDigest` function):

```typescript
export interface ScoringBacklog {
  total: number;
  scored: number;
  pending: number;
  drainable: number;
  stuck_at_cap: number;
  in_flight_batches: number;
  last_reaper_run: string | null;
  enabled: boolean;
  est_cost_inr: number;
}

export async function getScoringBacklog(): Promise<ScoringBacklog> {
  const { data } = await api.get('/api/admin/tender-scoring/backlog');
  return data;
}

export async function drainScoring(
  mode: 'live' | 'batch' = 'live',
  limit?: number,
): Promise<any> {
  const { data } = await api.post('/api/admin/tender-scoring/drain', { mode, limit });
  return data;
}
```

- [ ] **Step 2: Rewire the DashboardPage AI panel**

In `DashboardPage.tsx`, change the import on line 14 from:

```typescript
import { analyzeBatch } from '../lib/api';
```

to:

```typescript
import { getScoringBacklog, drainScoring, type ScoringBacklog } from '../lib/api';
import { useEffect } from 'react';
```

Replace the batch state/handler block (lines 22-36) with backlog-driven state:

```typescript
  const [backlog, setBacklog] = useState<ScoringBacklog | null>(null);
  const [draining, setDraining] = useState(false);
  const [drainMsg, setDrainMsg] = useState<string | null>(null);

  const loadBacklog = async () => {
    try {
      setBacklog(await getScoringBacklog());
    } catch {
      /* backlog is best-effort on the dashboard */
    }
  };

  useEffect(() => {
    void loadBacklog();
  }, []);

  const handleDrain = async () => {
    if (!backlog) return;
    setDraining(true);
    setDrainMsg(null);
    try {
      // large backlogs go cheap+async via batch; small ones drain live for instant feedback
      const mode = backlog.drainable > 50 ? 'batch' : 'live';
      const result = await drainScoring(mode);
      setDrainMsg(
        mode === 'batch'
          ? `Submitted ${result.submitted ?? backlog.drainable} tenders for cheap batch scoring${
              result.deduped_saved ? ` (${result.deduped_saved} duplicates skipped)` : ''
            }.`
          : `Scored ${result.scored ?? 0} tenders. ${result.failed ?? 0} failed.`,
      );
      await loadBacklog();
    } catch (err: any) {
      setDrainMsg(err.response?.data?.detail || 'Scoring drain failed. Check API key in .env');
    } finally {
      setDraining(false);
    }
  };
```

Replace the AI Intelligence `<section>` body (lines 87-126) button + metric cards so they read `backlog`:

```tsx
        {/* AI Intelligence */}
        <section>
          <div className="flex items-center justify-between mb-4">
            <SectionHeading className="mb-0">AI Intelligence</SectionHeading>
            <button
              onClick={handleDrain}
              disabled={draining || !backlog || backlog.drainable === 0}
              className="flex items-center gap-2 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-semibold hover:bg-accent/90 transition-colors disabled:opacity-50 shadow-sm"
            >
              <Zap size={14} strokeWidth={2} />
              {draining ? 'Scoring…' : 'Score Pending'}
            </button>
          </div>

          {drainMsg && (
            <div className="mb-4 px-4 py-3 bg-accent/10 border border-accent/20 rounded-lg text-sm text-accent">
              {drainMsg}
            </div>
          )}

          {backlog && (
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <MetricCard
                value={stats?.avg_relevance != null ? `${Math.round(stats.avg_relevance * 100)}%` : '—'}
                label="Avg. Relevance"
                color="text-blue-600 dark:text-blue-400"
              />
              <MetricCard
                value={backlog.scored}
                label="Scored"
                color="text-emerald-600 dark:text-emerald-400"
              />
              <MetricCard
                value={backlog.pending}
                label="Pending Scoring"
                color="text-foreground"
              />
            </div>
          )}
        </section>
```

- [ ] **Step 3: Type-check + build the frontend**

Run: `cd drpl-frontend && npm run build`
Expected: `tsc -b && vite build` completes with no type errors. If `Brain` import becomes unused, leave it (still used by the top StatCard row).

- [ ] **Step 4: Commit**

```bash
git add drpl-frontend/src/lib/api.ts drpl-frontend/src/pages/DashboardPage.tsx
git commit -m "feat(dashboard): scoring backlog panel + one-click drain"
```

---

### Task 4: Batch-API drain (50% off) + scoring result processing + dedupe fan-out

**Files:**
- Modify: `drpl-backend/app/services/batch_service.py` (add `create_tender_scoring_batch`; branch `process_batch_results` on `batch_type`)
- Modify: `drpl-backend/app/services/scoring_backlog_service.py` (implement `mode="batch"`)
- Test: `drpl-backend/tests/test_scoring_batch.py`

**Interfaces:**
- Consumes: `dedupe_by_content_hash`, `content_hash` (Task 1); `get_scoring_system_prompt`; `auto_scoring_service._user_prompt`; `parse_score_reply`, `compute_segment`, `is_below_threshold`; `get_scoring_settings`.
- Produces:
  - `batch_service.create_tender_scoring_batch(db, tender_ids: list[int], fanout: dict[str, list[int]], user_id: int | None = None) -> MessageBatch` — one request per representative tender, `batch_type="tender_scoring"`, stores `fanout` in `metadata_json`.
  - `batch_service.apply_scoring_results(db, batch_record, tender_results: dict[int, str]) -> int` — writes score/segment and fans out to duplicate ids; returns tenders updated.
  - `scoring_backlog_service.drain_backlog(..., mode="batch")` returns `{"mode": "batch", "submitted": int, "deduped_saved": int, "batch_id": str}`.

- [ ] **Step 1: Write the failing test for the scoring-result application (pure, no network)**

```python
# drpl-backend/tests/test_scoring_batch.py
import json
from app.models.tender import Tender
from app.models.message_batch import MessageBatch  # adjust import if grep shows another path
from app.services import batch_service


def _mk(db, title, score=None):
    t = Tender(title=title, portal="ireps", tender_id=title,
               description="traction spares", ai_relevance_score=score)
    db.add(t); db.commit(); db.refresh(t)
    return t


def test_apply_scoring_results_writes_and_fans_out(db):
    rep = _mk(db, "rep")
    dup = _mk(db, "dup")           # same content hash as rep in real use
    fanout = {"h1": [rep.id, dup.id]}
    batch = MessageBatch(
        batch_id="msgbatch_test", batch_type="tender_scoring",
        status="ended", model="claude-haiku-4-5-20251001",
        total_requests=1,
        metadata_json=json.dumps({"fanout": fanout}),
    )
    db.add(batch); db.commit()

    # one result for the representative only
    updated = batch_service.apply_scoring_results(
        db, batch, {rep.id: "MATCH: 82\nREASON: core railway electrical"})
    db.refresh(rep); db.refresh(dup)

    assert updated == 2                      # representative + fanned-out duplicate
    assert rep.ai_relevance_score == 0.82
    assert dup.ai_relevance_score == 0.82    # duplicate got the same score, no tokens
    assert rep.segment in ("to_bid", "not_bidable", "discarded")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && pytest tests/test_scoring_batch.py -v`
Expected: FAIL — `AttributeError: module 'app.services.batch_service' has no attribute 'apply_scoring_results'`.

- [ ] **Step 3: Implement `apply_scoring_results` in `batch_service.py`**

```python
def apply_scoring_results(db, batch_record, tender_results: dict) -> int:
    """Write ai_relevance_score/segment from scoring replies, fanning out to duplicates.

    tender_results maps representative tender_id -> raw reply text.
    """
    import json as _json
    from app.services.auto_scoring_helpers import (
        parse_score_reply, compute_segment, is_below_threshold,
    )
    from app.services.auto_scoring_settings import get_scoring_settings

    scoring = get_scoring_settings(db)
    meta = _json.loads(batch_record.metadata_json or "{}")
    fanout = meta.get("fanout", {})
    # rep_id -> [all ids sharing its hash]
    rep_to_ids: dict[int, list[int]] = {}
    for ids in fanout.values():
        if ids:
            rep_to_ids[ids[0]] = ids

    updated = 0
    for rep_id, text in tender_results.items():
        score, reason = parse_score_reply(text)
        target_ids = rep_to_ids.get(rep_id, [rep_id])
        tenders = db.query(Tender).filter(Tender.id.in_(target_ids)).all()
        for t in tenders:
            t.ai_relevance_score = score
            t.fit_reasoning = reason
            if is_below_threshold(t.estimated_value, scoring["value_threshold_inr"]):
                t.below_threshold = True
            if not t.segment_overridden:
                t.segment = compute_segment(
                    t.ai_relevance_score, t.estimated_value,
                    discard_below=scoring["segment_discard_below"],
                    bidable_at=scoring["segment_bidable_at"],
                    value_threshold=scoring["value_threshold_inr"],
                )
            updated += 1
    db.commit()
    return updated
```

(`Tender` is already imported at module scope in `batch_service.py`; if not, add `from app.models.tender import Tender`.)

- [ ] **Step 4: Run the apply test**

Run: `cd drpl-backend && pytest tests/test_scoring_batch.py -v`
Expected: PASS.

- [ ] **Step 5: Branch `process_batch_results` on `batch_type`**

In `batch_service.py`, in `process_batch_results`, the block at lines 447-482 ("Apply results to tenders") assumes the 4-agent deep path. Guard it so scoring batches use the new path. Right before the `for tender_id, agent_results in tender_results.items():` loop (line 449), insert:

```python
    if batch_record.batch_type == "tender_scoring":
        # tender_results here is rep_id -> {"score": text}; flatten to rep_id -> text
        flat = {tid: parts.get("score", "") for tid, parts in tender_results.items()}
        tenders_updated = apply_scoring_results(db, batch_record, flat)
    else:
        tenders_updated = 0
        for tender_id, agent_results in tender_results.items():
            # ... existing deep-analysis application, unchanged ...
```

Indent the existing loop body under the `else:`. Keep everything after the loop (batch_record bookkeeping, usage log, commit, summary) unchanged. Note the scoring path's `item_type` will be `"score"` (set in Step 6), so `tender_results[tender_id]["score"]` holds the reply text.

- [ ] **Step 6: Implement `create_tender_scoring_batch`**

Add to `batch_service.py`, modeled on `create_tender_analysis_batch` but one request per tender:

```python
async def create_tender_scoring_batch(db, tender_ids: list, fanout: dict, user_id=None):
    """One scoring request per representative tender. batch_type='tender_scoring'."""
    from app.services.seed_scoring_agent import get_scoring_system_prompt
    from app.services.auto_scoring_service import _user_prompt
    from app.services.auto_scoring_settings import get_scoring_settings

    api_key = _get_api_key(db)
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured. Set it in Platform Settings or .env")

    scoring = get_scoring_settings(db)
    model = scoring["auto_scoring_model"]
    system = get_scoring_system_prompt(db)

    tenders = db.query(Tender).filter(Tender.id.in_(tender_ids)).all()
    if not tenders:
        raise ValueError("No tenders to score")

    batch_requests, batch_items = [], []
    for t in tenders:
        custom_id = f"score_{t.id}"
        batch_requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": 128,
                "system": [{"type": "text", "text": system,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": _user_prompt(t)}],
            },
        })
        batch_items.append({"custom_id": custom_id, "item_type": "score", "tender_id": t.id})

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            ANTHROPIC_BATCHES_URL, headers=_get_headers(api_key),
            json={"requests": batch_requests},
        )
        if response.status_code != 200:
            raise Exception(f"Batch API error ({response.status_code}): {response.text[:500]}")
        data = response.json()

    batch_record = MessageBatch(
        batch_id=data["id"], batch_type="tender_scoring",
        status=data.get("processing_status", "in_progress"), model=model,
        total_requests=len(batch_requests),
        processing_count=data.get("request_counts", {}).get("processing", len(batch_requests)),
        results_url=data.get("results_url"),
        metadata_json=json.dumps({"fanout": fanout, "tender_ids": tender_ids}),
        created_by=user_id,
        expires_at=datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if data.get("expires_at") else None,
    )
    db.add(batch_record)
    for it in batch_items:
        db.add(MessageBatchItem(batch_id=data["id"], custom_id=it["custom_id"],
                                item_type=it["item_type"], tender_id=it["tender_id"]))
    db.commit()
    db.refresh(batch_record)
    return batch_record
```

(Verify `MessageBatchItem` construction matches how the deep path builds items at lines 262-270 — copy any required non-null fields.)

- [ ] **Step 7: Implement `mode="batch"` in `drain_backlog`**

In `scoring_backlog_service.py`, replace the `NotImplementedError` branch:

```python
    if mode == "batch":
        from app.services.batch_service import create_tender_scoring_batch
        scoring = get_scoring_settings(db)
        take = limit or (scoring["auto_scoring_batch_size"] * 10)  # batches can be large
        pending = _pending_query(db).filter(
            Tender.scoring_attempts < scoring["auto_scoring_max_retries"]
        ).order_by(Tender.created_at.desc()).limit(take).all()
        if not pending:
            return {"mode": "batch", "submitted": 0, "deduped_saved": 0, "batch_id": None}
        unique, fanout = dedupe_by_content_hash(pending)
        import asyncio
        batch = asyncio.run(create_tender_scoring_batch(
            db, [t.id for t in unique], fanout))
        return {
            "mode": "batch",
            "submitted": len(unique),
            "deduped_saved": len(pending) - len(unique),
            "batch_id": batch.batch_id,
        }
```

- [ ] **Step 8: Add a test for the dedupe wiring in batch drain (mock the network)**

```python
# append to tests/test_scoring_batch.py
def test_batch_drain_dedupes_before_submit(db, monkeypatch):
    from app.services import scoring_backlog_service as svc

    a = _mk(db, "same")
    b = _mk(db, "same")            # identical title+scope -> one representative
    c = _mk(db, "other")

    captured = {}
    async def _fake_create(db_, ids, fanout, user_id=None):
        captured["ids"] = ids
        captured["fanout"] = fanout
        class _B:  # minimal stand-in
            batch_id = "msgbatch_fake"
        return _B()

    monkeypatch.setattr("app.services.batch_service.create_tender_scoring_batch", _fake_create)
    out = svc.drain_backlog(db, mode="batch")
    assert out["mode"] == "batch"
    assert out["submitted"] == 2          # a/b collapsed, c separate
    assert out["deduped_saved"] == 1
    assert len(captured["ids"]) == 2
```

- [ ] **Step 9: Run the batch tests**

Run: `cd drpl-backend && pytest tests/test_scoring_batch.py -v`
Expected: PASS (all).

- [ ] **Step 10: Commit**

```bash
git add drpl-backend/app/services/batch_service.py drpl-backend/app/services/scoring_backlog_service.py drpl-backend/tests/test_scoring_batch.py
git commit -m "feat(scoring): batch-API drain (50% off) with content-hash dedupe fan-out"
```

---

### Task 5: Admin scoring page — backlog card + two drain buttons

**Files:**
- Modify: `drpl-frontend/src/pages/admin/AdminTenderScoringPage.tsx`
- Modify: `drpl-frontend/src/lib/api.ts` (reuse `getScoringBacklog`/`drainScoring` from Task 3 — no new fns)

**Interfaces:**
- Consumes: `getScoringBacklog`, `drainScoring` (Task 3).
- Produces: no new exports; a backlog panel on the existing page.

- [ ] **Step 1: Load backlog alongside the existing settings/stats/digest load**

In `AdminTenderScoringPage.tsx`, add to the imports from `../../lib/api`: `getScoringBacklog, drainScoring, type ScoringBacklog`. Add state `const [backlog, setBacklog] = useState<ScoringBacklog | null>(null);` and `const [draining, setDraining] = useState(false);`. In `load()`, add `getScoringBacklog()` to the `Promise.all` and `setBacklog(...)`.

- [ ] **Step 2: Add the backlog panel + drain buttons in the render**

Insert above the settings form (after the stats section), reusing the page's existing card styling classes:

```tsx
{backlog && (
  <div className="bg-card rounded-xl border border-border p-5 shadow-card space-y-4">
    <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
      <Gauge size={16} /> Scoring backlog
    </div>
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-center">
      <div><p className="text-2xl font-bold">{backlog.scored}</p><p className="text-xs text-muted-foreground">Scored</p></div>
      <div><p className="text-2xl font-bold">{backlog.pending}</p><p className="text-xs text-muted-foreground">Pending</p></div>
      <div><p className="text-2xl font-bold">{backlog.drainable}</p><p className="text-xs text-muted-foreground">Drainable</p></div>
      <div><p className="text-2xl font-bold">{backlog.in_flight_batches}</p><p className="text-xs text-muted-foreground">Batches in flight</p></div>
    </div>
    <p className="text-xs text-muted-foreground">
      Est. batch cost ≈ ₹{backlog.est_cost_inr} · last reaper run: {backlog.last_reaper_run ?? '—'} ·
      auto-scoring {backlog.enabled ? 'enabled' : 'disabled'} · {backlog.stuck_at_cap} stuck at retry cap
    </p>
    <div className="flex gap-2">
      <button
        disabled={draining || backlog.drainable === 0}
        onClick={async () => {
          setDraining(true);
          try { await drainScoring('live'); await load(); } finally { setDraining(false); }
        }}
        className="px-3 py-2 rounded-lg text-sm font-semibold bg-muted hover:bg-muted/80 disabled:opacity-50"
      >Drain now (fast, full price)</button>
      <button
        disabled={draining || backlog.drainable === 0}
        onClick={async () => {
          setDraining(true);
          try { await drainScoring('batch'); await load(); } finally { setDraining(false); }
        }}
        className="px-3 py-2 rounded-lg text-sm font-semibold bg-accent text-accent-foreground hover:bg-accent/90 disabled:opacity-50"
      >Submit batch (cheap, async)</button>
    </div>
  </div>
)}
```

(`Gauge` is already imported on line 2 of this page.)

- [ ] **Step 3: Build the frontend**

Run: `cd drpl-frontend && npm run build`
Expected: no type errors.

- [ ] **Step 4: Commit**

```bash
git add drpl-frontend/src/pages/admin/AdminTenderScoringPage.tsx
git commit -m "feat(admin): scoring backlog card + drain buttons on tender-scoring page"
```

---

### Task 6: Chat-agent tool — `tender_scoring_status`

**Files:**
- Create: `drpl-backend/app/services/langchain/tools/scoring_status_tool.py`
- Modify: `drpl-backend/app/services/langchain/tools/tool_loader.py:44-77`
- Test: `drpl-backend/tests/test_scoring_status_tool.py`

**Interfaces:**
- Consumes: `scoring_backlog_service.backlog_stats`, `drain_backlog` (Tasks 1-4).
- Produces: `ScoringStatusTool` (`name="tender_scoring_status"`) registered under key `"tender_scoring_status"` in `_TOOL_CLASS_REGISTRY`.

- [ ] **Step 1: Write the failing test**

```python
# drpl-backend/tests/test_scoring_status_tool.py
import json
from app.models.tender import Tender
from app.services.langchain.tools.scoring_status_tool import ScoringStatusTool


def _mk(db, title, score=None):
    t = Tender(title=title, portal="ireps", tender_id=title,
               description="x", ai_relevance_score=score)
    db.add(t); db.commit()
    return t


def test_status_tool_reports_backlog(db):
    _mk(db, "scored", score=0.7)
    _mk(db, "pending")
    tool = ScoringStatusTool(db=db)
    out = json.loads(tool._run(action="status"))
    assert out["pending"] == 1
    assert out["scored"] == 1


def test_status_tool_can_drain(db, monkeypatch):
    from app.services.langchain.tools import scoring_status_tool as mod
    monkeypatch.setattr(mod, "drain_backlog",
                        lambda db, mode, limit=None: {"mode": mode, "scored": 5, "failed": 0})
    tool = ScoringStatusTool(db=db)
    out = json.loads(tool._run(action="drain", mode="live"))
    assert out["scored"] == 5
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && pytest tests/test_scoring_status_tool.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the tool (modeled on `tender_lookup_tool.py`)**

```python
# drpl-backend/app/services/langchain/tools/scoring_status_tool.py
"""LangChain tool: report the tender-scoring backlog and optionally drain it."""
import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.services.scoring_backlog_service import backlog_stats, drain_backlog

logger = logging.getLogger(__name__)


class ScoringStatusInput(BaseModel):
    action: str = Field("status", description="'status' to report the backlog, 'drain' to score pending tenders")
    mode: str = Field("live", description="Drain mode when action='drain': 'live' (fast, full price) or 'batch' (cheap, async)")
    limit: Optional[int] = Field(None, description="Optional cap on how many tenders to drain")


class ScoringStatusTool(BaseTool):
    name: str = "tender_scoring_status"
    description: str = (
        "Report how many tenders are scored vs pending relevance scoring, and optionally "
        "trigger scoring of the pending backlog. Use action='status' to check, action='drain' "
        "to score pending tenders (mode='batch' is cheapest for large backlogs)."
    )
    args_schema: Type[BaseModel] = ScoringStatusInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(self, action: str = "status", mode: str = "live",
             limit: Optional[int] = None) -> str:
        if not self.db:
            return "Error: Database session not available"
        if action == "drain":
            return json.dumps(drain_backlog(self.db, mode=mode, limit=limit), default=str)
        return json.dumps(backlog_stats(self.db), default=str)
```

- [ ] **Step 4: Register the tool**

In `tool_loader.py`, add the import in the `_register` import block (near line 50):

```python
    from app.services.langchain.tools.scoring_status_tool import ScoringStatusTool
```

and add to the `_TOOL_CLASS_REGISTRY.update({...})` dict:

```python
        "tender_scoring_status": ScoringStatusTool,
```

- [ ] **Step 5: Run the tool test + a registry smoke check**

Run: `cd drpl-backend && pytest tests/test_scoring_status_tool.py -v`
Expected: PASS.
Run: `cd drpl-backend && python -c "from app.services.langchain.tools.tool_loader import get_available_tool_keys as g; import app.services.langchain.tools.tool_loader as m; m._register() if hasattr(m,'_register') else None; print('tender_scoring_status' in g())"`
Expected: prints `True` (if `_register` is named differently, call whatever the module uses to populate the registry — check the top of `tool_loader.py`).

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/langchain/tools/scoring_status_tool.py drpl-backend/app/services/langchain/tools/tool_loader.py drpl-backend/tests/test_scoring_status_tool.py
git commit -m "feat(agent): tender_scoring_status tool for backlog awareness + drain"
```

---

### Task 7 (optional): In-process fallback drainer for local `uvicorn`-only dev — **SKIPPED (deliberate)**

**Decision (2026-07-11):** Not implemented. The local `.env` points at the live Neon
production DB, so an in-process startup ticker would begin spending real API budget on
the ~1310-tender backlog every time `uvicorn` starts. The manual "Score Pending" (batch)
button on the dashboard / admin page gives the same result with the operator controlling
when spend happens. Prod (Railway) already runs the self-rescheduling reaper via Redis +
worker, so no coverage gap in production. Revisit only if a local no-Redis auto-drain is
explicitly wanted against a non-production DB.

<details><summary>Original (unimplemented) task steps</summary>

**Files:**
- Modify: `drpl-backend/app/main.py` (startup registration, guarded)
- Test: `drpl-backend/tests/test_scoring_inprocess_ticker.py`

**Interfaces:**
- Consumes: `drain_backlog(mode="live", limit=...)`; `get_queue`; `get_scoring_settings`.
- Produces: a guarded startup coroutine that no-ops when a real RQ queue exists or auto-scoring is disabled.

- [ ] **Step 1: Write the failing test (guard logic only — no live loop)**

```python
# drpl-backend/tests/test_scoring_inprocess_ticker.py
def test_ticker_disabled_when_queue_present(monkeypatch, db):
    from app.main import _should_run_inprocess_scorer
    monkeypatch.setattr("app.main.get_queue", lambda: object())  # queue exists
    assert _should_run_inprocess_scorer(db) is False


def test_ticker_enabled_when_no_queue_and_enabled(monkeypatch, db):
    from app.main import _should_run_inprocess_scorer
    monkeypatch.setattr("app.main.get_queue", lambda: None)
    from app.services.auto_scoring_settings import set_scoring_setting
    set_scoring_setting(db, "auto_scoring_enabled", True)
    assert _should_run_inprocess_scorer(db) is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && pytest tests/test_scoring_inprocess_ticker.py -v`
Expected: FAIL — `_should_run_inprocess_scorer` not defined.

- [ ] **Step 3: Add the guard + a lightweight startup ticker in `main.py`**

Add near the other startup helpers:

```python
from app.core.redis_client import get_queue  # if not already imported

def _should_run_inprocess_scorer(db) -> bool:
    from app.services.auto_scoring_settings import get_scoring_settings
    if get_queue() is not None:      # a real worker/reaper will handle it
        return False
    return bool(get_scoring_settings(db)["auto_scoring_enabled"])
```

In the FastAPI startup path, register a background task that, every `auto_scoring_interval_seconds`, opens a `SessionLocal`, checks `_should_run_inprocess_scorer`, and if true runs `drain_backlog(db, mode="live", limit=batch_size)` once. Model the loop on the existing `asyncio` startup patterns in `main.py`; keep it a strict no-op in prod (queue present).

- [ ] **Step 4: Run the guard test**

Run: `cd drpl-backend && pytest tests/test_scoring_inprocess_ticker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/main.py drpl-backend/tests/test_scoring_inprocess_ticker.py
git commit -m "feat(scoring): guarded in-process drainer for local no-Redis dev"
```

</details>

---

## Final verification (manual, run once after Tasks 1-6)

- [x] Run the full backend suite: `cd drpl-backend && pytest tests/ -q`. **Result: 93 passed, 5 failed. All 5 failures verified pre-existing** (reproduced identically on pre-work commit `93bba19` in a scratch worktree): 2 auth 401-vs-403 in `test_api.py`, 2 relevance-sort in `test_tender_card_fields.py` (blocked by an unrelated `below_threshold` query filter), 1 in `test_offline_signature_overlay.py`. **Zero new failures introduced.**
- [x] Build frontend: `cd drpl-frontend && npm run build`. **Result: clean.**
- [ ] Start backend (`python -m uvicorn app.main:app --port 8000`), log in as master admin:
  - `GET /api/admin/tender-scoring/backlog` → `pending ≈ 1310`, `scored ≈ 1`.
  - Dashboard AI panel now shows Scored / Pending Scoring from the backlog endpoint; "Score Pending" drains (batch when >50).
  - `/admin/tender-scoring` shows the backlog card + both drain buttons.
  - Chat: "how many tenders are unscored?" → agent calls `tender_scoring_status`; "score them" → drain.
- [ ] Confirm a `MessageBatch(batch_type="tender_scoring")` row is created on a batch drain and, after processing, duplicates share the representative's score.

---

## Self-review notes

- **Status (2026-07-11):** Tasks 1–6 implemented, tested, committed. Task 7 deliberately SKIPPED (see task note — local `.env` targets live Neon; manual batch drain preferred over auto-spend on startup). Backend + frontend automated verification done (see Final verification). Remaining = the manual live-server drain of the real backlog, which requires an operator-initiated backend restart (spends real API budget; key confirmed present in Platform Settings).
- **Spec coverage:** backlog_stats (T1) ✓; live drain + endpoints (T2) ✓; dashboard rewire to scoring source of truth + one-click drain (T3) ✓; batch-API 50%-off + dedupe fan-out + result processing (T4) ✓; admin status page card + two drain buttons (T5) ✓; agent tool (T6) ✓; local no-Redis reliability (T7, optional) SKIPPED-by-decision; prod reaper reliability = unchanged (documented in spec §4.3, no task needed). No new columns/infra ✓.
- **Ambiguity resolved:** drain-mode threshold fixed at `drainable > 50` → batch. "Pending" vs "Scored" wording replaces the old deep-analysis "Analyzed"/"Pending Analysis".
- **Verify-before-code:** T1 Step 4 and T4 Step 1 both require confirming the `MessageBatch`/`MessageBatchItem` import path and required fields against the actual model before finalizing — do not assume `app.models.message_batch`.
