# Tender Sub-sections (Decision-Stage Views) — Design

**Date:** 2026-07-11
**Status:** Approved (design sample validated → spec)
**Scope package:** `drpl-frontend` (primary) + `drpl-backend` (one additive filter)

---

## 1. Problem

The Tenders page (`TendersPage.tsx`) presents ~11 filter dropdowns plus a chip row and an
Advanced toggle all at once. For a non-technical user this is a wall of controls with no
obvious starting point. The user wants tenders split into **sub-sections in the sidebar** under
the "Tenders" tab — each a pre-filtered *view* the user selects and works out of — so the choice
becomes "where is the work?" instead of "assemble the right filter combination."

Design sample built and approved (artifact): sidebar sub-nav with live count badges, a clean
per-view header, and a collapsible **Refine** bar.

## 2. Decisions (locked via the design sample)

- **Sub-sections organized by decision stage (segment-driven), five views:**
  | Label (UI) | Filter definition |
  | --- | --- |
  | **To Bid** | `segment = "to_bid"` |
  | **Worth a Look** | `segment = "not_bidable"` |
  | **Closing Soon** | `status = open` AND `score_min = 0.70` AND `closing_before = now + 7d` |
  | **New / Unscored** | `unscored = true` (`ai_relevance_score IS NULL`) |
  | **All Tenders** | no view filter (current default list) |
- **Labels are user-facing plain language** exactly as above (not the raw segment names).
- **Sidebar rendering:** the "Tenders" nav item expands into a nested list (reusing the existing
  "Platform Admin" expander pattern in `SidebarNav.tsx`), each row with a **semantic color dot** and
  a **live count badge**.
- **Filter bar → collapsible "Refine":** the view sets the base list; a slim toolbar (search box +
  Refine button + Sort) sits above the results. Refine expands to the full existing filter set
  (portal, location, worth, eligibility, closing window, match band), applied **within** the view.
  A badge on Refine shows how many refinements are active.
- **Semantic color per view** (emerald = To Bid, amber = Closing Soon, blue = New, slate = Worth a
  Look / All) echoed on the sidebar dot + the view header pill.
- **Deep-linkable views:** each view is a route (`/tenders/to-bid`, `/tenders/worth-a-look`,
  `/tenders/closing-soon`, `/tenders/new`, `/tenders` = All), so views are bookmarkable/shareable.

## 3. Architecture

```
SidebarNav ──expands──▶ Tenders sub-nav (5 views, count badges)
                                  │ route: /tenders/:view?
                                  ▼
                        TendersPage(view)
                          ├─ resolves view → base filters (TENDER_VIEWS map)
                          ├─ ViewHeader (pill + plain-language blurb + count)
                          ├─ RefineBar (collapsed: search+Refine+Sort; expanded: full filters)
                          └─ TenderCardList (existing) + Pagination (existing)
```

- **Single source of truth for views:** a `TENDER_VIEWS` constant (frontend) maps
  `viewKey → { label, color, blurb, routeSlug, baseFilters }`. The sidebar, the route resolver,
  and the header all read it. Adding/renaming a view is a one-place edit.
- **Counts:** the sidebar badges call the existing tenders list count for each view's base filters
  (a lightweight `count`-only fetch). To avoid five separate round-trips, add one endpoint
  `GET /api/tenders/view-counts` returning all five counts in one call.
- **Refine** re-uses the current `TenderFilters` component's fields, but rendered inside a
  collapsible container and **merged on top of** the active view's base filters (view filters win as
  the floor; refine narrows further).

## 4. Backend changes (additive, no schema change)

### 4.1 `unscored` filter — `tender_service._apply_tender_filters` + `tenders.py` route
Add an `unscored: bool = False` param. When true: `query.filter(Tender.ai_relevance_score.is_(None))`.
Wire it through `get_tenders`, `count_tenders`, and the `GET /api/tenders` route as a query param
`unscored`. The "New / Unscored" view needs unscored tenders regardless of `below_threshold`, so the
route passes `include_below_threshold=True` when `unscored` is set (unscored rows can't be below a
threshold that was never computed).

### 4.2 `GET /api/tenders/view-counts`
Returns `{to_bid, worth_a_look, closing_soon, new_unscored, all}` — each computed with
`count_tenders(...)` using the same filter definitions as §2. One endpoint, five counts, so the
sidebar badges are a single request. `closing_soon` reuses the dashboard's window
(open + `score_min≥0.70` + closing within 7 days). Auth via the existing route dependency.

## 5. Frontend changes

- **`src/lib/tenderViews.ts` (NEW):** the `TENDER_VIEWS` map + `resolveView(slug)` helper. One place
  defining label, color token, blurb, slug, and base filters per view.
- **`SidebarNav.tsx`:** replace the single Tenders `NavItem` with an expandable group (reuse the
  `adminOpen` expander pattern): parent "Tenders" toggles a nested list of the five views; each row
  shows a color dot + label + count badge (counts from `getTenderViewCounts()`), active-route styled.
  Collapsed-rail mode keeps a single Tenders icon linking to `/tenders`.
- **`src/router.tsx`:** add `{ path: 'tenders/:view', element: <TendersPage /> }` alongside the
  existing `tenders` route (both render `TendersPage`; the param selects the view; unknown slug →
  redirect to `/tenders`).
- **`TendersPage.tsx`:** read the `:view` param, resolve base filters from `TENDER_VIEWS`, render a
  `ViewHeader` (semantic pill + blurb + result count) and the new `RefineBar`; merge refine filters
  over base filters before calling `useTenders`. Keep the existing selection/batch tools under
  Advanced.
- **`src/components/tenders/RefineBar.tsx` (NEW):** collapsed toolbar (search + Refine toggle + Sort)
  and, when expanded, the existing `TenderFilters` fields; emits the merged filter delta. Shows an
  active-refinement count badge.
- **`src/lib/api.ts`:** add `getTenderViewCounts()` and thread `unscored` into the tenders list
  filter params.

## 6. Data flow

`SidebarNav` fetches `getTenderViewCounts()` once on mount for the badges. `TendersPage` resolves the
route's view → base filters, merges any Refine deltas, and drives the existing `useTenders(filters)`.
Changing view = navigation (new route) which resets Refine; refining = local filter merge within the
view. Counts refresh when the page regains focus (best-effort; stale counts never block the list).

## 7. Error handling

- `getTenderViewCounts()` failing → badges hide (show label only); the views still work. Best-effort.
- Unknown `:view` slug → redirect to `/tenders` (All).
- Empty view → a friendly per-view empty state ("No tenders ready to bid yet — try Worth a Look or
  New.") rather than a blank list.
- Refine applied on top of a view that yields zero → "No matches in this view. Clear refinements or
  switch views." with a one-click clear.

## 8. Testing

**Backend (pytest, in-memory SQLite):**
- `unscored` filter: returns only `ai_relevance_score IS NULL`, and includes below-threshold-null rows.
- `GET /api/tenders/view-counts`: each of the five counts matches an equivalent direct filter query;
  `closing_soon` respects the open+score+7d window; `new_unscored` counts nulls.

**Frontend:**
- `npm run build` clean.
- `resolveView` maps every slug (and unknown → All); `TENDER_VIEWS` base filters match §2.
- Sidebar renders five views with counts; active view is route-highlighted; Refine expands/collapses
  and merges over base filters (view filter is the floor).

## 9. Non-goals

- No change to how tenders are scored/segmented — this only *navigates* existing segments.
- No new DB columns or migrations.
- No removal of any existing filter capability — every current filter survives inside Refine.
- No change to the tender card, detail page, or command-center flows.

## 10. Sequencing (ship order)

1. Backend: `unscored` filter + `GET /api/tenders/view-counts` + tests.
2. `tenderViews.ts` (view map) + `api.ts` helpers (`getTenderViewCounts`, `unscored`).
3. Sidebar expandable Tenders sub-nav with count badges + the `:view` route.
4. `RefineBar` + `TendersPage` recompose (view header + refine-over-base).
5. Empty states + visual polish pass (match the approved design sample).
