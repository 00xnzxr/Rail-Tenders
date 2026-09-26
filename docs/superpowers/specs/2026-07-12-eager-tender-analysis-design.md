# Eager Tender Analysis — Design Spec (Piece A)

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan
**Scope:** Piece A of 3 of the "eager auto-extraction pipeline". This piece makes
the backend run the existing deep document analysis **automatically and eagerly**
for in-scope tenders that already have documents, instead of only lazily on user
click. Pieces B (extension gated auto-capture of PDFs) and C (metadata
reconciliation/cleaning) are out of scope here and get their own specs.

---

## 1. Problem

Today the platform's clean, structured tender extraction — the vision-first
analyzer (`tender_analyzer_v2`: Haiku-per-PDF + Sonnet synthesis producing
requirements, dates, amounts, eligibility, critical clauses) — runs **lazily**:
only when a user opens a tender and clicks "Analyse", or when a downstream agent
(costing/proposal/chat) needs it via `_ensure_tender_analysis`. Nothing triggers
it on ingest.

Result: a tender the AI scored as relevant sits with un-analyzed documents until
a human happens to open it. The clean content the user wants "on the first pass"
is never produced automatically.

Meanwhile, eager auto-**scoring** (Haiku, metadata-only) already runs on ingest
(`_dispatch_scoring` → `score_new_tenders`), and there is an existing
self-rescheduling reaper pattern (`reap_unscored_tenders`) that sweeps for
unscored tenders every `auto_scoring_interval_seconds`. This piece mirrors that
proven pattern for analysis.

### What already exists (do not rebuild)
- `run_document_analysis` / `_run_v2_analysis` (`document_analysis_agent.py`) — the
  analyzer. Persists `DocumentExtractionResult`, `CriticalClauseFlag`,
  `TenderAnalysisSummary`. This is the extraction; we only change *when* it fires.
- `analyze_all_documents` (`tender_analysis_service.py`) — the service entry the
  UI route calls; delegates to v2.
- Auto-scoring reaper `reap_unscored_tenders` + `find_unscored_tender_ids`
  (`auto_scoring_tasks.py`, `auto_scoring_service.py`) — the pattern to mirror.
- `GET /health/capacity` internals (`llm_metrics`, RQ queue depth, DB pool) — the
  backpressure signal.
- `tender_analyzer_max_parallel = 1` — **deliberately** sequential;
  `config.py` documents that parallel multi-PDF runs caused silent
  empty-synthesis failures (memory pressure + SQLAlchemy session contention).

---

## 2. Approved decisions

1. **Gate to in-scope tenders only** — never auto-analyze all ~1,300 tenders.
2. **Trigger mechanism = a self-rescheduling sweep** (mirror `reap_unscored_tenders`),
   not a direct hook on doc-upload/score-completion. The sweep is order-independent
   (handles docs-before-score and score-before-docs), idempotent, and naturally
   backpressure-friendly. (A low-latency direct trigger can be added later; YAGNI now.)
3. **Respect the sequential-analysis constraint** — bounded concurrency, backpressure
   off `/health/capacity`, and a daily cap. Degrade gracefully to today's lazy
   behavior under load.
4. **Feature-flagged off by default** until validated.

---

## 3. Design

### 3.1 In-scope definition
A tender is an eager-analysis candidate when ALL hold:
- `ai_relevance_score >= eager_analysis_min_score` (default **0.70** — the
  Pursue tier only), OR it is IREPS eligible-flagged (`is_eligible_indicator`).
- It has at least one `TenderDocument` (documents present to analyze).
- It has NOT already been analyzed (no `DocumentExtractionResult` /
  `TenderAnalysisSummary.requirement_summary` for it) — idempotency.
- It is not archived / not a duplicate.
- No analysis is currently running or queued for it (see 3.4).

### 3.2 The sweep (core mechanism)
A self-rescheduling RQ job `enqueue_eager_analysis_sweep` (mirrors
`reap_unscored_tenders`), running every `eager_analysis_interval_seconds`
(default **180**):
1. **Backpressure check** — read the `/health/capacity` signals and skip this
   tick (log + reschedule) if ANY hold: RQ `queue_depth > eager_analysis_queue_ceiling`
   (default **20**); `llm_429_last_5m > 0` (any recent rate-limiting); DB pool
   `checked_out / (size + overflow) > 0.80`. Analysis is deferred, never dropped.
2. **Daily cap** — if today's eager-analysis count `>= eager_analysis_daily_cap`
   (default **200**), skip enqueuing more today (log). The cap is a runaway-cost
   backstop.
3. **Select candidates** — `find_eager_analysis_candidates(db, min_score, limit)`
   returns up to `eager_analysis_batch_size` (default **5**) candidate tender IDs
   per the 3.1 rules, oldest-first.
4. **Enqueue** — for each candidate, enqueue a per-tender analysis job on a
   dedicated low-priority path with **bounded concurrency** (see 3.3), mark it
   in-flight (3.4), and increment the daily counter.

### 3.3 Per-tender analysis job
`eager_analyze_tender(tender_id)` — reuses `run_document_analysis` (v2) verbatim.
- Runs inside a `run_id_scope` for correlatable logging.
- Bounded concurrency: at most `eager_analysis_max_concurrency` (default **1**,
  honoring the sequential constraint) eager analyses run at once — enforced by a
  concurrency guard (e.g. a Redis/DB lock or a single-worker queue), NOT by
  spawning parallel jobs.
- Idempotency re-check at job start (a candidate may have been analyzed between
  selection and execution) — skip if already analyzed.
- On success/failure, clear the in-flight marker (3.4).

### 3.4 In-flight tracking
Prevent double-enqueue and respect "not currently running" with a dedicated,
unambiguous marker on the `Tender` row: `eager_analysis_status`
(`null` / `queued` / `running` / `done` / `failed`) + `eager_analysis_at`
timestamp (new columns; Alembic + self-heal per the schema-drift discipline).
The sweep only selects tenders whose status is `null`/`failed` (with a backoff
on `failed`). A startup reaper — mirroring `_reap_stuck_executions_on_startup` —
flips orphaned `queued`/`running` markers older than a threshold back to `null`
so a worker killed mid-analysis doesn't wedge a tender forever. Idempotency
(3.1: "already analyzed") is checked against `DocumentExtractionResult`
existence, independent of this marker.

### 3.5 Configuration (all conservative defaults)
| Flag | Default | Meaning |
|---|---|---|
| `eager_analysis_enabled` | `False` | master switch |
| `eager_analysis_min_score` | `0.70` | relevance gate (Pursue tier) |
| `eager_analysis_interval_seconds` | `180` | sweep cadence |
| `eager_analysis_batch_size` | `5` | candidates enqueued per tick |
| `eager_analysis_max_concurrency` | `1` | concurrent eager analyses (sequential) |
| `eager_analysis_daily_cap` | `200` | runaway-cost backstop |
| `eager_analysis_queue_ceiling` | `20` | skip tick when RQ queue depth exceeds this |

---

## 4. Error handling & safety

- **Backpressure defers, never drops** — a skipped tick is re-tried next interval.
- **Daily cap** caps spend; when hit, tenders wait for tomorrow (or a manual
  click still works — the lazy path is untouched).
- **Idempotent** at both selection and job start — no duplicate analyses.
- **Orphan recovery** — stuck in-flight markers reaped on worker startup.
- **Flag off = today's behavior exactly** — the lazy/manual analysis path and all
  existing triggers are unchanged; this only *adds* an eager path.
- **No change to the analyzer itself** — same extraction, same persistence, same
  `tender_analyzer_max_parallel=1`.

---

## 5. Verification (definition of done)

- Seed an in-scope tender (score ≥ 0.70) with a `TenderDocument` and no prior
  analysis → run the sweep → assert a per-tender analysis job is enqueued and,
  when executed, a `DocumentExtractionResult` / `TenderAnalysisSummary` is created.
- Assert candidates are correctly EXCLUDED: score < 0.70 (and not eligible-flagged);
  no documents; already analyzed; archived/duplicate; already in-flight.
- Assert **backpressure**: with capacity signals over threshold, the sweep enqueues
  nothing and reschedules.
- Assert **daily cap**: at the cap, no new enqueues; counter resets next day.
- Assert **idempotency**: running the sweep twice does not double-enqueue or
  re-analyze.
- Assert **flag off**: with `eager_analysis_enabled=False`, the sweep is a no-op
  and the lazy path is unaffected.

---

## 6. Explicitly out of scope (later pieces)

- **Piece B** — extension gated auto-capture: the extension auto-running the
  document hunt (download PDFs) for scope-matched tenders in-session. Until B
  ships, Piece A analyzes whatever documents the *existing* manual document hunt
  has already uploaded — still valuable (no more waiting for a user click).
- **Piece C** — metadata reconciliation: cleaning the DOM-scraped list fields
  (title/value/dates/department) from the doc-extracted truth, and the cheap
  metadata-normalization piggyback on the scoring call.
- Any change to the analyzer's extraction quality, the scoring logic, or the
  extension.
