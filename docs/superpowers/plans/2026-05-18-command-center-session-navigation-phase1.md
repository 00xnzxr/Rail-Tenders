# Command Center Session Navigation — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a collapsible left sidebar (inside-session view) and a global Cmd/Ctrl+K quick-switcher palette to the Command Center, so users can navigate between sessions without round-tripping through the dashboard.

**Architecture:** Two new React components (`SessionSidebar`, `SessionSwitcherPalette`) plus a small `useSidebarCollapsed` hook and a pure `searchSessions` utility. Both components are driven by the existing `sessionList` state in `CommandCenterPage`, passed via props — no new context provider. Cmd/Ctrl+K is a global `document`-level keyboard listener attached in `CommandCenterPage`. Pure search/filter logic lives in its own file so it's testable in isolation when test infrastructure lands.

**Tech Stack:** React 18 + TypeScript + Tailwind CSS + lucide-react. No new dependencies.

**Important context for engineer:**
- The frontend has NO test framework today (no vitest, no React Testing Library). Verification gates in this plan are: (a) `npm run build` (which runs `tsc -b && vite build`) must pass, (b) `npm run dev` and manual UI walkthrough per the step-by-step checks. When you see a "Run test" step in this plan, it's actually `npm run build` or a manual browser check.
- `CommandCenterPage.tsx` is ~2767 lines. Don't restructure it. Only touch the regions called out in each step.
- The session list is already loaded into state — your job is to consume it from new components, not refetch.
- The spec for this work lives at `docs/superpowers/specs/2026-05-18-command-center-session-navigation-design.md`. Re-read it if any of the design intent below is unclear.

**Spec reference for this plan:** `docs/superpowers/specs/2026-05-18-command-center-session-navigation-design.md` — Phase 1 section.

---

## File Structure

**Create:**
- `drpl-frontend/src/components/command-center/searchSessions.ts` — pure search/filter utility
- `drpl-frontend/src/components/command-center/SessionSidebar.tsx` — collapsible left rail
- `drpl-frontend/src/components/command-center/SessionSwitcherPalette.tsx` — Cmd/Ctrl+K modal
- `drpl-frontend/src/hooks/useSidebarCollapsed.ts` — localStorage-backed boolean hook

**Modify:**
- `drpl-frontend/src/pages/CommandCenterPage.tsx` — wire sidebar into inside-session layout, mount palette at root, attach global keyboard listener, call `refreshSessionList()` on SSE `done` events

---

## Task 1: Pure Search Function

**Why this first:** It's a pure function with no UI dependencies. Get the matching/ranking logic right in isolation, then plug it into the palette.

**Files:**
- Create: `drpl-frontend/src/components/command-center/searchSessions.ts`

- [ ] **Step 1: Create the search utility**

Create `drpl-frontend/src/components/command-center/searchSessions.ts`:

```typescript
import type { CommandCenterSession } from '../../types/command-center';

/**
 * Pure search/rank function for the session palette.
 *
 * Matching: case-insensitive substring across title, tender_title,
 * tender_organisation.
 *
 * Ranking:
 *   1. Exact prefix match on any field (highest)
 *   2. Substring match on any field
 *   3. (non-matching sessions are excluded for non-empty queries)
 *
 * Within each rank bucket, ordered by updated_at desc (falling back to
 * created_at when updated_at is absent), so recently-touched tenders
 * surface first.
 *
 * Empty query returns ALL sessions, recency-sorted.
 */
export function searchSessions(
  sessions: CommandCenterSession[],
  query: string,
): CommandCenterSession[] {
  const q = query.trim().toLowerCase();
  const byRecency = (a: CommandCenterSession, b: CommandCenterSession) => {
    const at = a.updated_at || a.created_at;
    const bt = b.updated_at || b.created_at;
    return bt.localeCompare(at);
  };

  if (q === '') {
    return [...sessions].sort(byRecency);
  }

  const fields = (s: CommandCenterSession): string[] =>
    [s.title, s.tender_title, s.tender_organisation]
      .filter((v): v is string => typeof v === 'string' && v.length > 0)
      .map((v) => v.toLowerCase());

  const rank = (s: CommandCenterSession): 0 | 1 | 2 => {
    const fs = fields(s);
    if (fs.some((f) => f.startsWith(q))) return 0;
    if (fs.some((f) => f.includes(q))) return 1;
    return 2;
  };

  return sessions
    .map((s) => ({ s, r: rank(s) }))
    .filter((x) => x.r < 2)
    .sort((a, b) => (a.r - b.r) || byRecency(a.s, b.s))
    .map((x) => x.s);
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: Build succeeds with no TS errors.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/command-center/searchSessions.ts
git commit -m "feat(command-center): pure searchSessions utility for palette"
```

---

## Task 2: Sidebar-Collapsed Hook

**Files:**
- Create: `drpl-frontend/src/hooks/useSidebarCollapsed.ts`

- [ ] **Step 1: Create the hook**

Create `drpl-frontend/src/hooks/useSidebarCollapsed.ts`:

```typescript
import { useEffect, useState } from 'react';

const STORAGE_KEY = 'drpl_cc_sidebar_collapsed';

/**
 * Persisted boolean state for the Command Center sidebar collapse toggle.
 * Defaults to `false` (expanded). Survives reloads via localStorage.
 */
export function useSidebarCollapsed(): [boolean, (next: boolean) => void] {
  const [collapsed, setCollapsedState] = useState<boolean>(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw === '1';
    } catch {
      return false;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0');
    } catch {
      // ignore quota / privacy-mode failures
    }
  }, [collapsed]);

  return [collapsed, setCollapsedState];
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/hooks/useSidebarCollapsed.ts
git commit -m "feat(command-center): useSidebarCollapsed hook with localStorage"
```

---

## Task 3: SessionSidebar Component (Scaffolding)

**Files:**
- Create: `drpl-frontend/src/components/command-center/SessionSidebar.tsx`

- [ ] **Step 1: Create the sidebar component**

Create `drpl-frontend/src/components/command-center/SessionSidebar.tsx`:

```tsx
import { Bot, ChevronLeft, ChevronRight, Plus, Trash2 } from 'lucide-react';
import type { CommandCenterSession } from '../../types/command-center';
import { useSidebarCollapsed } from '../../hooks/useSidebarCollapsed';

interface SessionSidebarProps {
  sessions: CommandCenterSession[];
  currentSessionId: number | null;
  onSwitch: (sessionId: number) => void;
  onNew: () => void;
  onDelete?: (sessionId: number) => void;
  isLoading?: boolean;
}

/**
 * Collapsible left rail showing all Command Center sessions. Visible only
 * inside-session (the dashboard view already IS a session list).
 *
 * Width: 240px expanded, 56px collapsed. State persisted to localStorage.
 *
 * Active session highlighted with blue-50 bg + 2px left border.
 * Status dot: emerald (workspace complete), blue (workspace active),
 *   slate (no workspace).
 */
export default function SessionSidebar({
  sessions,
  currentSessionId,
  onSwitch,
  onNew,
  onDelete,
  isLoading,
}: SessionSidebarProps) {
  const [collapsed, setCollapsed] = useSidebarCollapsed();

  const widthClass = collapsed ? 'w-14' : 'w-60';

  return (
    <nav
      role="navigation"
      aria-label="Sessions"
      className={`flex-shrink-0 ${widthClass} bg-white border-r border-slate-200 flex flex-col transition-[width] duration-150 overflow-hidden`}
    >
      {/* Header: collapse toggle */}
      <div className="flex items-center justify-between px-3 py-3 border-b border-slate-100">
        {!collapsed && (
          <span className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
            Sessions
          </span>
        )}
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="p-1.5 rounded-md hover:bg-slate-100 text-slate-400 hover:text-slate-700 transition-colors ml-auto"
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
        </button>
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto py-2">
        {isLoading && sessions.length === 0 ? (
          <SkeletonRows collapsed={collapsed} />
        ) : sessions.length === 0 ? (
          <EmptyState collapsed={collapsed} onNew={onNew} />
        ) : (
          sessions.map((s) => (
            <SessionRow
              key={s.id}
              session={s}
              isActive={s.id === currentSessionId}
              collapsed={collapsed}
              onSwitch={() => onSwitch(s.id)}
              onDelete={onDelete ? () => onDelete(s.id) : undefined}
            />
          ))
        )}
      </div>

      {/* Footer: new session */}
      <div className="border-t border-slate-100 p-2">
        <button
          onClick={onNew}
          className={`w-full flex items-center gap-2 px-2 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors ${
            collapsed ? 'justify-center' : ''
          }`}
          title={collapsed ? 'New session' : undefined}
        >
          <Plus size={14} />
          {!collapsed && <span>New Session</span>}
        </button>
      </div>
    </nav>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

function SessionRow({
  session,
  isActive,
  collapsed,
  onSwitch,
  onDelete,
}: {
  session: CommandCenterSession;
  isActive: boolean;
  collapsed: boolean;
  onSwitch: () => void;
  onDelete?: () => void;
}) {
  const wp = session.workspace_progress;
  const hasWorkspace = wp?.has_workspace && wp.total > 0;
  const isComplete = hasWorkspace && wp!.completed >= wp!.total;
  const dotClass = isComplete
    ? 'bg-emerald-500'
    : hasWorkspace
    ? 'bg-blue-500'
    : 'bg-slate-300';

  const title =
    (session.tender_id ? session.tender_title : null) ||
    session.title ||
    'Untitled Session';

  return (
    <div
      className={`group relative mx-2 my-0.5 rounded-md ${
        isActive
          ? 'bg-blue-50 border-l-2 border-blue-500'
          : 'hover:bg-slate-50 border-l-2 border-transparent'
      }`}
    >
      <button
        onClick={onSwitch}
        className={`w-full flex items-center gap-2 px-2 py-2 text-left ${
          collapsed ? 'justify-center' : ''
        }`}
        title={collapsed ? title : undefined}
      >
        <div className="relative flex-shrink-0 w-7 h-7 rounded-md bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center">
          <Bot size={14} className="text-white" />
          <span
            className={`absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full ring-2 ring-white ${dotClass}`}
          />
        </div>
        {!collapsed && (
          <span className="flex-1 min-w-0 text-sm text-slate-700 truncate">
            {title}
          </span>
        )}
      </button>
      {!collapsed && onDelete && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          className="absolute right-1.5 top-1/2 -translate-y-1/2 p-1 rounded opacity-0 group-hover:opacity-100 bg-white text-red-500 hover:bg-red-50 transition-opacity"
          title="Delete session"
          aria-label="Delete session"
        >
          <Trash2 size={12} />
        </button>
      )}
    </div>
  );
}

function SkeletonRows({ collapsed }: { collapsed: boolean }) {
  return (
    <div className="space-y-2 px-2">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className={`flex items-center gap-2 px-2 py-2 animate-pulse ${
            collapsed ? 'justify-center' : ''
          }`}
        >
          <div className="w-7 h-7 rounded-md bg-slate-200" />
          {!collapsed && <div className="flex-1 h-3 rounded bg-slate-200" />}
        </div>
      ))}
    </div>
  );
}

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

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/command-center/SessionSidebar.tsx
git commit -m "feat(command-center): SessionSidebar component (rail + collapse)"
```

---

## Task 4: SessionSwitcherPalette Component (Cmd/Ctrl+K Modal)

**Files:**
- Create: `drpl-frontend/src/components/command-center/SessionSwitcherPalette.tsx`

- [ ] **Step 1: Create the palette component**

Create `drpl-frontend/src/components/command-center/SessionSwitcherPalette.tsx`:

```tsx
import { useEffect, useMemo, useRef, useState } from 'react';
import { Bot, Plus, Search } from 'lucide-react';
import type { CommandCenterSession } from '../../types/command-center';
import { searchSessions } from './searchSessions';

interface SessionSwitcherPaletteProps {
  open: boolean;
  onClose: () => void;
  sessions: CommandCenterSession[];
  currentSessionId: number | null;
  onSwitch: (sessionId: number) => void;
  onNew: () => void;
}

/**
 * Cmd/Ctrl+K quick-switcher palette. Opens centered as a modal overlay
 * (~520px wide). Search input filters the session list via searchSessions.
 *
 * Keyboard:
 *   - Esc: close
 *   - Enter: select focused row
 *   - ArrowUp / ArrowDown: move focus
 *   - any printable: types into search input (always focused on open)
 *
 * Click outside the palette card to dismiss.
 */
export default function SessionSwitcherPalette({
  open,
  onClose,
  sessions,
  currentSessionId,
  onSwitch,
  onNew,
}: SessionSwitcherPaletteProps) {
  const [query, setQuery] = useState('');
  const [focusedIndex, setFocusedIndex] = useState(0);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Reset query + focus when the palette opens/closes
  useEffect(() => {
    if (open) {
      setQuery('');
      setFocusedIndex(0);
      // Defer focus so the input is mounted
      requestAnimationFrame(() => searchRef.current?.focus());
    }
  }, [open]);

  const matches = useMemo(
    () => searchSessions(sessions, query),
    [sessions, query],
  );

  // Clamp focusedIndex when matches shrink
  useEffect(() => {
    if (focusedIndex >= matches.length) {
      setFocusedIndex(Math.max(0, matches.length - 1));
    }
  }, [matches.length, focusedIndex]);

  // Keep focused row scrolled into view
  useEffect(() => {
    if (!listRef.current) return;
    const focusedEl = listRef.current.querySelector<HTMLElement>(
      `[data-palette-index="${focusedIndex}"]`,
    );
    focusedEl?.scrollIntoView({ block: 'nearest' });
  }, [focusedIndex]);

  if (!open) return null;

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setFocusedIndex((i) => Math.min(matches.length - 1, i + 1));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setFocusedIndex((i) => Math.max(0, i - 1));
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      const picked = matches[focusedIndex];
      if (picked) {
        onSwitch(picked.id);
        onClose();
      }
      return;
    }
  };

  return (
    <div
      role="dialog"
      aria-label="Switch session"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-start justify-center bg-slate-900/30 backdrop-blur-sm pt-[12vh] px-4"
      onClick={onClose}
      onKeyDown={handleKey}
    >
      <div
        className="w-full max-w-[520px] bg-white rounded-xl shadow-2xl border border-slate-200 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-100">
          <Search size={16} className="text-slate-400" />
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setFocusedIndex(0);
            }}
            placeholder="Search sessions by tender, title, organisation…"
            className="flex-1 bg-transparent text-sm text-slate-800 placeholder-slate-400 focus:outline-none"
            aria-label="Search sessions"
          />
          <kbd className="hidden sm:inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-mono text-slate-400 bg-slate-100 border border-slate-200">
            esc
          </kbd>
        </div>

        {/* Results */}
        <div ref={listRef} className="max-h-[50vh] overflow-y-auto py-1">
          {matches.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <p className="text-sm text-slate-500">No sessions match.</p>
              <button
                onClick={() => {
                  onNew();
                  onClose();
                }}
                className="mt-2 inline-flex items-center gap-1.5 text-xs text-blue-600 hover:text-blue-800 hover:underline"
              >
                <Plus size={12} />
                Start a new session
              </button>
            </div>
          ) : (
            matches.map((s, i) => (
              <PaletteRow
                key={s.id}
                session={s}
                isFocused={i === focusedIndex}
                isCurrent={s.id === currentSessionId}
                index={i}
                onClick={() => {
                  onSwitch(s.id);
                  onClose();
                }}
                onHover={() => setFocusedIndex(i)}
              />
            ))
          )}
        </div>

        {/* Footer hint */}
        <div className="flex items-center justify-between px-4 py-2 border-t border-slate-100 bg-slate-50 text-[10px] text-slate-400">
          <span>
            <kbd className="font-mono px-1 py-0.5 rounded bg-white border border-slate-200">↑↓</kbd> navigate
            <span className="mx-1.5">·</span>
            <kbd className="font-mono px-1 py-0.5 rounded bg-white border border-slate-200">↵</kbd> select
          </span>
          <span>{matches.length} session{matches.length === 1 ? '' : 's'}</span>
        </div>
      </div>
    </div>
  );
}

function PaletteRow({
  session,
  isFocused,
  isCurrent,
  index,
  onClick,
  onHover,
}: {
  session: CommandCenterSession;
  isFocused: boolean;
  isCurrent: boolean;
  index: number;
  onClick: () => void;
  onHover: () => void;
}) {
  const title =
    (session.tender_id ? session.tender_title : null) ||
    session.title ||
    'Untitled Session';
  const subtitle = session.tender_organisation || '';

  return (
    <button
      data-palette-index={index}
      onMouseEnter={onHover}
      onClick={onClick}
      className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${
        isFocused ? 'bg-blue-50' : 'hover:bg-slate-50'
      }`}
    >
      <div className="w-7 h-7 rounded-md bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center flex-shrink-0">
        <Bot size={14} className="text-white" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm text-slate-800 truncate">{title}</p>
        {subtitle && <p className="text-xs text-slate-400 truncate">{subtitle}</p>}
      </div>
      {isCurrent && (
        <span className="text-[10px] font-medium text-blue-600 bg-blue-100 px-1.5 py-0.5 rounded">
          current
        </span>
      )}
    </button>
  );
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/command-center/SessionSwitcherPalette.tsx
git commit -m "feat(command-center): SessionSwitcherPalette (Cmd+K modal)"
```

---

## Task 5: Wire Sidebar Into Inside-Session Layout

This is the only step that modifies `CommandCenterPage.tsx`. The page is 2767 lines — DO NOT touch anything outside the regions called out.

**Files:**
- Modify: `drpl-frontend/src/pages/CommandCenterPage.tsx`

- [ ] **Step 1: Add imports**

Open `drpl-frontend/src/pages/CommandCenterPage.tsx`. Find the existing import block near the top of the file. Add these imports near the other command-center component imports (search for `import ArtifactPanel` to find the right region):

```tsx
import SessionSidebar from '../components/command-center/SessionSidebar';
import SessionSwitcherPalette from '../components/command-center/SessionSwitcherPalette';
```

- [ ] **Step 2: Add palette-open state**

Find the existing `useState` block (the file has many — anchor to where `setArtifactPanelOpen` is defined, line ~124). Right after `setArtifactPanelOpen`, add:

```tsx
const [paletteOpen, setPaletteOpen] = useState(false);
```

- [ ] **Step 3: Insert sidebar as the first child of the existing horizontal flex container**

Find the existing horizontal flex container in the inside-session render branch. Anchor: search for the comment

```
{/* ── Main content: horizontal split (chat + artifact panel | workspace | documents) ── */}
```

The line immediately after that comment opens an existing flex container:

```tsx
<div className="flex flex-1 overflow-hidden relative">
```

The existing children of this div are (in order): Workspace tab, Documents tab, Chat tab (which contains ArtifactPanel + chat view). Only one tab is visible at a time based on `activeTab`.

**Insert the sidebar as the FIRST child of this div, before the Workspace tab.** The result should look like:

```tsx
{/* ── Main content: horizontal split (chat + artifact panel | workspace | documents) ── */}
<div className="flex flex-1 overflow-hidden relative">

  {/* NEW: Session sidebar — visible regardless of active tab */}
  <SessionSidebar
    sessions={sessionList}
    currentSessionId={session?.id ?? null}
    onSwitch={(id) => navigate(`/command-center/${id}`)}
    onNew={handleNewSession}
    onDelete={(id) => setDeleteTargetId(id)}
    isLoading={false}
  />

  {/* EXISTING — Workspace tab — UNCHANGED */}
  <div className={`flex-1 overflow-y-auto ${activeTab !== 'workspace' ? 'hidden' : ''}`}>
    {/* ... */}
  </div>

  {/* EXISTING — Documents tab — UNCHANGED */}
  {/* ... */}

  {/* EXISTING — Chat tab — UNCHANGED */}
  {/* ... */}

</div>
```

⚠️ **Do NOT add an extra wrapper `<div>`**. The existing `<div className="flex flex-1 overflow-hidden relative">` is already the horizontal flex container — the sidebar slots in as a new sibling, not a new outer layer. Adding an extra wrapper will break the inherited `flex-1` sizing for the tabs.

- [ ] **Step 4: Mount the palette at root**

Still in the inside-session render branch (or at the top of the page render, doesn't matter — it's a fixed-position modal), add the palette JSX just before the closing JSX of the page component:

```tsx
<SessionSwitcherPalette
  open={paletteOpen}
  onClose={() => setPaletteOpen(false)}
  sessions={sessionList}
  currentSessionId={session?.id ?? null}
  onSwitch={(id) => navigate(`/command-center/${id}`)}
  onNew={() => {
    setPaletteOpen(false);
    handleNewSession();
  }}
/>
```

Also mount the palette in the dashboard render branch (search for `showSessionList && !session`) so Cmd+K works on the dashboard too. Add it inside the dashboard's outer `<div>` near the bottom.

- [ ] **Step 5: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: Build succeeds with no TS errors.

- [ ] **Step 6: Manual UI walkthrough**

Run: `cd drpl-frontend && npm run dev`

Open http://localhost:5173, log in, go to Command Center.

Check:
- Dashboard view (no session selected): looks identical to before. No sidebar visible.
- Click into a session: sidebar appears on the left. Current session is highlighted with blue-50 background + left border.
- Click another session in the sidebar: navigates to that session, highlight moves.
- Hover over a session row: trash icon appears on the right. Click it: existing delete confirm dialog opens.
- Click collapse chevron: sidebar narrows to 56px, only icons visible. Click again: expands back. Reload page: collapsed state persists.
- "+ New Session" button at the bottom: creates a new session, sidebar refreshes.

- [ ] **Step 7: Commit**

```bash
git add drpl-frontend/src/pages/CommandCenterPage.tsx
git commit -m "feat(command-center): wire SessionSidebar into inside-session layout"
```

---

## Task 6: Global Cmd/Ctrl+K Keyboard Handler

**Files:**
- Modify: `drpl-frontend/src/pages/CommandCenterPage.tsx`

- [ ] **Step 1: Add the global keyboard listener**

Find an existing `useEffect` block in `CommandCenterPage.tsx` near the top of the component (after the state declarations). Add a new `useEffect`:

```tsx
// Global Cmd/Ctrl+K to toggle the session switcher palette. Captures
// before chat/textarea focus so it works while typing.
useEffect(() => {
  const onKey = (e: KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      setPaletteOpen((v) => !v);
    }
  };
  document.addEventListener('keydown', onKey);
  return () => document.removeEventListener('keydown', onKey);
}, []);
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 3: Manual UI walkthrough**

Run: `cd drpl-frontend && npm run dev`

Open Command Center, then:
- Press Cmd+K (Mac) or Ctrl+K (Win/Linux). Palette opens, search input is focused.
- Type a few letters. List filters live.
- Arrow Down a few times. Highlight moves through rows.
- Press Enter. Navigates to the focused session, palette closes.
- Reopen with Cmd+K. Press Esc. Palette closes.
- Reopen. Click outside the white card (on the backdrop). Palette closes.
- Press Cmd+K while focused in the chat textarea. Palette still opens (preventDefault works).
- Open palette, type a non-matching query. "No sessions match." + "Start a new session" link visible.

- [ ] **Step 4: Commit**

```bash
git add drpl-frontend/src/pages/CommandCenterPage.tsx
git commit -m "feat(command-center): global Cmd+K to open session switcher palette"
```

---

## Task 7: Refresh Session List On SSE `done` Events

The sidebar shows `artifact_count` and `message_count` for each session. Without this step, those badges go stale during a streaming response until the next manual refresh.

**Files:**
- Modify: `drpl-frontend/src/pages/CommandCenterPage.tsx`

- [ ] **Step 1: Locate the `done` SSE event handler**

In `CommandCenterPage.tsx`, search for `case 'done':` — there are multiple SSE consumers (the streaming has several call sites). Anchor: line ~794 has the pattern:

```tsx
case 'done':
  currentOutputType = data.output_type || currentOutputType;
  if (data.agents_used) currentRoutedFrom = data.agents_used.join(',');
  if (session.id) {
    getCommandCenterSession(session.id).then((s) => {
      // ... pipeline_state diff handling ...
    });
  }
```

- [ ] **Step 2: Add `refreshSessionList()` call to each `done` handler**

After the existing `getCommandCenterSession(session.id).then(...)` block, but inside the same `case 'done':`, add a call to refresh the full session list so the sidebar's per-session counts update. There are 4 occurrences in this file (search for `case 'done':` — the file has multiple identical SSE consumer blocks). Apply this change to each:

```tsx
case 'done':
  currentOutputType = data.output_type || currentOutputType;
  if (data.agents_used) currentRoutedFrom = data.agents_used.join(',');
  if (session.id) {
    getCommandCenterSession(session.id).then((s) => {
      // ... existing pipeline_state diff handling ...
    });
    // NEW: refresh the session list so sidebar counts stay accurate
    refreshSessionList().catch(() => {
      // non-fatal; sidebar just shows stale counts until next refresh
    });
  }
  break;
```

⚠️ Some of the four blocks may not have a `session.id` check — match the change to the surrounding existing logic. The key invariant: refresh AFTER the response is done, regardless of where the SSE consumer lives.

- [ ] **Step 3: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 4: Manual UI walkthrough**

Run: `cd drpl-frontend && npm run dev`

- Open a session, send a chat message that triggers an agent run.
- While the response is streaming, glance at the sidebar — its session row's message count for the current session may still be stale (that's fine, mid-stream).
- After the response completes (the "running" pill disappears), the sidebar's message count should bump to match.
- Trigger a pipeline step that creates an artifact (e.g. run the analyzer). After it completes, the sidebar's artifact-count badge should bump.

- [ ] **Step 5: Commit**

```bash
git add drpl-frontend/src/pages/CommandCenterPage.tsx
git commit -m "feat(command-center): refresh session list on SSE done events"
```

---

## Task 8: Mobile Breakpoints

**Files:**
- Modify: `drpl-frontend/src/components/command-center/SessionSidebar.tsx`

- [ ] **Step 1: Add responsive width classes**

Open `SessionSidebar.tsx`. Find the `widthClass` computation:

```tsx
const widthClass = collapsed ? 'w-14' : 'w-60';
```

Replace with:

```tsx
// <640px: hidden entirely (Cmd+K is the only switcher on mobile)
// <960px: auto-collapse to icon rail regardless of user preference
// >=960px: respect user preference
const widthClass = collapsed
  ? 'w-14 hidden sm:flex md:flex'   // ≥640px (sm): icon rail; mobile: hidden
  : 'w-14 sm:w-14 md:w-60 hidden sm:flex'; // ≥960px (md): full width; 640-960: forced rail; mobile: hidden
```

⚠️ Tailwind defaults: `sm:` = 640px, `md:` = 768px. The spec asks for 960px as the rail-vs-full breakpoint, but Tailwind has no built-in `lg-` for 960. The simplest portable choice: use `md:` (768px) for the rail-vs-full split — close enough to the 960px target and avoids modifying the Tailwind config.

If you specifically want 960px, add a `lg-` override in `tailwind.config.js` (out of scope for this task — file an issue).

Final classes for the simplest portable behavior:

```tsx
const widthClass = collapsed
  ? 'hidden sm:flex w-14'
  : 'hidden sm:flex w-14 md:w-60';
```

- [ ] **Step 2: Add mobile palette button to the header**

Open `CommandCenterPage.tsx`. Find the page header inside the inside-session view (search for the section with `{session?.tender_title && (` around line 2152). Add a button visible only on mobile that opens the palette:

```tsx
{/* Mobile-only: open palette since sidebar is hidden < 640px */}
<button
  onClick={() => setPaletteOpen(true)}
  className="sm:hidden flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white border border-slate-200 text-slate-700 text-xs"
  title="Switch session"
  aria-label="Switch session"
>
  <Search size={14} />
  Switch
</button>
```

Don't forget to import `Search` from `lucide-react` if it isn't already imported in this file (it usually is — grep first).

- [ ] **Step 3: Verify TypeScript compiles**

Run: `cd drpl-frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 4: Manual UI walkthrough**

Run: `cd drpl-frontend && npm run dev`

In the browser DevTools, toggle device toolbar and resize:
- 1280px wide: sidebar visible at full 240px width.
- 700px wide (sm range): sidebar collapses to 56px icon rail regardless of user preference.
- 500px wide (mobile): sidebar hidden entirely. A "Switch" button appears in the header. Tapping it opens the palette.

- [ ] **Step 5: Commit**

```bash
git add drpl-frontend/src/components/command-center/SessionSidebar.tsx drpl-frontend/src/pages/CommandCenterPage.tsx
git commit -m "feat(command-center): mobile breakpoints for sidebar + mobile palette button"
```

---

## Verification (End of Phase 1)

After all tasks complete, do a final end-to-end pass.

- [ ] **Step 1: Type check + build**

```bash
cd drpl-frontend
npm run build
```
Expected: clean build, no TS errors.

- [ ] **Step 2: Manual end-to-end test**

Run: `cd drpl-frontend && npm run dev`

Walkthrough:
1. Hard refresh the Command Center.
2. From dashboard, press Cmd+K → palette opens → type → arrow → enter → enters session.
3. Inside session: sidebar visible, current highlighted.
4. Collapse sidebar via chevron → reload → still collapsed (localStorage).
5. Expand sidebar.
6. Click another session in sidebar → switches cleanly, no broken streams.
7. Delete a session from the sidebar trash → confirm dialog → session disappears from sidebar AND dashboard.
8. Send a chat message that triggers the analyzer. After completion, sidebar count bumps.
9. Resize to mobile (DevTools): sidebar hides, header "Switch" button visible, tap → palette opens.
10. Press Esc in the palette → closes.

- [ ] **Step 3: Push the branch (optional — depends on team flow)**

```bash
git push origin <branch>
```

Phase 1 ships when this passes. Phases 2 (filter/sort) and 3 (smart-title diagnosis) get their own plans.

---

## Notes for the Engineer

- **No tests in this plan**: the frontend has no vitest / RTL today. The pure `searchSessions` function is structured to be unit-testable if test infra lands later. Don't add a test framework as part of this PR — it's scope creep.
- **TypeScript is the safety net**: `npm run build` runs `tsc -b` first and will fail on any type error. Trust it.
- **Don't touch unrelated regions of `CommandCenterPage.tsx`**: the file is 2767 lines and has many delicate SSE consumers. If a step says "find X and modify it", do exactly that — don't refactor surrounding code.
- **localStorage keys used**: `drpl_cc_sidebar_collapsed`. Don't reuse this key for anything else.
- **Breakpoints**: I chose `sm:` (640px) and `md:` (768px) for portability with Tailwind defaults. The spec mentions 960px — if you really want that, update `tailwind.config.js` to add a custom breakpoint, but it's not required for this PR.
