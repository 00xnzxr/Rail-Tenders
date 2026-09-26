# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

Three independently-buildable packages under one repo, deployed separately:

- `drpl-backend/` — FastAPI + SQLAlchemy + RQ. Two process types share the same image: a `web` (uvicorn) and a `worker` (`worker.py`). See `Procfile`, `railway.json` (web), `railway.worker.json` (worker, `numReplicas=4`).
- `drpl-frontend/` — React 18 + Vite SPA. Deployed to Vercel (`vercel.json` rewrites all routes to `index.html`).
- `drpl-extension/` — Chrome Manifest V3 extension (TypeScript + React popup). Built with Vite; the build emits a `dist/` that is loaded unpacked into Chrome.

The user logs into IREPS / GeM manually with their DSC token; the extension scrapes the DOM and POSTs to the backend, which stores, deduplicates, and runs an AI pipeline.

## Common commands

### Backend (`drpl-backend/`)

```bash
# First-time local setup — writes .env from .env.example, creates SQLite DB, seeds admin, prints a test JWT
python run_local.py

# Dev server (auto-reload)
uvicorn app.main:app --reload --port 8000

# RQ worker (needs REDIS_URL set; otherwise prints a one-liner and exits)
python worker.py                       # WORKER_CONCURRENCY=3 by default — forks 3 subprocess workers

# Tests
pytest tests/                          # all
pytest tests/test_api.py::test_health  # single test

# Alembic (Postgres prod path)
alembic upgrade head
alembic revision --autogenerate -m "description"
```

The default `database_url` is SQLite (`sqlite:///./drpl_local.db`) so no Postgres is needed for local dev. `.env` overrides this; production uses Postgres on Railway.

### Frontend (`drpl-frontend/`)

```bash
npm run dev       # vite dev server on :5173
npm run build     # tsc -b && vite build
```

API base URL comes from `VITE_API_URL` (defaults to `http://localhost:8000`). JWT is stored in `localStorage` as `drpl_token`; `src/lib/api.ts` attaches it to every request and redirects to `/login` on 401.

### Extension (`drpl-extension/`)

```bash
npm run dev       # vite build --watch (rebuilds dist/ on every change)
npm run build     # one-shot production build
npm run package   # build + zip dist/ for Chrome Web Store
```

Load `drpl-extension/dist/` as an unpacked extension in Chrome. After each `npm run dev` rebuild, click the reload button on the extension card in `chrome://extensions`.

## Architecture notes that are not obvious from the file tree

### Backend startup runs a pile of in-process migrations

`app/main.py` is doing a lot more than mounting routers. On import it:

1. Calls `Base.metadata.create_all(bind=engine)` — fine for new tables, never alters existing ones.
2. Runs `_apply_schema_drift_fixes()` (Postgres `ALTER TABLE … ADD COLUMN IF NOT EXISTS`) for columns that pre-date proper Alembic revisions.
3. Runs `_add_missing_columns()` — same idea but for SQLite (and a couple of `ALTER COLUMN TYPE` fixes for Postgres, e.g. `pipeline_state` TEXT → JSONB).
4. Seeds: system agent tools, workspace document agents, system agents (`costing_researcher`, `tender_doc_analyzer`), format templates.
5. One-time content migrations: `_migrate_workspace_content()` (markdown → HTML in stored documents) and `_backfill_session_titles_from_tenders()`.
6. `_reap_stuck_executions_on_startup()` — flips orphaned `AgentExecution` rows from `running` → `cancelled` (RQ jobs killed mid-flight by SIGKILL / deploy can't mark themselves terminal in their own `finally`).

When adding new columns: prefer an Alembic revision **and** an entry in `_apply_schema_drift_fixes()` / `_add_missing_columns()` so existing deployments self-heal. When adding a new system agent / tool, write an idempotent seeder under `app/services/seed_*.py` and call it from `main.py`.

**Idempotent means "a no-op against its own output", not "safe to re-run".** This whole block runs on every web boot, and Railway restarts the process on every deploy, crash and failed health check — so a seeder that writes unconditionally does not run once, it runs forever. `seed_tender_analyzer` rewrote `system_prompt` and `tools` and incremented `current_version` every boot, and the list it wrote was the canonical default, which is exactly the fingerprint `seed_agent_tools` clears (it reads such a list as the old backfill's cap and frees the agent to the shared repo). The two spent every boot undoing each other: six tools written and a version burned, then the tools cleared again. `current_version` stopped meaning "the prompt changed" and became a count of process starts. Runtime was unaffected — `resolve_tool_keys` answers from the canonical registry when the column is empty, and the generalist's belt is a code constant — but the churn was real and the audit trail was noise. Seeders now compare before they write, and tool assignment belongs to `seed_agent_tools` and Agent Builder, not to the prompt seeders. `tests/test_startup_idempotence.py` pins it.

### AI pipeline: LangChain graphs + RQ workers

The AI work lives under `app/services/langchain/`:

- `graphs/` — LangGraph state machines per pipeline (`tender_pipeline_graph`, `costing_agent`, `enhanced_costing_agent`, `document_analysis_agent`, `proposal_agent`, `agent_router_graph`, `decision_maker_agent`). `chat_agent_wrappers.py` exposes them as chat-callable agents.
- `tools/` — LangChain tools the agents can invoke (`document_reader_tool`, `cost_calculator_tool`, `xlsx_generator_tool`, `web_search_tool`, `anonymizing_web_search_tool`, `costing_training_retrieval_tool`, etc.). New tools should be registered via `tool_loader.py`.
- `llm_factory.py` + `model_factory.py` + `failover_model.py` + `provider_config.py` — multi-provider LLM dispatch. Default provider is Anthropic; `failover_providers` env var (default `openai,google`) defines the fallback chain. Don't hard-code provider names in graphs — go through the factory.
- `canonical_registry.py` — the single source of truth mapping logical agent keys (`costing_researcher`, `tender_doc_analyzer`, …) to their underlying graph + system-prompt + model config. `seed_costing_researcher_agent.py` / `seed_tender_analyzer_agent.py` write CustomAgent rows for these so admins can override prompts via the Agent Builder UI without losing the canonical defaults (the seeders only re-sync `system_prompt` + `tools` when `is_user_customized=False`).

**One self-hosted model, on no production path.** `provider_config` knows a
fourth provider, `runpod`: a model served by vLLM on a RunPod serverless
endpoint, reached through `ChatOpenAI` pointed at
`https://api.runpod.ai/v2/{runpod_chat_endpoint_id}/openai/v1` (`get_base_url`).
Measured 2026-09-22 against the live endpoint: `qwen2.5-7b`, 6.5 s warm, 47 in /
34 out tokens on a probe. Four things are deliberate, and each one is a way this
could have gone wrong quietly rather than loudly. The model id is what the
ENDPOINT serves (`qwen2.5-7b`) -- the RunPod dashboard *displays*
`Qwen/Qwen2.5-7B-Instruct` and the endpoint returns HTTP 500 for it, which looks
exactly like a broken endpoint. `runpod_chat_api_key` is separate from
`runpod_api_key` because **RunPod scopes keys per endpoint on this account**:
measured in both directions on 2026-09-22, the OCR key gets 403 on the chat
endpoint and the chat key gets 403 on the OCR endpoint, while each returns 200
on its own. The fallback to the OCR key is kept for a single-key account but on
this one it lands on a 403, so the error path names the *credential* for a
401/403 and the *model name* for a 5xx -- blaming the model for a 403 sends
someone to the wrong setting. `runpod_chat_endpoint_id` ships as a default
(`9d05slartfxnql`): an endpoint id is an identifier, not a credential, it does
nothing without the key, `RUNPOD_OCR_ENDPOINT_ID` is already committed in
`.env.example` the same way, and it leaves production exactly ONE thing to
configure. Cold start measured 152 s against the 240 s
`runpod_chat_timeout_s`, so that ceiling is a cold start and not a latency
budget. Failover is
severed in **both** directions (`NO_FAILOVER_PROVIDERS`): falling forward off it
would spend money the operator thinks they are saving, falling back onto it
would answer a Claude-tier request with a 7B model, and `force_provider_override`
cannot name it. And `PRICING["qwen2.5-7b"]` is `0.0/0.0` -- not a placeholder,
but the fact that RunPod bills GPU-seconds; without the entry every call would be
reported to the admin dashboard at `DEFAULT_PRICING`'s $3/$15. A missing base URL
raises rather than defaulting to api.openai.com.

The only agent on it is `local_model_probe` ("Local Model Probe (RunPod Qwen)"),
seeded by `seed_system_agents` so it appears in Agent Builder like every other
agent, runs from the same **Run** button, and logs to `api_usage_logs` under
provider `runpod` so the AI-usage dashboard counts it. It has no tools, is absent
from the canonical registry (so nothing can delegate to it), and is not in
`PREFERRED_AGENT_MODELS` -- and `TIER_MODELS["runpod"]` exists precisely so the
startup refresh does not rewrite its row to `claude-haiku-4-5` and silently stop
the endpoint being called. Qwen2.5-7B-Instruct is **text-only**, which is the
standing reason this is not a stepping stone to the annexure or analyzer paths
whatever its cost looks like: those read pages, and this model cannot see one.
`tests/test_local_model_provider.py`.

There is also a parallel `app/services/openai_agents/` package implementing the OpenAI Assistants/Responses API path (`execution_service`, `tool_bridge`, `guardrails`, `human_in_the_loop`) — used when an agent is configured to run on OpenAI rather than the LangChain stack.

Long-running agent runs are dispatched to RQ. `runs.router` (`app/api/routes/runs.py`) creates an `AgentRun` row + enqueues a job; `app/worker/run_tasks.py` is the RQ job entrypoint. Clients subscribe via SSE for streaming output.

**Stopping a run stops the run.** `POST /api/runs/{run_id}/cancel` sets a Redis flag that `app/worker/run_tasks._pump` reads between the SSE chunks it fans out; on cancel it closes the generator explicitly, so the pipeline's own `finally` blocks run rather than waiting on garbage collection. Cooperative, not a kill: a work horse killed mid-statement leaves this pipeline's artifact, message and usage writes half-done. A *queued* run is cancelled outright (RQ job cancelled, row closed, stream ended, concurrency slot released) and reports `cancelled`; a *running* one reports `cancelling` and stays `running` until the worker writes the terminal status itself — calling that "cancelled" would be the same lie as the Stop button it replaces, which aborted the browser's fetch while the worker ran on billing the user's monthly cap. The worker re-checks the flag *and* the row's status before marking a job running, which is what guarantees a cancelled run never spends a model call: RQ cannot pull back a job already handed to a worker, and the flag is left in place (TTL) rather than cleared so a worker that dequeued a moment earlier still sees it — clearing it raced that check. The remaining limitation is granularity — a worker inside one long model call emits nothing, so the stop lands when that call returns. `tests/test_run_cancellation.py`.

**A run that stops early leaves something readable.** Streamed text lived only in the Redis event stream (one hour), and the final answer was saved only when a run finished, so a run killed at token 2,900 of 3,000 left a `cancelled` row and nothing to read. `run_tasks._pump` now rebuilds the answer-so-far from the events it fans out (`run_service.PartialProgress` — the same rules the browser uses, including `token_reset`) and writes it to `agent_runs.partial_output` / `partial_trace` every five seconds and on exit, each write on its own short session so the run's held session never carries an open transaction. A run ending `cancelled` or `failed` has that written into the session history by the worker; a worker killed outright has it written by the startup reaper (`save_partial_turn`). Idempotent — nothing is written if an assistant turn already exists since the run started. Two new nullable columns, added under Rule 3: an Alembic revision (`20260909_run_partial_output`, force-added because `alembic/versions/*.py` is gitignored) **and** entries in both self-heal lists; a database that predates them heals on first boot, and `tests/test_partial_output.py` pins both lists.

**A tool that only reads does so on a session of its own.** Under the concurrent batches, the worker log filled with `Costing training retrieval failed: cursor already closed` and `This session is provisioning a new connection; concurrent operations are not permitted`. Not the batches colliding — each builds its own agent on its own session — but the ReAct agent issuing its tool calls in parallel: `costing_training_retrieval` and `anonymizing_web_search` start in the same millisecond, every tool of a batch shares the one injected session, and `db_recycle` ends that transaction after whichever call finishes first. The retrieval returned an error string, the model moved on, and the batch priced without its rate-card evidence; nothing raised. `CostingTrainingRetrievalTool._run` now opens its own `SessionLocal()` per call and closes it — the injected `db` is still required (it is the "database available" gate `test_tool_db_injection` pins) but no longer queried. `tests/test_costing_retrieval_own_session.py`. The same pattern is the right one for any other read-only tool that shows the same two errors under parallel tool calls.

**A material list is transcribed, never calculated, and a printed total is the check.** Tender 5156's workbook (the Liluah conversion, eighth report) priced a sliding door at ten times its printed total and a hollow tube at forty times its weight: the extractor had been asked to multiply Qty x Weight/Item itself and read `1,02,783.33` as 1027833.3, and the quantity reconciler then "fixed" the quantity to match the misread money. The chunk prompt now asks for the literal cells (`printed_quantity`, `weight_per_item`, `quantity_percent`, `subassembly`); `_ungrounded_annexure_numbers` rejects any transcribed number that does not occur in the page text, allows one bounded repair call on the same source, and blanks what still does not match (`_numeric_source_issue`, confidence low) rather than inventing a basis; `_normalize_annexure_operands` does the weight and percentage arithmetic in Python and prices a total-only row as one lot. Around it: a printed `Labour Cost` footer is a row, recovered from the page text if the model dropped it as a subtotal, and dropped again only when the same charge is already a schedule item (`_drop_separately_scheduled_labour`); equal parts under different sub-assembly headings survive both dedup passes (`subassembly` is part of the key); `merge_batch_rates` refuses a word code that names several rows (`CONVERSION MAT`) unless the serial picks one, and recomputes every money band from the skeleton quantity instead of trusting the model's arithmetic; `normalize_copied_rates` no longer lowers an evidenced rate that happens to equal the published one; the workbook keeps the tender's reference columns on unpriced rows, orders components under their parent, and the Summary sheet is taken after the final roll-up. The costing prompts now say evidence may put a cost above the published rate, not that it must sit below it. `tests/test_costing_export_regressions.py`.

**Every annexure row answers to its printed total, and the check is enforced, not reported.** Tender 5157 (the same Liluah PDFs, ninth report) costed Schedule A at Rs 2,300 crore against Rs 2 crore, and the summary said only that three rows "disagree with their captured printed totals". Transcribing literal cells had not been enough: the extractor still put a cell in the wrong field. A Rs 4,360.13 price merged across five sliding-door parts landed in one part's Weight/Item (279,048 kg of screws); a "15304 mm (80%)" drawing length landed in a plate's Qty (4.4 million kg); rows that print no weight had the Rate/Item in the weight cell and the total in the rate cell; a wrapped "Rs 1,213.04" became a louvre frame's weight; and a Top Angle came back with its Rs 29.29 dropped and its total as the kg rate. Grounding cannot catch any of these -- every number is on the page. `_normalize_annexure_operands` now holds each row to Qty x Weight x % x Rate = Total. A weight cell that makes the count's total is the rate (`_rounding_slack`, half a paisa per item). One price printed against a count other than one is a merged assembly lot, and `_fold_merged_assembly_parts` folds that table's unpriced parts into it by serial run. Any other quantity that cannot make the printed total is total / rate, unless the two sit a clean power of ten apart (`_power_of_ten_apart`, a shifted digit whose side is unknown -- the 5156 regressions still stand). A printed amount with nothing to count it by is a lot, whichever money cell it was read into. A row the list totals at Rs 0.00 is costed at nil (`_printed_nil_total`). Two page-text checks need the source and run in `_call_boq_chunk`: `_money_marked_numbers` reads which numbers are printed as rupees, including one wrapped under a dangling Rs sign, and `_restore_dropped_weight_rates` restores a dropped kg rate only when the division lands on a rupee amount on that page to the paisa. Two defects outside the arithmetic: Annexure-II's "Labour Cost (B)" footer sits above the ANNEXURE-III heading on one page and was labelled III -- Rs 3.34 lakh in a Rs 65,000 item, with III's own Rs 36,386.76 dropped. `_recover_annexure_labour_rows` now places a footer by the heading above it or, above the first heading, by the annexure the chunk continues (`annexure_carry`, carried on the page dicts so no chunk signature changed), and replaces the extractor's own copy. And `_is_valid_schedule_item` dropped a numbered priced row whose description ends in a full stop ("Health Faucet."). A schedule captured before these fixes is held to the same check when the costing skeleton is built (`component_basis_from_printed_total`, components only). Replayed locally on the real material list: Annexure-I Rs 5,70,000, II Rs 8,16,018.79 against the printed Rs 8,16,051, III Rs 65,000.01, every row within 1% of its own total and none unpriced. `tests/test_annexure_printed_totals.py`.

**A nil row is priced at nil, a count is not a weight, and a formula margin is named as one.** The second 5157 workbook, read line by line. The two rows the list totals at Rs 0.00 came out with an empty Qty and `[NEEDS RATE]`, and the Summary said "Complete the missing quantities/rates": `_normalize_line_dict` resolved `line.get("quantity") or line.get("qty")`, which turns a real 0 into None. It now prefers `quantity` by presence; `build_skeleton_from_boq` prices such a component at nil (`PRINTED_NIL_SOURCE`, amount 0, never `needs_input`), `split_costable_rows` does not send it for research, and `normalize_copied_rates` leaves it alone. Five rows the list prints under "kg" with **no** weight (Angle 6 mm, Top Rail, Bottom Flange, Hand Hold, protection tube) were researched per kilogram at Rs 52 against a printed Rs 536 a piece: with no weight the quantity is a count, and `_normalize_annexure_operands` now labels it `Nos`. And 73 rows -- all of Schedule B but paint, every labour line, the guard-room lots -- are `derived_estimate` (published / 1.25), which shows the org's default margin by construction; `build_strategic_summary` now says how many lines and what share of the estimated cost that is, and gives the margin on the researched lines alone, so a 19% on Schedule B is not read as a finding. On the client, a costing outlives the browser's connection (about fifteen minutes in the field), and `reattachAfterDrop` made one attempt and then said "reopen this session in a minute"; it now reattaches with backoff (2 s to 30 s, eight tries) until the run reports done, and a mid-stream drop inside `resumeActiveRun` keeps the run pointer so the next attempt has something to reattach to. Not fixed by code: Annexures IV--VI are still missing from the upload (Schedule B items 1--3 have no material list behind them), and the paint set and low-alloy sheet rates are flagged in the sheet for a hand check.

**A scanned page has two readers, and the RunPod one reads regions, not pages.** `runpod_ocr_service` is the platform's OCR engine: a RunPod serverless vLLM endpoint hosting `PaddlePaddle/PaddleOCR-VL` (`RUNPOD_API_KEY` + `RUNPOD_OCR_ENDPOINT_ID` in `.env`, or the `runpod_api_key` / `runpod_ocr_endpoint_id` PlatformSettings, which override it; unset = the reader is skipped, never an error). It replaces Tesseract, which is not on the deployed image, at every OCR slot: the last resort of the PDF cascade (`_ocr_pdf_page`), image uploads (`extract_text_from_image`), and, through `page_ocr_provider_order`, the per-page vision slot itself. `pdf_ocr_provider` (PlatformSetting) orders the two readers: `claude_then_runpod` is the default -- Claude vision first, RunPod when Claude fails, is unconfigured or the document's `pdf_vision_max_pages_per_doc` cap is spent (`claude_allowed=False`) -- with `runpod_then_claude`, `runpod` and `claude` for an admin to flip live. The order is deliberate and was measured, not assumed: on the Liluah material list the RunPod model read text and Annexure-I perfectly, and on dense numeric tables mis-read money to the paisa (Rs 1,81,892.26 as 1,817.226; Rs 3,282.59 as 32,282.59; a duplicated Rs 4,653.23), and those are the cells a costing lives on. Given a whole page it drifts into invented rows and other scripts within a few lines -- it ships behind a layout detector the vLLM endpoint does not host -- so the service segments the page itself (`band_bounds`: bands of at most `_BAND_MAX_FRACTION` of the page, cut at an ink-free row so no table row is split), reads each band under the `Table Recognition:` prompt concurrently through `/run` + `/status` (a `/runsync` returns IN_QUEUE past ~90 s; cold start was 160 s, warm bands 1.5 s), converts the cell markup to the pipe-separated rows the schedule extractor reads (`cells_to_text`), strips the IREPS signature stamp, and rejects the **whole page** if any band `looks_like_garbage` (foreign scripts, `<|LOC_n|>` tokens) so the cascade moves on rather than persisting half a table. Results go into the same `document_page_vision_cache` with `method=runpod_ocr`, and a cached page is served whichever reader wrote it. `tests/test_runpod_ocr_provider.py`.

**A Redis restart is a blip, not an outage.** Railway security-patched the managed Redis on 2026-09-12 at 10:29 UTC, ten minutes into a costing (the patch window was Sat 10:00–Sun 18:00 UTC; nobody had connected the two). Three things followed, none recoverable without a redeploy. RQ's work loop gave up (`Redis connection timeout, quitting...`) and `worker._run_single_worker` returned **0** — a clean exit — so every replica exited 0, Railway's `ON_FAILURE` policy left them down, and the queue had no consumers: every run enqueued afterwards sat there with nothing listening, which reads as "not a single output". `worker.serve` now runs the work loop until a stop is *requested*; when `Worker.work()` returns for any other reason it waits for Redis (`wait_for_redis`, backoff 1–15 s, up to `WORKER_REDIS_RECONNECT_WAIT_S`=900) and starts a fresh worker (the old one registered its death in teardown), exiting 1 — the exit Railway restarts on — only when Redis never answers. Second, `GET /runs/{id}/events` ended on the first failed XREAD with "Event stream error." while the worker was still running; it now retries under `_redis_retry_delay` (1, 2, 4, 8, 15… s, `_SSE_REDIS_RETRY_TOTAL_S`=180) keeping the browser's fetch alive with a keepalive comment and the cursor where it was, and gives up only when the window is spent. Third, the in-flight run died with its worker; that one is bounded, not removed — a worker that survives the blip is what keeps the next one alive. `tests/test_redis_blip_resilience.py`. Operationally: a Redis restart is safe to schedule, but not while a costing is running.

**A run's staleness is measured from the clock it actually runs on.** `run_service._stale_run_clause` is the single predicate behind both `reap_stuck_runs` (startup) and `find_live_run_for_session` (`GET /runs/active`), so the two can never disagree about whether a run is alive. A *running* row is stale past `started_at + job_timeout + grace`; a *queued* row past `created_at + 2× job_timeout`. Both cutoffs used to be `created_at + job_timeout`, but RQ's `job_timeout` starts when a worker **picks the job up** — so a run that waited ten minutes behind the queue (twelve worker slots, and `/health/capacity` exists because they fill) and then ran for twenty-five was 35 minutes old while sitting inside its 30-minute timeout. That row got cancelled out from under a live worker on the next web boot, and reported as "no active run" to a user reattaching from another device — the exact failure `/runs/active` was added to fix. Capacity is observable at `GET /health/capacity`: queue depth, active runs, DB pool, recent 429s — used by `scripts/load_test_command_center.py` and dashboards.

**Which documents costing reads is decided by exclusion, not by an allowlist.** `boq_parser_service._pick_nit_source_docs` feeds the structured schedule (`BOQItem`), and it now skips a PDF only on positive evidence that the document carries no priced line items — `_NON_SCHEDULE_DOC_TYPES` = `{drawing, terms_conditions}` out of the per-doc Haiku pass's vocabulary (defined in `document_analysis_agent`). Everything else is read, including a document the pass has not classified yet. It used to be an allowlist of `("BOQ", "schedule_of_rates", "RFP")` scanned in priority order, taking the **first non-empty bucket** and discarding every other document, which lost scope three silent ways (issue #7, the Liluah tender — Annexure 2 held 30 priced items and 6 were costed): `annexure` is its own tag and was in no bucket, so a price bid printed as an annexure could not be read at all; first-bucket-wins meant a tender holding both a BOQ and a schedule of rates costed only the BOQ, and an annexure mis-tagged `BOQ` evicted the NIT itself; and an unclassified document belongs to no bucket either, so every chat upload was dropped the moment one sibling had been classified — which is the ordinary case when a user uploads four files and asks for a costing in the same breath. The asymmetry is the whole argument: parsing a document that holds nothing costs a parse, while skipping one that holds thirty items costs the bid and leaves a run that looks like it succeeded. `tests/test_costing_source_documents.py` pins it, including that drawings and T&C are still skipped and that a classification excluding *everything* is ignored rather than obeyed.

The same membership question is asked in one other place and it is **not** the same answer: `context_budget._NIT_CLASS_DOC_TYPES` is a token-budget valve, used by `apply_trim_policy` to shed annexed/T&C/drawing PDFs from the native-vision blocks once the run is over budget *and* the bidding schedule has already been captured. That is legitimate — the structured schedule covers the content by then. `enhanced_costing_agent._build_tender_pdf_content_blocks` imports that set rather than restating it, precisely so the rebuild cannot re-attach what the policy just dropped; its ordinary caller passes no allowlist at all, so annexures reach the agent on the normal path. Its `max_docs` cap now applies **after** the allowlist (it ran in SQL before it, so a tender whose first three PDFs by id were a drawing set and two T&C booklets attached zero documents while the priced schedule sat fourth). If you find yourself writing a third copy of "which doc types carry prices", that is the bug repeating — import one of the two that exist.

**A schedule row does not need a printed rate.** Issue #7 had a second half, downstream of selection: once the annexures were finally being read, `_is_valid_schedule_item` threw most of their rows away. Every branch of that filter demanded either an item code or a printed number (a rate, a basic value, an amount) — but in a price bid / schedule of quantities the tender prints the item, its quantity and its unit and leaves **Rate and Amount empty for the bidder to quote**. Those rows are the ones the platform exists to price, and they were the exact rows it discarded: of a 30-item annexure only the 6 that happened to print a serial in the Item Code column survived. The filter now also accepts a positive quantity together with a unit of measure (branch (e) — short, alphabetic, `_MAX_UNIT_CHARS`), which is what separates a table row from prose; the quantity alone is deliberately not enough, because a stray number lands on an eligibility clause often enough that `_is_instruction_prose` runs first and is the guard that matters. `_BOQ_AI_SYSTEM_PROMPT` carried the same rule in words ("A valid row has an Item Code OR both an Item Qty and a Unit Rate") and was rewritten to match — widening the filter alone would have left the AI pass never emitting the rows for the filter to keep. `tests/test_boq_row_filter.py` pins both halves, including the 30-item annexure as reported and the full prose corpus that must stay rejected.

**A re-uploaded document re-captures the schedule; nothing else does.** The Liluah tender came back a second time: told to re-upload after the annexure fix, the bidder re-uploaded the NIT and three annexures into the same Command Center session and was costed the NIT schedules only, again. Nothing new had broken — `ensure_boq_parsed` found `BOQItem` rows already on the tender (the NIT-only schedule captured under the old rules) and served them, because "rows exist and are not thin" was the whole of its freshness test. A re-upload changes nothing that test looked at: a session keeps its tender, and a file uploaded under its *existing* name creates no new `TenderDocument` (the dual-write in `command_center.upload_attachments` de-duplicates by file name) — only a new `ChatAttachment`, which nothing consulted. `_schedule_predates_latest_upload` now dates the schedule against the newest document that could carry priced rows: a schedule-bearing `TenderDocument` (filtered through `_pick_nit_source_docs`, so a late drawing set does not count) or a PDF `ChatAttachment` on any session bound to the tender. Newer than the capture means stale, and `ensure_boq_parsed` re-captures through `parse_boq_from_tender(force=True)` — the path that snapshots and re-binds existing `CostBreakdownLine` rows, so saved costing survives. It fires once per change to the document set and then goes quiet, because a successful re-capture writes rows newer than every upload; it repeats only if the re-capture yields nothing and the old rows are kept, and then only when the user next asks for a costing. A schedule that cannot be dated (a row with no `created_at`) reads as current, never stale — "unknown" must not become "re-extract on every run". Timestamps are normalised with `_as_utc` because SQLite returns them naive and Postgres aware. One re-capture per tender at a time: `ensure_boq_parsed` takes a Redis `SET NX` lock with the run's 30-minute TTL (`drpl:boq:{tender}:recapture`, the same primitive as the cancel flag), and a second costing that finds it held waits up to three minutes for the first to finish and costs from the schedule it wrote — otherwise two runs on one tender would each spend a full AI parse and, in one interleaving, leave duplicate rows. No Redis means unlocked, which is the behaviour before the lock existed. `tests/test_schedule_recapture_on_upload.py`.

This is the trigger the rows-per-page ratio never was, and the contrast is the point: the ratio fires on a *correct* capture forever (a 200-page NIT with 50 real rows never clears four rows a page), while an upload is a one-time event with a timestamp. The ratio stays inert on R2 and commented as such; do not resurrect it. A schedule captured under an older rule with **no** new upload is still re-captured deliberately, via `POST /tenders/{id}/analyze`.

**A scanned table under a printed heading is still a table.** The Liluah tender's third report: the annexures were *material lists* — `MaterialListPaintAnnexure-VII.pdf` (one scanned page) and `MaterialListICFtoNMGHSR_1.pdf` (13 pages, 7,076 characters in total) — and costing took the NIT schedules and nothing from either, with no line in the log saying why. Two capture gates, both silent. First, the vision cascade (`advanced_document_parser`) sends a page to vision only when its text layer is under `pdf_vision_density_min_chars` (40): a scanned table under a printed heading — tender number, "Annexure-VII", "Material List" — clears that with the heading alone, is tagged `text`, and the extractor is handed a title. `_load_pdf_pages` now runs `_recover_image_table_pages` first: a `text` page shorter than `_SPARSE_PAGE_MAX_CHARS` with fewer than `_ROW_LIKE_MIN_LINES` row-like lines is given to `_force_page_vision_text` (cached; capped at `_VISION_RECOVERY_MAX_PAGES` per document), and replaced only when vision returns *more* than the text layer had. The platform-wide density setting is untouched — it serves the analyzer too; this is the schedule parser's own second look. Second, `_extract_chunk_with_retry` retried an empty extraction only on the IREPS markers `Description:-` / `Item Code`, so a material list's empty first result was final and, because the warning was gated on the same test, unlogged. `_text_looks_like_schedule` now also accepts two of the generic `_BOQ_HEADER_PATTERNS` signals, and a chunk that ends at zero rows is **always** logged with its character count and first hundred characters — a page with no rows and a page whose rows were not read look identical from the total, and the log is the only way to tell them apart. `_BOQ_AI_SYSTEM_PROMPT` now says an annexure table (material list, list of items to supply, schedule of quantities, price bid) is a line-item source with no schedule banner, and that the "annexure references" exclusion means citations *to* an annexure in prose, not the annexure's table. `parse_boq_from_tender` logs each document's row count, and a document contributing nothing is a WARNING. `tests/test_annexure_capture_gates.py`. A materials list can also print **no quantity column at all** — `Sr | Description of Material | Specification` — and every branch of `_is_valid_schedule_item` needed a number such a table does not print, so every row was discarded. Branches (f) and (g) now accept a row with a printed serial (`_serial_of`, positive int; the pdfplumber path writes 0 when the column is absent) plus either a unit or a description that reads like a cell (`_reads_like_a_cell`: at most `_SERIAL_ROW_MAX_WORDS` words, no terminal full stop). The rate is researched per item and the amount is left for the user — every downstream consumer coerces a missing quantity. The serial is a weaker signal than a quantity, so `_INSTRUCTION_PROSE_RE`, which runs first, learned the openers of the numbered eligibility and commercial clauses this could otherwise admit (minimum turnover, work experience of/in, EMD, security deposit, validity of, payment terms, delivery period, liquidated damages, penalty for, warranty period, the tenderer shall, rates quoted, taxes and duties, completion period, arbitration, jurisdiction, force majeure); `tests/test_boq_row_filter.py` pins a corpus of those against a corpus of real material rows that must survive it, including cells that merely contain a clause word mid-text. Per-document logging says how many rows are rate-only.

Reading the run: Railway's log export is per service, and costing runs in the **worker** — the web service's logs show uploads and `run.queued` and nothing from `[boq_parser]` or `[costing]`. Diagnose a costing from the worker service's logs (or `logs/costing_run.log` on the worker), looking for `-> N row(s)` per document, `yielded 0 rows` per chunk, and `looked like image tables` per document.

**An annexure is the breakdown of the schedule item that cites it, not scope on top of it.** The Liluah NIT prices "Material Cost for Conversion work … (As per Annexure-II of Material list uploaded in Document Section of NIT)" at ₹8,16,051 per coach set, fifteen coach sets; Annexure-II is the thirty materials that make up *one* coach set. Once the fixes above could finally read the annexures, their rows were persisted as flat `BOQItem`s with no schedule and costed as their own lines: the estimated cost carried the annexure twice (the item plus its thirty parts), the strategic summary showed the parts as a negative-margin "Other" bucket, and a per-set quantity ("12 LTR" per paint set) read as a contract quantity. Three columns and one deterministic pass fix that. `_ANNEXURE_HEADING_RE` reads the label printed above an annexure table (line-anchored; `ann+exure` because the NIT prints "Annnexure- IV"); `_walk_pages_chunked` carries it page to page like a schedule banner, cuts a chunk before a page that starts a new annexure, and stamps each schedule-less row with `annexure_ref` (the extractor's own `annexure` field is honoured only when it names a heading the chunk's text actually shows). `normalize_annexure_ref` makes "2", "ii" and "Annexure-II" the same label. `_link_annexure_components` binds every annexure row to the schedule item whose description cites its annexure — **by position in the parsed list**, never by `(schedule, item_code)`, because the Liluah schedule prints "CONVERSION MAT" on six rows and a key would bind the wrong one — and `parse_boq_from_tender` persists that as `BOQItem.parent_item_id`, with a synthetic `item_code` of `ANX-<ref>-<sr>` so two annexures both starting at serial 1 keep distinct identities (the same label is in `_boq_row_key` and the twin-collapse key, or cross-document dedup collapsed the ICF list into the Hybrid list). `source_document_id` records which file each row came from.

Downstream, a `CostBreakdownLine` with `parent_boq_item_id` set is a *component*: `build_skeleton_from_boq` groups it under "Annexure-N — components of Schedule X item Y (per set; rolled into that item)" instead of "Other", `_compute_totals` and `build_strategic_summary` skip it, the workbook's consolidated sheet shows its group total but leaves it out of the grand total (`_is_component_group`), and the editor's client-side totals skip it too. `rollup_component_parents` (dicts) / `rollup_component_lines` (ORM) set the parent's rate to the sum of its priced components' amounts — a per-set cost — times the parent's own quantity for its amount, against the parent's published rate for its margin, `rate_source="component_buildup"`, and a `cost_buildup_note` that says "N of M components; floor" when some are unpriced. It runs in `run_costing_batched_node` **before** `normalize_copied_rates` (which would otherwise derive the still-unpriced parent from the published rate), in `recompute_breakdown_totals`, and in `replace_lines` so a user editing a component's rate moves the parent on save. `split_costable_rows` keeps a parent with linked components out of the agent's batches — it is built up, not researched — and `_render_bidding_schedule_block` tells the agent a component's quantity is per set and names the item it belongs to. `_rebind_cost_lines` rebinds annexure rows by their synthetic code and refreshes `parent_boq_item_id` from the fresh parent, so a re-parse keeps the link.

**The deterministic IREPS parser wins outright, so it has to be whole.** `parse_boq_from_tender` runs `nit_schedule_parser.parse_nit_text` first and, when it returns rows, uses them and skips the AI path for that document. It read a fixed eight-token run per row — serial, code, qty, unit, rate, basic, escl, amount — and the Liluah NIT renders its Item Code cell narrow, so "CONVERSION MAT" arrived as two lines, every field shifted by one, the numeric checks failed, and the row was skipped: **ten of sixteen rows, including the two that cite the annexures, silently**, and both schedules' printed totals too, because "(INCLUSIVE OF ALL TAXES AND CHARGES)" sat between the banner and the number. Nothing downstream could notice — the totals it would have reconciled against were the thing it had lost. `_parse_lines` now lets the code span 0–`_MAX_CODE_LINES` lines (the run starts at the first token that reads as qty, word, three amounts; width 0 is a code pushed past a page break, recovered when the serial reappears before the description), splits a unit merged with its rate ("Set 223465.00", `_split_merged_cells`), refuses a code line that is a description or boilerplate or longer than `_MAX_CODE_CHARS` (without that, an unreadable row's search reached into the *next* row and swallowed it — Parel B-43/B-44), and reads a stated total up to `_MAX_BANNER_TRAILER_LINES` past the banner, stopping at the first row serial. Behind it, `_deterministic_parse_gap` is the guard that should always have existed: fewer rows than the text prints `Description:-` lines (summary placeholders excluded), or a schedule whose summed amounts miss its printed total by more than ₹1, and the deterministic result is discarded for that document and the AI/vision union runs instead — which reconciles. Three real NITs are fixtures now (`tests/fixtures/nit_liluah_vanbrake_2026.pdf`, `nit_5374229.pdf`, and the existing `nit_parel_2531.pdf`); `tests/services/costing/test_nit_schedule_parser_variants.py` pins 16/16, 97/97 and 84/84 rows summing to their printed totals to the paisa, and that Parel parses byte-for-byte as before.

Two more things the same pass fixed. The PUT payload in `routes/cost_breakdown.py` declared none of the NIT-mirror fields, so Pydantic dropped them and **one save from the editor stripped every line of `boq_item_id`, `schedule_name`, `item_code`, its tax flag and its web source**; the payload now declares them (all optional) and `to_dict` returns them. And `schedule_capture_report` says, in the costing reply itself (`_format_capture_report`), what the schedule was built from — rows per document, which annexures the schedule cites, which arrived and were bound to which item, and which are **missing** — because a costing that quietly omits an annexure looks exactly like one that read it. The serial-only row branch is also gated on `table_context` now: `_walk_pages_chunked` grants it only when the chunk shows a table header and no IREPS banner, and a compliance-matrix cell ("No No Not Allowed") is rejected outright (`_YES_NO_CELL_RE`). New columns under Rule 3: Alembic `20260911_boq_annexure_components` (force-added; `alembic/versions/*.py` is gitignored) plus entries in both self-heal lists. `tests/test_annexure_components.py`.

**An annexure's serial is not an identity, and a sub-table's number is not a schedule.** The fifth look at the Liluah tender, run locally against the real four PDFs: the NIT parsed 16/16, Annexure-I 4 rows, Annexure-VII 9, and Annexure-II 176 — of which **79 came back as "Schedule 20" … "Schedule 30"**. Annexure-II is thirty numbered sub-tables ("24 | Door Frame Complete", then its fourteen parts, each restarting at serial 1), and the extractor returned the sub-table number as `schedule_name`; a row with a schedule was never stamped `annexure_ref`, never bound to the item citing the annexure, and was costed as scope of its own. `_walk_pages_chunked` now knows an *annexure-only* chunk — annexure headings in its text, no IREPS banner, no schedule carried in — and clears any `schedule_name` the extractor emits there before stamping; `_normalize_schedule_names` no longer fills an annexure row from the running schedule. The same restart broke three identities keyed on the serial: the synthetic code is now `ANX-<ref>-<seq>` by position within the annexure (`_assign_annexure_codes`), not the printed serial, so thirty rows are not all `ANX-II-1`; `_boq_row_key` adds the quantity for annexure rows (one part name recurs across sub-assemblies); and `_collapse_value_redundant_twins` treats an `ANX:` group as thirty rows, not one — a twin must match on description too, a shell is absorbed only by a valued row of the same description, and distinct values at one serial are not a conflict. `order_lines_for_display` (cost_breakdown_service) is the order `to_dict` and the workbook use: schedules first in code order and serial order, then the component groups Annexure-I, II, … VII each in the order its table was read — the ORM relationship orders by serial alone, which put "Schedule 11, B, A, 10" on the sheet and would interleave the thirty sub-tables. `platform_build()` (config) stamps the git commit into the web and worker boot logs and the reply's capture section, because two costings that read the same six NIT rows an hour apart were indistinguishable from "the fix changed nothing" when the fix had not shipped.

**The costing has to fit inside the job that runs it, and a row is bound by what identifies it.** The sixth look at the Liluah tender: capture was finally whole (~190 rows: NIT 16, Annexure-I 4, Annexure-II ~176, Annexure-VII 9) and the costing never finished. Four faults, each pinned by `tests/test_costing_fits_the_job.py`. **Time** — `run_costing_batched_node` priced four batches of sixty one after another, plus up to two re-cost sweeps, under a `_costing_timeout_seconds` budget of 2,880 s; the RQ job (`run_service._RUN_JOB_TIMEOUT_SECONDS`, 1,800 s) and the Master Agent's own `wait_for` (`master_agent_max_execution_time_s`, 1,800 s) both killed it first — batch 3/4 logged at ~29 min, then `JobTimeoutException` and nothing returned. Batches now run `costing.batch_concurrency` (4) at a time under a semaphore, each on its **own** `SessionLocal` session (the web-search tool and the callback read settings from executor threads, and the merge commits; no two batches share a session, and the node's own session is released with `_release_transaction` before they write — on SQLite an open reader blocks every commit). The budget is per *wave* (`waves × 240 s × (1 + sweeps)`, headroom growing with the wave size) and **capped** by `_costing_budget_cap_s()` = the tighter of the job and Master limits minus 300 s, so it can never outlive its process; the node stops issuing work `_FINALIZE_RESERVE_S` (180 s) before that timer and finalises what the batches saved, and if the outer timer fires anyway `_recover_partial_batched_result` finds the skeleton this run built and finalises it through the same `_finalize_batched_breakdown` (roll-up, copied-rate guard, summary, totals) instead of returning the "pipeline error" line. A component with a printed rate is not web-researched (`_COMPONENT_RESEARCH_RULE`; the material list prints each item's rate and weight, and per-row research is what made an annexure batch take ten minutes). **Merge** — batch 3 came back 60 rows, 19 "merged", 41 unmatched, none by `boq_item_id`: annexure rows carry no schedule, so `merge_batch_rates`'s `(schedule, item_code)` index never held them, and the bare `sr_no` fallback bound their prices to whichever NIT row shared the serial. Annexure rows match by their unique `ANX-<ref>-<n>` code (`_is_annexure_code`, normalised, whatever label the agent put beside it); the serial fallback is `(schedule, sr_no)` for schedule rows only and only where unique; a serial alone binds nothing and the row stays `needs_input` for the sweep. **Concurrency** — `document_analysis_agent` called `parse_boq_from_tender(force=True)` on every analysis with no lock, replacing every BOQItem id under a running costing. It now goes through `recapture_schedule_for_analysis`, which takes the same `SET NX` re-capture lock `ensure_boq_parsed` takes and, while a costing has the counted `drpl:boq:{tender}:costing` flag up (`mark_costing_running` / `clear_costing_running`, raised around the graph in `_run_enhanced_costing_research_inner`), does not force the re-capture (a schedule that does not exist yet is still captured). **Logs** — the worker echoed every SQL statement (`echo=settings.debug`) and Railway drops lines past 500/s, so the lines that mattered were the ones lost; `worker._silence_sql_logging` raises the SQLAlchemy loggers to WARNING *and* turns the engine's echo off, because `echo=True` bypasses the logger level.

Run locally end to end against the four Liluah PDFs with all of the above: capture 206 rows in 8 min, four batches concurrently, **202 of 202 rows merged, 0 unmatched, in 3.5 min**. That run exposed what the roll-up was summing. **A quantity that contradicts its printed total is a misread, and an annexure's basis is what its totals say it is.** Annexure-II's printed totals reconcile to 99% of the published per-set rate, so they are the trusted figure; the weight column is read by vision and came back "12243.2 kg" on a chequered plate whose total is Rs 22,523 at Rs 39.18/kg, and one screw row had its Rs 4,360.13 total read into *both* the quantity and the rate cells (Rs 1.5 crore of screws in one coach set). `_reconcile_annexure_quantities` (boq_parser_service, before the components are linked) re-derives the quantity as total / rate where the two printed figures disagree with it by more than `_ANNEXURE_QTY_TOLERANCE`, and reads quantity == rate with no total as one lot at that total; a row with no printed total is left as read. Annexure-I prints "15" against every stripping item — the fifteen coach sets — and its totals sum to Rs 38,000 × 15, not Rs 38,000; summed as per-set and multiplied by the fifteen sets again, that parent came out at twelve times its published rate. `_component_quantity_divisor` (cost_breakdown_service) settles the basis from the printed totals: when they sum to the parent's contract value and not to its unit rate, the sum is divided by the parent's quantity, and the note says so. A build-up more than `_ROLLUP_OVERRUN_FLAG` (1.5x) above the published rate is called out in the parent's `cost_buildup_note` and in the breakdown's assumptions — Annexure-VII prints no rates, and the researched paint rates came out 9.6x the published Rs 22,547 per set; the number stays, the reply says to check it. After both: Annexure-I 80% of published, Annexure-II 74%, Annexure-III 35%, Annexure-VII flagged.

**A costing request finishes in one pass, and the workbook it made is announced.** The seventh Liluah report: four PDFs attached, "do the costing properly and completely", and after fourteen minutes "Something went wrong while processing your request" over a list of twenty-five completed tool calls — `tender_lookup`, `document_reader` seven times, `semantic_search` six, `memory_retrieve` three, `call_deep_analyzer` twice — and never `call_costing_researcher`. Twenty-five is `master_agent_max_iterations`: LangGraph raised `GraphRecursionError`, which `format_user_error` did not know, so saved work was shown under the generic apology. "Continue" then costed the tender in one pass and ended by offering to "generate the Excel workbook" — which `chat_costing_research` had already written and attached as `xlsx_artifact`, a key `_worker_result_payload` dropped and the ReAct path never turned into an `artifact_created` event (only the plan-execution path did). Four things, pinned by `tests/test_master_one_pass.py`: `run_decision_maker` catches `GraphRecursionError` and returns the same partial-work message the timeout does (`budget_exhausted_headline`, status `partial`), and `error_utils` names a spent budget; `make_budget_hook` is a `pre_model_hook` that, once `BUDGET_NUDGE_REMAINING` (6) calls remain, appends a `[Platform budget notice]` HumanMessage to `llm_input_messages` — the model had never seen the `decision_budget` UI event — telling it to make the one outstanding `call_<agent>` or write the answer (a HumanMessage because `langchain_anthropic` rejects a mid-list SystemMessage; `llm_input_messages` so the graph's history is not polluted); `DELEGATION_DOCTRINE` now says the costing call is the FIRST call — the worker runs its own prerequisite analysis and reads every document itself, so reading first only spends the budget; and `orchestrator_tools.worker_artifacts` / `announce_worker_artifacts` carry a worker's files into the Master's payload (with a note not to offer to generate them) and emit `artifact_created` live on the ReAct path.

**The platform's rate is its own, and the railway's rate is what it is measured against.** The Mid-Life Rehabilitation NIT (Liluah, 286 rows, schedules A-T): "the rate the platform used is the same railway rate". It was, scaled. Replayed locally, every one of 285 rows came back `derived_estimate` with "Derived from published Rs X by stripping ~23%" -- RULE 2b *invited* that as a fallback and the model took it for every row -- beside a build-up that summed to a fraction of the rate it carried; `normalize_copied_rates` only catches a rate *equal* to the published one, so nothing did. Three layers now. **The prompt** no longer offers the derivation: a `derived_estimate` is a first-principles build-up and its rate is that build-up's sum; with no build-up the row is `needs_user_input` and the platform's own labelled fallback applies. **Merge refuses it**: `rate_anchored_to_published` reads the agent's `source_ref` / `cost_buildup_note` for a stated derivation ("derived from published", "published ref ... stripped of 15% margin", "N% of published", "published / 1.25") and `merge_batch_rates` leaves such a schedule row `needs_input` (`rejected_anchored` in its result, only when non-zero) for the re-cost sweep -- a comparison ("below the published Rs 9,000"), a DSR, and "stripping of coaches" as work do not match; components are exempt (`_COMPONENT_RESEARCH_RULE` sanctions their printed basis). **The batched agent never sees the number**: told not to strip a margin, the model back-solved "0.45 kg @ Rs 65 + labour + overhead = Rs 330" to exactly 0.800 of the published Rs 413 on 52 rows, so `_render_bidding_schedule_block(withhold_published=True)` blanks `estimated_rate` / `basic_value` on schedule rows for the batched path. That path's skeleton owns `tender_rate` and merge never takes it from the agent, so margin is unaffected; the single-call path cannot withhold, because its agent emits `tender_rate` itself. Blind, the model is independent -- and it had nothing to be independent *with*: with no Gemini or Tavily key the web-search chain fell to DuckDuckGo, whose `duckduckgo_search` library (deprecated, renamed `ddgs`) answers "4 sq mm cable price per metre" with dictionary pages for "flexible", so all 286 rows were guesses ("Fitting & Repairing Charges" Rs 6,500 a coach against Rs 1,88,800; an underframe front part with its child parts at 120 kg). `WebSearchTool` now has a third tier before DuckDuckGo, **Claude's server-side web search** on the Anthropic key the platform already holds (`_search_anthropic`: `web_search_20250305` on `anthropic_search_model`, Haiku 4.5, `max_uses` = `anthropic_search_max_uses`, located in India, usage logged to `api_usage_logs` as agent `web_search`; the per-search fee is not in that estimate). It returns dated, cited prices from Indian sellers and says so when no page prices the exact item. Even with search working the agent searched eleven times for 286 rows, so research is not left to it: `costing/market_price_research.py` runs beside the batches (`_start_market_research` / `_merge_market_research`, switch `costing.market_research`, concurrency `costing.market_research_concurrency`, same deadline) and makes one Claude web-search call per row that can have a market price (`not_a_market_item` skips drawing parts, labour, charges, components). A price is accepted only if the model rates the listing an exact or close match for the specification AND its URL is one that same call's search returned (a cited page cannot be invented); it is merged as `web_search` with its `source_url`. Measured on this NIT: public listings for RDSO/EDTS-spec items essentially do not exist (the ELRS elastomeric cable, EDTS-200 crimping sockets, a Sanrok valve connector -- none, on the open web or GeM), and the model correctly declines them; roughly Rs 300 of API cost per costing. Then the platform, not the model, decides each row's figure: `settle_rates_on_evidence` (finalisation, after the copied-rate guard, same `costing.derive_cost_from_reference` switch) keeps evidence -- `_has_market_evidence`: a web price with its URL, or the firm's rate card / training data / memory with a reference -- within `_PLAUSIBLE_BAND` (0.5-2x) of the reference cost (published / (1 + overhead + margin)), and gives every other row with a published rate the reference cost. The model's blind build-ups ran 0.15x-6.45x of the reference between the tenth and ninetieth percentile, uniformly on a log scale, so a build-up near the reference is luck as often as knowledge and is kept in the note only as a cross-check; the railway's estimate is built from last accepted rates. A row with no published rate keeps its figure. Every settled line opens its build-up note with its basis in plain words (`BASIS_MARKET` / `BASIS_REFERENCE` / `BASIS_BUILDUP`), and the Summary counts them and says that uploading the firm's own purchase rates as training data is how more lines get costed from real prices. Idempotent. The Summary's "derived by formula" line counts only `_is_formula_derived` rows (the guard's note, a stated derivation, or no build-up at all), since an agent's own build-up is `derived_estimate` too. `tests/test_published_rate_anchor.py`, `tests/test_rates_settled_on_evidence.py`, `tests/test_web_search_claude_tier.py`, `tests/test_market_price_research.py`. The last step -- every row without evidence at the railway's cost -- is what Sahil reported next, and is superseded by the platform's own build-up (the paragraph after the capture fault below).

Behind the rates sat a capture fault. The deterministic IREPS parser read 285 of 286 rows, so `_deterministic_parse_gap` sent the NIT to the AI extractor, which paired rates with the wrong descriptions (a 4.5 kW RBC unit at Rs 883.82; it is Rs 1,64,020) -- and every costing compared against them. Schedule R row 6 printed its cells at the foot of one page and its serial on the next, just before its description; row 5's description swallowed the cells as text. `_parse_lines` now ends a description at a field run and reads a serial-less run's serial from `_displaced_serial` (a bare integer directly before a `Description:-` line, within `_MAX_DISPLACED_SERIAL_GAP`). Both new call sites use `_whole_field_run` -- the shape plus basic value = qty x rate -- because a description's last line, the next serial and the next row's first cells can fit the shape ("required for SBC." "7" "Elect" "2.00"). The three existing fixtures parse identically; Mid-Life is 286/286, summing to the advertised Rs 17,31,51,893.83 to the paisa, in one second instead of seven minutes (`tests/fixtures/nit_liluah_midlife_2026.pdf`).

**The platform builds each row's cost itself; the railway's rate checks it and never becomes it.** After the fix above shipped, the same NIT came back 284 of 286 rows at exactly the railway's rate / 1.25 (grand total Rs 13,83,25,482 against Rs 17,31,51,894, 0.799), and Sahil's team said again that the rates were the railway's. Settlement had done it: no verified market price, so the railway's cost. The agent's blind build-ups deserved that, but for reasons that could be fixed. It never saw the schedule banners, which are the only place an IREPS NIT says what a row is -- "Web to Drg No LE11185" is the web itself in schedule A ("(Mechanical) Cost of Material", Rs 279.66) and the labour to fit it in schedule B ("COST OF LABOUR", Rs 2,239.21) -- and it priced sixty rows a call on Haiku with no wages, no current prices and its own arithmetic. Now: **the banner is captured** -- `NITSchedule.full_title` (the banner and its continuation lines, IREPS's repeated "Tenderer should submit break-up ..." instruction stripped) is stored as `BOQScheduleTotal.title` (Alembic `20260925_boq_schedule_title` plus both self-heal lists), and `costing/schedule_context.py` classifies it (`MATERIAL` / `LABOUR` / `MATERIAL_AND_LABOUR`, and whether it says "INCLUSIVE OF ALL TAXES"); a tender captured earlier has its IREPS document read once and the banners stored. The agent's schedule block is headed with them too. **The platform builds up the cost** -- `costing/cost_buildup.py`: a rate basis researched once per costing on the server-side web search (`research_rate_basis`: the central minimum wages for the work's area and current prices for the material families the rows name; a figure is kept only if its URL is one that call's search returned and it sits in a plausible range, and wages are all-or-nothing, falling back to `FALLBACK_WAGES` -- CLC construction category, Area A, from 01-04-2026: Rs 827 / 918 / 1,008 / 1,094 a day), loaded by `costing.labour_statutory_loading_pct` (30%: PF, ESI, bonus, leave); then a few rows of one schedule per call on `costing.buildup_model` (Opus 5, adaptive thinking, `web_search_20260209`, refusal `fallbacks: "default"` dropped if the org refuses the option), with the banner, the tender's scope and the rate basis in the cached prefix and the railway's rate withheld. The model returns, through a strict `submit_cost_buildups` tool, what one unit is, materials (quantity, unit, price and whether it came from the rate basis, a cited page or its own estimate), hours by skill and other costs; `price_buildup` does the sums -- a rate-basis price replaces the model's for the same unit, a cited page counts only if the search returned it, hours are priced at the loaded wage -- and writes the plain-words note under `BUILDUP_MARKER`. **The railway's figure only checks it**: `benchmark_cost` is the published rate less overhead and margin, and less GST where the banner says the rate includes it; a build-up more than 2x away gets one second look told only "FAR ABOVE/BELOW the benchmark", and `choose` keeps a second look inside the band, else the nearer of the two -- both the platform's own. Two rows naming the same item in one schedule (the NIT lists the LA51100 door arrangement twice) are built up once and given one figure (`distinct_rows`). A model the account cannot use (not found, not permitted) falls to the next in `model_chain` -- Opus 5, Sonnet 5, Haiku 4.5 -- for the rest of the run, rather than losing every build-up and putting every row back on the railway's figure. Merges are serialised, written as groups finish, and refused after the node's deadline so a late call cannot overwrite a settled row. **Routing**: with no firm rate data, rows with a published rate skip the agent's batches and go to the build-up (`model_routing`, nothing skipped when no key or `costing.platform_buildup` is off); with firm data the agent prices first and `_rows_without_evidence` sends what it priced blind to the build-up. The market research now merges only a listing within the band of the row's benchmark and never over the firm's own rate (`line_has_firm_rate`). **Settlement** keeps, in order, a verified market price or firm rate in band, the platform's build-up (it has had its second look), and the agent's own `derived_estimate` in band; only a row with none of these takes the railway's cost (GST-aware), its note naming why -- with the build-up on, a row it could not finish in time. `merge_batch_rates` strips `BUILDUP_MARKER` from every caller but the platform, as it strips `VERIFIED_WEB_PRICE`, so the agent cannot claim a build-up it did not have checked. The copied-rate guard leaves a build-up that happens to equal the published rate. The costing reply states how many lines rest on each basis instead of "please verify ... before submitting" (the readers bid without a review step), and the workbook's Source cell says it in words ("Cost build-up", "Market price (web)", "Railway estimate (fallback)") while the stored code, which the editor and the artifact preview key on, is unchanged. The Summary also takes the GST out of a tax-inclusive schedule's margin (`_gst_inclusive_margin_observation`): its tender value carries 18% GST and its estimated cost does not, so the table's margin for it counted the GST as the firm's -- about 15 points on Mid-Life schedules A to O. Settings: `costing.platform_buildup`, `costing.buildup_model`, `costing.buildup_concurrency` (8), `costing.buildup_rows_per_call` (8), `costing.buildup_second_look`, `costing.labour_statutory_loading_pct`. `tests/test_platform_cost_buildup.py`, `tests/test_schedule_banners.py`, `tests/test_rates_settled_on_evidence.py`, `tests/test_costing_model_routing.py`.

**A budget has to fit inside the job that carries it.** The eighth failure on
that path, from production on 2026-09-11: `run_router_task[727de58b...] failed:
Task exceeded maximum timeout value (1800 seconds)`, fifty-two annexure lines
into a costing run. The Master has a graceful timeout -- `asyncio.TimeoutError`
-> `render_partial_timeout_message`, status `partial`, the trace of what it
actually did -- and it could not fire, because `master_agent_max_execution_time_s`
(1800) was exactly `run_service._RUN_JOB_TIMEOUT_SECONDS` (1800). RQ's death
penalty got there first, so a run that had produced real work was recorded as
*failed* wearing a raw RQ message. `resolve_master_budget` now clamps the budget
to `_RUN_JOB_TIMEOUT_SECONDS - _JOB_TIMEOUT_MARGIN_S` (120 s), which is what the
partial message, the terminal status write, `save_partial_turn`, the `run_done`
event and the notification need after the graceful return. Raising the setting
past the job timeout is a no-op rather than a silent regression to a hard kill;
a deliberately *shorter* budget is still respected. The pricing stage loses
nothing: `_costing_budget_cap_s` is computed against the job timeout, not the
clamped budget, so the three limits nest costing 1500 < Master 1680 < RQ 1800.
`TestMasterBudgetFitsInsideTheJob` in `tests/test_master_one_pass.py`.

**The rejected transcription escalates; it does not ask the same model again.**
Pass 2 has always rejected its own output and retried when a transcription came
back with descriptive placeholders, an unusable envelope or an empty body -- and
both attempts ran on the same model. The reason the two-pass split exists is
that verbatim transcription of dense legal text is where Haiku's
instruction-following degrades, so the retry was asking the model that had just
failed to try harder with a sterner prompt. Attempt 2 now runs on
`annexure_transcription_escalation_model` (`claude-sonnet-5`; empty restores
same-model retries), per annexure, and only after a defect has actually been
detected -- so a clean form still costs exactly one Haiku call and the
two-model economics survive. `transcription_meta` records `model`, `attempts`
and `escalated`, and `run_annexure_extraction` now returns `transcribed`,
`transcription_failed`, `escalated` and `placeholder_defects` in its counts:
that last one was computed and dropped on the floor, which is why every Liluah
report found paraphrased annexures by reading the output instead of being told.
`TestTranscriptionEscalation` in `tests/test_annexure_verbatim.py`.

**The costing prefix is read from the cache, and an unchanged document is not
read twice.** A token audit (2026-09-24) found the three largest spends were
work repeated on identical input. The costing agent's system prompt (44.5k
chars plus up to 200k of training data) went out in full on every ReAct step
of every batch: `_system_message_for_model` now sends it as one
`cache_control: ephemeral` block on Anthropic (a plain string elsewhere;
`FailoverChatModel._strip_cache_control` removes the marker if the chain
falls over to another provider), and `_warm_prompt_cache` writes the entry
with one tiny call before the batch wave and before each sweep, so the four
concurrent batches read it instead of each paying the 1.25x write. The
single-call path also stops sending the same PDFs twice: the pdfplumber text
extract is dropped when every page is text-layer and the same documents are
attached as native blocks (`_extract_duplicates_blocks`); a scanned page keeps
it. Then `app/services/analysis_reuse.py`: a per-document vision read is
stored with its provenance (`summary_json["_source"]` = file sha1, model,
prompt hash — no schema change; stripped before the synthesis prompt so that
prompt is byte-identical), and `_analyze_single_document_native` returns the
stored summary for the same bytes, model and prompt without an LLM call.
`analyze_all_documents` therefore no longer deletes `per_doc_summary` rows
before a re-run. Annexure discovery is cached the same way in the page-vision
cache table under an `annex:` key (that table is only ever read by exact key,
so nothing that counts extraction rows sees it), and a form whose PDF is
unchanged and whose workspace already has a body is reported
`skipped_unchanged` rather than transcribed again. `chat_document_analysis`
returns the stored analysis when `analysis_is_current` — completed, has a
report, and every PDF on the tender today hashes to a per-doc row — and runs
the pipeline only when a document changed or the wording asks for it
(`wants_fresh_analysis`: "re-analyze", "again", "fresh", ...); the
post-analysis fan-out is re-kicked only when it left no rows. The forced BOQ
re-capture after an analysis is skipped when every per-doc result was a cache
hit and no schedule-bearing upload postdates the capture. And
`_ensure_tender_analysis` compared readable documents against all PDFs, so a
tender with one unreadable PDF was "stale" on every router-path call; it now
counts `per_doc_unreadable_count` too and prefers the hash check. Switch:
`tender_analyzer.reuse_document_results` (PlatformSetting, default on);
`force_refresh` threads through `run_document_analysis` /
`analyze_all_documents`. `tests/test_token_reuse.py`.

`logs/costing_run.log` is a dedicated file handler attached in `main.py` for the costing pipeline (graphs + tools + canonical registry + cost_breakdown route). `tail -f drpl-backend/logs/costing_run.log` is the canonical way to watch a costing run end-to-end.

**A deploy you cannot see is a deploy you will argue about.** The GeM ministry
commit reached `main`, Railway deployed both services, and confirming that took
reading `/openapi.json` and getting lucky -- the commit happened to change a
request model's description, so the schema moved. A fix to a service function
does not move the schema at all, which is how "did my change ship?" becomes
guesswork, and the expensive version of that guess is the worker: it is the
process that reads the NIT and prices the schedule, so a build that reaches the
web service and not the worker reads as *the fix changed nothing*. `GET /health`
now answers `{"status": "healthy", "build": "<sha>"}` from `platform_build()`
(Railway's own `RAILWAY_GIT_COMMIT_SHA`, so it is the deployed commit rather
than something the code asserts about itself), and `GET /health/capacity` adds
`build`, a `worker_builds` tally and a `workers` map -- each forked child writes
`host:pid -> sha` into the `drpl:worker:build` Redis hash at boot (`worker.py`,
best-effort, 24 h TTL).

**That hash is written once and never refreshed, so the reader has to prune
it.** The very first deploy after this shipped reported 24 children on two
different builds, twelve of them containers Railway had already replaced --
which is the same class of wrong answer the endpoint exists to prevent. RQ
already heartbeats its own worker registry, so the endpoint intersects the hash
with `rq.Worker.all()` (matched on the same `host:pid` pair RQ names a worker
with) rather than inventing a second heartbeat. If that intersection comes back
empty while the hash is not, the registry is the thing that is wrong and the
unfiltered map is reported instead: losing a worker from this list is worse than
showing one too many. `worker_builds` is the one-glance answer -- two entries
means a rollout in flight or a replica stuck behind, which is exactly what is
worth noticing. Both endpoints were already unauthenticated counters, so
checking a deploy is one `curl` on either tier with no Railway login.
`tests/test_build_identity.py`.

**A masked secret has to say whether it is set.** Every `is_secret`
PlatformSetting is served as the same `••••••••`
mask and the page renders an empty password box, so a key that had never been
configured looked exactly like one that had. On a tab carrying 55 settings that
is not cosmetic: it is how a master admin concludes a key is in place, runs the
thing it powers, gets an authorisation error and goes looking for the cause
anywhere but the empty field. `SettingResponse.is_set` is computed from the
stored value *before* masking (whitespace is not a value), both list routes
carry it, and the secret input now says "Saved -- type a new value to replace
it" or "Not set", with a per-state line under it instead of an
`sk-ant-api03-...` placeholder on every key the platform has. The tab also has
a search box, because scrolling was the only way to reach one of 55 rows.

**Saving a setting whose row does not exist yet is not a 404.** Rows are
created by `seed_defaults`, which runs at startup and on every settings GET --
so a setting added by a release exists only once something has read the list.
Until then `set_setting` raised "Setting 'x' not found" and the route returned
404, which reads as *this setting does not exist* about one that does; a
deep-link or a cached page hit exactly that. `set_setting` now creates the row
from `DEFAULT_SETTINGS` when the key is one the platform ships, and still
raises for a key that is not -- it fills in rows, it does not let the API
invent settings. `tests/test_settings_secret_state.py`.

### GeM Search: the server-side collector

`drpl-backend/collector/` is the GeM sweep (vendored from the `DRPL-scrapper` repo; `docs/GEM-collector.md` is its measured account of the portal). It runs **inside** the backend -- no second Railway service, no service token, no HTTP hop -- and a collect run **is an `AgentRun`**: same table, same Redis stream and cancel keys, same `GET /api/runs/{id}/events`, same Stop and reattach the Command Center already has. `tests/collector/test_contract.py` pins the three shared contracts against the backend's own source. What was rewired to run in-process, and why it is the boundary that matters: `collector/db.py` uses the backend's engine and session; `collector/models.AgentRun` **is** `app.models.agent_run.AgentRun` (the four `collect_*` ledger tables stay the collector's own and are created on first use by `ensure_ledger`); `collector/runbus.get_redis` is the backend's client; `collector/sink.InProcessSink` calls `tender_service.ingest_tender_batch` directly as the user who pressed Search and fires the same scoring / NIT-fetch fan-outs the extension route does; `collector/scope.fetch_profile` reads the admin's Tender Scope profile from the database in the shape `GET /api/extension/config` serialises it. The HTTP `Sink` and the token path remain for a collector deployed on its own.

`app/api/routes/collect.py` (`POST /api/collect/runs`, `GET /active`, `/recent`, `/coverage`) is gated on the `tenders` surface and enqueues the job **by string name** onto its own queue, `COLLECT_QUEUE = "collect"`; `worker.py` listens on both queues, so the ordinary worker serves it. **One sweep at a time, platform-wide** (`MAX_ACTIVE_COLLECTS`): a sweep is minutes of traffic against one portal from one egress IP and takes one of the twelve worker slots, so a second person who presses Search is handed the running sweep to watch (409 carries its id; the page reattaches), and a stale row (`run_service._stale_run_clause`) never blocks the button. The GeM path is httpx-only -- `beautifulsoup4` + `lxml` are the whole dependency cost; nothing under `collector/session` or `portals/ireps.py` imports Playwright at module level, and IREPS raises `IrepsNotConfigured` rather than half-working. The page (`drpl-frontend/src/pages/GemSearchPage.tsx`, sidebar "GeM Search", surface `tenders`) sends `gem_full` -- the enumeration that reports coverage as arithmetic -- in one of three modes: **Search GeM** (`incremental`, about a minute, answers "what is new?" and reports no coverage), **Complete sweep** (`ministry`, ~13 minutes, complete against GeM's own count for the ministry) and **Full portal audit** (`full`, reads every live bid on the portal). It shows rows as `tenders_ingested` events land, and reattaches on reload through `GET /api/collect/active` (anyone's run, since there is only ever one). Smoke-tested end to end locally against the live portal: two pages, twenty tenders ingested through the in-process sink, run row `completed`. `tests/test_gem_search_route.py`; the collector's own 322 tests run under `tests/collector/`.

**The portal will filter by ministry -- on an endpoint the walk never used.** For a
long time this collector's own notes said GeM has no ministry filter. What is
true is that `/all-bids-data` has none. GeM's own Advanced Search page calls
`POST /search-bids`, which takes `searchType: "ministry-search"` and a ministry
name, answers an anonymous request, and returns the same Solr envelope.
Measured 2026-09-22 through the integrated collector: `numFound` **1,692** for
"Ministry of Railways" against **43,816** live bids -- 170 listing pages
instead of 4,382, for the identical set of tenders, and every row on page one
already carries `ba_official_details_minName == "Ministry of Railways"`, so the
filter and the scope check agree by construction rather than by luck. It is not
a drop-in replacement for the walk, for one measured reason: `/search-bids`
ignores the sort key and pages unstably, so a single pass returned 1,426 of
1,703 and would have looked exactly like a complete one -- the same failure
shape as the 40-page cap this collector was built to remove. Eight consecutive
passes converged 1,405 -> 1,628 -> 1,684 -> **1,703** and then sat on the
portal's own count for four more. So `mode="ministry"` is written as *repeat
until the distinct count reaches `numFound`*, never *read every page once*
(`gem_ministry_max_passes`, `gem_ministry_settle_passes`): the denominator
belongs to GeM, so the mode cannot quietly under-collect -- it either reaches
that number and says `complete: true`, or it stops short and reports coverage
below 1. Repeating passes costs listing pages, not documents: the detail stage
is driven by the rows a page contributed that the run had not already seen, so
a bid read on pass 1 is a duplicate on pass 2 and its PDF is never fetched
again -- 2,208 requests end to end against the walk's 6,476, for the same
~1,700 documents, which is what a run's wall clock is actually made of. `full`
stays, and not out of nostalgia: it is the only mode that reads the whole
corpus, so it is the only one that could ever catch a railway tender whose
`minName` GeM has filled in wrongly. Cheap mode routinely, audit nightly.
`portal_done` now carries `coverage`, `complete`, `rows_distinct`,
`pages_failed` and `final_total` **only when the sweep measured them** -- an
incremental sweep stops on known ground and has no coverage to report, so it
omits them and the page says "newest bids only" rather than letting 23% of the
corpus read as all of it. `Batch.rows_distinct` rides the same way on
`collect_progress`, because the progress bar's old numerator (pages fetched
over pages expected) reads 198.8% on a second convergence pass while distinct
rows over the portal's total is the figure that is converging.
`TestMinistryMode` in `tests/collector/test_gem_full.py` pins the convergence,
the settle guard, that a partial sweep is reported as partial, that 0/0 does
not round up to done, and that `full` never asks for the filter;
`tests/test_gem_search_route.py` pins that the route takes exactly the three
modes and refuses a ministry list GeM's filter cannot serve.

**The listing's "Z" is Indian Standard Time.** `final_start_date_sort` and
`final_end_date_sort` come back as `2026-09-25T11:00:00Z`, and the portal's own
UI shows that same bid closing at 11:00 AM -- the same wall clock, so the digits
are IST and the `Z` is decoration. Read literally it put **every deadline 5h30m
late**, which on a tender system is the one field that must not be wrong.
Sampling 40 live bids against each bid's own PDF (which `gemdoc` has always
converted correctly): 0 of 40 matched before, 35 of 40 match after, and the
remaining 5 differ by whole days in both columns -- bids a corrigendum has since
extended, where the **listing is right and the document is stale**. That is also
why the detail stage only fills a gap and never overwrites (`_set` is
fill-if-empty). `gem.ist_instant` does the conversion, once, on the raw listing
field in `to_tender`; it is deliberately **not** idempotent, because a real
`+00:00` and GeM's counterfeit one are byte-identical, and a test pins that as a
warning rather than an endorsement. An unparseable value is passed through
unchanged with a warning -- silently losing every date is a worse failure than
the one being fixed. Already-stored rows self-heal on the next sweep for
`closing_date` (`_update_existing_tender` reassigns it when it differs); it
never assigns `opening_date`, so a stored opening time stays as it was.
`docs/GEM-collector.md` carries both measurement tables.

**The portal decides by address, not by request.** The first deploy failed with
`ConnectError: All connection attempts failed` from the Railway worker while
the identical `GET /all-bids` returned 200 from a laptop in India and from a
non-cloud US address: GeM's F5 edge drops TCP from cloud egress ranges before
TLS, so no header, retry or fingerprint changes the answer. Every GeM-facing
httpx client is therefore built by `collector/netconfig.make_client`, which
honours `gem_proxy_url` (Admin > Platform Settings, secret) over
`GEM_PROXY_URL` (env) -- `http://`, `https://` or `socks5://`, credentials in
the URL, empty means direct. When a bootstrap still cannot connect,
`gem.bootstrap` re-does the resolution and the TCP handshake itself
(`netconfig.diagnose_connect`) and puts the per-address OS error and the
setting to change into the `PortalUnavailable` message, which is what the
GeM Search page shows; with a proxy configured the message blames the proxy
and prints its host without its password. `tests/test_gem_proxy.py` pins the
resolution order, that no GeM client is built any other way, and the wording
of both failure messages.

### Tender analyzer v2 (vision-first PDF path)

`tender_analyzer_v2_enabled=True` is the default. Each PDF in a tender is sent as a Claude vision document block (handles scanned PDFs natively) to Haiku for per-doc extraction, then a Sonnet pass synthesizes. Important settings in `app/core/config.py`:

- `tender_analyzer_max_parallel=1` — **deliberately sequential**. Comments in `config.py` document why: multi-PDF parallel runs produced silent empty-synthesis failures from memory pressure + SQLAlchemy session contention. Raise via `TENDER_ANALYZER_MAX_PARALLEL` env var only after weighing this. Auto-chain prerequisite timeout (`_ensure_tender_analysis` in `chat_agent_wrappers.py`) is 480s, which fits ~3 PDFs at 60s each.
- `tender_analyzer_per_doc_max_pages=200` — Anthropic native PDF supports ~100 pages standard / ~600 on the document API beta.

### Global assistant + the write-confirmation gate

The `decision_maker` master agent is reachable from every page via
`GlobalAssistant` (mounted once in `drpl-frontend/src/components/layout/AppLayout.tsx`,
`Ctrl/Cmd+K`). It talks to the normal Command Center SSE endpoint against the
user's latest `ProposalSession` with `agent_type="global_assistant"`. “New chat”
creates a separate session; the popup's History view lists and switches among
them. These sessions stay excluded from the normal Command Center listing and
undeletable through its generic delete endpoint.

The model roster is two models, not a ladder. **Haiku 4.5** runs everything
that has to be right — the Master, tender analysis, PDF extraction, costing,
proposal and compliance drafting. **OpenAI Luna** runs work that is short and
structurally simple — scoring, classification, routing, checklists, letters,
scope extraction. `seed_agent_models.py` applies this once to existing rows
using `agent_model_allocation_version` (`haiku-luna-v1`), then preserves later
Agent Builder choices.

Three things load-bear here, all pinned by `tests/test_haiku_luna_allocation.py`:

1. **The roster and `TIER_MODELS` must agree.** `seed_agent_models` applies the
   roster and then, in the same call, walks every row again and moves anything
   off its tier's model. A roster entry the tier table would overwrite is
   applied and reverted on one startup, which looks exactly like the rollout
   never ran. Anthropic's worker and bulk tiers are therefore both Haiku.
2. **Config pins are outside the roster's reach.** `ai_model`,
   `master_agent_model`, `tender_analyzer_synthesis_model` and
   `annexure_transcription_model` live in `config.py`, and `ai_model` also has
   a `PlatformSetting` row that *beats* the config default in
   `_get_effective_model` — so `settings_service.DEFAULT_SETTINGS` and
   `_KNOWN_STALE_VALUES` have to move too. The stale map used to migrate
   haiku → sonnet; left that way it would have undone the rollout on every
   startup.
3. **`RESPECTED_LEGACY_MODELS` keeps the picker honest.** No tier targets
   Sonnet any more, so without it the refresh would read an admin's deliberate
   Sonnet choice as below-tier and overwrite it on the next restart. The
   rollout moves everyone off Sonnet once; it does not take Sonnet away.

`advisor_model` deliberately stays on the flagship tier: a second opinion from
the same model that asked for it is not a second opinion, and the API rejects
an advisor weaker than the executor.

**Per-run cost.** `APIUsageLog.run_id` is stamped from the ambient
`run_id_scope`, the same way `actor_context` stamps `user_id` — no call site
passes it. `run_cost_service` derives a run's spend as a `SUM` over the rows
that name it (never a separate counter, the rule `budget_service` follows), and
`attach_run_cost` writes it into the assistant `ProposalMessage.metadata_json`
at all five save sites in `streaming_handler`, so the number survives a reload;
the `done` SSE event carries the same payload so it appears live.
`GET /api/admin/usage/runs` is the master-admin-only per-run listing.

**`app/services/langchain/tool_policy.py` gates every write.** The agent stays
autonomous but any tool that changes user data suspends and waits for an
explicit confirmation. Three things to know before touching agent tooling:

1. **Adding a tool means classifying it.** `_READ_TOOLS` / `_WRITE_TOOLS` /
   `_DESTRUCTIVE_TOOLS` is a whitelist — an unclassified tool is gated as a
   write. That fails safe, but it means a new *read* will start demanding
   confirmations until you classify it. Two drift tests
   (`test_tool_policy.py`, `test_tool_policy_integration.py`) fail loudly on an
   unclassified tool; do not silence them.
2. **The gate is applied at two choke points**, because tools reach agents two
   ways: `run_decision_maker` (orchestrator catalog) and
   `tool_loader._apply_policy` (every specialist agent). A tool that bypasses
   both is an ungated write.
3. **A gated tool called with no `policy_scope` open refuses to run.** There is
   no way to ask the user, so executing would be the silent write the module
   exists to prevent. `streaming_handler` opens the scope (and `run_id_scope`)
   at each execution point.

Turn it off with `ASSISTANT_CONFIRM_GATE_ENABLED=false` — no deploy needed.

**Tools are a shared repo, not per-agent allowlists.** An empty
`CustomAgent.tools` means the agent reaches *every* registered tool, not none —
see `tool_loader.resolve_agent_tool_keys`. The old default was the opposite,
which is how `costing_researcher` ended up bound to zero tools while its
canonical registry declared eight. Two exceptions: an explicit assignment still
wins (narrowing is deliberate), and `TOOL_FREE_AGENTS` (scoring, classification,
routing) stay tool-free because they make one short call at ~750/hour and the
catalog is ~7,900 tokens of schema. The Master Agent can also grant a worker
extra tools for a single call via `extra_tools` on `call_<agent>`; grants are
validated against the repo and still pass through the write gate. Adding a tool
means adding it to `_TOOL_CLASS_REGISTRY` **and** `SYSTEM_TOOLS` — a test fails
if a tool can run but cannot be assigned.

### The capability registry (read this before touching any agent's tools)

`app/services/langchain/capability_registry.py` is the single source of truth for
every agent capability: its build kind (`class` instantiated by the loader vs
`factory` closed over request context), its risk tier, its minimum role, its
surfaces, its domain, and — authored — its manual text (`purpose` / `use_when` /
`not_for` / `produces`). `tool_policy`'s tier sets, `tool_loader`'s class
registry, and `agent_tools_service.SYSTEM_TOOLS` are all **derived** from it.
Adding a capability is one registry entry (plus an activity label in
`activity_labels.py`); drift tests fail on an entry with no manual text, an
unimportable target, or a runnable-but-unassignable key. The canonical graphs no
longer carry `default_keys` literals — a source-level test keeps them out — and
`resolve_tool_keys` honours an Agent Builder assignment whole instead of
intersecting it away (`resolve_unknown_tool_keys` reports keys nothing
implements).

### The Master Agent is the chat entrypoint

`chat_engine` (PlatformSetting, read live) decides who answers Command Center
chat: `master` (default) sends every message to `decision_maker`; `router`
restores classify-then-dispatch exactly. The Master's catalog is two-tier
(`build_master_catalog`): the deduplicated worker roster (`WORKER_ALIASES`
collapses the legacy analyzer/checklist/proposal alias rows) plus cross-cutting
reads bound directly, everything else through the `use_capability` dispatcher
(`capability_dispatcher.py`) — which is pinned by tests as *not* a bypass of the
role check or the write gate, and is itself classified READ because the resolved
tool carries its own gate. Its prompt opens with the Platform Capability Manual
(`render_capability_manual`) and the delegation doctrine (`DELEGATION_DOCTRINE`):
produce vs explain, costing is never done by the Master itself, answer the
question asked without launching unrequested production jobs. A parity test
asserts by enumeration that the catalog is a superset of everything the old
five-builder assembly offered.

The Master's run budget is `master_agent_max_iterations` (25) /
`master_agent_max_execution_time_s` (1800) in `config.py`, not literals in the
graph, and `render_budget` substitutes the real numbers into the prompts. Its
*output* budget is `master_agent_max_output_tokens` (16k, clamped per model):
`create_react_agent` runs its own message loop, so `safe_ainvoke`'s
auto-continuation never sees a Master call, and an answer that hits the ceiling
would otherwise be returned as though it were whole. `run_decision_maker` reads
the final message's `stop_reason` and says so instead. The
budget has to outlast the longest thing the Master delegates — a single
`_ensure_tender_analysis` is allowed 1200s — because the wall-clock is enforced
by `asyncio.wait_for` around the whole graph: exceeding it cancels a worker
mid-flight. When this was 120s, any request that produced real work (analyze,
then cost) died there and told the user to continue, which restarted the run
into the same wall. On a genuine timeout the run now returns
`streamer.partial_trace` and names the steps that completed instead of
discarding them (`render_partial_timeout_message`).

**A worker's answer is the deliverable, and the handoff has a budget.** The
Master's message is what the user reads, so anything a delegation drops on the
way back is work the platform did correctly and then did not say. The
delegation used to return `result["output"][:1500]` under the name
`output_preview` — a 30,000-character forensic analysis or an item-wise costing
narrative reached the Master as its opening paragraphs, and the Master
summarized those. Three rules now:

1. **The budget is `master_worker_output_max_chars` (20,000), not a literal**,
   and the field is `output`, not `output_preview` — a model told it holds a
   preview has no reason to look for the rest.
2. **What does not fit is retrievable, not gone.** `WorkerOutputStore` holds
   the full text for the run; the result carries `output_complete`,
   `output_handle` and `next_offset`, and `read_worker_output` pages through
   the remainder. Store and reader are created together in
   `build_master_catalog` — a reader over a different store reports every
   handle the Master was just handed as unknown. `build_agent_wrapper_tools`
   stays workers-only because Agent Builder enumerates it as the assignable
   roster.
3. **`_safe_dump` trims fields, never the serialized string** — and never
   returns nothing when it could return something. It used to slice
   `json.dumps(payload)[:8000]`, so a large `structured_data` — a 375-row
   costing crosses that easily — reached the model as JSON cut mid-token and
   was read as fact rather than as damage. Fields are allocated a max-min fair
   share of the budget in a single pass (`_fit_mapping`): one pass, because an
   iterative largest-first trim re-trims the same field and then reports the
   second cut against the first cut's length. A list keeps whole leading
   entries and counts the rest; a mapping is replaced whole, because half a
   line-item map reads as the complete breakdown. Fields too small to say
   anything are dropped whole and named in `_fields_omitted` — spreading 2,000
   characters across 120 fields buys 120 markers and no data. `structured_data`
   has its own `_STRUCTURED_DATA_BUDGET` so it is not simply the largest field
   the trimmer reaches for first. Callers pass their limit *into* the dump;
   slicing its output re-creates the original bug.

The budget alone would not have been enough — a Master told to summarize will —
so all three prompts carry the relay rule: reproduce the worker's substance,
and never write a final answer from a partial output without saying so.
`tests/test_worker_output_handoff.py`.

**The Master remembers the conversation, and the window is the recent end of
it.** Three defects made "use what we discussed earlier" unanswerable, each
sufficient on its own:

1. `memory_service.get_conversation_history` ordered `created_at ASC` and *then*
   applied the LIMIT, so it returned the **oldest** N turns. Past turn 20 the
   agent's window stopped advancing — it re-read the opening of the chat on
   every message. The same query backs `GET /command-center/sessions/{id}/history`
   at limit 50, so a long session's own transcript hid the user's most recent
   messages from them on reload. It now orders DESC, limits, and reverses, with
   `id` as the tiebreaker because turns saved in one request share a timestamp.
2. `run_decision_maker` accepted `conversation_history`, forwarded it to its
   worker tools, and invoked its graph with a single `HumanMessage`. The Master
   is the default `chat_engine`, so the platform's assistant had no memory at
   all — every turn was turn one. `chat_messages.build_conversation_messages`
   now owns the message list for **both** chat agents (the generalist had its
   own copy of half these rules) and guarantees three things, each of which
   fails only once a conversation is long enough or a run has failed: the list
   begins with a user message (a window can open mid-exchange, and Anthropic
   and Google reject a leading assistant message); no two consecutive messages
   share a role (`streaming_handler` saves the user turn on arrival and the
   assistant turn on completion, so a failed run leaves an unanswered user turn
   — and the next message would then send two user messages in a row, which
   Gemini rejects, turning one failed job into every later message failing);
   and the history costs at most `master_history_max_total_chars`, oldest turns
   dropped first, because `master_history_max_turns` × `..._max_chars_per_turn`
   alone is 48,000 characters of prompt before the request is read.
3. Nothing could reach a turn older than the window — `memory_retrieve` reads
   `AgentMemory`, curated facts, not the transcript — so an explicit "what did I
   say about X" was answered from what happened to be visible.
   `conversation_history_search` searches and pages back through the current
   session. Its session is bound by the loader from the run, never named by the
   model, the way `clarify` is: that binding *is* the firewall, so there is no
   argument by which an agent could read another conversation.

`tests/test_conversation_history_reasoning.py`.

`confirm_gate_scope` (PlatformSetting): `all_writes` is the shipped default and
what an ABSENT row resolves to — loosening to `destructive_only` (ordinary
writes run uninterrupted but audit-logged via `audit_ungated_write`; destructive
still confirms) is an owner's explicit admin-panel choice, never a deploy side
effect. An unregistered tool gates under both scopes.

### The Command Center's general assistant

`general_assistant` (`app/services/langchain/graphs/general_assistant_agent.py`)
handles every message the intent classifier does not place as a specialist job:
research, drafting an email or letter, ad-hoc calculations, and questions about
output the platform already produced. It is a `create_react_agent` with an
explicit twelve-tool belt (`GENERAL_ASSISTANT_TOOL_KEYS`), seeded as a
`CustomAgent` so its prompt and tools are editable in Agent Builder.

Three things to know before changing it:

1. **The belt is deliberately explicit, not the whole repo.** The full catalog is
   ~7,900 tokens of schema and large tool sets measurably degrade tool selection.
   A new capability belongs in the belt (and in `tool_policy`), not in a new agent.
2. **Both general paths must stay collapsed.** `agent_router_graph.general_response_node`
   and the general branch of `streaming_handler` both call `run_general_assistant`.
   They were previously two copies of a toolless LLM call that had already drifted
   apart; that is how the platform shipped an assistant which invented its sources.
3. **The generalist may not call itself.** It is on the worker roster (the
   Master delegates cheap conversational work to it), so its own `_build_tools`
   excludes `call_general_assistant` from its belt.

It streams a `token_reset` SSE event when a model turn ends in a tool call. The
Command Center page concatenates `token` events into the string it saves, so
without the reset a ReAct agent's pre-tool narration is persisted as part of the
answer. All four of the page's SSE consumers handle it.

### Tools and the database session

`tool_loader` injects a `db` session only into tools that declare one, decided by
`_wants_db()`. It must check `model_fields`, not `hasattr`: these tools are
Pydantic v2 models, where a declared field is *not* a class attribute. When this
check was `hasattr(tool_cls, "db")` it was False for every tool in the registry,
so every tool ran with `db=None` — silently, because the tools treat a missing
session as "no database" and degrade. The visible symptom was months of poor web
search: `web_search` could only read API keys from `.env`, never from
`PlatformSetting`, so it fell past Gemini grounding and Tavily to DuckDuckGo on
every call. Keep `tests/test_tool_db_injection.py` green.

That one session is also held for the **whole** run — minutes of model calls
between tool calls — and `SessionLocal` is `autocommit=False`, so a read opens
an implicit transaction nothing closes. Neon ships
`idle_in_transaction_session_timeout = 5min` and reaps exactly that, and the
next tool call dies with `SSL connection has been closed unexpectedly`, shown
to the user as "A temporary database error occurred." `pool_pre_ping` and
`pool_recycle` in `database.py` cannot help: both act at *checkout*, and this
connection was already checked out. `db_recycle.recycle_session_between_calls`
ends the transaction after every tool call, applied immediately outside the
write gate at the two assemblers (`tool_loader._apply_policy` and
`build_master_catalog`) — a tool reaching an agent through neither is a tool
that can still strand a connection. It is deliberately outside the gate's
`enabled` switch, it never rolls back a session carrying pending work, and its
wrapper delegates unknown attributes to the inner tool so injected `db` /
`agent_key` stay readable. `tests/test_db_session_recycling.py`.

**Role bounding is separate from the gate.** The gate asks the user whether to
proceed; it does not ask whether they are *allowed* to. `platform_tools.py`
declares a minimum role per tool, enforced both at build time (omitted from the
catalog) and at call time (the actual control), with the role read from the
database inside `run_decision_maker` — never from the request. Secrets
(`PlatformSetting.is_secret`) are redacted on read and rejected on write.

`quality_tools.diagnose_tender_outputs` checks generated output against this
repo's known failure modes (empty synthesis, annexures paraphrased into
`[Name of Bidder]`-style placeholders, blank documents, failed runs) using
deterministic Python, not an LLM grading its own work.

Designs: `docs/superpowers/specs/2026-09-02-global-assistant-and-confirm-gate-design.md`
and `docs/superpowers/specs/2026-09-02-platform-tools-and-quality-diagnosis-design.md`.

### Roles, the per-user firewall, and usage budgets

`app/core/roles.py` is the single table of roles → surfaces: `master_admin`
(everything), `tender_search` (the platform minus `/admin/*`), `costing_research`
(Ask DRPL, tenders, dashboard, tender workspace, cost breakdowns + XLSX, archive,
notifications). Legacy `admin`/`operator` values are **mapped forward by
`normalize`, not migrated** — live users hold them. An unrecognised role resolves
to the *narrowest* surface. `require_surface("<surface>")` is the backend gate,
applied at router level so a new route cannot be added ungated; `src/lib/roles.ts`
mirrors the table for nav and route guards and is **cosmetic only**
(`localStorage.drpl_role` is user-editable).

`app/core/ownership.py` decides who may read a row. The split is *fact about a
tender* vs *somebody's work*: tenders, their documents, analysis summaries,
checklists and annexures are SHARED (walling them would bill the same PDFs to
every user's budget); sessions, cost breakdowns, generated documents,
signatures and runs are OWNED, per individual user. It fails closed — no actor
means no rows, and a row with a NULL owner is master_admin's alone, because
guessing an owner could hand one user another's work. Two drift tests fail on an
owned table that is in neither map; unlike an unclassified *tool*, whose failure
mode is a needless confirmation, an unclassified *table* leaks in silence.

**The firewall's sharp edge is not HTTP.** Agent tools query with a raw `db`
session and no notion of who is asking, so a tool reading an owned model must
call `scoped_query(db, Model, current_actor())` — see `cost_breakdown_tool`. The
Command Center's own endpoints go through `_load_owned_session`, which delegates
to `artifact_authz.assert_session_access` (403 for a non-owner, 404 for absent)
rather than `ownership.assert_can_read` (404 for both): the latter is the better
posture but the platform already documents and tests the former, and two answers
to one question is worse than either.

`app/core/actor_context.py` carries the acting user into the agent layer the way
`run_context` carries the run id, and for the same reason — the code that needs
it has no request. `streaming_handler` opens it at all three run entry points;
the RQ path inherits it. **Both features fail open without it**, so a new
background entry point that runs agent work should open an `actor_scope`.

Budgets: `budget_service` derives spend as `SUM(cost_estimate)` over
`APIUsageLog` for the calendar month — never a stored counter that can drift
from its own ledger. `UserBudget` holds only policy (NULL limit = the platform
default, so the common case needs no row). Bands: ok / warning at 80% / exceeded
at 100%. `assert_within_budget` runs **once at run entry, never per LLM call** —
killing a run mid-flight discards everything it already spent — so overshoot by
one run is accepted and reported honestly while `percent_used` caps at 100.
master_admin is metered but never blocked. `BUDGET_ENFORCEMENT_ENABLED=false`
meters without blocking. A source-level drift test fails if a new run entry
point ships ungated. Attribution comes from `ai_service._log_usage`, which reads
the ambient actor; `provider_config.estimate_cost` prices cache reads at 0.1x
and cache writes at 1.25x (it used to ignore both, overstating cost).

Designs: `docs/superpowers/specs/2026-09-03-role-based-access-and-per-user-firewall-design.md`
and `docs/superpowers/specs/2026-09-03-usage-metering-and-budget-caps-design.md`.

### Storage

`storage_backend` config switches between local filesystem (`./uploads/`) and Cloudflare R2 (S3-compatible). All file IO must go through `app/services/storage_service.py` so both backends keep working — never write to `uploads/` directly.

### Tender archive sweep

`app/services/tender_archive_service.py` implements the archive/purge lifecycle for tenders. Two columns on `tenders`: `archived_at` (tz-aware `DateTime`, nullable, indexed) and `archive_reason` (`String(30)`, nullable: `'past_due' | 'auto_discard' | 'manual'`).

- `archive_candidates_query()` selects tenders that are past-due (`closing_date` older than `archive_grace_days`, default **3**), still `workflow_status == "new"`, unassigned, not `segment_overridden`, and have no row in any `WORK_ARTIFACT_TABLES` table (cost breakdowns, proposal sessions, checklist items, workspace configs, BOQ items, generated documents, etc. — `tender_documents` is deliberately excluded since document capture is automatic, not evidence of human work). `run_archive_pass()` flags a batch of these `is_archived=True`, `archive_reason='past_due'`.
- `purge_candidates_query()` / `run_purge_pass()` hard-delete (via `delete_tenders_deep()`) tenders archived `archive_reason='past_due'` more than `archive_purge_days` (default **7**) ago. **Only `past_due` rows are ever purged** — `auto_discard` and `manual` archives, and legacy rows with `archive_reason IS NULL`, live indefinitely.
- **`archive_purge_days <= 0` means NEVER PURGE** (archive-only mode): `purge_candidates_query` short-circuits to an empty set. Taken literally, `0` would compute a cutoff of `now` and delete every archived row — the most destructive reading of the number an admin is most likely to type meaning "off" — so it deliberately means the opposite. This is the recommended setting when the archive backlog is large and you want tenders out of the way without losing their documents.
- The self-rescheduling RQ job is `drpl-archive-sweep` (`run_archive_sweep` in `app/worker/scheduled_tasks.py`), seeded by `seed_scheduled_jobs.py` and re-enqueued after every tick regardless of outcome. It ships **disabled** (`archive_sweep_enabled=False` in `app/core/config.py`); other defaults: `archive_sweep_interval_hours=6`, `archive_sweep_batch_size=500`. Run `scripts/archive_sweep_dryrun.py` (read-only — never calls `run_archive_pass`/`run_purge_pass`/`delete_tenders_deep`, never commits) against production before flipping the flag.
- Three endpoints in `app/api/routes/tenders.py`: `GET /tenders/archive` (list), `POST /tenders/{id}/restore` (sets `segment_overridden=True` so the row isn't immediately re-archived), `POST /tenders/archive/purge-now` (manual trigger).
- `below_threshold` tenders (sub-threshold rows, hidden from the default tender list) **are** in scope for both archiving and purging. A sub-threshold tender can therefore be permanently deleted having never been seen in the UI. This is intended.
- See the "Tender child tables" invariant in [drpl-backend/CONSTITUTION.md](drpl-backend/CONSTITUTION.md) before adding any new table with a `tender_id` column — the purge and the sweep's work-artifact guard both need to know about it.

#### Operator runbook: enabling the sweep in production

This is the authoritative enablement procedure (the implementation plan under `docs/superpowers/plans/` points here). Enabling this hard-deletes production data on a 7-day delay, so do it deliberately.

1. **Dry run first.** `cd drpl-backend && python scripts/archive_sweep_dryrun.py` against the target database. It is read-only — it never calls `run_archive_pass` / `run_purge_pass` / `delete_tenders_deep` and never commits. Read the "WOULD ARCHIVE" / "WOULD DELETE" counts and the sample rows.
2. **Where the switch is.** Frontend → **Admin → Tender Scoring Agent** (`/admin/tender-scoring`, master-admin only), section **"Archive & Purge Sweep"**. That section holds all five settings: `archive_sweep_enabled`, `archive_grace_days`, `archive_purge_days`, `archive_sweep_interval_hours`, `archive_sweep_batch_size`. They persist through `PUT /api/admin/tender-scoring/settings` into `PlatformSetting` and are read live by the job — no deploy needed to flip the flag.
3. **Expect the dry-run count to be lower than you predict.** The archive predicate also excludes any row with `segment_overridden` set (`tender_archive_service.py:279-280`). That filter exists so Restore sticks, but it applies to **every** tender whose segment a human ever set — not only restored ones. So "past-due and untouched" over-counts what the sweep will actually take. The gap is the filter working, not the sweep being broken.
4. **Expect the backlog to drain slowly.** At most `archive_sweep_batch_size` (500) rows are archived per tick, and ticks are `archive_sweep_interval_hours` (6) apart — ~2,000 rows/day. A five-figure backlog takes days. Counts dropping slowly is normal.
5. **`guard_ok` is the health signal.** A broken work-artifact guard and a genuinely empty queue both report `archived: 0` — they are indistinguishable from the count alone. After the first enabled tick, check the job result / log line at `app/worker/scheduled_tasks.py:339-343` and confirm it reads `guard_ok=True`. `guard_ok=False` means the batch was archived-zero *by force*, and the log says so explicitly.
6. **The first purge lands `archive_purge_days` (7) after the first archive run** — there is a week's window to review the Archive page and Restore anything wrong before anything is deleted.

### Chrome extension build quirks

Manifest V3 content scripts cannot use ES module imports — they run as plain scripts. The popup and service worker *can* use modules. `drpl-extension/vite.config.ts` has a custom `bundle-content-scripts` plugin in `closeBundle`: it scans the emitted `dist/content-scripts/*.js`, inlines any chunk imports via an IIFE pattern, removes them, and deletes chunks not also used by the popup/service worker. If you add a new content script or shared util used by content scripts, make sure it still ends up self-contained after build — verify there are no `import` statements left in `dist/content-scripts/*.js`.

`src/config/selectors.json` is shipped inside the extension **and** served OTA by the backend so selectors can be updated without a Chrome Web Store re-review. Treat any change here as needing a backend push too. The extension's host_permissions list (`public/manifest.json`) is the source of truth for which portals it touches: IREPS, GeM, mkp.gem, plus aggregators (tendertiger, bidassist, tenderdetail, tendersinfo, projectstoday).

Three content-script entry points correspond to three portal families: `ireps.ts`, `gem.ts` (also handles `gem-search-driver.ts`), `aggregators.ts`. The `service-worker.ts` is the only piece that talks to the backend; content scripts message it via `chrome.runtime.sendMessage`.

### Frontend auth + routing

`src/router.tsx` uses three guards: `ProtectedRoute` (any logged-in user), `AdminRoute` (`admin` or `master_admin`), `MasterAdminRoute` (master_admin only). All `/admin/*` routes are master-admin only. The role is read from `localStorage.drpl_role`, set at login.

The Command Center (`/command-center/:sessionId`) and per-tender Workspace (`/tenders/:id/workspace/*`) are the two large multi-component features — when touching either, check `src/components/command-center/` and `src/components/workspace/`.

### Run-ID logging

`app/core/run_context.py` installs a logging filter that stamps `[run=<first8>]` onto every log line emitted inside a `run_id_scope(run_id)` context manager. Use this when adding new background-task entry points so logs are correlatable across the graphs + tools + DB writes.

## Constitution layer (System Pilot)

Per-package Constitutions (rules, schemas, invariants) and SOPs live alongside each package:

- [drpl-backend/CONSTITUTION.md](drpl-backend/CONSTITUTION.md) + [drpl-backend/architecture/](drpl-backend/architecture/) + [drpl-backend/execution/probes/](drpl-backend/execution/probes/)
- [drpl-frontend/CONSTITUTION.md](drpl-frontend/CONSTITUTION.md) + [drpl-frontend/architecture/](drpl-frontend/architecture/) + [drpl-frontend/execution/probes/](drpl-frontend/execution/probes/)
- [drpl-extension/CONSTITUTION.md](drpl-extension/CONSTITUTION.md) + [drpl-extension/architecture/](drpl-extension/architecture/) + [drpl-extension/execution/probes/](drpl-extension/execution/probes/)

This `CLAUDE.md` stays focused on "how to work in this codebase." The Constitutions encode the load-bearing rules (determinism boundary, mandatory run-ID logging, schema drift discipline) with refusal triggers; SOPs document each surface's goal / inputs / outputs / determinism boundary / failure modes / verification command.
