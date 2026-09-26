# Tender Section — Full Tender Title (Expandable Rows)

**Date:** 2026-06-27
**Status:** Approved (pending spec review)
**Scope:** `drpl-frontend/src/components/tenders/TenderRow.tsx` (single file)

## Problem

In the tender table, each tender's title is rendered truncated with an ellipsis
(`max-w-xs truncate` in `TenderRow.tsx:66`). Users see only a short, clipped
version of the title and have no way to read the entire title without leaving the
list and opening the tender detail page. They need to be able to see the full
title from within the table.

## Goal

Let users read the complete tender title directly in the tender table, while
preserving the option to keep rows compact.

## Decisions (from brainstorming)

- **Reveal mode:** Expandable rows. When expanded, the complete title is shown
  on its own **full-width row** spanning all columns directly beneath the main
  row (via a second `<tr>` with `colSpan`). This guarantees even very long
  titles are fully visible and never squeezed into the narrow title column.
  (Earlier iteration wrapped within the title column; that still clipped huge
  titles, so it was replaced by the full-width row.)
- **Expand trigger:** A chevron (`▸`/`▾`) placed before the title text. Clicking
  the chevron toggles only that row's title. Clicking anywhere else on the row
  still navigates to `/tenders/:id` (unchanged).
- **Chevron visibility:** Shown only when the title is long enough to be clipped,
  decided by a **character-length threshold (~45 chars)** rather than DOM pixel
  measurement. Short titles render with no chevron (plus a small spacer so the
  column stays aligned).
- **Default state on load:** **Expanded (full title shown)** for every row. Users
  collapse individual rows to compact when they want density. This directly
  satisfies the core request — full titles are visible by default.
- **Persistence:** None. Expand/collapse state is per-row, in-memory React state;
  it resets on navigation or refresh.

## Design

### Component changes — `TenderRow.tsx`

1. Add local state: `const [expanded, setExpanded] = useState(true)` (default
   expanded per the decision above).
2. Add a threshold constant, e.g. `const TITLE_TRUNCATE_THRESHOLD = 45`, and
   `const isLong = (tender.title?.length ?? 0) > TITLE_TRUNCATE_THRESHOLD;`.
3. Title cell (`<td>`) layout:
   - When `isLong`: render a chevron `<button>` (using `ChevronRight` /
     `ChevronDown` from `lucide-react`, already a dependency) before the title.
     - `onClick={(e) => { e.stopPropagation(); setExpanded(v => !v); }}` so it
       does not trigger row navigation.
     - `aria-expanded={expanded}` and `aria-label` ("Collapse title" /
       "Show full title").
   - When `!isLong`: render a small fixed-width spacer in the chevron's place so
     titles line up across rows.
4. Title text rendering:
   - **Collapsed:** keep current single-line clipped look
     (`max-w-xs truncate` / `whitespace-nowrap` with overflow ellipsis).
   - **Expanded:** drop the truncation clamp so the title wraps to full text
     within the column (`whitespace-normal break-words`). Row height grows
     naturally.

### What does NOT change

- No backend, API, or type changes — `tender.title` already carries the full
  title.
- `TenderTable.tsx` is untouched; rows are independent and own their own state
  (no "expand all" requirement, so state is not lifted to the table).
- Row navigation behavior is unchanged for all clicks except on the chevron.
- Checkbox-select behavior is unchanged.

## Component boundaries

- **What it does:** `TenderRow` renders one tender's row and now also owns the
  local expand/collapse state for its own title.
- **How you use it:** Same props as today (`tender`, `selected`,
  `onToggleSelect`). No new props.
- **Dependencies:** `lucide-react` chevron icons (existing), React `useState`.

## Accessibility

- Chevron is a real `<button>` (keyboard-focusable, Enter/Space activatable)
  with `aria-expanded` and a descriptive `aria-label`.

## Testing / Verification

- Manual: load the tenders page; confirm long titles show in full by default
  with a `▾` chevron; clicking the chevron collapses to the truncated single
  line (`▸`); clicking elsewhere on the row navigates to the detail page; short
  titles show no chevron and stay aligned.
- `npx tsc -b` passes (no type errors).

## Out of scope

- Global "expand/collapse all" control.
- Persisting expand state across navigation/refresh.
- Pixel-accurate overflow detection (using a character threshold instead).
