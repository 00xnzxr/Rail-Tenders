# Tender Scoring Backlog: Awareness + Low-Cost Drain — Design

**Date:** 2026-07-11
**Status:** Approved (brainstorming → spec)
**Scope package:** `drpl-backend` + `drpl-frontend`

---

## 1. Problem

The dashboard "AI Intelligence" panel shows **1 Analyzed / 1310 Pending Analysis / 38% Avg. Relevance**. The
platform does not reliably ensure that new or unanalyzed tenders get **scored**, and nothing surfaces the
backlog to the user or the chat agent. The user wants:

1. **Tender relevance *scoring* only** — `ai_relevance_score` + `segment` via the existing
   `auto_scoring_service`. **NOT** the heavy deep-analysis (requirements/eligibility/risk) pipeline.
2. **Low cost** — scoring must stay a very cheap activity across a large backlog and continuously on new
   tenders.
3. **Awareness** — an admin scoring status page, a dashboard banner with one-click drain, and a chat agent
   that knows the backlog and can trigger a drain.

## 2. Root cause (why 1310 sit "pending")

The scoring path already exists and is well-built (`auto_scoring_service.py`, `auto_scoring_tasks.py`,
`seed_scoring_agent.py`). Two concrete gaps, not a missing system:

- **The reaper isn't running in every environment.** `reap_unscored_tenders` self-reschedules only on the RQ
  worker with Redis. Prod (Railway) has both; local dev runs `uvicorn` alone, so the reaper never fires and
  only *new* uploads get inline scoring — never the standing backlog.
- **The dashboard is wired to the wrong notion of "analyzed."** The frontend "Batch Analyze" button calls
  `analyzeBatch(10)` → the old `ai_service.analyze_batch`, which is keyed on `ai_category` (the *deep* path),
  not `ai_relevance_score` (scoring). So the button, the "Analyzed" counter, and the scoring reaper refer to
  three different things. The panel's `analyzed_tenders`/`avg_relevance` come from deep-analysis stats, not
  scoring.

**Correctness fix baked into this design:** the scoring UI, counters, and drain all read from ONE source of
truth keyed on `ai_relevance_score`, matching the reaper's exact filter.

## 3. Cost model (already cheap; where the savings come from)

Per-score cost is already minimal: Haiku 4.5, ~800-token **prompt-cached** system digest
(`force_cache_system=True`), user prompt capped at ~1500 chars of scope, 2-line output. We do NOT re-architect
the prompt. Savings come from:

- **Batch API (50% off)** for the standing backlog — async submit + poll. New/individual tenders stay on the
  existing live inline/reaper path (fast feedback).
- **Never re-score / dedupe** — the `ai_relevance_score IS NULL` filter already guarantees once-only;
  additionally, exact/near-duplicate tenders (re-posts, corrigenda) that share a normalized `(title+scope)`
  content hash are scored **once** and the result is copied to the rest at **zero extra tokens**. This is the
  biggest real saving on a scraped backlog.
- **Prompt caching stays** — no change; the cached digest already amortizes the system prompt across a batch.

## 4. Architecture — three thin layers over one new service

```
                    ┌──────────────────────────────────────────┐
                    │  scoring_backlog_service.py  (NEW)         │
                    │  • backlog_stats(db)                       │
                    │  • drain_backlog(db, mode)                 │
                    │  • dedupe_by_content_hash(pending) helper  │
                    └───────────────┬──────────────────────────┘
             ┌──────────────────────┼───────────────────────────┐
             ▼                      ▼                             ▼
   Admin scoring status     Dashboard banner +           Chat agent tool
   page /admin/scoring      one-click drain              tender_scoring_status_tool
   (tender_scoring_admin)   (DashboardPage.tsx)          (LangChain tool)
```

### 4.1 `scoring_backlog_service.py` (NEW — backend, single source of truth)

**`backlog_stats(db) -> dict`**
Returns everything the three consumers need, computed with the reaper's *exact* pending filter so counts match
behavior:

- `total` — non-archived tenders.
- `scored` — `ai_relevance_score IS NOT NULL`.
- `pending` — `ai_relevance_score IS NULL AND is_archived == False`.
- `drainable` — pending **and** `scoring_attempts < auto_scoring_max_retries` (what the reaper will actually
  attempt; `pending - drainable` = stuck-at-retry-cap, surfaced separately).
- `in_flight_batches` — count of `MessageBatch` rows with `batch_type="tender_scoring"` and a non-terminal
  status.
- `last_reaper_run` — `PlatformSetting["auto_scoring_last_run"]`.
- `enabled` — `get_scoring_settings(db)["auto_scoring_enabled"]`.
- `est_cost_inr` — rough estimate: `drainable × per_score_tokens × price`, halved when the batch path is
  used. Constants live in the service; clearly labelled an estimate.

**`drain_backlog(db, mode, limit=None) -> dict`**

- `mode="batch"` (default for large backlogs): dedupe pending by content hash → for each **unique** tender
  submit **one** scoring request (system = cached digest, user = `_user_prompt(tender)` reused verbatim from
  `auto_scoring_service`) via a new lean `create_tender_scoring_batch` in `batch_service.py`. Persist a
  `MessageBatch(batch_type="tender_scoring")` + `MessageBatchItem` rows, storing the content-hash→tender_ids
  fan-out map in `metadata_json` so duplicates get the same score on result processing. Returns
  `{mode, submitted, deduped_saved, batch_id}`.
- `mode="live"` (small/urgent): loop `find_unscored_tender_ids` + `score_tenders_batch` synchronously until
  empty or `limit` reached. Returns `{mode, scored, failed}`.
- **Never re-score:** both modes start from the `ai_relevance_score IS NULL` filter; content-hash dedupe
  prevents paying twice within a drain.

**`dedupe_by_content_hash(tenders) -> (unique, fanout_map)`** — normalize `(title + scope[:1500])`
(lowercase, collapse whitespace), `sha256`; return one representative per hash + a `{hash: [tender_ids]}` map.

### 4.2 `batch_service.py` — add `create_tender_scoring_batch` + scoring result handler

- New `create_tender_scoring_batch(db, tender_ids, user_id)` mirrors `create_tender_analysis_batch` but emits
  **one** request per tender (the scoring call), `batch_type="tender_scoring"`, `system` = cached scoring
  digest from `get_scoring_system_prompt(db)`, `model` = `auto_scoring_model`. **Does not** touch the
  4-request classifier/relevance/risk/summary fan-out.
- New branch in `process_batch_results` for `batch_type="tender_scoring"`: parse each reply with
  `parse_score_reply`, write `ai_relevance_score` / `fit_reasoning` / `segment` (reusing
  `auto_scoring_helpers.compute_segment` + the same threshold settings as the live path), and **fan out** the
  score to every `tender_id` sharing the representative's content hash (from `metadata_json`). Increments
  `scoring_attempts` on failure, honoring the retry cap.
- Reuse the existing `poll_and_process_if_ready` / `poll_batch_status` plumbing; the reaper (or a small
  periodic job) polls in-flight scoring batches.

### 4.3 Reaper reliability everywhere (recommended approach)

- **Prod (Railway):** already correct — Redis + worker replicas run the self-rescheduling reaper. No change.
- **Local / no-Redis:** add `POST /admin/tender-scoring/drain` (mode selectable) so a human or the agent can
  drain without a worker. Optionally, a lightweight in-process fallback: on startup, if `auto_scoring_enabled`
  and `get_queue()` is `None`, register a guarded `asyncio` ticker that runs one `drain_backlog(mode="live",
  limit=batch_size)` per interval. Guarded so it is a strict no-op when a real queue/worker exists (prod).
  **No new infra.**

### 4.4 Surfacing — three consumers of `backlog_stats`

- **Admin scoring status page** — extend `tender_scoring_admin.py`:
  - `GET /admin/tender-scoring/backlog` → `backlog_stats(db)`.
  - `POST /admin/tender-scoring/drain` body `{mode: "batch"|"live", limit?}` → `drain_backlog`.
  - Frontend page `/admin/scoring` (master-admin only, via `MasterAdminRoute`): backlog size, scored/pending,
    drainable vs stuck-at-cap, last reaper run, in-flight batches, cost estimate, enable/disable toggle
    (reuses existing `PUT /settings`), and **two drain buttons**: "Drain now (fast, full price)" and "Submit
    batch (cheap, async)".
- **Dashboard banner + one-click drain** — in `DashboardPage.tsx`, replace the mis-wired "Batch Analyze" card:
  read `pending` from `backlog_stats` (via a new `getScoringBacklog()` in `api.ts`); the button calls the
  scoring drain — defaulting to **batch** when `drainable` is large (threshold, e.g. > 50), **live** when
  small — and shows a live shrinking count while a batch is in flight. The panel's "Analyzed"/"Pending"
  numbers switch to the scoring source of truth. Deprecate the `analyzeBatch()` call from this card (leave the
  old deep-analysis endpoint intact for its own uses).
- **Chat agent awareness** — new lean LangChain tool `tender_scoring_status_tool` (registered via
  `tool_loader.py`): returns a compact `backlog_stats` summary and can trigger `drain_backlog`. Lets the agent
  answer "how many tenders are unscored?" and act on request. Tool return is terse (keeps agent tokens low);
  no prompt-dumping of tender lists.

## 5. Data / model touchpoints

- **No new columns.** Reuses `Tender.ai_relevance_score`, `fit_reasoning`, `segment`, `segment_overridden`,
  `scoring_attempts`, `below_threshold`, `is_archived`.
- **`MessageBatch` / `MessageBatchItem`** reused with a new `batch_type="tender_scoring"`; fan-out map stored
  in the existing `metadata_json`. No schema change.
- **`PlatformSetting`** reused for `auto_scoring_*` settings + `auto_scoring_last_run`.

## 6. Error handling

- Batch submit failure (no API key, API error) → surfaced to the caller; drain endpoint returns a clear error;
  dashboard shows it (mirrors existing `analyzeBatch` error display).
- Score parse failure per item → `scoring_attempts += 1`, respect retry cap, never crash the batch.
- Redis/queue absent → live-mode drain still works (synchronous); in-process ticker guarded to no-op when a
  queue exists.
- Content-hash fan-out is best-effort: if the map is missing/mismatched, fall back to scoring each tender
  individually (correctness over savings).

## 7. Testing

Backend (pytest, SQLite in-memory as existing tests do):

- `backlog_stats`: counts match the reaper filter; `drainable` excludes retry-capped and archived; `pending`
  excludes scored.
- `dedupe_by_content_hash`: identical `(title+scope)` collapse to one representative; distinct scope →
  distinct; fan-out map covers all input ids.
- `drain_backlog(mode="live")`: scores all pending, idempotent (re-run scores 0), respects `limit`.
- `create_tender_scoring_batch`: emits exactly one request per unique tender; `batch_type="tender_scoring"`;
  system prompt = cached digest (monkeypatch the API call).
- `process_batch_results` scoring branch: writes score/segment; fans out to duplicate tender_ids;
  failure increments attempts.
- Drain endpoints: master-admin gate; `GET /backlog` shape; `POST /drain` both modes.

Frontend:

- Dashboard card reads `getScoringBacklog()` and its button hits the drain endpoint (mode selection by
  threshold). `/admin/scoring` renders stats and both drain buttons behind `MasterAdminRoute`.

Agent:

- `tender_scoring_status_tool` returns compact stats and triggers a drain when asked (unit test the tool
  function directly).

## 8. Verification (manual, against live Neon in dev — scope carefully)

1. `GET /admin/tender-scoring/backlog` → `pending` ≈ 1310, `scored` ≈ 1.
2. `POST /admin/tender-scoring/drain {"mode":"live","limit":10}` → 10 scored; `pending` drops by ≤10 (less if
   dupes fanned out); re-run scores 0 for those.
3. `POST /drain {"mode":"batch"}` → a `MessageBatch(batch_type="tender_scoring")` appears `in_progress`;
   `deduped_saved` > 0 on a duplicate-heavy backlog; after poll/process, scores land and duplicates share the
   representative's score.
4. Dashboard panel now reads the scoring source of truth; banner count shrinks as batches process.
5. Chat: "how many tenders are unscored?" → agent answers from the tool; "score them" → triggers a drain.

## 9. Sequencing (ship order)

1. `scoring_backlog_service.py` + `backlog_stats` + `drain_backlog(mode="live")` + drain endpoints + unit
   tests. (Immediately drains the backlog anywhere, no Redis.)
2. Dashboard rewire to the scoring source of truth + one-click live drain.
3. `create_tender_scoring_batch` + scoring result branch + content-hash dedupe + `mode="batch"` (the 50%
   saving).
4. `/admin/scoring` page + enable/disable + cost estimate.
5. `tender_scoring_status_tool` for agent awareness.
6. (Optional) in-process fallback ticker for local `uvicorn`-only dev.

## 10. Non-goals

- No deep-analysis (requirements/eligibility/risk) changes.
- No prompt/digest re-architecture.
- No new DB columns or infra.
- No change to the extension.
