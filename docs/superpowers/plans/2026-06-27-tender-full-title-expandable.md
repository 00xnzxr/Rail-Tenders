# Expandable Full Tender Titles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users read the complete tender title inline in the tender table, with full titles shown by default and a per-row chevron to collapse long titles to the compact one-line view.

**Architecture:** A single-file change to `TenderRow.tsx`. Each row owns local React state for whether its title is expanded. A chevron button (shown only for long titles) toggles that row's title between full-wrap and truncated. All other row behavior (navigation, selection) is unchanged.

**Tech Stack:** React 18, TypeScript, Tailwind CSS, `lucide-react` icons (all existing dependencies).

## Global Constraints

- Frontend has **no JS test runner** (no vitest/jest/testing-library). Verification is `npx tsc -b` (type safety) plus manual browser check. Do **not** add a test framework.
- No backend, API, or `types/tender.ts` changes — `tender.title` already carries the full title.
- Do not change row navigation (`navigate('/tenders/:id')`) or checkbox-select behavior.
- Follow existing Tailwind/class conventions already used in `TenderRow.tsx`.

---

### Task 1: Per-row expandable title with chevron

**Files:**
- Modify: `drpl-frontend/src/components/tenders/TenderRow.tsx`

**Interfaces:**
- Consumes: existing `TenderRowProps` (`tender`, `selected?`, `onToggleSelect?`) — no prop changes.
- Produces: no new exports; behavior change only.

- [ ] **Step 1: Add imports for state + chevron icons**

At the top of `TenderRow.tsx`, add `useState` and the two chevron icons. Update the existing React/lucide imports:

```tsx
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { Tender } from '../../types/tender';
```

(Keep the existing `StatusBadge`, `PortalBadge`, `PriorityBadge`, `WorkflowBadge`, `formatCurrency`, `formatDate`, and `date-fns` imports as they are.)

- [ ] **Step 2: Add the truncation threshold constant**

Above the `TenderRow` component (near the other module-level helpers like `DaysPendingBadge`), add:

```tsx
// Titles longer than this (chars) are clipped when collapsed and get an
// expand/collapse chevron. Short titles render plain with no chevron.
// ~45 chars is roughly what fits on one line in the `max-w-xs` title column.
const TITLE_TRUNCATE_THRESHOLD = 45;
```

- [ ] **Step 3: Add expand state inside the component, default expanded**

Inside `TenderRow`, alongside the existing `const navigate = useNavigate();` and `const selectable = ...;`, add:

```tsx
  // Full titles are shown by default; the user can collapse long ones to the
  // compact single-line view. Per-row, in-memory only (resets on nav/refresh).
  const [expanded, setExpanded] = useState(true);
  const isLong = (tender.title?.length ?? 0) > TITLE_TRUNCATE_THRESHOLD;
```

- [ ] **Step 4: Replace the title `<td>` with the chevron + expandable title**

Find the current title cell:

```tsx
      <td className="px-4 py-3 text-sm text-foreground font-medium max-w-xs truncate">
        {tender.title}
      </td>
```

Replace it with:

```tsx
      <td className="px-4 py-3 text-sm text-foreground font-medium max-w-xs">
        <div className="flex items-start gap-1.5">
          {isLong ? (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
              className="mt-0.5 shrink-0 text-muted-foreground hover:text-foreground transition-colors"
              aria-expanded={expanded}
              aria-label={expanded ? 'Collapse title' : 'Show full title'}
            >
              {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
          ) : (
            // Spacer keeps titles aligned with rows that do have a chevron.
            <span className="w-[14px] shrink-0" aria-hidden="true" />
          )}
          <span className={expanded ? 'whitespace-normal break-words' : 'truncate'}>
            {tender.title}
          </span>
        </div>
      </td>
```

Notes for the implementer:
- The `max-w-xs` stays on the `<td>` so the column width is bounded in both states; `truncate` (collapsed) clips to one line, `whitespace-normal break-words` (expanded) wraps to full text.
- `e.stopPropagation()` on the chevron prevents the row's `onClick` navigation from firing.
- The chevron is a real `<button>` so it is keyboard-focusable and Enter/Space-activatable.

- [ ] **Step 5: Type-check the frontend**

Run: `cd drpl-frontend && npx tsc -b`
Expected: exit code 0, no errors. (If `tsconfig.tsbuildinfo` shows as modified afterward, that is expected build cache output.)

- [ ] **Step 6: Manual browser verification**

With the Vite dev server running (`npm run dev`) and the backend up, open the Tenders page and confirm:
1. On load, long titles are shown in **full** (wrapped) with a `▾` chevron.
2. Clicking the `▾` chevron collapses that row's title to a single truncated line and the chevron becomes `▸`; clicking again re-expands. Other rows are unaffected.
3. Clicking anywhere on the row **except** the chevron navigates to the tender detail page.
4. Short titles (≤45 chars) show **no** chevron and stay column-aligned with longer rows.
5. The select-all / per-row checkbox (when present) still works.

- [ ] **Step 7: Commit**

```bash
git add drpl-frontend/src/components/tenders/TenderRow.tsx
git commit -m "feat(tenders): expandable full tender titles in tender table"
```

(If `drpl-frontend/tsconfig.tsbuildinfo` was regenerated and you want it included, add it in the same commit; otherwise leave it.)

---

## Self-Review

**Spec coverage:**
- Reveal mode = expandable rows → Step 4 (`whitespace-normal` vs `truncate`). ✓
- Trigger = chevron before title; chevron click does not navigate → Step 4 (`stopPropagation`). ✓
- Chevron only for long titles via ~45-char threshold → Steps 2–4 (`isLong`). ✓
- Default state expanded → Step 3 (`useState(true)`). ✓
- No persistence (per-row in-memory) → Step 3 comment. ✓
- No backend/API/type changes; `TenderTable.tsx` untouched → confirmed (only `TenderRow.tsx` modified). ✓
- Accessibility (real button, `aria-expanded`, `aria-label`) → Step 4. ✓
- Verification via `tsc -b` + manual → Steps 5–6. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; all code shown verbatim. ✓

**Type consistency:** `expanded`/`setExpanded`, `isLong`, `TITLE_TRUNCATE_THRESHOLD` used consistently across steps; icon names `ChevronDown`/`ChevronRight` match imports. ✓
