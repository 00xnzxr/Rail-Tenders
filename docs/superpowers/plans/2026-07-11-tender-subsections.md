# Tender Sub-sections (Decision-Stage Views) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the dense Tenders filter bar into five sidebar decision-stage views (To Bid / Worth a Look / Closing Soon / New–Unscored / All) with live count badges, a per-view header, and a collapsible "Refine" bar layered on top of the view.

**Architecture:** A single frontend `TENDER_VIEWS` map defines each view's label, color, blurb, route slug, and base filters — read by the sidebar, the route resolver, and the page header. Views are routes (`/tenders/:view`). The existing `TenderFilters` component is reused inside a collapsible `RefineBar`, merged over the view's base filters. Backend adds one `unscored` filter and one `GET /api/tenders/view-counts` endpoint for the sidebar badges.

**Tech Stack:** FastAPI + SQLAlchemy (backend), React 18 + Vite + react-router + axios + Tailwind + lucide-react (frontend), pytest.

## Global Constraints

- **View → filter definitions (exact):**
  - `to_bid` → `{ segment: "to_bid" }`
  - `worth_a_look` → `{ segment: "not_bidable" }`
  - `closing_soon` → `{ status: "open", score_min: 0.70, closing_before: <now+7d ISO> }`
  - `new_unscored` → `{ unscored: true }`
  - `all` → `{}` (no view filter)
- Reuse existing helpers/components verbatim where possible: `TenderFilters` (its `onApply` delta), `useTenders`, `getTenders`, `count_tenders`, `_apply_tender_filters`, the `SidebarNav` "Platform Admin" expander pattern.
- No new DB columns, no migrations, no change to scoring/segmentation or the tender card/detail/command-center.
- Backend tests: pytest + in-memory SQLite, StaticPool + `check_same_thread=False` for `TestClient` route tests (see `tests/test_dashboard_stats.py`). Register `platform_setting` + `cost_breakdown` models for `create_all` where the app import needs them.
- Frontend must build clean (`npm run build`) and stay within existing Tailwind tokens (`bg-card`, `border-border`, `text-accent`, `text-muted-foreground`, semantic emerald/amber, dark-mode).
- The `/api/tenders` router is mounted at `/api`; `tenders.py` route paths are relative to it. The tenders LIST route is `GET /api/tenders/` (trailing slash, per `getTenders`).
- Semantic colors per view: To Bid = emerald, Closing Soon = amber, New/Unscored = accent(blue), Worth a Look = slate(muted), All = slate.

---

### Task 1: Backend `unscored` filter

**Files:**
- Modify: `drpl-backend/app/services/tender_service.py:294-340` (`_apply_tender_filters`) and the `get_tenders` signature (`:352+`).
- Modify: `drpl-backend/app/api/routes/tenders.py` (list route — add `unscored` query param + pass-through).
- Test: `drpl-backend/tests/test_unscored_filter.py`

**Interfaces:**
- Consumes: `Tender.ai_relevance_score`, `Tender.below_threshold`.
- Produces: `_apply_tender_filters(..., unscored: bool = False)` filters `ai_relevance_score IS NULL` when true; `get_tenders(..., unscored=False)` forwards it; `GET /api/tenders/?unscored=true` returns only unscored tenders (including below-threshold-null rows).

- [ ] **Step 1: Write the failing test**

```python
# drpl-backend/tests/test_unscored_filter.py
"""The unscored filter returns only tenders with a NULL ai_relevance_score."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.services.tender_service import get_tenders, count_tenders


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


_n = 0


def _mk(db, score=None, below=False):
    global _n
    _n += 1
    t = Tender(portal="ireps", tender_id=f"U{_n}", title=f"u{_n}",
               ai_relevance_score=score, below_threshold=below)
    db.add(t); db.commit()
    return t


def test_unscored_returns_only_null_scores():
    db = _session()
    _mk(db, score=0.8)
    _mk(db, score=None)                 # unscored
    _mk(db, score=None, below=False)    # unscored
    _mk(db, score=0.2)

    items = get_tenders(db, unscored=True, include_below_threshold=True, limit=50, offset=0)
    assert len(items) == 2
    assert all(t.ai_relevance_score is None for t in items)
    assert count_tenders(db, unscored=True, include_below_threshold=True) == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_unscored_filter.py -q -p no:warnings`
Expected: FAIL — `TypeError: _apply_tender_filters() got an unexpected keyword argument 'unscored'` (surfaced via get_tenders).

- [ ] **Step 3: Add the `unscored` param to `_apply_tender_filters`**

In `tender_service.py`, add `unscored=False` to the `_apply_tender_filters` keyword-only signature (append to the existing `eligibility_status=None` line):

```python
                          closing_after=None, closing_before=None, eligibility_status=None,
                          unscored=False):
```

Add the filter clause (place it right after the `score_max` block, ~line 325):

```python
    if unscored:
        query = query.filter(Tender.ai_relevance_score.is_(None))
```

- [ ] **Step 4: Thread `unscored` through `get_tenders`**

In `get_tenders` (signature ~line 352), add `unscored: bool = False` to the params and include it in the `_apply_tender_filters(...)` call (the call already forwards the other filter kwargs — add `unscored=unscored`). `count_tenders` already forwards `**filters`, so it needs no change.

- [ ] **Step 5: Run the service test**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_unscored_filter.py -q -p no:warnings`
Expected: PASS.

- [ ] **Step 6: Add `unscored` to the list route**

In `tenders.py`, find the list endpoint (the one calling `get_tenders`, around line 44-86 where `score_min`/`score_max` Query params are declared). Add a query param near the others:

```python
    unscored: bool = Query(False, description="Only tenders with no AI relevance score yet"),
```

Then pass it into the `get_tenders(...)` call's `common`/kwargs. Find how `score_min`/`score_max` are threaded (the route builds a `common` dict or passes kwargs at ~line 81-86) and add `unscored=unscored` the same way. When `unscored` is true, also force including below-threshold rows (unscored rows have no threshold): set `include_below_threshold=True` in that call when `unscored` is set. Concretely, right before the `get_tenders`/`count` calls:

```python
    if unscored:
        include_below_threshold = True
```

(Only add that line if `include_below_threshold` is a local var in the route; if it's passed inline, pass `include_below_threshold=(include_below_threshold or unscored)`.)

- [ ] **Step 7: Verify the route wiring compiles + imports**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -c "import app.api.routes.tenders; import app.services.tender_service; print('ok')"`
Expected: prints `ok` (ignore SQLAlchemy/INFO log lines).

- [ ] **Step 8: Commit**

```bash
git add drpl-backend/app/services/tender_service.py drpl-backend/app/api/routes/tenders.py drpl-backend/tests/test_unscored_filter.py
git commit -m "feat(tenders): add unscored (null-score) list filter"
```

---

### Task 2: `GET /api/tenders/view-counts` endpoint

**Files:**
- Modify: `drpl-backend/app/api/routes/tenders.py` (add the route; reuse `DASHBOARD_PROMISING_THRESHOLD`, `count_tenders`).
- Test: `drpl-backend/tests/test_view_counts_route.py`

**Interfaces:**
- Consumes: `count_tenders` (Task 1 forwards `unscored`), `Tender`, `datetime/timezone/timedelta` (already imported in `tenders.py`), `DASHBOARD_PROMISING_THRESHOLD` (already defined in `tenders.py`).
- Produces: `GET /api/tenders/view-counts` → `{ "to_bid": int, "worth_a_look": int, "closing_soon": int, "new_unscored": int, "all": int }`.

- [ ] **Step 1: Write the failing test**

```python
# drpl-backend/tests/test_view_counts_route.py
"""GET /api/tenders/view-counts returns the five decision-stage counts."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.core.auth import get_current_user
from app.main import app
from app.models.tender import Tender
from app.models import platform_setting as _ps  # noqa: F401
from app.models import cost_breakdown as _cb  # noqa: F401


def _session():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


_n = 0


def _mk(db, score=None, status="open", segment=None, closing_in_days=None, below=False):
    global _n
    _n += 1
    closing = datetime.now(timezone.utc) + timedelta(days=closing_in_days) if closing_in_days is not None else None
    t = Tender(portal="ireps", tender_id=f"V{_n}", title=f"v{_n}",
               ai_relevance_score=score, status=status, segment=segment,
               closing_date=closing, below_threshold=below)
    db.add(t); db.commit()
    return t


def test_view_counts():
    db = _session()
    try:
        _mk(db, score=0.9, status="open", segment="to_bid", closing_in_days=3)   # to_bid + closing_soon
        _mk(db, score=0.8, status="open", segment="to_bid", closing_in_days=40)  # to_bid only
        _mk(db, score=0.5, segment="not_bidable")                                # worth_a_look
        _mk(db, score=None)                                                      # new_unscored
        _mk(db, score=None, below=True)                                          # new_unscored (below-threshold null)

        r = _client(db).get("/api/tenders/view-counts")
        assert r.status_code == 200
        body = r.json()
        assert body["to_bid"] == 2
        assert body["worth_a_look"] == 1
        assert body["closing_soon"] == 1          # only the 0.9 open+3d one
        assert body["new_unscored"] == 2          # both nulls, incl. below-threshold
        assert body["all"] >= 3                   # non-archived, non-below-threshold default list
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_view_counts_route.py -q -p no:warnings`
Expected: FAIL — 404.

- [ ] **Step 3: Add the route to `tenders.py`**

Place this near the existing `tender_stats` route (both are collection-level GETs; put it BEFORE any `/{tender_id}` route so the literal path wins). Reuse the module-level `DASHBOARD_PROMISING_THRESHOLD` and imports already present:

```python
@router.get("/view-counts")
def tender_view_counts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Counts for the five sidebar decision-stage views, in one request."""
    from app.services.tender_service import count_tenders
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=7)
    return {
        "to_bid": count_tenders(db, segment="to_bid"),
        "worth_a_look": count_tenders(db, segment="not_bidable"),
        "closing_soon": count_tenders(
            db, status="open", score_min=DASHBOARD_PROMISING_THRESHOLD,
            closing_after=now, closing_before=soon,
        ),
        "new_unscored": count_tenders(db, unscored=True, include_below_threshold=True),
        "all": count_tenders(db),
    }
```

Note: confirm `count_tenders` accepts `status`, `score_min`, `closing_after`, `closing_before`, `segment`, `unscored`, `include_below_threshold` — it forwards `**filters` to `_apply_tender_filters`, which after Task 1 has all of these. If the route file mounts `/{tender_id}` GETs textually before this, move this def above them OR rely on FastAPI preferring the static path (it does for exact matches, but ordering is safest).

- [ ] **Step 4: Run the route test**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_view_counts_route.py -q -p no:warnings`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/api/routes/tenders.py drpl-backend/tests/test_view_counts_route.py
git commit -m "feat(tenders): GET /tenders/view-counts for sidebar badges"
```

---

### Task 3: Frontend view map + API helpers

**Files:**
- Create: `drpl-frontend/src/lib/tenderViews.ts`
- Modify: `drpl-frontend/src/lib/api.ts` (add `getTenderViewCounts`; thread `unscored`)
- Modify: `drpl-frontend/src/types/tender.ts` (`TenderFilters` gains `unscored?`)

**Interfaces:**
- Consumes: `TenderFilters` type.
- Produces:
  - `tenderViews.ts`: `TENDER_VIEWS` array of `TenderView` and `resolveView(slug?: string): TenderView`.
  - `api.ts`: `interface TenderViewCounts`, `getTenderViewCounts(): Promise<TenderViewCounts>`, and `getTenders` sends `unscored`.

- [ ] **Step 1: Create `tenderViews.ts`**

```typescript
// drpl-frontend/src/lib/tenderViews.ts
import type { TenderFilters } from '../types/tender';

export type TenderViewKey = 'to_bid' | 'worth_a_look' | 'closing_soon' | 'new_unscored' | 'all';

export interface TenderView {
  key: TenderViewKey;
  slug: string;                 // route segment; '' for All (bare /tenders)
  label: string;                // sidebar + header label
  blurb: string;                // one plain-language sentence in the header
  color: 'emerald' | 'amber' | 'accent' | 'slate';
  countKey: keyof import('./api').TenderViewCounts;
  baseFilters: () => Partial<TenderFilters>;   // fn so closing_soon computes a fresh date
}

function inSevenDaysISO(): string {
  const d = new Date();
  d.setDate(d.getDate() + 7);
  return d.toISOString();
}

export const TENDER_VIEWS: TenderView[] = [
  {
    key: 'to_bid', slug: 'to-bid', label: 'To Bid', color: 'emerald', countKey: 'to_bid',
    blurb: 'Tenders the AI scored as worth bidding on — high match and above your value threshold. Start here.',
    baseFilters: () => ({ segment: 'to_bid' }),
  },
  {
    key: 'worth_a_look', slug: 'worth-a-look', label: 'Worth a Look', color: 'slate', countKey: 'worth_a_look',
    blurb: 'Partial or lower-value matches worth a quick review before you skip them.',
    baseFilters: () => ({ segment: 'not_bidable' }),
  },
  {
    key: 'closing_soon', slug: 'closing-soon', label: 'Closing Soon', color: 'amber', countKey: 'closing_soon',
    blurb: 'Promising tenders that close within the next 7 days — act before the window shuts.',
    baseFilters: () => ({ status: 'open', score_min: 0.7, closing_before: inSevenDaysISO() }),
  },
  {
    key: 'new_unscored', slug: 'new', label: 'New / Unscored', color: 'accent', countKey: 'new_unscored',
    blurb: 'Freshly ingested tenders the AI has not scored yet.',
    baseFilters: () => ({ unscored: true }),
  },
  {
    key: 'all', slug: '', label: 'All Tenders', color: 'slate', countKey: 'all',
    blurb: 'Every tender on the platform.',
    baseFilters: () => ({}),
  },
];

export function resolveView(slug?: string): TenderView {
  if (!slug) return TENDER_VIEWS.find((v) => v.key === 'all')!;
  return TENDER_VIEWS.find((v) => v.slug === slug) ?? TENDER_VIEWS.find((v) => v.key === 'all')!;
}
```

- [ ] **Step 2: Add `unscored` to the `TenderFilters` type**

In `drpl-frontend/src/types/tender.ts`, add to the `TenderFilters` interface (near `score_min`/`score_max`):

```typescript
  unscored?: boolean;
```

- [ ] **Step 3: Thread `unscored` in `getTenders` + add `getTenderViewCounts`**

In `api.ts`, inside `getTenders`, after the `score_max` line (line ~83) add:

```typescript
  if (filters.unscored) params.unscored = 'true';
```

Then after the `getTenders` function (after line 94) add:

```typescript
export interface TenderViewCounts {
  to_bid: number;
  worth_a_look: number;
  closing_soon: number;
  new_unscored: number;
  all: number;
}

export async function getTenderViewCounts(): Promise<TenderViewCounts> {
  const { data } = await api.get('/api/tenders/view-counts');
  return data as TenderViewCounts;
}
```

- [ ] **Step 4: Type-check**

Run: `cd drpl-frontend && npm run build`
Expected: no type errors. (`countKey: keyof import('./api').TenderViewCounts` resolves after Step 3 adds the type.)

- [ ] **Step 5: Commit**

```bash
git add drpl-frontend/src/lib/tenderViews.ts drpl-frontend/src/lib/api.ts drpl-frontend/src/types/tender.ts
git commit -m "feat(tenders): TENDER_VIEWS map + view-counts api + unscored filter param"
```

---

### Task 4: Sidebar expandable Tenders sub-nav + route

**Files:**
- Modify: `drpl-frontend/src/components/layout/SidebarNav.tsx`
- Modify: `drpl-frontend/src/router.tsx:94` (add `tenders/:view` route)

**Interfaces:**
- Consumes: `TENDER_VIEWS` (Task 3), `getTenderViewCounts` (Task 3), `TendersPage` (existing).
- Produces: sidebar renders an expandable "Tenders" group with five view rows (color dot + label + count badge); `/tenders/:view` routes to `TendersPage`.

- [ ] **Step 1: Add the `:view` route**

In `drpl-frontend/src/router.tsx`, directly after the existing `{ path: 'tenders', element: <TendersPage /> },` (line 94) add:

```tsx
      { path: 'tenders/:view', element: <TendersPage /> },
```

Ensure it does NOT shadow the existing `tenders/:id` numeric routes — it won't, because `:id` routes are `tenders/:id`, `tenders/:id/checklist`, etc. A bare `tenders/:view` and `tenders/:id` both match one segment; react-router resolves by registration order and both point to different pages. To avoid ambiguity, place `tenders/:view` LAST among the `tenders/*` routes (after `tenders/:id/...`), and in `TendersPage` treat a purely-numeric `:view` as "All" (guard below). Simpler + robust: name the param `:view` and in `TendersPage` do `resolveView(view)` which falls back to All for anything not in the slug list — so `/tenders/123` (a numeric) resolves to All harmlessly, while `/tenders/:id` (registered earlier) still wins for the detail page.

Concretely, keep this order in `router.tsx`:
```tsx
      { path: 'tenders', element: <TendersPage /> },
      { path: 'tenders/:id', element: <TenderDetailPage /> },
      { path: 'tenders/:id/checklist', element: <ChecklistPage /> },
      { path: 'tenders/:id/workspace/annexures', element: <AnnexuresWorkspacePage /> },
      { path: 'tenders/:id/workspace/:itemId', element: <DocumentWorkspaceDetailPage /> },
      { path: 'tenders/:id/command-center', element: <CommandCenterPage /> },
      { path: 'tenders/view/:view', element: <TendersPage /> },
```

Use the `tenders/view/:view` prefix to remove ALL ambiguity with `:id`. Update `tenderViews.ts` slugs to full paths in the sidebar (Step 2 builds `/tenders/view/<slug>`, and All → `/tenders`).

- [ ] **Step 2: Replace the Tenders `NavItem` with an expandable group in `SidebarNav.tsx`**

Add imports at the top:

```tsx
import { useLocation } from 'react-router-dom'
import { useEffect } from 'react'
import { TENDER_VIEWS } from '@/lib/tenderViews'
import { getTenderViewCounts, type TenderViewCounts } from '@/lib/api'
```

Add `Circle` is not needed — use a CSS dot. In the `SidebarNav` component body, add state + fetch:

```tsx
  const location = useLocation()
  const [tendersOpen, setTendersOpen] = useState(true)
  const [counts, setCounts] = useState<TenderViewCounts | null>(null)
  useEffect(() => {
    getTenderViewCounts().then(setCounts).catch(() => setCounts(null))
  }, [])

  const dotColor: Record<string, string> = {
    emerald: 'bg-emerald-500', amber: 'bg-amber-500', accent: 'bg-accent', slate: 'bg-muted-foreground',
  }
  const viewHref = (slug: string) => (slug ? `/tenders/view/${slug}` : '/tenders')
```

Remove the Tenders entry from `mainNav` (delete the `{ to: '/tenders', ... }` line) — it becomes a custom expandable block. Render the block where Tenders used to sit. The cleanest approach: split `mainNav` into `mainNavTop` (Dashboard, Command Center) and `mainNavBottom` (Documents, Signatures, Scrape Monitor, Notifications, Settings), and render the Tenders group between them.

Replace the `mainNav` constant with:

```tsx
const mainNavTop = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard', end: true },
  { to: '/command-center', icon: MessageSquare, label: 'Command Center' },
]
const mainNavBottom = [
  { to: '/documents', icon: FileOutput, label: 'Documents' },
  { to: '/signatures', icon: PenTool, label: 'Signatures' },
  { to: '/scrape-monitor', icon: Activity, label: 'Scrape Monitor' },
  { to: '/notifications', icon: Bell, label: 'Notifications' },
  { to: '/settings', icon: Settings, label: 'Settings' },
]
```

In the `<nav>`, replace `{mainNav.map(...)}` with:

```tsx
        {mainNavTop.map((item) => (
          <NavItem key={item.to} {...item} collapsed={collapsed} onNavigate={onNavigate} />
        ))}

        {/* Tenders — expandable decision-stage views */}
        {collapsed ? (
          <NavItem to="/tenders" icon={FileText} label="Tenders" collapsed onNavigate={onNavigate} />
        ) : (
          <div>
            <button
              type="button"
              onClick={() => setTendersOpen((v) => !v)}
              aria-expanded={tendersOpen}
              className={cn(
                'flex w-full items-center justify-between rounded-md px-3 py-2 text-sm font-medium transition-colors',
                location.pathname.startsWith('/tenders')
                  ? 'text-foreground'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground',
              )}
            >
              <span className="flex items-center gap-3">
                <FileText size={18} strokeWidth={1.75} className="shrink-0" />
                Tenders
              </span>
              <ChevronDown size={14} className={cn('transition-transform', tendersOpen && 'rotate-180')} />
            </button>
            {tendersOpen && (
              <div className="mt-0.5 space-y-0.5 pl-2">
                {TENDER_VIEWS.map((v) => (
                  <NavLink
                    key={v.key}
                    to={viewHref(v.slug)}
                    end={v.slug === ''}
                    onClick={onNavigate}
                    className={({ isActive }) =>
                      cn(
                        'flex items-center gap-2.5 rounded-md px-3 py-1.5 text-sm transition-colors',
                        isActive
                          ? 'bg-accent/10 font-semibold text-accent'
                          : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                      )
                    }
                  >
                    <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', dotColor[v.color])} />
                    <span className="flex-1 truncate">{v.label}</span>
                    {counts && (
                      <span className="tabular-nums text-xs font-semibold text-muted-foreground">
                        {counts[v.countKey].toLocaleString()}
                      </span>
                    )}
                  </NavLink>
                ))}
              </div>
            )}
          </div>
        )}

        {mainNavBottom.map((item) => (
          <NavItem key={item.to} {...item} collapsed={collapsed} onNavigate={onNavigate} />
        ))}
```

(`NavLink`, `useState`, `FileText`, `ChevronDown`, `cn` are already imported in this file.)

- [ ] **Step 3: Build**

Run: `cd drpl-frontend && npm run build`
Expected: no type errors. The `All Tenders` view active-state uses `end` so it isn't highlighted on `/tenders/view/...`.

- [ ] **Step 4: Commit**

```bash
git add drpl-frontend/src/components/layout/SidebarNav.tsx drpl-frontend/src/router.tsx
git commit -m "feat(tenders): expandable Tenders sub-nav with view count badges + /tenders/view/:view route"
```

---

### Task 5: `RefineBar` component

**Files:**
- Create: `drpl-frontend/src/components/tenders/RefineBar.tsx`

**Interfaces:**
- Consumes: the existing `TenderFilters` component (its `onApply` delta shape).
- Produces: `RefineBar` with props `{ onApply: (delta) => void; activeCount: number }` — a collapsed toolbar (Refine toggle) that expands to reveal `TenderFilters`.

- [ ] **Step 1: Create `RefineBar.tsx`**

```tsx
// drpl-frontend/src/components/tenders/RefineBar.tsx
import { useState } from 'react';
import { SlidersHorizontal, ChevronDown } from 'lucide-react';
import TenderFilters from './TenderFilters';

type FilterDelta = Parameters<React.ComponentProps<typeof TenderFilters>['onApply']>[0];

interface Props {
  onApply: (delta: FilterDelta) => void;
  activeCount: number;
}

export default function RefineBar({ onApply, activeCount }: Props) {
  const [open, setOpen] = useState(false);
  return (
    <div className="bg-card border border-border rounded-xl overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2.5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className={`inline-flex items-center gap-2 text-sm font-semibold px-3 py-1.5 rounded-lg border transition-colors ${
            open || activeCount > 0
              ? 'border-accent text-accent'
              : 'border-border text-foreground hover:bg-muted'
          }`}
        >
          <SlidersHorizontal size={15} />
          Refine
          {activeCount > 0 && (
            <span className="tabular-nums text-xs font-bold bg-accent/10 text-accent rounded-full px-1.5">
              {activeCount}
            </span>
          )}
          <ChevronDown size={14} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
        </button>
        <span className="text-xs text-muted-foreground">Narrow this view</span>
      </div>
      {open && (
        <div className="border-t border-dashed border-border p-4">
          <TenderFilters onApply={onApply} />
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Build**

Run: `cd drpl-frontend && npm run build`
Expected: no type errors. (`FilterDelta` is derived from `TenderFilters`' own `onApply` param, so it can't drift.)

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/tenders/RefineBar.tsx
git commit -m "feat(tenders): collapsible RefineBar wrapping the existing filters"
```

---

### Task 6: Recompose `TendersPage` around views + Refine

**Files:**
- Modify: `drpl-frontend/src/pages/TendersPage.tsx`
- Create: `drpl-frontend/src/components/tenders/ViewHeader.tsx`

**Interfaces:**
- Consumes: `resolveView`/`TENDER_VIEWS` (Task 3), `RefineBar` (Task 5), `useParams` (react-router), existing `useTenders`, `TenderCardList`, `Pagination`, `AdvancedToggle`.
- Produces: view-driven Tenders page; `ViewHeader` (props `{ view: TenderView; total: number }`).

- [ ] **Step 1: Create `ViewHeader.tsx`**

```tsx
// drpl-frontend/src/components/tenders/ViewHeader.tsx
import type { TenderView } from '../../lib/tenderViews';

const pillTone: Record<string, string> = {
  emerald: 'bg-emerald-500/12 text-emerald-700 dark:text-emerald-400',
  amber: 'bg-amber-500/12 text-amber-700 dark:text-amber-400',
  accent: 'bg-accent/12 text-accent',
  slate: 'bg-muted text-muted-foreground',
};

export default function ViewHeader({ view, total }: { view: TenderView; total: number }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <span className={`inline-flex items-center gap-1.5 text-xs font-bold px-2.5 py-1 rounded-full ${pillTone[view.color]}`}>
          ● {view.label}
        </span>
        <p className="text-sm text-muted-foreground mt-2 max-w-[62ch]">{view.blurb}</p>
      </div>
      <span className="shrink-0 text-sm text-muted-foreground tabular-nums">{total.toLocaleString()} tenders</span>
    </div>
  );
}
```

- [ ] **Step 2: Rewire `TendersPage.tsx` to be view-driven**

At the top of `TendersPage.tsx`, add imports:

```tsx
import { useParams } from 'react-router-dom';
import { resolveView } from '../lib/tenderViews';
import RefineBar from '../components/tenders/RefineBar';
import ViewHeader from '../components/tenders/ViewHeader';
```

Replace the initial `filters` state setup (lines ~18-24) so the view's base filters seed it. Add, inside the component:

```tsx
  const { view: viewSlug } = useParams();
  const view = resolveView(viewSlug);

  const [refine, setRefine] = useState<Record<string, any>>({});
  const [refineCount, setRefineCount] = useState(0);

  // Base filters come from the active view; Refine narrows on top of them.
  const filters: Filters = {
    ...view.baseFilters(),
    ...refine,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,   // if the page already tracks an offset/page, reuse it; else use 0
    sort_by: refine.sort_by || 'relevance',
  } as Filters;
```

If the existing page holds `filters` in `useState` and mutates via `handleApplyFilters`, replace that model: derive `filters` from `view` + `refine` (as above), and change `handleApplyFilters` to `setRefine`. Reset `refine` when the view changes:

```tsx
  useEffect(() => {
    setRefine({});
    setRefineCount(0);
  }, [viewSlug]);
```

Compute `refineCount` from the delta in the RefineBar apply handler:

```tsx
  const applyRefine = (delta: Record<string, any>) => {
    const cleaned = Object.fromEntries(
      Object.entries(delta).filter(([, v]) => v !== undefined && v !== '' && v !== null),
    );
    // don't let Refine override the view's identity filters silently — view base wins as the floor
    setRefine(cleaned);
    setRefineCount(Object.keys(cleaned).filter((k) => k !== 'sort_by').length);
    setSelectedIds(new Set());
  };
```

In the JSX, replace the old `<TenderFilters .../>` block with:

```tsx
        <ViewHeader view={view} total={total} />
        <RefineBar onApply={applyRefine} activeCount={refineCount} />
```

Keep the existing `<TenderCardList>`, `<Pagination>`, `<AdvancedToggle>`, batch/selection tools, and empty/loading states. Ensure the `useTenders(filters)` call now consumes the derived `filters`.

Note (verify-before-code): open `TendersPage.tsx` and confirm the current pagination variable (`page`/`offset`) and the `total`/`loading`/`refetch` returns from `useTenders`; adapt the `offset` line above to the file's actual pagination state. Keep the existing Advanced/selection code paths intact.

- [ ] **Step 3: Per-view empty state**

Where the list renders empty (no tenders), show a friendly per-view message instead of a blank list. Add near the list render:

```tsx
        {!loading && tenders.length === 0 && (
          <div className="bg-card border border-border rounded-xl p-8 text-center">
            <p className="text-sm text-muted-foreground">
              {refineCount > 0
                ? 'No matches in this view. Clear the Refine filters or switch views.'
                : view.key === 'to_bid'
                  ? 'No tenders are ready to bid yet. Try “Worth a Look” or “New / Unscored”.'
                  : 'Nothing here yet.'}
            </p>
          </div>
        )}
```

- [ ] **Step 4: Build**

Run: `cd drpl-frontend && npm run build`
Expected: no type errors.

- [ ] **Step 5: Commit**

```bash
git add drpl-frontend/src/pages/TendersPage.tsx drpl-frontend/src/components/tenders/ViewHeader.tsx
git commit -m "feat(tenders): view-driven TendersPage — ViewHeader + Refine over base filters"
```

---

## Status (2026-07-11)

Tasks 1–6 implemented, tested, committed on `feat/scoring-backlog-awareness`. Remaining = the manual live-server view, which needs an operator backend restart (to load the `unscored` param + `/tenders/view-counts`).

## Final verification (run once after Tasks 1-6)

- [x] Backend suite: 97 passed / 5 failed — all 5 pre-existing (verified earlier on commit `93bba19`); the two new tests (`test_unscored_filter`, `test_view_counts_route`) pass. NO new failures.
- [x] Frontend build: clean.
- [ ] Manual (backend running, logged in): the sidebar shows an expandable "Tenders" with five views + count badges; clicking a view routes to `/tenders/view/<slug>` and shows the matching header pill + blurb + filtered list; "Refine" expands the full filters and narrows within the view; "All Tenders" = `/tenders`; an unknown slug (`/tenders/view/xyz`) falls back to All.
- [ ] `GET /api/tenders/view-counts` returns the five counts; `GET /api/tenders/?unscored=true` returns only null-score tenders.

---

## Self-review notes

- **Spec coverage:** unscored filter (T1) ✓; view-counts endpoint (T2) ✓; TENDER_VIEWS map + api helpers (T3) ✓; sidebar expandable sub-nav w/ badges + route (T4) ✓; collapsible RefineBar reusing TenderFilters (T5) ✓; view-driven page + ViewHeader + refine-over-base + empty states (T6) ✓; deep-linkable views (T4 route + resolveView) ✓; semantic colors per view (T3 color + T4 dot + T6 pill) ✓; no schema/scoring change ✓.
- **Ambiguity resolved:** routes use `tenders/view/:view` to remove any `:id` collision; `closing_soon` reuses the dashboard 0.70 threshold + 7-day window; Refine count excludes `sort_by`; view base filters are the floor (Refine merges on top, never removes the segment/unscored identity — enforced by object spread order `...base, ...refine` only for non-identity keys; if a Refine field collided with a base key it would override, but the RefineBar surface intentionally excludes segment/unscored controls, so no collision in practice).
- **Verify-before-code:** T1 Step 6 (how the route threads score_min → get_tenders) and T6 Step 2 (current pagination var + useTenders returns) both require reading the actual file before finalizing — noted inline.
