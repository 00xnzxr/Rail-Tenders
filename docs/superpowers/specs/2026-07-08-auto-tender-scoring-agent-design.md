# Design: Auto Tender-Scoring Agent

Date: 2026-07-08
Status: Approved (design), pending spec review

## Goal

Make AI relevance scoring of scraped tenders a **mandatory, always-on automation**. When a new tender enters the platform it is scored automatically; a periodic reaper continuously picks up any tender still missing a score. Scoring compares the tender's **title + scope-of-work summary** against a lean, prompt-cached digest of DRPL's **training dataset + admin scope profile**, producing a **0–100% match** (the "MATCH" figure on the tender card). The agent also reads the tender **value** and flags tenders **below ₹50 lakh** so they drop out of the default list. It must be **quick and extremely cheap** — Haiku 4.5 via the synchronous Messages API (no Batches API), with prompt caching on the shared context.

Non-goals: no change to how tenders are scraped/ingested; no new deep-analysis pipeline; the Batches API path is explicitly out of scope (delayed results).

## Approved decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Score input | Tender **title + scope-of-work summary** (`title` + `ai_summary`/`description`) |
| Score basis | Lean, **short-but-proper** system prompt: a **distilled digest** of the training dataset + admin scope profile (~800 tokens), not raw dataset dumps |
| Digest source | **Auto-distilled at seed time**; regenerated when the training dataset / scope profile changes |
| Below-₹50L behavior | **Flag** (`below_threshold=true`), filtered out by default, still viewable — never delete |
| Missing value | **Keep + score normally**; never flag when value is null/unknown |
| Model + cost | **Haiku 4.5** (`claude-haiku-4-5`), synchronous Messages API, **prompt caching** on the shared digest |
| Runtime | **RQ on the worker** in production (on-upload enqueue **and** a periodic reaper), **inline fallback** when `REDIS_URL` is empty (local dev) |
| Batching | Batch per reaper tick, bounded size + bounded concurrency; idempotent; retry cap |

---

## Part A — The scoring logic

### A.1 What "unscored" means

A tender needs scoring when `Tender.ai_relevance_score IS NULL` and it is not already permanently failed (see A.5). This is the single predicate that drives both entry points and the reaper.

### A.2 The scoring call (per tender)

One **Haiku 4.5** call via the normal synchronous Messages API (`app/services/ai_service.py::call_ai` already wraps the Anthropic HTTP path with provider fallback — reuse it; do not hand-roll a new client):

- **System prompt (prompt-cached):** the scoring agent's stored, distilled digest (A.3). Marked with `cache_control: {"type": "ephemeral"}` so the shared block bills full price once per ~5-min window and ~0.1× thereafter. Because the reaper runs every ~2 min the cache stays warm across ticks.
- **User message (varies per tender):** the tender's `title` plus its scope summary (`ai_summary` if present, else a truncated `description`/`full_description`). Keep this block small and after the cached prefix so caching is not invalidated.
- **Output (structured):** use structured output (`output_config.format` json_schema, supported on Haiku 4.5) with fields `match_percent` (integer 0–100) and `reasoning` (short string). Convert `match_percent/100` → `ai_relevance_score` (float 0–1); store `reasoning` → `fit_reasoning`.

### A.3 The distilled digest (short but proper)

The scoring agent's `system_prompt` is a **compact** block (~800 tokens, not the full dataset), structured as:

1. Role + task: "Score how well this railway/electrical tender fits DRPL's business."
2. Output contract: exact schema (`match_percent` 0–100, one-line `reasoning`), and a short scoring rubric (what 90%+, 50–70%, <40% mean).
3. Relevance reference: the scope profile's keyword groups / target ministries / value range rendered compactly, plus a **condensed digest** of the linked training dataset — recurring themes, buyer types, work categories — **not** every file's text.

**Generation:** an idempotent seeder distills the digest once and stores it on the scoring agent's `CustomAgent.system_prompt`. It regenerates when the training dataset or scope profile changes (detected via a stored hash of their inputs; if the hash differs, re-distill). Distillation itself is a single Haiku/Sonnet call over the dataset+scope, run at seed time only — never per-tender. Follows the existing canonical-registry seeding convention (`app/services/seed_*.py`, only re-syncs when `is_user_customized=False`).

### A.4 The value filter (below ₹50 lakh)

After (or alongside) scoring, evaluate `estimated_value`:
- `estimated_value` is a real number **and** `< value_threshold_inr` (default 5_000_000 = ₹50L) → set `below_threshold = True`.
- `estimated_value` is `None`/unknown → **leave `below_threshold` False**; score normally. Never flag on unknown value.
- `estimated_value >= threshold` → `below_threshold` stays False.

This is a pure comparison on data already on the row — no extra LLM call. The tenders list filters `below_threshold == True` out of the default view (it stays viewable via an explicit filter, mirroring the existing `is_archived` filtering).

### A.5 Idempotency, retries, and no double-scoring

- A tender is only picked up while `ai_relevance_score IS NULL`. On success the row is written (`ai_relevance_score`, `fit_reasoning`, `below_threshold`, `updated_at`) and drops out of the next sweep.
- **Retry cap:** add `scoring_attempts` (Int, default 0). Each failed attempt increments it. When `scoring_attempts >= auto_scoring_max_retries` (default 3), the reaper's query excludes the row (`scoring_attempts < max_retries`) so a permanently-failing tender (malformed data, persistent API error) is not retried forever. A one-line WARNING logs the give-up.
- **Batch-claim dedupe:** reuse the existing Redis dedupe pattern (`_claim_dedupe` in `scheduled_tasks.py`) with a key like `drpl:tender:scoring_claim:{id}` and a short TTL, so two worker replicas don't score the same tender concurrently. No Redis → no dedupe but still correct (single-process local path).

---

## Part B — Runtime: where it runs

All scoring goes through one service function; three entry points feed it.

### B.1 The batch-scoring service

New module `app/services/auto_scoring_service.py`:

- `score_tenders_batch(db, tender_ids: list[int]) -> dict` — loads the digest once, scores each tender with **bounded concurrency** (an `asyncio.Semaphore(auto_scoring_max_concurrency)`, default 3, mirroring the existing `_RELEVANCE_SEMAPHORE`), writes results, returns `{"scored": n, "flagged_below_threshold": m, "failed": k}`. Runs inside a `run_id_scope` so logs carry the `[run=…]` stamp.
- `find_unscored_tender_ids(db, limit) -> list[int]` — the reaper query: `ai_relevance_score IS NULL AND scoring_attempts < max_retries AND is_archived == False`, limited to `auto_scoring_batch_size`.

### B.2 Production (Redis present) — RQ worker

Add two things beside the existing scheduled tasks:

1. **On-upload enqueue (trigger).** In `app/api/routes/extension.py::upload_tenders` (lines 53–57), replace the `background_tasks.add_task(score_relevance_background, new_ids)` FastAPI BackgroundTask with an **RQ enqueue** of `app.worker.auto_scoring_tasks.score_new_tenders` (`new_ids`) **when a queue is available** (`get_queue() is not None`); fall back to the inline BackgroundTask only when there is no queue (B.3). This moves scoring off the web process in production.
2. **Periodic reaper.** New `app/worker/auto_scoring_tasks.py` with `reap_unscored_tenders()`, following the **self-rescheduling pattern** of `scan_closing_dates` exactly: do the batch, then `q.enqueue_in(timedelta(seconds=auto_scoring_interval_seconds), "app.worker.auto_scoring_tasks.reap_unscored_tenders", job_id="drpl-auto-scoring-reaper", result_ttl=…)`. Primed at startup by extending `app/services/seed_scheduled_jobs.py` with a `drpl-auto-scoring-reaper` first run in ~1 minute (idempotent via the existing `_job_already_pending`). Guarded by `auto_scoring_enabled`.

`score_new_tenders(tender_ids)` and `reap_unscored_tenders()` both open their own `SessionLocal()` (like the existing tasks) and call `score_tenders_batch`.

### B.3 Local dev (Redis empty) — inline fallback

When `REDIS_URL` is empty, `get_queue()` returns `None`, so:
- `upload_tenders` keeps the existing inline `background_tasks.add_task(...)` path — but pointed at `score_tenders_batch` (via a thin sync wrapper) so dev exercises the same scoring code.
- No reaper runs (no worker) — acceptable for local dev; documented in the seeder log line ("queue unavailable — auto-scoring reaper disabled"), matching the existing pattern.

This means: **production = robust RQ (trigger + reaper), local = inline on-upload**, same scoring logic either way.

---

## Part C — Data, config, observability

### C.1 Schema changes

Two columns on `Tender` (`app/models/tender.py`):
- `below_threshold` (Boolean, default False, not null) — the <₹50L flag.
- `scoring_attempts` (Integer, default 0, not null) — retry-cap counter.

Per the repo's **schema-drift discipline** (CLAUDE.md): add an **Alembic revision** AND entries in `_apply_schema_drift_fixes()` (Postgres `ADD COLUMN IF NOT EXISTS`) and `_add_missing_columns()` (SQLite) in `app/main.py`, so existing deployments self-heal.

A small store for the digest-input hash (to detect dataset/scope changes) — a `PlatformSetting` row (`auto_scoring_digest_hash`) is sufficient; no new table.

### C.2 API / frontend

- `TenderResponse` (list serialization) + the frontend `Tender` type gain `below_threshold`. The tenders list service filters `below_threshold == True` out of the default query (new `include_below_threshold` param, default False, mirroring `include_archived`).
- The tender card already renders `ai_relevance_score` as the "% MATCH" figure — no card change needed beyond it now being reliably populated.

### C.3 Config (`app/core/config.py`, all env-overridable)

- `auto_scoring_enabled: bool = True` — mandatory-automation master switch (`AUTO_SCORING_ENABLED`).
- `auto_scoring_model: str = "claude-haiku-4-5"`.
- `auto_scoring_interval_seconds: int = 120`.
- `auto_scoring_batch_size: int = 25`.
- `auto_scoring_max_concurrency: int = 3`.
- `auto_scoring_max_retries: int = 3`.
- `value_threshold_inr: float = 5_000_000` (₹50 lakh).

### C.4 Observability

- Every batch run logs one structured line: `found / scored / flagged_below_threshold / failed` (inside `run_id_scope`).
- Give-ups (retry cap hit) log a WARNING naming the tender.
- Expose a small counter on the existing `/health/capacity` (or a dedicated `/health` field): unscored-backlog count + last-reaper-run timestamp, so the reaper's liveness and backlog drain are observable.

---

## Component / interface summary

| Unit | Responsibility | Depends on |
|---|---|---|
| `auto_scoring_service.score_tenders_batch` | score a set of tenders (bounded concurrency), write results | `ai_service.call_ai`, digest, `Tender` |
| `auto_scoring_service.find_unscored_tender_ids` | the reaper query | `Tender` |
| `auto_scoring_tasks.reap_unscored_tenders` | self-rescheduling periodic sweep | queue, `score_tenders_batch` |
| `auto_scoring_tasks.score_new_tenders` | on-upload RQ job | `score_tenders_batch` |
| `seed_scoring_agent.*` | distill + store the digest; regen on input change | training dataset, scope profile |
| `seed_scheduled_jobs` (extended) | prime the reaper at startup | queue |
| `extension.upload_tenders` (modified) | enqueue RQ job (prod) / inline (dev) | queue presence |
| `Tender` (+2 cols), `TenderResponse` (+1), frontend `Tender` type | carry `below_threshold` | Alembic + drift fixes |
| config (`auto_scoring_*`, `value_threshold_inr`) | tunables + master switch | — |

## Testing

- **Scoring logic:** mocked LLM → asserts `ai_relevance_score` written from `match_percent/100`; `fit_reasoning` stored; `below_threshold` set **only** when `estimated_value < ₹50L` and value is not null; unknown value → not flagged, still scored.
- **Retry cap:** a tender whose scoring raises increments `scoring_attempts`; once `>= max_retries` it is excluded from `find_unscored_tender_ids`.
- **Fallback path:** with `get_queue()` returning None (no Redis), `upload_tenders` scores inline and the reaper is not seeded (assert the seeder logs disabled and enqueues nothing).
- **No double-scoring:** a tender with a non-null `ai_relevance_score` is not returned by `find_unscored_tender_ids`.
- Backend suite green (`pytest tests/`); note the two known pre-existing auth-test failures are unrelated.
