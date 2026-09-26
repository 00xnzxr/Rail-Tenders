# Tenders — Detailed Card Redesign

**Date:** 2026-07-07
**Status:** Approved (design direction confirmed via interactive mockup)
**Packages touched:** `drpl-frontend`, `drpl-backend`, `drpl-extension`

---

## 1. Context & Goal

The current Tenders page (`drpl-frontend/src/pages/TendersPage.tsx`) renders a terse
data **table** (`TenderTable` → `TenderRow`): TID, title, portal, priority, workflow,
status, value, AI score, closing date. It reads as a spreadsheet, not an analysis surface.

The client wants the section to present each tender as a **detailed card**, in the spirit
of the TenderTiger reference screenshot — match %, TID, category, location, source portal,
bid type, worth/EMD/due-date/days-to-go — so tenders coming in from *different portals*
(GeM, IREPS, and the aggregators) can be scanned and analyzed at a glance.

The design was validated with an interactive mockup rendered in DRPL's own design tokens
(navy/slate "Trust & Authority" palette, blue accent, Plus Jakarta Sans). This spec
translates that mockup into concrete changes across all three packages.

### Goal
Replace the tender **table** with a **detailed, expandable card list** in DRPL's visual
language, and capture the four fields the cards need that the platform does not store today:
**location**, **bid type**, **originating portal**, and **category/sector**.

---

## 2. Non-Goals

- No change to the AI analysis pipeline, workspace, checklist, or proposal features. The
  card only *triggers* the existing `POST /tenders/{id}/analyze` and workspace flows.
- No clone of TenderTiger's styling (no orange "Buy for Points" CTA). DRPL styling only.
- No change to the tender **detail page** (`TenderDetailPage.tsx`) beyond consuming any
  new fields if trivial. The card's inline expansion is the primary "detail at a glance".
- No new AI classification model for category — we capture the **portal-stated** category
  string; `ai_category` remains a separate, AI-derived field.
- No historical backfill of location/bid-type/category for already-scraped tenders beyond
  what a normal re-scrape self-heals (new fields are nullable; old rows show "—").

---

## 3. Success Criteria

1. The Tenders page renders cards matching the approved mockup, in both light and dark mode,
   collapsed by default, expandable inline.
2. A newly scraped TenderTiger/GeM/IREPS tender shows its location, bid type, and (for
   aggregator sources) its originating portal on the card.
3. Default sort is by **match** (`ai_relevance_score` desc); Closing-date and Value sorts work.
4. The existing Advanced-mode multi-select + batch analyze/archive/delete tools still work.
5. Existing deployments self-heal (new columns added via drift-fix + Alembic) without manual DB steps.

---

## 4. Data Model Changes

Four new nullable columns on `Tender` (`drpl-backend/app/models/tender.py`):

| Column | Type | Purpose | Example |
|---|---|---|---|
| `location` | `String(255)` | City/state as scraped (free text). | `"Saran, Bihar, India"` |
| `bid_type` | `String(50)` | Portal's competition type. | `"NCB"`, `"GCB"`, `"Limited"`, `"Single"` |
| `source_portal` | `String(50)` | Originating portal when `portal` is an aggregator; null otherwise. | `portal="tendertiger"`, `source_portal="gem"` |
| `category` | `String(255)` | Portal-stated sector/category (distinct from AI `ai_category`). | `"Railways Transport Services"` |

Design decisions:
- **`location` is free text**, not normalized city/state columns. The portals emit
  inconsistent formats; a single string is what the card shows and what filtering matches
  with `ILIKE`. Normalization is a possible later enhancement, explicitly out of scope.
- **`source_portal` is nullable and only set for aggregators.** For a direct GeM/IREPS
  scrape it stays null (the `portal` column already is the source). The card shows the
  `via <aggregator> · <source_portal>` badge only when `source_portal` is present.
- All four are **nullable** — old rows and portals that don't expose a field render "—".

### Self-healing (schema drift discipline — per CLAUDE.md)
Add all four columns in **three** places so new and existing deployments converge:
1. An Alembic revision (`alembic revision -m "add tender card fields"`).
2. `_apply_schema_drift_fixes()` in `app/main.py` — Postgres `ADD COLUMN IF NOT EXISTS`.
3. `_add_missing_columns()` in `app/main.py` — SQLite path.

---

## 5. Extension Changes (`drpl-extension`)

### 5.1 Shared type — `src/utils/types.ts`
Add to `TenderData` (all optional):
```ts
location?: string;         // "Saran, Bihar, India"
bidType?: string;          // "NCB" | "GCB" | "Limited" | "Single" | ...
sourcePortal?: PortalName | string; // originating portal for aggregator listings
category?: string;         // portal-stated sector/category
```

### 5.2 Scrapers
- **`content-scripts/aggregators.ts`** — TenderTiger: `location` is *already parsed*
  (`locationMatch`) but discarded; wire it into the emitted `TenderData`. Additionally
  extract `bidType` (the "NCB"/"GCB" token next to the portal chip), `category` (the
  sector link text, e.g. "Railways Transport Services"), and `sourcePortal` (the portal
  chip — "GeM"/"IREPS" — shown on each TenderTiger row). Apply the same additions to the
  BidAssist / TenderDetail / generic extractors where the tokens exist; leave undefined otherwise.
- **`content-scripts/gem.ts`** — capture `location` (buyer address / delivery location),
  `bidType` (GeM bid type field), and `category` (GeM category) where the DOM/selectors expose them.
- **`content-scripts/ireps.ts`** — capture `location` and `bid_type` equivalents where available.

Selectors that are OTA-served (`src/config/selectors.json`) get new keys for these fields;
**changing `selectors.json` requires a backend push too** (per CLAUDE.md) so the OTA copy
matches. New content-script logic must remain self-contained after the
`bundle-content-scripts` build step (no leftover `import` in `dist/content-scripts/*.js`).

### 5.3 Build & reload
`npm run build` in `drpl-extension`, reload unpacked in `chrome://extensions`, verify
`dist/content-scripts/*.js` has no `import` statements.

---

## 6. Backend Changes (`drpl-backend`)

1. **`app/schemas/__init__.py`**
   - `TenderInput`: add `location`, `bidType`, `sourcePortal`, `category` (all `Optional`, default None).
   - `TenderResponse` (list endpoint — **this is what the card consumes**): add `location`,
     `bid_type`, `source_portal`, `category`, plus `emd_amount` and `ai_summary` (the card's
     collapsed money row needs EMD; the expanded panel shows the AI summary). Currently
     `TenderResponse` omits these — without adding them the card can't render them.
   - `TenderDetailResponse`: add the four new fields for completeness.

2. **`app/services/tender_service.py`**
   - `ingest_tender_batch()` — map the four new input fields onto the new model columns for
     inserts.
   - `_update_existing_tender()` — populate the new columns when a re-scrape supplies them
     and the stored value is empty (mirror the existing `search_match_keyword` fill-if-empty pattern).
   - `get_tenders()` — add a `relevance` sort option: `ORDER BY ai_relevance_score DESC NULLS LAST`.
     Add optional `bid_type` and `location` filter params (ILIKE for location, exact for bid_type).

3. **`app/api/routes/tenders.py`**
   - `list_tenders()` — accept `bid_type` and `location` query params, pass through; extend
     the `sort_by` docstring to include `relevance`.

4. **`app/models/tender.py`** + **`app/main.py`** drift fixes + Alembic — per §4.

---

## 7. Frontend Changes (`drpl-frontend`)

### 7.1 Types — `src/types/tender.ts`
Add to `Tender`: `location`, `bid_type`, `source_portal`, `category`, `emd_amount`, `ai_summary`
(nullable). Add `bid_type` and `location` to `TenderFilters`.

### 7.2 New components (`src/components/tenders/`)
- **`TenderCard.tsx`** — one card. Left rail: match % (emerald/amber/red scale, reuse the
  existing `AIScoreBadge` thresholds) + rank. Body: TID · location; title; org · department;
  badge row (portal / `via … origin` / bid type / category / status); money row (Worth / EMD /
  Due date). Right: days-to-go pill (reuse `DaysPendingBadge` thresholds) + expander. Expanded:
  scope (`description`), key terms, document list (`document_links`), AI summary box, and the
  two action buttons **Analyze** (calls `triggerTenderAnalysis`) and **Open workspace**
  (navigates to the tender workspace), plus quiet ghost actions (assign / priority / archive).
  Bid-type badge uses a violet hue (a distinct semantic color, *not* the blue accent).
- **`TenderCardList.tsx`** — replaces `TenderTable` as the list container; renders `TenderCard`s,
  owns the Advanced-mode multi-select checkbox affordance (a checkbox overlaid on the left rail),
  and the `EmptyState`.
- Keep `TenderRow.tsx` / `TenderTable.tsx` in the tree until the card list is wired and verified,
  then remove them in the same PR to avoid dead code.

### 7.3 `TendersPage.tsx`
Swap `<TenderTable>` for `<TenderCardList>`, preserving all existing selection / batch-analysis /
bulk archive-delete state and the Advanced toggle (that logic is list-container-agnostic and stays).
Default `filters.sort_by` becomes `relevance`.

### 7.4 `TenderFilters.tsx` → toolbar
Restyle into the single-row toolbar from the mockup: search (title/ID/org), Portal, Bid type,
Location, Sort (Match / Closing date / Value). Keep Advanced toggle to the right. Wire the new
`bid_type` / `location` params through `onApply`.

### 7.5 `useTenders.ts`
Add `filters.bid_type` and `filters.location` to the `useCallback` dependency array so changing
them refetches.

---

## 8. Failure Modes & Edge Cases

- **Field absent on a portal** → column stays null → card shows "—". No error.
- **`ai_relevance_score` null** (unanalyzed) → `relevance` sort places it last (NULLS LAST);
  match badge shows "—".
- **`source_portal` equals `portal`** (bad scrape) → treat as null (no `via` badge) to avoid
  "via GeM · GeM". Guard in `TenderCard`.
- **EMD "Refer Document"** → stored as null `emd_amount`; card shows the literal "Refer Document"
  only if we also keep a raw string — decision: show "—" for null EMD (keep it simple; the
  detail page/PDF has the real value). Documented so it's a conscious choice.
- **Long titles / categories** → wrap (never truncate titles — matches current `TenderRow` rule).
- **Extension build regression** → content script left with `import` → verify step in §5.3 catches it.

---

## 9. Verification

- **Backend:** `pytest tests/` (add a test: ingest a tender with the four new fields → assert
  persisted + present in `GET /tenders/` response). Manually hit `GET /tenders/?sort_by=relevance`.
- **Extension:** build, load unpacked, scrape a live TenderTiger results page, confirm the POST
  body carries `location`/`bidType`/`sourcePortal`/`category`, and no `import` remains in the bundle.
- **Frontend:** `npm run build` (tsc clean), then run the app and confirm cards render, expand,
  sort by match, and Advanced multi-select still batches — per the `verify` skill (drive the flow,
  don't just typecheck).

---

## 10. Rollout Order

1. Backend model + drift-fix + Alembic + schemas + service mapping + sort (self-contained, ships first).
2. Extension scraper fields + types + `selectors.json` (+ backend OTA push).
3. Frontend types + card components + toolbar + page swap.

Each layer is backward-compatible: the frontend renders "—" until the extension starts sending
the new fields, and the extension's new fields are ignored by an un-migrated backend (they're
optional). No lockstep deploy required.
