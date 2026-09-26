# Design: Tender Segmentation, Scoring-Agent Admin, Filters & Pagination

Date: 2026-07-09
Status: Approved (design), pending spec review

## Goal

Build on the just-shipped auto-scoring agent to make its output actionable and manageable:

1. **3-segment categorization** of every scored tender — **To Bid** / **Not Bidable** / **Discarded** — auto-computed from AI score + value, with a persistent manual override.
2. **Weekly auto-discard cleanup** — clearly-irrelevant (Discarded) tenders older than 7 days get reversibly soft-archived to keep the platform clean.
3. **Scoring-agent admin control panel** — a `/admin/tender-scoring` page to view stats, tune thresholds/model/interval, toggle the agent, and view/regenerate the scoring digest. Settings move from static env/config to DB-backed, UI-editable, live-read.
4. **More filters** — a Segment dropdown plus AI-score range, Value & EMD range, closing-date range, and Eligibility filters.
5. **Real pagination** — 100 per page, total count, "Page X of Y", first/prev/numbered/next/last navigation. The list API returns `{items, total}` instead of a bare list.

Non-goals: no change to how scoring itself works (Haiku 4.5, digest, reaper — all as shipped); no new AI pipeline.

## Approved decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Segment logic | **Auto-computed, with a persistent manual override** (override wins, isn't recomputed away) |
| Segment boundaries | **Three score bands + value:** `<40%` → discarded; `40–60%` → not_bidable; `≥60% & value≥₹50L` → to_bid; `≥60% & value<₹50L` → not_bidable. **Value unknown + score≥60% → to_bid** (don't penalize a strong match). |
| Auto-cleanup | **Only Discarded**, older than 7 days, **reversible soft-archive** (`is_archived=True`); skip if overridden or workflow moved past `new`. |
| Segment UI | **Segment dropdown** in the filter bar (All / To Bid / Not Bidable / Discarded), composes with other filters. |
| Agent management | **Full admin control panel** (view stats + tune + toggle + digest regenerate). Settings become DB-backed. |
| Pagination | **`{items, total}` response**, 100/page, Page X of Y, full page nav. |
| New filters | Segment + **AI score range, Value & EMD range, closing-date range, Eligibility** (numeric ranges grouped under Advanced). |

---

## Part A — The 3 segments

### A.1 Data model

New columns on `Tender` (`app/models/tender.py`), via Alembic revision + `_apply_schema_drift_fixes()` (Postgres) + `_add_missing_columns()` (SQLite), per the repo's schema-drift discipline:

- `segment` (String(20), nullable, default None) — `"to_bid"` | `"not_bidable"` | `"discarded"` | NULL (unscored).
- `segment_overridden` (Boolean, default False, not null) — True once a user manually sets the segment; the auto-computer then skips this row.

### A.2 Auto-computation

New pure helper `compute_segment(score: float | None, estimated_value: float | None, *, discard_below: float, bidable_at: float, value_threshold: float) -> str | None` in `app/services/auto_scoring_helpers.py`:

- `score is None` → `None` (unscored — never miscategorized).
- `score < discard_below` → `"discarded"`.
- `discard_below <= score < bidable_at` → `"not_bidable"`.
- `score >= bidable_at`:
  - `estimated_value is None` OR `estimated_value >= value_threshold` → `"to_bid"`.
  - else (`value < threshold`) → `"not_bidable"`.

Scores are 0–1 floats; the thresholds `discard_below`/`bidable_at` are stored as fractions (0.40 / 0.60) so they compare directly to `ai_relevance_score`. The admin UI shows them as percentages.

**Where it runs:**
- In `auto_scoring_service.score_tenders_batch` (`_score_and_apply`), right after `ai_relevance_score`/`below_threshold` are written: set `tender.segment = compute_segment(...)` **unless `tender.segment_overridden`**.
- A batch re-segmentation function `resegment_all(db) -> dict` (used by the admin "Apply & re-segment" action) recomputes `segment` for every scored, non-overridden tender using the current thresholds.

### A.3 Manual override

- New endpoint `POST /tenders/{id}/segment` body `{segment: "to_bid"|"not_bidable"|"discarded"}` — sets `tender.segment` + `tender.segment_overridden = True`. (Auth: any logged-in user, mirroring existing tender update routes.)
- The override sticks: `score_tenders_batch` and `resegment_all` both skip `segment_overridden == True` rows.
- (Optional future: a "reset to auto" that clears the override — not in this scope.)

---

## Part B — Weekly auto-discard cleanup

New self-rescheduling RQ job, mirroring `scan_closing_dates` exactly (`app/worker/scheduled_tasks.py`):

- `scan_discarded_tenders() -> dict` — opens `SessionLocal`, inside `run_id_scope`:
  - Selects `Tender` where `segment == "discarded"` AND `created_at < now - timedelta(days=auto_discard_days)` AND `segment_overridden == False` AND `workflow_status == "new"` AND `is_archived == False`.
  - Sets `is_archived = True` on each; `db.commit()`.
  - Logs `scan_discarded_tenders: archived N discarded tenders older than Nd`.
  - `_reschedule_discard_cleanup()` → `q.enqueue_in(timedelta(hours=auto_discard_interval_hours), "app.worker.scheduled_tasks.scan_discarded_tenders", job_id="drpl-discard-cleanup", result_ttl=86400)`.
- Gated on `auto_discard_enabled`. No-op when `get_queue()` is None.
- Primed at startup in `seed_scheduled_jobs()` (new block after the closing-scan block, idempotent via `_job_already_pending(q, "drpl-discard-cleanup")`, first run +5 min), gated on `auto_discard_enabled`.

Only DISCARDED tenders are archived; Not-Bidable stays visible. Archive is reversible (row stays in DB, visible via `include_archived`). A mis-scored good tender is recoverable.

---

## Part C — Scoring-agent admin control panel

### C.1 Settings become DB-backed

The `auto_scoring_*` values (and the new segmentation thresholds + cleanup knobs) move to the `PlatformSetting` table (category `"auto_scoring"`), read live with a fallback to `config.py` defaults.

- New accessor `app/services/auto_scoring_settings.py::get_scoring_settings(db) -> dict` — returns a dict of all knobs, reading each `PlatformSetting` key with `get_setting_value(db, key, default=<config default>)`. Keys: `auto_scoring_enabled`, `auto_scoring_model`, `auto_scoring_interval_seconds`, `auto_scoring_batch_size`, `auto_scoring_max_retries`, `value_threshold_inr`, `segment_discard_below`, `segment_bidable_at`, `auto_discard_enabled`, `auto_discard_days`, `auto_discard_interval_hours`.
- Writer `set_scoring_setting(db, key, value, updated_by)` — **upserts** (the existing `settings_service.set_setting` raises if the key is absent, so this writer inserts a `PlatformSetting` row when missing, else updates — same pattern the digest seeder uses).
- The reaper (`reap_unscored_tenders`), `score_tenders_batch`, `compute_segment` callers, and the cleanup job all read via `get_scoring_settings(db)` instead of `get_settings().auto_scoring_*`. `config.py` values remain as the defaults.
- New `config.py` defaults: `segment_discard_below: float = 0.40`, `segment_bidable_at: float = 0.60`, `auto_discard_enabled: bool = True`, `auto_discard_days: int = 7`, `auto_discard_interval_hours: int = 24`.

### C.2 Backend routes (`app/api/routes/tender_scoring_admin.py`, MasterAdmin-only)

- `GET /admin/tender-scoring/settings` → current settings dict (from `get_scoring_settings`).
- `PUT /admin/tender-scoring/settings` body = partial settings → validate + upsert each; returns the updated dict. Optional `?resegment=true` triggers `resegment_all` after saving threshold changes.
- `GET /admin/tender-scoring/stats` → `{total_scored, unscored_backlog, last_reaper_run, counts: {to_bid, not_bidable, discarded, unscored}}`. Counts via grouped `COUNT`. `last_reaper_run` from a `PlatformSetting` the reaper stamps each run (`auto_scoring_last_run`).
- `GET /admin/tender-scoring/digest` → the current `auto_scoring_digest` PlatformSetting value (read-only).
- `POST /admin/tender-scoring/digest/regenerate` → clears `auto_scoring_digest_hash` so `get_scoring_system_prompt` rebuilds on next call (or calls it directly to rebuild now); returns the new digest.
- Mount the router in `main.py`.

### C.3 Frontend page (`/admin/tender-scoring`, MasterAdminRoute)

- New `AdminTenderScoringPage.tsx` under `src/pages/admin/`, nav entry in `SidebarNav.tsx` masterAdminNav ("Tender Scoring"), route in `router.tsx`.
- Sections: **Controls** (toggle, model, interval, batch, retries, `discard_below %`, `bidable_at %`, value threshold, cleanup toggle + days) with a Save button (and "Apply & re-segment" for threshold changes); **Stats** (scored / backlog / last run / per-segment counts); **Digest** (view + Regenerate button).
- API client functions in `src/lib/api.ts`: `getScoringSettings`, `updateScoringSettings`, `getScoringStats`, `getScoringDigest`, `regenerateScoringDigest`.

---

## Part D — Filters & pagination

### D.1 New filters

Extend `get_tenders` (`tender_service.py`), the `GET /tenders/` route, `TenderFilters.tsx`, and the frontend `TenderFilters` type:

- **Segment** — `segment` param (`to_bid`/`not_bidable`/`discarded`); `query.filter(Tender.segment == segment)`. Dropdown in the main filter row.
- **AI score range** — `score_min`/`score_max` (0–1 floats); `filter(Tender.ai_relevance_score >= score_min)` / `<= score_max`. UI: presets (≥90/70–90/40–70/<40) mapping to min/max, under Advanced.
- **Value range** — `value_min`/`value_max` (rupees); filters on `estimated_value`. UI band presets (<50L / 50L–1Cr / ≥1Cr), under Advanced.
- **EMD range** — `emd_min`/`emd_max`; filters on `emd_amount`, under Advanced.
- **Closing-date range** — `closing_after`/`closing_before` (ISO dates); filters on `closing_date`. UI presets (This week / ≤30 days / Custom), under Advanced.
- **Eligibility** — `eligibility_status` param (`eligible`/`not_eligible`/`unknown`); `filter(Tender.eligibility_status == ...)`. Dropdown in the main row.

All compose as plain AND-ed query params with the existing filters. Numeric-range inputs live under the existing **Advanced** toggle to keep the default bar clean.

### D.2 Pagination — `{items, total}` response

- **New response schema** `TenderListResponse { items: list[TenderResponse]; total: int }`. `GET /tenders/` returns this instead of `list[TenderResponse]`.
- **`count_tenders(db, **filters) -> int`** in `tender_service.py` — applies the SAME filters as `get_tenders` (extract the shared filter-building into a helper `_apply_tender_filters(query, **filters)` used by both `get_tenders` and `count_tenders`, so they can't drift).
- `list_tenders` route returns `{items: get_tenders(...), total: count_tenders(...)}`.
- **Frontend:** `PAGE_SIZE = 100` in `TendersPage.tsx`; `useTenders` reads `{items, total}` (returns `{tenders, total, loading, refetch}`); every `getTenders` consumer updated (the hook is the single chokepoint).
- **`Pagination.tsx`** gains a `total: number` prop; computes `totalPages = ceil(total/limit)`, renders "Showing X–Y of Z", "Page A of B", and First / Prev / numbered (windowed) / Next / Last controls; disables appropriately at bounds.

---

## Component / interface summary

| Unit | Responsibility | Depends on |
|---|---|---|
| `Tender.segment` + `segment_overridden` | store the 3-way segment + override | Alembic + drift fixes |
| `compute_segment(...)` | pure score+value → segment | — |
| `resegment_all(db)` | batch recompute for non-overridden | `compute_segment`, settings |
| `score_tenders_batch` (extended) | set segment after scoring | `compute_segment`, settings |
| `POST /tenders/{id}/segment` | manual override | `Tender` |
| `scan_discarded_tenders` | weekly reversible archive of stale Discarded | queue, settings |
| `auto_scoring_settings.get_scoring_settings` / `set_scoring_setting` | DB-backed live settings w/ config fallback + upsert | `PlatformSetting`, `settings_service` |
| `tender_scoring_admin` routes | settings/stats/digest CRUD | settings, `resegment_all`, digest seeder |
| `AdminTenderScoringPage` | admin control panel UI | admin API client |
| `_apply_tender_filters` + `count_tenders` | shared filters + total count | `Tender` |
| `TenderListResponse {items,total}` | paginated list response | schemas |
| `Pagination` (extended) | 100/page, page X of Y, full nav | `total` prop |
| `TenderFilters` (extended) | segment + 4 new filters | route params |

## Testing

- **Segment logic:** unit-test `compute_segment` across all bands incl. null score (→None), null value + high score (→to_bid), value<50L + high score (→not_bidable), boundary values (exactly 0.40, 0.60).
- **Override:** `score_tenders_batch`/`resegment_all` skip `segment_overridden` rows; `POST /tenders/{id}/segment` sets both fields.
- **Cleanup:** `scan_discarded_tenders` archives only `discarded` + >7d + not-overridden + workflow `new`; leaves not_bidable, overridden, recent, and in-progress rows untouched.
- **Settings:** `get_scoring_settings` returns config defaults when unset, DB values when set; `set_scoring_setting` upserts (inserts when key absent).
- **Filters + count:** `count_tenders` matches `len(get_tenders(...))` under the same filters (incl. segment, score range, value/emd range, date range, eligibility); pagination math (`total`, `totalPages`).
- Backend suite green (`pytest tests/`); frontend `npm run build`; the 2 known pre-existing auth failures remain unrelated.
