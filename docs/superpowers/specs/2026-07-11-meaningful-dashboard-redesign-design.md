# Meaningful Dashboard Redesign — Design

**Date:** 2026-07-11
**Status:** Approved (brainstorming → spec)
**Scope package:** `drpl-frontend` (primary) + `drpl-backend` (additive stats/endpoint)

---

## 1. Problem

The current dashboard (`DashboardPage.tsx`) is dense with low-signal, technical panels: raw
per-portal counts, "Portal Health" scrape internals (`Last Scrape / 24h Tenders / Success Rate /
Selectors v1.0.0`), a redundant "Tenders by Portal" row duplicating the top stat cards, and the
scoring backlog panel. None of it answers the questions a **non-technical** user actually asks:

1. **What matters?** — how many *meaningful* tenders are on the platform.
2. **What's the money picture?** — what the latest costings are.
3. **What do I do next?** — concrete next actions on tenders.

The user also wants **TenderTiger and BidAssist removed** from the dashboard's portal displays (the
"videos" in their words = the Portal Health cards / portal tiles that show those two portals).

## 2. Decisions (locked via brainstorming)

- **Hero "meaningful" number = high relevance (AI relevance score ≥ 0.70).** Not the segment, not
  open-only — a simple "good match" count is the headline.
- **Include all four "what next" aids:** action queue, latest-costings panel, plain-language
  guidance sentence, and closing-soon urgency.
- **Remove:** Portal Health cards (where TenderTiger + BidAssist appear), the "Tenders by Portal"
  row, and the raw IREPS/GeM/AI-Analyzed cards in the top stat row.
- **Keep:** the Recent Tenders table (bottom, not hero).
- **Scoring panel:** keep, but **compact** — folded into a slim strip; the "Score Pending" action
  stays reachable.
- **Action-queue priority (top → down):** promising + closing-soon first → promising-without-costing
  → unscored-open-to-review.
- **Visual scope:** fuller visual redesign (use the `frontend-design` skill during implementation),
  built on the app's existing design tokens (card/badge/color components) for consistency.

Non-technical framing principle: replace jargon with plain human sentences; remove scrape plumbing
(still available under the Scrape Monitor page).

## 3. Layout (top → bottom)

**A. Plain-language hero summary** — one computed sentence, e.g.:
> "You have **47 promising tenders** (≥70% match). **12** are still open, **5** close this week,
> and **8** already have costings."
Degrades gracefully when scoring hasn't run (e.g. "Scoring is still in progress — 1,311 tenders
waiting.").

**B. Meaningful stat cards** (replaces the raw top row) — four cards:
- **Promising** — relevance ≥ 0.70 (hero).
- **To Bid** — `segment = "to_bid"`.
- **Closing This Week** — open AND relevance ≥ 0.70 AND `closing_date` within 7 days.
- **Total Tenders** — retained for scale/context.

**C. "Do This Next" action queue** — a prioritized list; each row one-click into the command center
(`/tenders/:id/command-center`). Priority order:
1. Promising (≥0.70) + open + closing within 7 days → "Review — closes in N days".
2. Promising + open + **no costing yet** → "Start costing".
3. Fallback when scoring is behind: top unscored open tenders → "Review".
Rendered from existing `GET /tenders` calls with filters (no new tender endpoint).

**D. Latest Costings panel** — the most recent cost breakdowns: tender title, grand total, margin %,
and a draft/finalized badge; each row links into that tender's costing. Answers "what latest costing
is for."

**E. Compact scoring strip** — the AI Intelligence panel folded to a slim one-line strip:
`Avg relevance · Scored · Pending` + the "Score Pending" button (batch when drainable > 50, else
live — unchanged behaviour from the scoring-backlog feature).

**F. Recent Tenders table** — kept as-is.

**Removed entirely from the dashboard:** Portal Health cards, "Tenders by Portal" row, raw
IREPS/GeM/AI-Analyzed top cards. The `usePortalHealth` hook remains for the Scrape Monitor page;
only the dashboard stops consuming it.

## 4. Backend changes (additive, no schema change)

### 4.1 Extend `GET /api/tenders/stats` (`tenders.py:tender_stats`)
Add these fields (all simple aggregate queries on existing columns; keep the current fields for
backward compatibility):
- `promising_count` — `ai_relevance_score >= 0.70`.
- `to_bid_count` — `segment == "to_bid"`.
- `closing_soon_count` — `status == "open"` AND `ai_relevance_score >= 0.70` AND `closing_date`
  between now and now+7d.
- `promising_open_count` — `status == "open"` AND `ai_relevance_score >= 0.70`.
- `with_costing_count` — distinct `tender_id` count in `cost_breakdowns`.
The 0.70 threshold is a module-level constant (`DASHBOARD_PROMISING_THRESHOLD = 0.70`) so it is
adjustable in one place.

### 4.2 New endpoint — recent costings
Add `GET /api/cost-breakdowns/recent?limit=5` in `cost_breakdown.py`: newest `CostBreakdown` rows
(order by `updated_at` desc), each joined to its tender's `title`. Returns
`[{tender_id, title, grand_total, margin_pct, status, updated_at}]`. Lean; auth via the existing
route dependency pattern in that router.

### 4.3 Action queue / closing-soon lists
No new tender endpoints — reuse `GET /api/tenders` with `segment`, `closing_before`, `score_min`,
`status`, and relevance/closing sort params that already exist (`tender_service.py`).

## 5. Frontend changes

- **Rewrite `DashboardPage.tsx`** as a thin composition of focused, independently-understandable
  sub-components (each its own file under `src/components/dashboard/`):
  - `HeroSummary` — the plain-language sentence (props: stats).
  - `MeaningfulStats` — the four meaningful cards.
  - `ActionQueue` — prioritized next-action list (fetches filtered tender lists).
  - `LatestCostings` — recent cost breakdowns (fetches `getRecentCostings`).
  - `ScoringStrip` — compact scoring backlog + drain (reuses `getScoringBacklog`/`drainScoring`).
  - Keep the existing recent-tenders table (extract to `RecentTendersTable` for clarity).
- **`api.ts`:** extend the `TenderStats` type with the new fields; add `getRecentCostings(limit?)`
  and a `RecentCosting` interface.
- **Remove** the Portal Health + "Tenders by Portal" JSX and the `usePortalHealth`/`useStats` raw
  usages that are no longer needed from the dashboard. Do not delete the hook (Scrape Monitor uses it).
- **Visual:** apply the `frontend-design` skill for hierarchy/typography/polish, staying within the
  existing Tailwind design tokens (`bg-card`, `border-border`, `text-accent`, etc.) and dark-mode
  support already used across the app.

## 6. Data flow

`DashboardPage` fetches in parallel: `getTenderStats()` (extended), `getRecentCostings()`, the
action-queue tender lists (filtered `getTenders`), and `getScoringBacklog()`. Each sub-component is
fed its slice as props (stats) or fetches its own list (queue, costings) — so a slow/failed costings
call never blocks the stat cards. Every fetch is best-effort with a graceful empty state.

## 7. Error handling

- Any panel's fetch failing renders that panel's empty/placeholder state; the rest of the dashboard
  still shows. (Matches the existing best-effort pattern for backlog on the dashboard.)
- Zero-data states are first-class: "No costings yet", "Nothing closing this week", "Scoring in
  progress" — never blank boxes or crashes.
- `avg_relevance == null` (no scored tenders) → hero sentence switches to the scoring-in-progress
  variant.

## 8. Testing

**Backend (pytest, in-memory SQLite):**
- `tender_stats` new fields: promising threshold (0.69 excluded / 0.70 included), `to_bid_count`
  matches segment, `closing_soon_count` respects open+score+7-day window, `with_costing_count`
  counts distinct tenders.
- `GET /cost-breakdowns/recent`: ordering by `updated_at` desc, `limit` honored, tender title joined,
  auth-gated.

**Frontend:**
- `npm run build` clean (tsc + vite).
- Each new sub-component renders under loading / empty / populated states without crashing.

## 9. Non-goals

- No change to the scoring pipeline, segments, or costing logic — only *surfacing* them.
- No removal of TenderTiger/BidAssist from ingestion or the Scrape Monitor — only from the dashboard.
- No new DB columns or migrations.
- No change to the Recent Tenders table's data source.

## 10. Sequencing (ship order)

1. Backend: extend `tender_stats` + add `/cost-breakdowns/recent` + unit tests.
2. `api.ts`: types + `getRecentCostings`.
3. Frontend sub-components: `MeaningfulStats`, `HeroSummary` (need only stats) — immediate visible win.
4. `ActionQueue` + `LatestCostings`.
5. `ScoringStrip` (fold existing panel) + remove Portal Health / Tenders-by-Portal.
6. Visual-design polish pass (frontend-design skill) + final build.
