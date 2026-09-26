# Command Center Session Navigation — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add filter (status / mode / date range) and sort (recency / created / name / status) controls to the Command Center, shared between the dashboard grid and the inside-session sidebar.

**Architecture:** A pure filter+sort utility (`applySessionFilters`), a shared state hook (`useSessionFilters`) backed by `useState` for filter and `localStorage` for sort, and a `SessionFilterBar` component with a `compact` prop. State lifts to `CommandCenterPage`; both surfaces (dashboard grid + sidebar) receive the same filtered+sorted session list.

**Tech Stack:** React 18 + TypeScript + Tailwind + lucide-react. No new dependencies.

**Important context for engineer:**
- Phase 1 is shipped. Sidebar + Cmd+K palette already exist. This phase adds filter/sort on top.
- The frontend has NO test framework (no vitest / RTL). Verification gates: `npm run build` (`tsc -b && vite build`) and a manual UI walkthrough.
- Spec reference: `docs/superpowers/specs/2026-05-18-command-center-session-navigation-design.md` (Phase 2 section).
- The user works in place on `main` — do NOT create branches/worktrees.
- `CommandCenterPage.tsx` is ~2800 lines. Don't restructure it; only touch the regions called out per task.

---

## File Structure

**Create:**
- `drpl-frontend/src/components/command-center/applySessionFilters.ts` — pure filter+sort function (~80 lines)
- `drpl-frontend/src/hooks/useSessionFilters.ts` — filter state + localStorage-backed sort (~60 lines)
- `drpl-frontend/src/components/command-center/SessionFilterBar.tsx` — chips + sort dropdown UI, with `compact` prop (~200 lines)

**Modify:**
- `drpl-frontend/src/pages/CommandCenterPage.tsx` — call `useSessionFilters`, mount full `SessionFilterBar` above dashboard grid, pass `applied(sessionList)` to dashboard grid + sidebar, pass filter props through to sidebar
- `drpl-frontend/src/components/command-center/SessionSidebar.tsx` — accept filter props, render compact `SessionFilterBar` at top of the rail

---

## Task 1: Pure Filter + Sort Utility

Get the filter and sort logic right in isolation before any UI work.

**Files:**
- Create: `drpl-frontend/src/components/command-center/applySessionFilters.ts`

- [ ] **Step 1: Create the utility**

Create `drpl-frontend/src/components/command-center/applySessionFilters.ts`:

```typescript
import type { CommandCenterSession } from '../../types/command-center';

export type SessionStatusFilter = 'all' | 'draft' | 'submitted' | 'approved';
export type SessionModeFilter = 'all' | 'tender_linked' | 'standalone';
export type SessionDateRange = 'all' | 'last_7d' | 'last_30d' | 'custom';
export type SessionSortKind = 'recency' | 'created' | 'name' | 'status';

export interface SessionFilter {
  status: SessionStatusFilter;
  mode: SessionModeFilter;
  dateRange: SessionDateRange;
  /** ISO date string (YYYY-MM-DD). Used only when dateRange === 'custom'. */
  customStart?: string;
  /** ISO date string (YYYY-MM-DD). Used only when dateRange === 'custom'. */
  customEnd?: string;
}

export const DEFAULT_FILTER: SessionFilter = {
  status: 'all',
  mode: 'all',
  dateRange: 'all',
};

/**
 * Number of filter chips currently set to a non-'all' value. Used by the
 * "N filters" badge.
 */
export function countActiveFilters(filter: SessionFilter): number {
  let n = 0;
  if (filter.status !== 'all') n++;
  if (filter.mode !== 'all') n++;
  if (filter.dateRange !== 'all') n++;
  return n;
}

/**
 * Apply filter then sort to a session list. Both operations are pure — same
 * inputs always produce the same output. The input array is not mutated.
 */
export function applySessionFilters(
  sessions: CommandCenterSession[],
  filter: SessionFilter,
  sort: SessionSortKind,
): CommandCenterSession[] {
  const filtered = sessions.filter((s) => matchesFilter(s, filter));
  return [...filtered].sort(comparatorFor(sort));
}

// ── Internals ───────────────────────────────────────────────────────────────

function matchesFilter(s: CommandCenterSession, f: SessionFilter): boolean {
  if (f.status !== 'all' && s.status !== f.status) return false;
  if (f.mode !== 'all' && s.mode !== f.mode) return false;

  if (f.dateRange !== 'all') {
    const created = Date.parse(s.created_at);
    if (Number.isNaN(created)) return false;
    const cutoff = computeCutoff(f);
    if (cutoff.from !== null && created < cutoff.from) return false;
    if (cutoff.to !== null && created > cutoff.to) return false;
  }

  return true;
}

function computeCutoff(f: SessionFilter): { from: number | null; to: number | null } {
  if (f.dateRange === 'last_7d') {
    return { from: Date.now() - 7 * 24 * 60 * 60 * 1000, to: null };
  }
  if (f.dateRange === 'last_30d') {
    return { from: Date.now() - 30 * 24 * 60 * 60 * 1000, to: null };
  }
  if (f.dateRange === 'custom') {
    // Inclusive on both ends: customStart 00:00:00, customEnd 23:59:59.999
    const from = f.customStart ? Date.parse(`${f.customStart}T00:00:00.000Z`) : null;
    const to = f.customEnd ? Date.parse(`${f.customEnd}T23:59:59.999Z`) : null;
    return {
      from: from !== null && !Number.isNaN(from) ? from : null,
      to: to !== null && !Number.isNaN(to) ? to : null,
    };
  }
  return { from: null, to: null };
}

function sessionTitle(s: CommandCenterSession): string {
  return (s.tender_id ? s.tender_title : null) || s.title || 'Untitled Session';
}

function recencyTimestamp(s: CommandCenterSession): string {
  return s.updated_at || s.created_at;
}

function comparatorFor(sort: SessionSortKind) {
  if (sort === 'recency') {
    return (a: CommandCenterSession, b: CommandCenterSession) =>
      recencyTimestamp(b).localeCompare(recencyTimestamp(a));
  }
  if (sort === 'created') {
    return (a: CommandCenterSession, b: CommandCenterSession) =>
      b.created_at.localeCompare(a.created_at);
  }
  if (sort === 'name') {
    return (a: CommandCenterSession, b: CommandCenterSession) =>
      sessionTitle(a).toLowerCase().localeCompare(sessionTitle(b).toLowerCase());
  }
  // status — primary asc by status string, fallback to recency
  return (a: CommandCenterSession, b: CommandCenterSession) => {
    const cmp = (a.status || '').localeCompare(b.status || '');
    if (cmp !== 0) return cmp;
    return recencyTimestamp(b).localeCompare(recencyTimestamp(a));
  };
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: clean build.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/command-center/applySessionFilters.ts
git commit -m "feat(command-center): pure filter+sort utility for session list"
```

---

## Task 2: useSessionFilters Hook

Wraps `useState` for filter (in memory) and `useState`+`localStorage` for sort.

**Files:**
- Create: `drpl-frontend/src/hooks/useSessionFilters.ts`

- [ ] **Step 1: Create the hook**

Create `drpl-frontend/src/hooks/useSessionFilters.ts`:

```typescript
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { CommandCenterSession } from '../types/command-center';
import {
  applySessionFilters,
  countActiveFilters,
  DEFAULT_FILTER,
  type SessionFilter,
  type SessionSortKind,
} from '../components/command-center/applySessionFilters';

const SORT_STORAGE_KEY = 'drpl_cc_session_sort';
const VALID_SORTS: ReadonlyArray<SessionSortKind> = ['recency', 'created', 'name', 'status'];

function readStoredSort(): SessionSortKind {
  try {
    const raw = localStorage.getItem(SORT_STORAGE_KEY);
    if (raw && (VALID_SORTS as readonly string[]).includes(raw)) {
      return raw as SessionSortKind;
    }
  } catch {
    // ignore
  }
  return 'recency';
}

export interface UseSessionFiltersReturn {
  filter: SessionFilter;
  setFilter: (next: SessionFilter) => void;
  sort: SessionSortKind;
  setSort: (next: SessionSortKind) => void;
  activeFilterCount: number;
  clearFilters: () => void;
  /** Memoized filtered+sorted view of the input list. */
  apply: (sessions: CommandCenterSession[]) => CommandCenterSession[];
}

/**
 * Shared filter+sort state for the Command Center session list.
 * Filter state is in-memory only; sort is persisted to localStorage so a
 * user's preference survives page reloads.
 */
export function useSessionFilters(): UseSessionFiltersReturn {
  const [filter, setFilter] = useState<SessionFilter>(DEFAULT_FILTER);
  const [sort, setSortState] = useState<SessionSortKind>(() => readStoredSort());

  // Persist sort to localStorage on change
  useEffect(() => {
    try {
      localStorage.setItem(SORT_STORAGE_KEY, sort);
    } catch {
      // ignore quota / privacy-mode failures
    }
  }, [sort]);

  const clearFilters = useCallback(() => {
    setFilter(DEFAULT_FILTER);
  }, []);

  const activeFilterCount = useMemo(() => countActiveFilters(filter), [filter]);

  // We DON'T memoize the filtered output here against sessionList — the
  // caller controls when to compute it (typically once per render).
  // Returning a stable function lets callers call it inline.
  const apply = useCallback(
    (sessions: CommandCenterSession[]) => applySessionFilters(sessions, filter, sort),
    [filter, sort],
  );

  return {
    filter,
    setFilter,
    sort,
    setSort: setSortState,
    activeFilterCount,
    clearFilters,
    apply,
  };
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: clean build.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/hooks/useSessionFilters.ts
git commit -m "feat(command-center): useSessionFilters hook (filter state + persisted sort)"
```

---

## Task 3: SessionFilterBar Component

UI for the filter chips + sort dropdown. Has a `compact` prop for the sidebar variant.

**Files:**
- Create: `drpl-frontend/src/components/command-center/SessionFilterBar.tsx`

- [ ] **Step 1: Create the component**

Create `drpl-frontend/src/components/command-center/SessionFilterBar.tsx`:

```tsx
import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Filter as FilterIcon, X } from 'lucide-react';
import type {
  SessionDateRange,
  SessionFilter,
  SessionModeFilter,
  SessionSortKind,
  SessionStatusFilter,
} from './applySessionFilters';

interface SessionFilterBarProps {
  filter: SessionFilter;
  onFilterChange: (next: SessionFilter) => void;
  sort: SessionSortKind;
  onSortChange: (next: SessionSortKind) => void;
  activeFilterCount: number;
  onClearFilters: () => void;
  /** When true, render a compact variant suitable for the narrow sidebar rail. */
  compact?: boolean;
}

const STATUS_OPTIONS: Array<{ value: SessionStatusFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'draft', label: 'Draft' },
  { value: 'submitted', label: 'Submitted' },
  { value: 'approved', label: 'Approved' },
];

const MODE_OPTIONS: Array<{ value: SessionModeFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'tender_linked', label: 'Tender' },
  { value: 'standalone', label: 'Ad-hoc' },
];

const DATE_OPTIONS: Array<{ value: SessionDateRange; label: string }> = [
  { value: 'all', label: 'All time' },
  { value: 'last_7d', label: 'Last 7d' },
  { value: 'last_30d', label: 'Last 30d' },
  { value: 'custom', label: 'Custom' },
];

const SORT_OPTIONS: Array<{ value: SessionSortKind; label: string }> = [
  { value: 'recency', label: 'Recency' },
  { value: 'created', label: 'Created' },
  { value: 'name', label: 'Name' },
  { value: 'status', label: 'Status' },
];

/**
 * Filter chips + sort dropdown for the Command Center session list.
 *
 * Two layouts:
 *   - Full (default): inline horizontal pills, full set of chips visible.
 *   - Compact: a single "Filter" button opens a small popover with the same
 *     controls, plus a small sort selector. Suitable for narrow surfaces.
 */
export default function SessionFilterBar({
  filter,
  onFilterChange,
  sort,
  onSortChange,
  activeFilterCount,
  onClearFilters,
  compact,
}: SessionFilterBarProps) {
  if (compact) {
    return (
      <CompactBar
        filter={filter}
        onFilterChange={onFilterChange}
        sort={sort}
        onSortChange={onSortChange}
        activeFilterCount={activeFilterCount}
        onClearFilters={onClearFilters}
      />
    );
  }
  return (
    <FullBar
      filter={filter}
      onFilterChange={onFilterChange}
      sort={sort}
      onSortChange={onSortChange}
      activeFilterCount={activeFilterCount}
      onClearFilters={onClearFilters}
    />
  );
}

// ── Full bar ────────────────────────────────────────────────────────────────

function FullBar(props: Omit<SessionFilterBarProps, 'compact'>) {
  const { filter, onFilterChange, sort, onSortChange, activeFilterCount, onClearFilters } = props;
  return (
    <div className="flex flex-wrap items-center gap-2 mb-4">
      <ChipGroup
        label="Status"
        value={filter.status}
        options={STATUS_OPTIONS}
        onChange={(v) => onFilterChange({ ...filter, status: v as SessionStatusFilter })}
      />
      <ChipGroup
        label="Mode"
        value={filter.mode}
        options={MODE_OPTIONS}
        onChange={(v) => onFilterChange({ ...filter, mode: v as SessionModeFilter })}
      />
      <DateChipGroup filter={filter} onFilterChange={onFilterChange} />

      {activeFilterCount > 0 && (
        <>
          <span className="text-xs font-medium text-slate-500 px-1.5">
            {activeFilterCount} filter{activeFilterCount === 1 ? '' : 's'}
          </span>
          <button
            onClick={onClearFilters}
            className="text-xs text-blue-600 hover:text-blue-800 hover:underline"
          >
            Clear all
          </button>
        </>
      )}

      <div className="ml-auto">
        <SortSelect value={sort} onChange={onSortChange} />
      </div>
    </div>
  );
}

// ── Compact bar (sidebar) ───────────────────────────────────────────────────

function CompactBar(props: Omit<SessionFilterBarProps, 'compact'>) {
  const { filter, onFilterChange, sort, onSortChange, activeFilterCount, onClearFilters } = props;
  const [open, setOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  // Click outside to close
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  return (
    <div className="px-2 pt-1 pb-2 border-b border-slate-100">
      <div className="flex items-center gap-1.5">
        <div className="relative flex-1" ref={popoverRef}>
          <button
            onClick={() => setOpen((v) => !v)}
            className={`w-full flex items-center justify-between gap-1.5 px-2 py-1 rounded-md text-xs border ${
              activeFilterCount > 0
                ? 'bg-blue-50 border-blue-200 text-blue-700'
                : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
            }`}
            title="Filter sessions"
            aria-label="Filter sessions"
            aria-expanded={open}
          >
            <span className="inline-flex items-center gap-1.5">
              <FilterIcon size={12} />
              <span>Filter</span>
              {activeFilterCount > 0 && (
                <span className="px-1 rounded bg-blue-100 text-blue-700 text-[10px]">
                  {activeFilterCount}
                </span>
              )}
            </span>
            <ChevronDown size={12} />
          </button>

          {open && (
            <div className="absolute left-0 right-0 top-full mt-1 z-30 bg-white rounded-lg border border-slate-200 shadow-xl p-3 space-y-3">
              <ChipGroup
                label="Status"
                value={filter.status}
                options={STATUS_OPTIONS}
                onChange={(v) => onFilterChange({ ...filter, status: v as SessionStatusFilter })}
                stack
              />
              <ChipGroup
                label="Mode"
                value={filter.mode}
                options={MODE_OPTIONS}
                onChange={(v) => onFilterChange({ ...filter, mode: v as SessionModeFilter })}
                stack
              />
              <DateChipGroup filter={filter} onFilterChange={onFilterChange} stack />
              {activeFilterCount > 0 && (
                <button
                  onClick={() => {
                    onClearFilters();
                    setOpen(false);
                  }}
                  className="text-xs text-blue-600 hover:text-blue-800 hover:underline inline-flex items-center gap-1"
                >
                  <X size={10} /> Clear all
                </button>
              )}
            </div>
          )}
        </div>
        <SortSelect value={sort} onChange={onSortChange} compact />
      </div>
    </div>
  );
}

// ── Shared sub-components ───────────────────────────────────────────────────

function ChipGroup<T extends string>({
  label,
  value,
  options,
  onChange,
  stack,
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (next: T) => void;
  stack?: boolean;
}) {
  return (
    <div className={stack ? 'space-y-1.5' : 'inline-flex items-center gap-1.5'}>
      <span className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide">
        {label}
      </span>
      <div className="inline-flex flex-wrap gap-1">
        {options.map((opt) => {
          const active = opt.value === value;
          return (
            <button
              key={opt.value}
              onClick={() => onChange(opt.value)}
              className={`px-2 py-0.5 rounded-full text-[11px] font-medium border transition-colors ${
                active
                  ? 'bg-blue-600 text-white border-blue-600'
                  : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
              }`}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function DateChipGroup({
  filter,
  onFilterChange,
  stack,
}: {
  filter: SessionFilter;
  onFilterChange: (next: SessionFilter) => void;
  stack?: boolean;
}) {
  return (
    <div className={stack ? 'space-y-1.5' : 'inline-flex items-center gap-1.5'}>
      <ChipGroup
        label="Date"
        value={filter.dateRange}
        options={DATE_OPTIONS}
        onChange={(v) => onFilterChange({ ...filter, dateRange: v as SessionDateRange })}
        stack={stack}
      />
      {filter.dateRange === 'custom' && (
        <div className="inline-flex items-center gap-1.5 ml-2">
          <input
            type="date"
            value={filter.customStart ?? ''}
            onChange={(e) =>
              onFilterChange({ ...filter, customStart: e.target.value || undefined })
            }
            className="px-1.5 py-0.5 rounded border border-slate-200 text-[11px]"
            aria-label="Custom start date"
          />
          <span className="text-[11px] text-slate-400">→</span>
          <input
            type="date"
            value={filter.customEnd ?? ''}
            onChange={(e) =>
              onFilterChange({ ...filter, customEnd: e.target.value || undefined })
            }
            className="px-1.5 py-0.5 rounded border border-slate-200 text-[11px]"
            aria-label="Custom end date"
          />
        </div>
      )}
    </div>
  );
}

function SortSelect({
  value,
  onChange,
  compact,
}: {
  value: SessionSortKind;
  onChange: (next: SessionSortKind) => void;
  compact?: boolean;
}) {
  return (
    <label className="inline-flex items-center gap-1.5">
      {!compact && (
        <span className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide">
          Sort
        </span>
      )}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as SessionSortKind)}
        className={`bg-white border border-slate-200 rounded text-slate-700 hover:bg-slate-50 focus:outline-none focus:ring-1 focus:ring-blue-300 ${
          compact ? 'text-[11px] px-1.5 py-0.5' : 'text-xs px-2 py-1'
        }`}
        aria-label="Sort sessions"
      >
        {SORT_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  );
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: clean build.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/command-center/SessionFilterBar.tsx
git commit -m "feat(command-center): SessionFilterBar component (chips + sort, full + compact)"
```

---

## Task 4: Wire Filter Bar Into CommandCenterPage Dashboard

**Files:**
- Modify: `drpl-frontend/src/pages/CommandCenterPage.tsx`

⚠️ This file is ~2800 lines. Make ONLY the changes called out — do NOT refactor.

- [ ] **Step 1: Add imports**

In `drpl-frontend/src/pages/CommandCenterPage.tsx`, find the existing block of command-center component imports (search for `import SessionSidebar`). Add:

```tsx
import SessionFilterBar from '../components/command-center/SessionFilterBar';
import { useSessionFilters } from '../hooks/useSessionFilters';
```

- [ ] **Step 2: Call useSessionFilters at the top of the component**

Find the state-declaration block near the top of the component (anchor: where `paletteOpen` was added in Phase 1, around line ~129). Right after that declaration, add:

```tsx
const sessionFilters = useSessionFilters();
const filteredSessionList = sessionFilters.apply(sessionList);
```

- [ ] **Step 3: Render filter bar above the dashboard grid**

Find the dashboard render branch (search for `if (showSessionList && !session)`). Inside, locate the section where the grid is rendered — the JSX block that wraps `sessionList.map((s) => (` rendering `SessionDashboardCard`.

Just BEFORE that grid block (and AFTER the bulk-action bar / error banner if present), add:

```tsx
{sessionList.length > 0 && (
  <SessionFilterBar
    filter={sessionFilters.filter}
    onFilterChange={sessionFilters.setFilter}
    sort={sessionFilters.sort}
    onSortChange={sessionFilters.setSort}
    activeFilterCount={sessionFilters.activeFilterCount}
    onClearFilters={sessionFilters.clearFilters}
  />
)}
```

The filter bar should NOT show when there are zero sessions total — the existing "No sessions yet" empty state owns that case.

- [ ] **Step 4: Switch the grid to render the filtered list**

In the same dashboard render branch, find the line:

```tsx
{sessionList.map((s) => (
```

Replace `sessionList` with `filteredSessionList` so the grid renders the filtered+sorted view:

```tsx
{filteredSessionList.map((s) => (
```

Be careful: the surrounding `sessionList.length === 0` empty-state check should STILL reference `sessionList` (the raw list), so the "No sessions yet" message only shows when there are zero sessions total — not when filters exclude all sessions.

- [ ] **Step 5: Add an empty-filtered state inside the dashboard branch**

In the dashboard branch, right after the existing `sessionList.length === 0 ? (...) : (...)` ternary, BUT INSIDE the else-branch (i.e., when `sessionList.length > 0`), add a sibling check for the filtered-but-empty case. Specifically:

Wrap the grid rendering so it falls back to a "No sessions match" message when `filteredSessionList.length === 0 && sessionList.length > 0`. The simplest insertion:

Find the grid div opener (`<div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">`), and just BEFORE it, add:

```tsx
{filteredSessionList.length === 0 ? (
  <div className="text-center py-16 border-2 border-dashed border-slate-200 rounded-xl">
    <p className="text-sm font-medium text-slate-500 mb-2">No sessions match these filters.</p>
    <button
      onClick={sessionFilters.clearFilters}
      className="text-xs text-blue-600 hover:text-blue-800 hover:underline"
    >
      Clear filters
    </button>
  </div>
) : (
```

And then add a closing `)}` after the closing `</div>` of the grid block (the one that closes the `grid grid-cols-1 ...` div). The "+ New Session" card inside the grid stays where it is.

Read the surrounding ~50 lines first to make sure your insertion balances brackets correctly. If the structure is unclear, STOP and report BLOCKED.

- [ ] **Step 6: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: clean build.

- [ ] **Step 7: Manual UI walkthrough**

Run: `cd drpl-frontend && npm run dev`

Check:
- Dashboard renders the filter bar above the grid when sessions exist.
- Click "Draft" chip in Status: grid narrows to draft sessions only. The "1 filter" badge appears with "Clear all".
- Click "Tender" chip in Mode: now "2 filters".
- Set Date to "Last 7d": now "3 filters".
- Click "Clear all": filters reset, grid shows everything.
- Pick "Name" in the Sort dropdown: grid re-sorts alphabetically.
- Reload the page: the Sort dropdown is still on "Name" (persisted to localStorage).
- Apply filters that match nothing: the "No sessions match these filters" + Clear filters message appears.
- Pick "Custom" in Date filter: two date inputs appear. Set them to an empty range: grid shows nothing or what was matched before — verify behavior is sensible.

- [ ] **Step 8: Commit**

```bash
git add drpl-frontend/src/pages/CommandCenterPage.tsx
git commit -m "feat(command-center): filter+sort bar on dashboard grid"
```

---

## Task 5: Wire Compact Filter Bar Into SessionSidebar

The sidebar needs to render the compact filter bar so users inside a session can change filters without going back to the dashboard. Per the spec, filter state is shared — same `useSessionFilters` instance owns both surfaces.

**Files:**
- Modify: `drpl-frontend/src/components/command-center/SessionSidebar.tsx`
- Modify: `drpl-frontend/src/pages/CommandCenterPage.tsx`

- [ ] **Step 1: Add new props to SessionSidebar**

Open `drpl-frontend/src/components/command-center/SessionSidebar.tsx`. Extend the props interface:

Find:
```tsx
interface SessionSidebarProps {
  sessions: CommandCenterSession[];
  currentSessionId: number | null;
  onSwitch: (sessionId: number) => void;
  onNew: () => void;
  onDelete?: (sessionId: number) => void;
  isLoading?: boolean;
}
```

Replace with:
```tsx
import type { SessionFilter, SessionSortKind } from './applySessionFilters';

interface SessionSidebarProps {
  sessions: CommandCenterSession[];
  currentSessionId: number | null;
  onSwitch: (sessionId: number) => void;
  onNew: () => void;
  onDelete?: (sessionId: number) => void;
  isLoading?: boolean;
  /** Filter bar state — passed through to the compact SessionFilterBar. */
  filter?: SessionFilter;
  onFilterChange?: (next: SessionFilter) => void;
  sort?: SessionSortKind;
  onSortChange?: (next: SessionSortKind) => void;
  activeFilterCount?: number;
  onClearFilters?: () => void;
}
```

(Add the `import type` line near the top of the file, alongside the existing type imports.)

Also import the filter bar at the top of the file:

```tsx
import SessionFilterBar from './SessionFilterBar';
```

- [ ] **Step 1b: Update the EmptyState sub-component to handle filtered-empty case**

Inside the same `SessionSidebar.tsx`, find the existing `EmptyState` sub-component near the bottom of the file:

```tsx
function EmptyState({ collapsed, onNew }: { collapsed: boolean; onNew: () => void }) {
  if (collapsed) {
    return (
      <div className="px-2 py-4 text-center">
        <Bot size={20} className="mx-auto text-slate-300" />
      </div>
    );
  }
  return (
    <div className="px-4 py-6 text-center">
      <p className="text-xs text-slate-400 mb-2">No sessions yet</p>
      <button
        onClick={onNew}
        className="text-xs text-blue-600 hover:text-blue-800 hover:underline"
      >
        Start your first session →
      </button>
    </div>
  );
}
```

Add a `filteredEmpty` boolean prop. When true, render a "No sessions match" + "Clear filters" message instead of the "No sessions yet" prompt. Replace with:

```tsx
function EmptyState({
  collapsed,
  onNew,
  filteredEmpty,
  onClearFilters,
}: {
  collapsed: boolean;
  onNew: () => void;
  filteredEmpty: boolean;
  onClearFilters?: () => void;
}) {
  if (collapsed) {
    return (
      <div className="px-2 py-4 text-center">
        <Bot size={20} className="mx-auto text-slate-300" />
      </div>
    );
  }
  if (filteredEmpty) {
    return (
      <div className="px-4 py-6 text-center">
        <p className="text-xs text-slate-400 mb-2">No sessions match</p>
        {onClearFilters && (
          <button
            onClick={onClearFilters}
            className="text-xs text-blue-600 hover:text-blue-800 hover:underline"
          >
            Clear filters
          </button>
        )}
      </div>
    );
  }
  return (
    <div className="px-4 py-6 text-center">
      <p className="text-xs text-slate-400 mb-2">No sessions yet</p>
      <button
        onClick={onNew}
        className="text-xs text-blue-600 hover:text-blue-800 hover:underline"
      >
        Start your first session →
      </button>
    </div>
  );
}
```

Then update the `EmptyState` call site in the main `SessionSidebar` JSX. Find:

```tsx
) : sessions.length === 0 ? (
  <EmptyState collapsed={collapsed} onNew={onNew} />
) : (
```

Replace with:

```tsx
) : sessions.length === 0 ? (
  <EmptyState
    collapsed={collapsed}
    onNew={onNew}
    filteredEmpty={(activeFilterCount ?? 0) > 0}
    onClearFilters={onClearFilters}
  />
) : (
```

The `(activeFilterCount ?? 0) > 0` check uses the new `activeFilterCount` prop you added in Step 1. When filters are active AND the filtered list is empty, the sidebar shows "No sessions match" + "Clear filters" instead of "No sessions yet".

- [ ] **Step 2: Render the compact filter bar at the top of the sidebar (expanded only)**

Inside the `SessionSidebar` component body, accept the new props in the destructure:

```tsx
export default function SessionSidebar({
  sessions,
  currentSessionId,
  onSwitch,
  onNew,
  onDelete,
  isLoading,
  filter,
  onFilterChange,
  sort,
  onSortChange,
  activeFilterCount,
  onClearFilters,
}: SessionSidebarProps) {
```

Find the existing JSX structure inside the `<nav>` — there's a header div, then the session list, then the footer "+ New Session" button.

Between the header and the session list, insert the compact filter bar — but only when:
- The sidebar is NOT collapsed (otherwise there's no room).
- All filter props are present (don't crash when caller doesn't pass them).

```tsx
{!collapsed &&
  filter !== undefined &&
  onFilterChange !== undefined &&
  sort !== undefined &&
  onSortChange !== undefined &&
  activeFilterCount !== undefined &&
  onClearFilters !== undefined && (
    <SessionFilterBar
      filter={filter}
      onFilterChange={onFilterChange}
      sort={sort}
      onSortChange={onSortChange}
      activeFilterCount={activeFilterCount}
      onClearFilters={onClearFilters}
      compact
    />
  )}
```

Place this block AFTER the `</div>` that closes the header (the one containing the collapse toggle) and BEFORE the `<div className="flex-1 overflow-y-auto py-2">` that opens the session list.

- [ ] **Step 3: Update CommandCenterPage to pass filter props AND filtered list to SessionSidebar**

Open `drpl-frontend/src/pages/CommandCenterPage.tsx`. Find the `<SessionSidebar>` mount (inserted in Phase 1 Task 5, around line 2240).

Current state:
```tsx
<SessionSidebar
  sessions={sessionList}
  currentSessionId={session?.id ?? null}
  onSwitch={(id) => navigate(`/command-center/${id}`)}
  onNew={handleNewSession}
  onDelete={(id) => setDeleteTargetId(id)}
  isLoading={sessionListLoading}
/>
```

Replace with:
```tsx
<SessionSidebar
  sessions={filteredSessionList}
  currentSessionId={session?.id ?? null}
  onSwitch={(id) => navigate(`/command-center/${id}`)}
  onNew={handleNewSession}
  onDelete={(id) => setDeleteTargetId(id)}
  isLoading={sessionListLoading}
  filter={sessionFilters.filter}
  onFilterChange={sessionFilters.setFilter}
  sort={sessionFilters.sort}
  onSortChange={sessionFilters.setSort}
  activeFilterCount={sessionFilters.activeFilterCount}
  onClearFilters={sessionFilters.clearFilters}
/>
```

Two things changed:
1. `sessions={filteredSessionList}` instead of `sessions={sessionList}` — sidebar now reflects active filters.
2. Six new filter props passed through.

- [ ] **Step 4: Update the palette to ALSO use the filtered list (optional but consistent)**

Find the two `<SessionSwitcherPalette>` mounts (one in dashboard branch, one in inside-session branch). Decide: should the palette respect filters or always show all sessions?

**Decision: palette should NOT respect filters.** The palette is a quick switcher — its job is to let you jump to ANY session. Filters apply only to the sidebar/grid presentation. **Leave both palette mounts unchanged** (they keep `sessions={sessionList}`).

This is intentional — confirm both palette `sessions={sessionList}` references remain as-is.

- [ ] **Step 5: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: clean build.

- [ ] **Step 6: Manual UI walkthrough**

Run: `cd drpl-frontend && npm run dev`

Enter a session. Then:
- The sidebar shows a "Filter" button at the top (expanded mode only).
- Click "Filter" — popover opens with Status/Mode/Date chip groups stacked vertically. A small sort selector lives next to the Filter button.
- Apply a filter: the popover closes (or stays open — verify behavior is sane). The sidebar's session list narrows. The count badge on the Filter button shows the active filter count.
- Switch back to the dashboard: the same filters are still active there (shared state). Verify the dashboard's filter bar shows the same chip selections.
- Collapse the sidebar: the Filter button hides; only the session icon-rail is visible.
- Re-expand: filter button reappears with the same state.
- Open Cmd+K palette: still shows ALL sessions (unfiltered) — confirms palette is intentionally exempt.
- Change sort in the sidebar: dashboard's sort dropdown also updates. Sort persists on reload.

- [ ] **Step 7: Commit**

```bash
git add drpl-frontend/src/components/command-center/SessionSidebar.tsx drpl-frontend/src/pages/CommandCenterPage.tsx
git commit -m "feat(command-center): compact filter bar in sidebar (shared state w/ dashboard)"
```

---

## Verification (End of Phase 2)

After all five tasks complete, do a final end-to-end pass.

- [ ] **Step 1: Clean build**

```bash
cd drpl-frontend && npm run build
```
Expected: exit 0, no TS errors.

- [ ] **Step 2: End-to-end test**

```bash
cd drpl-frontend && npm run dev
```

Walkthrough:
1. Hard refresh `/command-center`. Filter bar visible above grid (assuming you have ≥1 session).
2. Apply Status=Draft, Mode=Tender, Date=Last 30d. Grid narrows. Badge shows "3 filters".
3. Pick "Name" in sort. Grid resorts.
4. Click into a session. Sidebar visible with "Filter" button at top.
5. Click Filter — popover shows same selections (shared state). Tweak Status to Approved. Sidebar list updates.
6. Press Cmd+K — palette opens with ALL sessions (unfiltered). Navigate to one.
7. Reload page. Sort is still "Name" (localStorage). Filter is reset (in-memory only — expected).
8. Resize to mobile (<640px). Sidebar hides; the existing "Switch" button still opens the palette.

- [ ] **Step 3: Self-review the diff**

```bash
git log --oneline <pre-phase-2-sha>..HEAD
git diff --stat <pre-phase-2-sha>..HEAD
```

Confirm:
- 3 new files added: `applySessionFilters.ts`, `useSessionFilters.ts`, `SessionFilterBar.tsx`
- 2 modified files: `CommandCenterPage.tsx`, `SessionSidebar.tsx`
- No untouched-region regressions in `CommandCenterPage.tsx` or `SessionSidebar.tsx`

Phase 2 ships when this passes. Phase 3 (smart-title diagnose) gets its own plan.

---

## Notes for the Engineer

- **No tests**: frontend has no vitest/RTL. Don't add a test framework here. The pure `applySessionFilters` function is structured to be unit-testable when infra lands later.
- **localStorage keys**: only `drpl_cc_session_sort` is added here. Don't reuse `drpl_cc_sidebar_collapsed` from Phase 1 or invent new keys outside this one.
- **Filter is in-memory, sort is persisted**: this is intentional. Filters often reflect transient intent ("show me only drafts right now"), while sort preference reflects ongoing taste. If you disagree, raise it as a concern in your report — don't quietly change the behavior.
- **Custom date range UX**: the two date inputs appear inline when Custom is selected. Empty start or empty end means open-ended on that side. If both are empty, the filter is effectively "all time" but stays marked as "Custom" — that's fine.
- **Don't restructure CommandCenterPage**: the file is 2800+ lines with delicate SSE consumers. Edits in this phase touch only the dashboard branch and the SessionSidebar mount — nothing else.
