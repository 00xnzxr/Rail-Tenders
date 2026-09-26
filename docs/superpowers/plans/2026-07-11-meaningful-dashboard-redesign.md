# Meaningful Dashboard Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scrape-internals dashboard with a decision-oriented one — plain-language hero, meaningful stat cards, a "Do This Next" action queue, a latest-costings panel, and a compact scoring strip — and drop TenderTiger/BidAssist from the dashboard's portal displays.

**Architecture:** Additive backend (extend `/api/tenders/stats` with meaningful counts; add a lean `GET /api/cost-breakdowns/recent`). Frontend: rewrite `DashboardPage.tsx` as a thin composition of focused `src/components/dashboard/*` sub-components, each fed a slice of stats or fetching its own list, all best-effort with graceful empty states. Action-queue/closing-soon reuse the existing `GET /api/tenders` filters — no new tender endpoints.

**Tech Stack:** FastAPI + SQLAlchemy (backend), React 18 + Vite + axios + Tailwind + lucide-react (frontend), pytest (backend tests).

## Global Constraints

- **Promising threshold = AI relevance score ≥ 0.70.** Define once as a backend constant `DASHBOARD_PROMISING_THRESHOLD = 0.70` in `tenders.py`; the frontend never hard-codes it (reads counts from the API).
- Scoring/segments/costing logic is **only surfaced**, never changed. No new DB columns, no migrations.
- Reuse existing `GET /api/tenders` filters for lists: `segment`, `score_min`, `closing_before`, `status`, `sort_by` (allowed: `created_at, closing_date, submission_deadline, priority, relevance`).
- `cost_breakdown.router` has `prefix="/tenders"`; the recent-costings endpoint goes on a **new** router `cost_breakdowns.py` with `prefix="/cost-breakdowns"` to avoid the `/{tender_id}` path clash. Mount it in `main.py` next to the existing `app.include_router(cost_breakdown.router, prefix="/api")` (line ~712).
- Backend tests use pytest with in-memory SQLite; model the `db`/session + import-models-for-`create_all` pattern on `tests/test_scoring_backlog_service.py` and `tests/test_scoring_backlog_routes.py` (StaticPool + `check_same_thread=False` for `TestClient` route tests).
- `TenderTiger`/`BidAssist` stay in ingestion + Scrape Monitor; only the dashboard stops displaying them. The `usePortalHealth` hook is NOT deleted (Scrape Monitor uses it) — only the dashboard stops importing it.
- Frontend must stay within existing Tailwind design tokens (`bg-card`, `border-border`, `text-accent`, `text-muted-foreground`, dark-mode variants) and build clean with `npm run build`.

---

### Task 1: Extend `/api/tenders/stats` with meaningful counts

**Files:**
- Modify: `drpl-backend/app/api/routes/tenders.py:137-160` (`tender_stats`) + add module constant near top.
- Test: `drpl-backend/tests/test_dashboard_stats.py`

**Interfaces:**
- Consumes: `Tender` model (`ai_relevance_score`, `segment`, `status`, `closing_date`), `CostBreakdown` model (`tender_id`).
- Produces: `GET /api/tenders/stats` JSON gains keys `promising_count`, `to_bid_count`, `closing_soon_count`, `promising_open_count`, `with_costing_count` (all ints), alongside the existing `total_tenders`, `by_portal`, `open_tenders`, `analyzed_tenders`, `avg_relevance`.

- [ ] **Step 1: Write the failing test**

```python
# drpl-backend/tests/test_dashboard_stats.py
"""Dashboard stats expose meaningful counts (promising / to_bid / closing_soon / costing)."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.core.auth import get_current_user
from app.main import app
from app.models.tender import Tender

# Register models the stats path touches so create_all builds their tables.
from app.models import platform_setting as _ps  # noqa: F401
from app.models import cost_breakdown as _cb  # noqa: F401


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


_n = 0


def _mk(db, score=None, status="open", segment=None, closing_in_days=None):
    global _n
    _n += 1
    closing = None
    if closing_in_days is not None:
        closing = datetime.now(timezone.utc) + timedelta(days=closing_in_days)
    t = Tender(portal="ireps", tender_id=f"T{_n}", title=f"t{_n}",
               ai_relevance_score=score, status=status, segment=segment,
               closing_date=closing)
    db.add(t)
    db.commit()
    return t


def test_stats_meaningful_counts():
    db = _session()
    try:
        _mk(db, score=0.80, status="open", segment="to_bid", closing_in_days=3)   # promising, open, to_bid, closing soon
        _mk(db, score=0.72, status="open", closing_in_days=30)                     # promising, open, not closing soon
        _mk(db, score=0.69, status="open", closing_in_days=2)                      # NOT promising (below 0.70)
        _mk(db, score=0.95, status="closed", closing_in_days=1)                    # promising but closed
        from app.models.cost_breakdown import CostBreakdown
        db.add(CostBreakdown(tender_id=1, version=1)); db.commit()

        r = _client(db).get("/api/tenders/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["promising_count"] == 3          # 0.80, 0.72, 0.95 (0.69 excluded)
        assert body["to_bid_count"] == 1
        assert body["closing_soon_count"] == 1        # only the 0.80 open+soon one (0.69 excluded, 0.95 closed)
        assert body["promising_open_count"] == 2      # 0.80, 0.72
        assert body["with_costing_count"] == 1
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_dashboard_stats.py -q -p no:warnings`
Expected: FAIL — `KeyError: 'promising_count'` (or assertion error on missing keys).

- [ ] **Step 3: Add the constant + extend `tender_stats`**

In `drpl-backend/app/api/routes/tenders.py`, add near the top (after imports):

```python
DASHBOARD_PROMISING_THRESHOLD = 0.70
```

Ensure these imports exist at the top of the file (add any missing):

```python
from datetime import datetime, timezone, timedelta
from app.models.cost_breakdown import CostBreakdown
```

Replace the `return {...}` block of `tender_stats` (currently lines ~154-160) with computed counts before it:

```python
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=7)

    promising_count = db.query(Tender).filter(
        Tender.ai_relevance_score >= DASHBOARD_PROMISING_THRESHOLD,
    ).count()
    promising_open_count = db.query(Tender).filter(
        Tender.ai_relevance_score >= DASHBOARD_PROMISING_THRESHOLD,
        Tender.status == "open",
    ).count()
    closing_soon_count = db.query(Tender).filter(
        Tender.ai_relevance_score >= DASHBOARD_PROMISING_THRESHOLD,
        Tender.status == "open",
        Tender.closing_date != None,  # noqa: E711
        Tender.closing_date >= now,
        Tender.closing_date <= soon,
    ).count()
    to_bid_count = db.query(Tender).filter(Tender.segment == "to_bid").count()
    with_costing_count = db.query(CostBreakdown.tender_id).distinct().count()

    return {
        "total_tenders": total,
        "by_portal": by_portal,
        "open_tenders": open_count,
        "analyzed_tenders": analyzed_count,
        "avg_relevance": round(avg_relevance, 2) if avg_relevance else None,
        "promising_count": promising_count,
        "promising_open_count": promising_open_count,
        "closing_soon_count": closing_soon_count,
        "to_bid_count": to_bid_count,
        "with_costing_count": with_costing_count,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_dashboard_stats.py -q -p no:warnings`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/api/routes/tenders.py drpl-backend/tests/test_dashboard_stats.py
git commit -m "feat(dashboard): meaningful counts in /tenders/stats"
```

---

### Task 2: `GET /api/cost-breakdowns/recent` endpoint

**Files:**
- Create: `drpl-backend/app/api/routes/cost_breakdowns.py`
- Modify: `drpl-backend/app/main.py` (import + `include_router` near line ~119 and ~712)
- Test: `drpl-backend/tests/test_recent_costings_route.py`

**Interfaces:**
- Consumes: `CostBreakdown` model (`tender_id, grand_total, margin_pct, status, updated_at, title`), `Tender` model (`title`), `get_db`, `get_current_user`.
- Produces: `GET /api/cost-breakdowns/recent?limit=5` → `[{tender_id, title, grand_total, margin_pct, status, updated_at}]`, newest `updated_at` first.

- [ ] **Step 1: Write the failing test**

```python
# drpl-backend/tests/test_recent_costings_route.py
"""GET /api/cost-breakdowns/recent returns newest breakdowns joined to tender title."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.core.auth import get_current_user
from app.main import app
from app.models.tender import Tender
from app.models.cost_breakdown import CostBreakdown


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


def test_recent_costings_newest_first_with_title():
    db = _session()
    try:
        db.add(Tender(id=1, portal="ireps", tender_id="A", title="Alpha tender"))
        db.add(Tender(id=2, portal="ireps", tender_id="B", title="Beta tender"))
        db.commit()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.add(CostBreakdown(tender_id=1, version=1, grand_total=100.0,
                             margin_pct=12.5, status="draft", updated_at=base))
        db.add(CostBreakdown(tender_id=2, version=1, grand_total=200.0,
                             margin_pct=20.0, status="finalized",
                             updated_at=base + timedelta(days=5)))
        db.commit()

        r = _client(db).get("/api/cost-breakdowns/recent?limit=5")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 2
        assert rows[0]["tender_id"] == 2                 # newest updated_at first
        assert rows[0]["title"] == "Beta tender"
        assert rows[0]["grand_total"] == 200.0
        assert rows[0]["status"] == "finalized"
        assert rows[1]["title"] == "Alpha tender"
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_recent_costings_route.py -q -p no:warnings`
Expected: FAIL — 404 (route not mounted).

- [ ] **Step 3: Create the router**

```python
# drpl-backend/app/api/routes/cost_breakdowns.py
"""Lean read endpoints for cost breakdowns not tied to a single tender path."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.user import User
from app.models.tender import Tender
from app.models.cost_breakdown import CostBreakdown

router = APIRouter(prefix="/cost-breakdowns", tags=["cost-breakdowns"])


@router.get("/recent")
def recent_costings(
    limit: int = Query(5, ge=1, le=50),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Most recently updated cost breakdowns, joined to their tender title."""
    rows = (
        db.query(CostBreakdown, Tender.title)
        .join(Tender, Tender.id == CostBreakdown.tender_id)
        .order_by(CostBreakdown.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "tender_id": cb.tender_id,
            "title": title,
            "grand_total": cb.grand_total,
            "margin_pct": cb.margin_pct,
            "status": cb.status,
            "updated_at": cb.updated_at.isoformat() if cb.updated_at else None,
        }
        for cb, title in rows
    ]
```

- [ ] **Step 4: Mount the router in `main.py`**

Add the import alongside the other route imports (near line ~119, where `from app.api.routes import cost_breakdown` is):

```python
from app.api.routes import cost_breakdowns
```

Add the mount right after the existing `app.include_router(cost_breakdown.router, prefix="/api")` (line ~712):

```python
app.include_router(cost_breakdowns.router, prefix="/api")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_recent_costings_route.py -q -p no:warnings`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/api/routes/cost_breakdowns.py drpl-backend/app/main.py drpl-backend/tests/test_recent_costings_route.py
git commit -m "feat(dashboard): GET /cost-breakdowns/recent endpoint"
```

---

### Task 3: Frontend API layer — stats type + recent-costings helper

**Files:**
- Modify: `drpl-frontend/src/types/tender.ts:57-63` (`TenderStats`)
- Modify: `drpl-frontend/src/lib/api.ts` (add `RecentCosting` + `getRecentCostings`)

**Interfaces:**
- Consumes: Task 1 stats fields; Task 2 endpoint.
- Produces: `TenderStats` gains the 5 new optional numeric fields; `api.ts` exports `interface RecentCosting` and `getRecentCostings(limit?: number): Promise<RecentCosting[]>`.

- [ ] **Step 1: Extend the `TenderStats` type**

In `drpl-frontend/src/types/tender.ts`, replace the `TenderStats` interface (lines ~57-63) with:

```typescript
export interface TenderStats {
  total_tenders: number;
  by_portal: Record<string, number>;
  open_tenders: number;
  analyzed_tenders: number;
  avg_relevance: number | null;
  promising_count?: number;
  promising_open_count?: number;
  closing_soon_count?: number;
  to_bid_count?: number;
  with_costing_count?: number;
}
```

- [ ] **Step 2: Add the recent-costings helper**

In `drpl-frontend/src/lib/api.ts`, after the `getScoringBacklog`/`drainScoring` block (added in the scoring-backlog feature), add:

```typescript
export interface RecentCosting {
  tender_id: number;
  title: string;
  grand_total: number | null;
  margin_pct: number | null;
  status: string;
  updated_at: string | null;
}

export async function getRecentCostings(limit = 5): Promise<RecentCosting[]> {
  const { data } = await api.get('/api/cost-breakdowns/recent', { params: { limit } });
  return data as RecentCosting[];
}
```

- [ ] **Step 3: Type-check**

Run: `cd drpl-frontend && npm run build`
Expected: `tsc -b && vite build` completes with no type errors.

- [ ] **Step 4: Commit**

```bash
git add drpl-frontend/src/types/tender.ts drpl-frontend/src/lib/api.ts
git commit -m "feat(dashboard): stats type + getRecentCostings api helper"
```

---

### Task 4: `MeaningfulStats` + `HeroSummary` components

**Files:**
- Create: `drpl-frontend/src/components/dashboard/MeaningfulStats.tsx`
- Create: `drpl-frontend/src/components/dashboard/HeroSummary.tsx`

**Interfaces:**
- Consumes: `TenderStats` (Task 3).
- Produces: `MeaningfulStats` (props `{ stats: TenderStats | null }`) — four cards: Promising / To Bid / Closing This Week / Total. `HeroSummary` (props `{ stats: TenderStats | null }`) — one plain-language sentence.

- [ ] **Step 1: Create `MeaningfulStats.tsx`**

```tsx
// drpl-frontend/src/components/dashboard/MeaningfulStats.tsx
import { Sparkles, Target, CalendarClock, FileText } from 'lucide-react';
import type { TenderStats } from '../../types/tender';

interface Props {
  stats: TenderStats | null;
}

function Card({ icon, value, label, tone }: {
  icon: React.ReactNode; value: number | string; label: string; tone: string;
}) {
  return (
    <div className="bg-card border border-border rounded-xl p-5 shadow-card">
      <div className={`inline-flex items-center justify-center w-9 h-9 rounded-lg mb-3 ${tone}`}>
        {icon}
      </div>
      <p className="text-3xl font-extrabold leading-none tabular-nums text-foreground">{value}</p>
      <p className="text-xs font-medium text-muted-foreground mt-1.5">{label}</p>
    </div>
  );
}

export default function MeaningfulStats({ stats }: Props) {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      <Card
        icon={<Sparkles size={18} className="text-blue-600 dark:text-blue-400" />}
        value={stats?.promising_count ?? '—'}
        label="Promising (≥70% match)"
        tone="bg-blue-500/10"
      />
      <Card
        icon={<Target size={18} className="text-emerald-600 dark:text-emerald-400" />}
        value={stats?.to_bid_count ?? '—'}
        label="Ready to Bid"
        tone="bg-emerald-500/10"
      />
      <Card
        icon={<CalendarClock size={18} className="text-amber-600 dark:text-amber-400" />}
        value={stats?.closing_soon_count ?? '—'}
        label="Closing This Week"
        tone="bg-amber-500/10"
      />
      <Card
        icon={<FileText size={18} className="text-muted-foreground" />}
        value={stats?.total_tenders ?? '—'}
        label="Total Tenders"
        tone="bg-muted"
      />
    </div>
  );
}
```

- [ ] **Step 2: Create `HeroSummary.tsx`**

```tsx
// drpl-frontend/src/components/dashboard/HeroSummary.tsx
import type { TenderStats } from '../../types/tender';

interface Props {
  stats: TenderStats | null;
}

export default function HeroSummary({ stats }: Props) {
  if (!stats) {
    return (
      <div className="h-16 bg-card border border-border rounded-xl animate-pulse" />
    );
  }

  // Scoring hasn't produced any relevance yet — steer the user to scoring instead.
  if (stats.avg_relevance == null || (stats.promising_count ?? 0) === 0) {
    const pending = stats.total_tenders - (stats.analyzed_tenders ?? 0);
    return (
      <div className="bg-card border border-border rounded-xl p-5 shadow-card">
        <p className="text-lg leading-relaxed text-foreground">
          Scoring is still finding your best matches
          {pending > 0 ? <> — <span className="font-bold">{pending.toLocaleString()}</span> tenders waiting.</> : '.'}
        </p>
      </div>
    );
  }

  const promising = stats.promising_count ?? 0;
  const open = stats.promising_open_count ?? 0;
  const soon = stats.closing_soon_count ?? 0;
  const costed = stats.with_costing_count ?? 0;

  return (
    <div className="bg-card border border-border rounded-xl p-5 shadow-card">
      <p className="text-lg leading-relaxed text-foreground">
        You have <span className="font-bold text-blue-600 dark:text-blue-400">{promising.toLocaleString()} promising tenders</span>.{' '}
        <span className="font-bold">{open.toLocaleString()}</span> {open === 1 ? 'is' : 'are'} still open,{' '}
        <span className="font-bold">{soon.toLocaleString()}</span> close this week, and{' '}
        <span className="font-bold">{costed.toLocaleString()}</span> already {costed === 1 ? 'has a costing' : 'have costings'}.
      </p>
    </div>
  );
}
```

- [ ] **Step 3: Type-check**

Run: `cd drpl-frontend && npm run build`
Expected: no type errors. (Components are not yet used — build still succeeds; they'll be wired in Task 7.)

- [ ] **Step 4: Commit**

```bash
git add drpl-frontend/src/components/dashboard/MeaningfulStats.tsx drpl-frontend/src/components/dashboard/HeroSummary.tsx
git commit -m "feat(dashboard): HeroSummary + MeaningfulStats components"
```

---

### Task 5: `ActionQueue` component

**Files:**
- Create: `drpl-frontend/src/components/dashboard/ActionQueue.tsx`

**Interfaces:**
- Consumes: `getTenders` (existing) with `score_min`, `status`, `closing_before`, `sort_by`; `Tender` type; `TenderStats` (for `with_costing` context is not needed — queue is list-driven).
- Produces: `ActionQueue` (no props) — self-fetches and renders a prioritized next-action list; each row navigates to `/tenders/:id/command-center`.

- [ ] **Step 1: Create `ActionQueue.tsx`**

```tsx
// drpl-frontend/src/components/dashboard/ActionQueue.tsx
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, CalendarClock, Calculator, Search } from 'lucide-react';
import { getTenders } from '../../lib/api';
import type { Tender } from '../../types/tender';
import { differenceInDays, parseISO } from 'date-fns';

type QueueItem = { tender: Tender; reason: string; icon: React.ReactNode };

const PROMISING = 0.7;

export default function ActionQueue() {
  const navigate = useNavigate();
  const [items, setItems] = useState<QueueItem[] | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const soon = new Date();
        soon.setDate(soon.getDate() + 7);
        // Priority 1: promising + open + closing within 7 days.
        const closing = await getTenders({
          status: 'open', score_min: PROMISING,
          closing_before: soon.toISOString(), sort_by: 'closing_date', limit: 5,
        } as any);
        // Priority 2: promising + open, most relevant first (fills remaining slots).
        const promising = await getTenders({
          status: 'open', score_min: PROMISING, sort_by: 'relevance', limit: 10,
        } as any);

        const seen = new Set<number>();
        const out: QueueItem[] = [];
        for (const t of closing.items) {
          if (seen.has(t.id) || !t.closing_date) continue;
          seen.add(t.id);
          const d = differenceInDays(parseISO(t.closing_date), new Date());
          out.push({
            tender: t,
            reason: d <= 0 ? 'Closes today — review now' : `Review — closes in ${d} day${d === 1 ? '' : 's'}`,
            icon: <CalendarClock size={16} className="text-amber-600 dark:text-amber-400" />,
          });
        }
        for (const t of promising.items) {
          if (seen.has(t.id) || out.length >= 6) continue;
          seen.add(t.id);
          out.push({
            tender: t,
            reason: 'Start costing',
            icon: <Calculator size={16} className="text-emerald-600 dark:text-emerald-400" />,
          });
        }
        // Fallback: if scoring is behind and nothing promising surfaced, suggest reviewing recent open tenders.
        if (out.length === 0) {
          const recent = await getTenders({ status: 'open', sort_by: 'created_at', limit: 6 } as any);
          for (const t of recent.items) {
            out.push({
              tender: t,
              reason: 'Review',
              icon: <Search size={16} className="text-muted-foreground" />,
            });
          }
        }
        setItems(out);
      } catch {
        setItems([]);
      }
    })();
  }, []);

  return (
    <div className="bg-card border border-border rounded-xl shadow-card overflow-hidden">
      <div className="px-5 py-3.5 border-b border-border">
        <h3 className="text-sm font-bold text-foreground">Do This Next</h3>
      </div>
      {items == null ? (
        <div className="p-5 space-y-3">
          {[0, 1, 2].map((i) => <div key={i} className="h-10 bg-muted/50 rounded animate-pulse" />)}
        </div>
      ) : items.length === 0 ? (
        <p className="p-5 text-sm text-muted-foreground">Nothing needs attention right now. 🎉</p>
      ) : (
        <ul className="divide-y divide-border">
          {items.map(({ tender, reason, icon }) => (
            <li key={tender.id}>
              <button
                onClick={() => navigate(`/tenders/${tender.id}/command-center`)}
                className="w-full flex items-center gap-3 px-5 py-3 text-left hover:bg-muted/50 transition-colors group"
              >
                <span className="shrink-0">{icon}</span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold text-foreground truncate">{tender.title}</span>
                  <span className="block text-xs text-muted-foreground">{reason}</span>
                </span>
                <ArrowRight size={16} className="shrink-0 text-muted-foreground group-hover:text-accent transition-colors" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd drpl-frontend && npm run build`
Expected: no type errors. (`as any` on the filter objects avoids over-tightening `TenderFilters`; `closing_date` exists on `Tender`.)

Note: verify `Tender` has `closing_date` and `title` — confirm with `grep -n "closing_date\|title" drpl-frontend/src/types/tender.ts`. If `getTenders` return omits `closing_date`, drop the day-count and use a static "Closing soon" label.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/dashboard/ActionQueue.tsx
git commit -m "feat(dashboard): prioritized 'Do This Next' action queue"
```

---

### Task 6: `LatestCostings` + compact `ScoringStrip` components

**Files:**
- Create: `drpl-frontend/src/components/dashboard/LatestCostings.tsx`
- Create: `drpl-frontend/src/components/dashboard/ScoringStrip.tsx`

**Interfaces:**
- Consumes: `getRecentCostings` (Task 3); `getScoringBacklog` / `drainScoring` / `ScoringBacklog` (existing); `formatCurrency` from `../../lib/formatters`.
- Produces: `LatestCostings` (no props) — self-fetches recent breakdowns. `ScoringStrip` (no props) — compact one-line backlog strip + "Score Pending" button.

- [ ] **Step 1: Create `LatestCostings.tsx`**

```tsx
// drpl-frontend/src/components/dashboard/LatestCostings.tsx
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { getRecentCostings, type RecentCosting } from '../../lib/api';
import { formatCurrency } from '../../lib/formatters';

export default function LatestCostings() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<RecentCosting[] | null>(null);

  useEffect(() => {
    getRecentCostings(5).then(setRows).catch(() => setRows([]));
  }, []);

  return (
    <div className="bg-card border border-border rounded-xl shadow-card overflow-hidden">
      <div className="px-5 py-3.5 border-b border-border">
        <h3 className="text-sm font-bold text-foreground">Latest Costings</h3>
      </div>
      {rows == null ? (
        <div className="p-5 space-y-3">
          {[0, 1, 2].map((i) => <div key={i} className="h-10 bg-muted/50 rounded animate-pulse" />)}
        </div>
      ) : rows.length === 0 ? (
        <p className="p-5 text-sm text-muted-foreground">No costings yet. Open a tender's Command Center to build one.</p>
      ) : (
        <ul className="divide-y divide-border">
          {rows.map((c) => (
            <li key={`${c.tender_id}-${c.updated_at}`}>
              <button
                onClick={() => navigate(`/tenders/${c.tender_id}/command-center`)}
                className="w-full flex items-center gap-3 px-5 py-3 text-left hover:bg-muted/50 transition-colors group"
              >
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold text-foreground truncate">{c.title}</span>
                  <span className="block text-xs text-muted-foreground">
                    {c.grand_total != null ? formatCurrency(c.grand_total) : '—'}
                    {c.margin_pct != null && <> · {Math.round(c.margin_pct)}% margin</>}
                  </span>
                </span>
                <span className={`shrink-0 px-2 py-0.5 rounded-md text-[0.68rem] font-bold border ${
                  c.status === 'finalized'
                    ? 'bg-emerald-500/12 text-emerald-700 dark:text-emerald-400 border-emerald-500/25'
                    : 'bg-muted text-muted-foreground border-border'
                }`}>{c.status}</span>
                <ArrowRight size={16} className="shrink-0 text-muted-foreground group-hover:text-accent transition-colors" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Create `ScoringStrip.tsx` (compact fold of the old AI Intelligence panel)**

```tsx
// drpl-frontend/src/components/dashboard/ScoringStrip.tsx
import { useEffect, useState } from 'react';
import { Zap } from 'lucide-react';
import { getScoringBacklog, drainScoring, type ScoringBacklog } from '../../lib/api';

export default function ScoringStrip() {
  const [backlog, setBacklog] = useState<ScoringBacklog | null>(null);
  const [draining, setDraining] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = async () => {
    try { setBacklog(await getScoringBacklog()); } catch { /* best-effort */ }
  };
  useEffect(() => { void load(); }, []);

  const drain = async () => {
    if (!backlog) return;
    setDraining(true);
    setMsg(null);
    try {
      const mode = backlog.drainable > 50 ? 'batch' : 'live';
      const r = await drainScoring(mode);
      setMsg(mode === 'batch'
        ? `Submitted ${r.submitted ?? backlog.drainable} for scoring${r.deduped_saved ? ` (${r.deduped_saved} dupes skipped)` : ''}.`
        : `Scored ${r.scored ?? 0}. ${r.failed ?? 0} failed.`);
      await load();
    } catch (e: any) {
      setMsg(e?.response?.data?.detail || 'Scoring failed. Check API key.');
    } finally {
      setDraining(false);
    }
  };

  if (!backlog) return null;

  return (
    <div className="flex items-center gap-4 flex-wrap bg-card border border-border rounded-xl px-5 py-3 shadow-card">
      <span className="text-xs text-muted-foreground">
        {backlog.avg_relevance != null && <><span className="font-bold text-foreground">{Math.round((backlog.avg_relevance ?? 0) * 100)}%</span> avg relevance · </>}
        <span className="font-bold text-foreground">{backlog.scored.toLocaleString()}</span> scored ·{' '}
        <span className="font-bold text-foreground">{backlog.pending.toLocaleString()}</span> pending
      </span>
      {msg && <span className="text-xs text-accent">{msg}</span>}
      <div className="flex-1" />
      <button
        onClick={drain}
        disabled={draining || backlog.drainable === 0}
        className="inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg bg-accent text-accent-foreground hover:bg-accent/90 disabled:opacity-50"
      >
        <Zap size={13} /> {draining ? 'Scoring…' : 'Score Pending'}
      </button>
    </div>
  );
}
```

Note: `ScoringBacklog` (from `api.ts`) does not currently include `avg_relevance`. Either (a) add `avg_relevance?: number | null` to the `ScoringBacklog` interface and to `backlog_stats` in `scoring_backlog_service.py` (returning `avg` of non-null scores), OR (b) drop the avg-relevance clause from the strip. **Choose (b)** to keep this task frontend-only — remove the `backlog.avg_relevance` line and its guard. (The four meaningful cards already cover the headline metric.)

- [ ] **Step 3: Apply choice (b)** — delete the `avg_relevance` clause from `ScoringStrip.tsx` so it reads:

```tsx
      <span className="text-xs text-muted-foreground">
        <span className="font-bold text-foreground">{backlog.scored.toLocaleString()}</span> scored ·{' '}
        <span className="font-bold text-foreground">{backlog.pending.toLocaleString()}</span> pending
      </span>
```

- [ ] **Step 4: Type-check**

Run: `cd drpl-frontend && npm run build`
Expected: no type errors.

- [ ] **Step 5: Commit**

```bash
git add drpl-frontend/src/components/dashboard/LatestCostings.tsx drpl-frontend/src/components/dashboard/ScoringStrip.tsx
git commit -m "feat(dashboard): LatestCostings panel + compact ScoringStrip"
```

---

### Task 7: Recompose `DashboardPage` + remove Portal Health / Tenders-by-Portal

**Files:**
- Create: `drpl-frontend/src/components/dashboard/RecentTendersTable.tsx` (extract existing table)
- Modify: `drpl-frontend/src/pages/DashboardPage.tsx` (full recompose)

**Interfaces:**
- Consumes: `HeroSummary`, `MeaningfulStats` (Task 4), `ActionQueue` (Task 5), `LatestCostings`, `ScoringStrip` (Task 6), `useStats` (existing), `useTenders` (existing).
- Produces: a recomposed dashboard. `usePortalHealth` and the portal-breakdown JSX are removed from this page only.

- [ ] **Step 1: Extract the recent-tenders table**

Create `drpl-frontend/src/components/dashboard/RecentTendersTable.tsx` and move the existing table + `AIScoreBadge` helper out of `DashboardPage.tsx` verbatim, exposing:

```tsx
// drpl-frontend/src/components/dashboard/RecentTendersTable.tsx
import { useNavigate } from 'react-router-dom';
import PortalBadge from '../ui/PortalBadge';
import StatusBadge from '../ui/StatusBadge';
import { formatCurrency, formatDate } from '../../lib/formatters';
import type { Tender } from '../../types/tender';

function AIScoreBadge({ score }: { score: number | null }) {
  if (score == null) return <span className="text-xs text-muted-foreground/50">—</span>;
  const pct = Math.round(score * 100);
  const color =
    pct >= 70 ? 'text-emerald-600 bg-emerald-50 dark:text-emerald-400 dark:bg-emerald-500/15' :
    pct >= 40 ? 'text-amber-600 bg-amber-50 dark:text-amber-400 dark:bg-amber-500/15' :
                'text-red-600 bg-red-50 dark:text-red-400 dark:bg-red-500/15';
  return <span className={`inline-block px-2 py-0.5 rounded-md text-xs font-semibold ${color}`}>{pct}%</span>;
}

export default function RecentTendersTable({ tenders }: { tenders: Tender[] }) {
  const navigate = useNavigate();
  return (
    <div className="bg-card rounded-xl border border-border overflow-hidden shadow-card">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border">
              <th className="px-4 py-3 text-left text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Title</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Portal</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Status</th>
              <th className="px-4 py-3 text-right text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Value</th>
              <th className="px-4 py-3 text-center text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">AI Score</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Closing</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {tenders.map((t) => (
              <tr key={t.id} onClick={() => navigate(`/tenders/${t.id}`)}
                  className="hover:bg-muted/50 cursor-pointer transition-colors">
                <td className="px-4 py-3 text-sm text-foreground font-medium max-w-xs truncate">{t.title}</td>
                <td className="px-4 py-3"><PortalBadge portal={t.portal} /></td>
                <td className="px-4 py-3"><StatusBadge status={t.status} /></td>
                <td className="px-4 py-3 text-sm text-muted-foreground text-right tabular-nums">{formatCurrency(t.estimated_value)}</td>
                <td className="px-4 py-3 text-center"><AIScoreBadge score={t.ai_relevance_score} /></td>
                <td className="px-4 py-3 text-sm text-muted-foreground tabular-nums">{formatDate(t.closing_date)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Rewrite `DashboardPage.tsx`**

Replace the entire file with:

```tsx
// drpl-frontend/src/pages/DashboardPage.tsx
import { useNavigate } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import HeroSummary from '../components/dashboard/HeroSummary';
import MeaningfulStats from '../components/dashboard/MeaningfulStats';
import ActionQueue from '../components/dashboard/ActionQueue';
import LatestCostings from '../components/dashboard/LatestCostings';
import ScoringStrip from '../components/dashboard/ScoringStrip';
import RecentTendersTable from '../components/dashboard/RecentTendersTable';
import { useStats } from '../hooks/useStats';
import { useTenders } from '../hooks/useTenders';

export default function DashboardPage() {
  const { stats, loading: statsLoading } = useStats();
  const { tenders, loading: tendersLoading } = useTenders({ limit: 10, offset: 0 });
  const navigate = useNavigate();

  if (statsLoading) return <><Header title="Dashboard" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Dashboard" subtitle="Your tenders at a glance" />
      <div className="p-6 space-y-6">
        <HeroSummary stats={stats} />
        <MeaningfulStats stats={stats} />
        <ScoringStrip />

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <ActionQueue />
          <LatestCostings />
        </div>

        <section>
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Recent Tenders</h3>
            <button onClick={() => navigate('/tenders')}
                    className="flex items-center gap-1 text-sm text-accent hover:text-accent/80 font-medium transition-colors">
              View all <ArrowRight size={14} strokeWidth={2} />
            </button>
          </div>
          {tendersLoading ? <LoadingSpinner /> : <RecentTendersTable tenders={tenders} />}
        </section>
      </div>
    </>
  );
}
```

This removes: the raw top stat cards, the Portal Health section (`usePortalHealth`, `PortalHealthCard`, alerts), the "Tenders by Portal" row, and the old AI Intelligence panel — dropping TenderTiger/BidAssist from the dashboard.

- [ ] **Step 3: Build + verify no dangling imports**

Run: `cd drpl-frontend && npm run build`
Expected: clean. If `usePortalHealth`, `PortalHealthCard`, `StatCard`, `MetricCard`, `Brain`, `Globe`, etc. are now unused **in this file**, they are simply not imported by the new file — confirm no other file lost its import. `usePortalHealth` must still exist for the Scrape Monitor page (do not delete the hook file).

- [ ] **Step 4: Commit**

```bash
git add drpl-frontend/src/components/dashboard/RecentTendersTable.tsx drpl-frontend/src/pages/DashboardPage.tsx
git commit -m "feat(dashboard): recompose page — drop portal-health/portal-breakdown, add meaningful panels"
```

---

### Task 8: Visual-design polish pass

**Files:**
- Modify: `drpl-frontend/src/components/dashboard/*.tsx`, `drpl-frontend/src/pages/DashboardPage.tsx`

**Interfaces:** No API changes — purely presentational refinement.

- [ ] **Step 1: Invoke the design skill**

Use the `frontend-design` skill to refine hierarchy, typography, spacing, and the hero's prominence across the dashboard components. Constraints: stay within existing Tailwind tokens (`bg-card`, `border-border`, `text-accent`, `text-muted-foreground`, `shadow-card`), keep light+dark parity, and do not change any data/props/API. Make the `HeroSummary` the clear focal point and ensure the four `MeaningfulStats` cards read as a single system.

- [ ] **Step 2: Build**

Run: `cd drpl-frontend && npm run build`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/dashboard drpl-frontend/src/pages/DashboardPage.tsx
git commit -m "style(dashboard): visual polish pass on the meaningful dashboard"
```

---

## Status (2026-07-11)

Tasks 1–8 implemented, tested, committed on `feat/scoring-backlog-awareness`. **Extra (post-plan, user-approved):** the Portal Health cards turned out to be consumed only by the dashboard, so instead of leaving dead code they were **moved to the Scrape Monitor page** (`ScrapeMonitorPage.tsx`) — `usePortalHealth`/`PortalHealthCard` stay in use. Remaining = the manual live-server view of the redesigned dashboard, which needs an operator backend restart (to load the new `/cost-breakdowns/recent` endpoint + extended stats).

## Final verification (run once after Tasks 1-8)

- [x] Backend suite: 95 passed / 5 failed — all 5 pre-existing (verified earlier on commit `93bba19`); the two new dashboard tests pass. NO new failures.
- [x] Frontend build: clean.
- [ ] Manual (backend running, logged in): dashboard shows the hero sentence, four meaningful cards, "Do This Next" queue (rows → command center), Latest Costings, compact scoring strip, and recent tenders — and NO Portal Health / Tenders-by-Portal / TenderTiger / BidAssist anywhere on the dashboard.
- [ ] `GET /api/tenders/stats` returns the 5 new fields; `GET /api/cost-breakdowns/recent?limit=5` returns newest-first rows with titles.

---

## Self-review notes

- **Spec coverage:** hero sentence (T4) ✓; meaningful cards ≥70%/to_bid/closing-soon/total (T1+T4) ✓; action queue with closing-soon-first priority (T5) ✓; latest costings panel (T2+T3+T6) ✓; compact scoring strip (T6) ✓; remove Portal Health + Tenders-by-Portal incl. TenderTiger/BidAssist (T7) ✓; keep recent tenders (T7) ✓; additive backend, no schema change (T1+T2) ✓; visual polish (T8) ✓; `usePortalHealth` preserved for Scrape Monitor (T7 note) ✓.
- **Ambiguity resolved:** `ScoringStrip.avg_relevance` dropped to keep Task 6 frontend-only (choice (b)); action-queue `TenderFilters` passed via `as any` to avoid over-tightening the shared type; recent-costings on a new `/cost-breakdowns` router to dodge the `/{tender_id}` path clash.
- **Verify-before-code:** T5 Step 2 requires confirming `Tender.closing_date`/`title` exist in the frontend type before relying on them; T1/T2 confirmed `CostBreakdown` fields (`grand_total, margin_pct, status, updated_at, tender_id`) against the model.
