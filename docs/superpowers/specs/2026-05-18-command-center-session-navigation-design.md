# Command Center Session Navigation — Design

**Date:** 2026-05-18
**Status:** Approved (brainstorming) — ready for implementation plan
**Scope:** Three phased polish improvements to the Command Center session list / sidebar surface

## Problem

The Command Center session list today is a single dashboard view: a 3-column grid of `SessionDashboardCard`s at `/command-center`. Once you click into a session (`/command-center/:sessionId`), there is no in-session switcher — to move to a different tender you must navigate back to the dashboard, find the card, and click in. Pain points reported:

1. **No in-session switcher** — every session-to-session move costs a round trip through the dashboard.
2. **No filter / sort** — once the session count grows beyond ~20, the unsorted grid is hard to scan.
3. **Untitled / weak session titles** — sessions often render as "Untitled Session" because auto-naming doesn't pull from enriched tender data.

This spec covers the design for fixing all three, phased as three independently-shippable PRs.

## Scope Decomposition

**In scope:**
- A collapsible left sidebar inside session pages (Phase 1)
- A Cmd/Ctrl+K quick switcher palette (Phase 1)
- Filter + sort controls on dashboard AND sidebar with shared state (Phase 2)
- Auto-rewriting `ProposalSession.title` from enriched tender data, idempotently (Phase 3)

**Out of scope:**
- Search across artifacts or message bodies (only session metadata is searched)
- Drag-to-reorder, right-click context menus, multi-select inside the sidebar
- Org filter in Phase 2 — too sparse to justify until many tenders per org exist
- Real-time updates to other-than-current session counts (sidebar refreshes only on `done` SSE events for the current session, on `+ New`, and on delete)

## Architecture

### Phase 1: Sidebar + Cmd/Ctrl+K Palette

**Layout** — only changes the inside-session view; dashboard is unchanged:

```
┌──────────────┬─────────────────────────────────┬──────────────┐
│              │                                 │              │
│   Sidebar    │   Chat / Workspace / Documents  │   Artifact   │
│  (sessions)  │          (existing tabs)        │  Panel       │
│   ~240px     │                                 │  (existing)  │
│  collapsible │                                 │ (when open)  │
│   to ~56px   │                                 │              │
└──────────────┴─────────────────────────────────┴──────────────┘
```

**New components** under `drpl-frontend/src/components/command-center/`:

- `SessionSidebar.tsx` — left rail. Renders a compact session list (icon + truncated title + small status dot), highlights current session by `useParams().sessionId`, has expand/collapse toggle pinned to top, "+ New Session" button at bottom. Reads `sessionList` via props from `CommandCenterPage`.
- `SessionSwitcherPalette.tsx` — Cmd/Ctrl+K modal. Centered overlay, ~520px wide, with a search input at top and a list of session matches below. Mounts at the root of `CommandCenterPage` so it's available from both dashboard and inside-session views.

**Modifications to `CommandCenterPage.tsx`**:

- Inside-session render branch wraps existing chat + artifact panel in a 3-column flex layout with `<SessionSidebar />` on the left.
- `<SessionSwitcherPalette />` rendered at root level (modal), unconditional.
- Dashboard view (`showSessionList && !session`) is unchanged.

**Sidebar collapsed state** persisted to `localStorage` under `drpl_cc_sidebar_collapsed`. Default: expanded.

**Mobile breakpoints** (match existing `ArtifactPanel.tsx` logic):
- `<960px`: sidebar auto-collapses to the icon rail.
- `<640px`: sidebar hides entirely; Cmd+K (also tappable via a header button) is the only switcher.

### Phase 2: Filter & Sort

**Component**: `SessionFilterBar.tsx` (new). Used in two surfaces with a `compact` prop:
- Above the dashboard grid (full size — chips inline)
- At the top of the sidebar (compact — chips wrap to two rows, or a single "Filter" dropdown)

**Filter chips** (single-select per category):
- Status — All / Draft / Submitted / Approved
- Mode — All / Tender-linked / Ad-hoc
- Date range — All / Last 7d / Last 30d / Custom (opens a small date-input popover)

Active filters show a count badge ("3 filters") and a "Clear all" link.

**Sort dropdown**: Recency (default — `updated_at` desc), Created (`created_at` desc), Name (`title` asc), Status. Persisted to `localStorage` under `drpl_cc_session_sort`.

**Shared filter state** lives in a new `useSessionFilters` hook (`drpl-frontend/src/hooks/useSessionFilters.ts`). Both the dashboard and the sidebar read/write through it so toggling a filter in either surface immediately reflects in the other.

**Empty filtered state**: "No sessions match these filters" + "Clear filters" button. Sidebar shows a single muted row with the same message + clear action.

**Order of operations**: filter first (bottleneck), then sort. All client-side — no API change.

### Phase 3: Smart Session Titles — Diagnose & Strengthen

**Discovery during spec review**: `tender_enrichment_service.enrich_tender_from_analysis` (lines 102-229) **already implements** the intended naming pipeline end-to-end:

- Composes title as `"{tender_reference} — {name_of_work[:80]}"` (lines 188-196).
- Falls back to `name_of_work` only, then `tender_reference` only, then None.
- Calls `_is_default_title()` before overwriting `ProposalSession.title` — handles `"command center …"`, `"new session"`, `"standalone session"`, `"tender analysis assistance needed"`, `"untitled …"`, and empty.
- Updates ALL linked sessions for a tender (lines 207-219).

A startup backfill `_backfill_session_titles_from_tenders()` exists in `drpl-backend/app/main.py:273` for old rows.

So Phase 3 turns from "implement" into "diagnose why this doesn't produce good titles in practice." Hypotheses to validate during implementation:

1. **Enrichment never runs** — `enrich_tender_from_analysis` is wrapped in try/except in `_run_v2_analysis`; a silent failure means no rename. Add diagnostic logging at the call site if absent.
2. **`tender_reference` / `name_of_work` extraction is too narrow** — current logic reads only the first per-doc with non-empty `key_facts` (lines 136-140). For multi-doc tenders where the master RFP isn't first, this misses. Strengthen by scanning all per-doc summaries and picking the most authoritative ref (longest match against `IREPS_REF_PATTERN` / `GEM_REF_PATTERN`).
3. **Sessions without `tender_id`** — ad-hoc sessions (no tender link) never get enriched. Out of scope for this phase; they keep their user-typed or default title.
4. **Backfill never ran in production** — confirm `_backfill_session_titles_from_tenders()` actually executes on startup for the relevant Tender rows. Add a one-line log of "backfilled N session titles" so it's auditable.

**Deliverable for Phase 3**: a small PR that adds (a) diagnostic logs at each enrichment branch point, (b) the multi-doc fallback for `tender_reference` / `name_of_work` extraction, (c) a one-time confirmatory log line in the startup backfill. No schema change. No frontend change.

**Naming format is unchanged** — `"{tender_ref} — {name_of_work[:80]}"` matches existing code at line 190.

## Data Flow

**Single source of truth: `sessionList` state in `CommandCenterPage`.** No new context provider in Phase 1 — props down.

```
CommandCenterPage (owns: sessionList, currentSessionId, refreshSessionList)
  ├─ SessionSidebar          ← reads sessionList, calls onSwitch(id), onNew()
  ├─ SessionSwitcherPalette  ← reads sessionList, calls onSwitch(id), onNew()
  └─ (existing chat + artifact panel + dashboard)
```

**Refresh triggers** (all existing or trivial extension):
- On mount — `useEffect` already loads `listCommandCenterSessions()`.
- After "+ New Session" — existing `handleNewSession` already refreshes.
- After delete — existing `refreshSessionList` already covers.
- After SSE `done` event — extend the existing `getCommandCenterSession(id)` call at `CommandCenterPage.tsx:798` to also call `refreshSessionList()` so message_count and artifact_count badges in the sidebar stay accurate.
- After SSE `artifact_created` — no refresh needed; sidebar only shows counts, not per-artifact details.

**Palette search** — client-side, plain JS filter (no fuzzy lib):
- Match fields: `session.title`, `session.tender_title`, `session.tender_id_external`, `session.tender_organisation`.
- Case-insensitive substring on each. Rank: exact-prefix > substring.
- Result order: matching first, then non-matching; within matching, sort by `updated_at` desc.
- Empty query → show all sessions, recency-sorted.

**Navigation transitions**:
- Switch session: `navigate(\`/command-center/${id}\`)`. Existing `useEffect` watching `:sessionId` handles state reset.
- Current session highlighted via `session.id === currentSessionId` (from `useParams`).
- Deleting current session from sidebar → confirm dialog → delete → `navigate('/command-center')`.

## Interaction & Keyboard (Phase 1)

**Cmd/Ctrl+K**:
- Global `document`-level listener attached via `useEffect` in `CommandCenterPage`. Detects `(e.metaKey || e.ctrlKey) && e.key === 'k'`, calls `preventDefault()`, toggles palette open.
- Esc closes. Enter selects focused row. ↑/↓ moves focus. Type to filter.
- Auto-focus search input on open; auto-select first match so Enter is always live.
- Click outside palette card dismisses (semi-transparent backdrop).
- Works even while typing in chat textarea (preventDefault gives global handler priority).

**Sidebar**:
- Collapse toggle button in the sidebar header (chevron-left ↔ chevron-right).
- Collapsed width: 56px (icons + small selected-session highlight bar).
- Expanded width: 240px.
- Width transition: ~150ms.
- Hover on collapsed icon shows tooltip with session title.
- "+ New Session" button always visible (icon-only when collapsed).

**Session row in sidebar**:
- Bot icon (matches dashboard).
- Title — single line, truncated.
- Status dot: blue (active workspace), emerald (workspace complete), grey (no workspace).
- Active session: blue-50 background + 2px left border in blue.
- Hover: slate-50 background.
- Trash icon on hover (right side) — uses existing delete flow.

**Accessibility**:
- Palette: `role="dialog"`, `aria-label="Switch session"`.
- Sidebar: `role="navigation"`, `aria-label="Sessions"`.
- Collapsed icons have `title` attribute for screen readers.

## Edge Cases

- **Streaming session + palette open**: user opens palette mid-stream. Original stream keeps running in background. If they switch sessions, the React Router unmount will abort the SSE client per existing cleanup.
- **Current session deleted**: navigate to `/command-center`.
- **No sessions yet**: sidebar shows a single muted row "No sessions yet — start one →" linking to "+ New".
- **Loading state**: sidebar shows 3 skeleton rows while initial `listCommandCenterSessions()` is pending.
- **Streaming dot in sidebar**: only shown for the current session (we only have `isStreaming` flag for the session the user is in). Other-session in-progress state is not surfaced. Accepted limitation.

## Testing

**Phase 1** (frontend):
- Unit tests for `SessionSwitcherPalette` search/match logic — verify field ranking, recency tie-break, empty-query behavior.
- Manual: keyboard nav (Cmd+K, Esc, Enter, arrows), sidebar collapse persistence across reloads, mobile breakpoints, deleting current session, switching mid-stream.

**Phase 2** (frontend):
- Unit tests for `useSessionFilters` hook — filter combinations, sort stability, empty-filtered output.
- Manual: filter chips in both dashboard and sidebar stay in sync, sort persists to localStorage, clear-all resets.

**Phase 3** (backend):
- Unit test extending coverage of `_pick_first` + the multi-doc tender_reference scan — verify it picks the best ref across N per-doc summaries, not just the first.
- Integration: run analyzer on a tender whose master RFP isn't the first doc uploaded; confirm `ProposalSession.title` is still rewritten.
- Log audit: confirm `[enrich] tender X: applied {...}` appears in logs for a real run, AND `_backfill_session_titles_from_tenders` emits a one-line summary on startup.

## Open Questions

None at the time of approval. If the user pushes back during plan execution on any of the above (e.g., sidebar width, default sort field), revisit and update this spec.

## Phasing Summary

| Phase | Scope | PR count | Backend changes |
|---|---|---|---|
| 1 | Sidebar + Cmd/Ctrl+K palette | 1 | None |
| 2 | Filter + sort (dashboard + sidebar) | 1 | None |
| 3 | Smart-titles diagnose & strengthen | 1 | `tender_enrichment_service` (multi-doc ref scan + diagnostic logs) |

Each phase is shippable independently. Phase 1 delivers the biggest user-pain payoff first.
