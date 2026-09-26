# Backend Constitution

System Pilot artifact for `drpl-backend/`. Onboarding lives in [../CLAUDE.md](../CLAUDE.md); this file is the Constitution layer — the rules that govern how agentic work is written, deployed, and changed.

---

## Data Schemas (Input → Output)

The four shapes the backend owns.

### 1. Extension ingest — `POST /api/extension/tenders`

Defined at [app/api/routes/extension.py:37-66](app/api/routes/extension.py). Dedup key: `(portal, tender_id)` at [app/services/tender_service.py:36-40](app/services/tender_service.py).

```jsonc
// Input
{
  "tenders": [
    {
      "portal": "ireps",                 // ireps | gem | tendertiger | bidassist | tenderdetail | tendersinfo | projectstoday
      "tender_id": "string",             // upstream tender identifier
      "title": "string",
      "url": "string",
      "closing_date": "ISO-8601 | null",
      "estimated_value": "number | null",
      // …deep-scrape fields per TenderInput
    }
  ]
}

// Output
{
  "received": 12,
  "new": 8,
  "duplicates": 4,
  "errors": [],
  "new_ids": [101, 102, 103, ...]        // picked up by the auto-scoring reaper (score_tenders_batch)
}
```

### 2. Agent run enqueue — `POST /api/runs/enqueue`

Defined at [app/api/routes/runs.py:138-301](app/api/routes/runs.py). Wraps `run_id_scope(run.id)` at lines 290-294.

```jsonc
// Input
{
  "agent_key": "costing_researcher | tender_doc_analyzer | ...",
  "session_id": 123,
  "tender_id": 456,
  "message": "string",
  "attachments": []
}

// Output
{
  "run_id": "uuid",
  "status": "queued",
  "stream_url": "/api/runs/{run_id}/events"
}
```

### 3. SSE event stream — `GET /api/runs/{run_id}/events`

Defined at [app/api/routes/runs.py:338-415](app/api/routes/runs.py). Consumer contract is the source of truth — frontend reads from this exact shape.

```
event: token
data: {"delta": "...", "ts": 1700000000}

event: tool_call
data: {"name": "cost_calculator", "args": {...}, "id": "..."}

event: tool_result
data: {"id": "...", "output": "..."}

event: status
data: {"status": "succeeded | failed | cancelled", "result": {...}}
```

### 4. R2 object key convention

All file IO routes through [app/services/storage_service.py](app/services/storage_service.py). Keys are namespaced:

```
tenders/<tender_id>/documents/<sha256>.<ext>
agent-runs/<run_id>/artifacts/<filename>
workspace/<tender_id>/<doc_id>/<version>.html
exports/<user_id>/<timestamp>-<filename>.<xlsx|docx|pdf>
_probe/<random>                            # used by execution/probes/r2.py only
```

No code outside `storage_service` may construct an S3 key or call boto3 directly.

---

## Behavioral Rules (enforceable, with refusal triggers)

### Rule 1 — Determinism boundary

**LLMs do not compute business logic.** Money, dedup keys, IDs, page counts, file paths, and any value that downstream code consumes as canonical truth MUST be produced by a deterministic Python tool, not by an LLM emitting JSON.

- **Refusal trigger.** Refuse to ship a graph or prompt where the LLM is instructed to compute arithmetic (totals, multiplications, percentages), assign primary keys, or compute dedup keys.
- **Reference example.** [app/services/langchain/graphs/costing_agent.py](app/services/langchain/graphs/costing_agent.py) — the prompt now mandates a `cost_calculator` invocation with `(overhead_percent=0, margin_percent=0, gst_percent=0)` and requires the LLM to transcribe `amount` from `cost_calculator.line_amounts[*].amount_expected`. See [architecture/sop-costing-pipeline.md](architecture/sop-costing-pipeline.md).
- **Why.** LLMs hallucinate decimals, drift across long contexts, and disagree with their own prior tokens. Money matters; transcription is checkable.

### Rule 2 — Run-ID logging always on

**Every background entry point wraps `run_id_scope(run_id)`.** A background log line without a `[run=…]` stamp is a defect.

- **API:** `from app.core.run_context import run_id_scope` — see [app/core/run_context.py:28-35](app/core/run_context.py).
- **Refusal trigger.** Refuse a new background-task function, RQ job, or graph entrypoint that does not open its work inside `with run_id_scope(...)`.
- **Gold-standard reference.** [app/worker/run_tasks.py:49](app/worker/run_tasks.py) — RQ entrypoint wraps the entire inner function.
- **Known gap.** As of 2026-05-18, `document_analysis_agent.run_document_analysis` and the costing graphs only inherit `run_id_scope` from upstream callers. These entrypoints are being wrapped in this pass.

### Tender child tables

Adding a table with a `tender_id` column requires adding it to `CHILD_TABLES`
in [app/services/tender_archive_service.py](app/services/tender_archive_service.py),
or the archive purge (`delete_tenders_deep()`) will orphan its rows. If the
table represents human work on a tender, also add it to `WORK_ARTIFACT_TABLES`
— otherwise the archive sweep will archive tenders someone was actively
working on (`tender_documents` is deliberately excluded from
`WORK_ARTIFACT_TABLES`: the extension and the backend NIT fetcher capture
documents automatically, with no human intent, so their presence is not
evidence of work).

Some tables link to a tender only *indirectly* — via a parent row's id, not a
`tender_id` column of their own (e.g. `cost_breakdown_lines` → `cost_breakdown_id`
→ `cost_breakdowns.tender_id`; `proposal_messages`/`proposal_documents`/`proposal_reviews`
→ `session_id` → `proposal_sessions.tender_id`; `extraction_feedback` →
`extraction_result_id` → `document_extraction_results.tender_id`). These
grandchild tables are **not** added to `CHILD_TABLES` — they can't be, since
the loop there does a flat `DELETE ... WHERE tender_id IN :ids`. Instead they
are cleared with explicit subquery `DELETE`s in `delete_tenders_deep()`,
executed *before* the `CHILD_TABLES` loop (which deletes their parent rows out
from under them). If you add a new grandchild table, follow this subquery
pattern rather than trying to force it into `CHILD_TABLES`.

Only `archive_reason == 'past_due'` rows (`PURGEABLE_ARCHIVE_REASON` in the
same file) are ever auto-purged by `run_purge_pass()` /
`purge_candidates_query()`. Rows archived by auto-discard, archived manually,
or predating the feature (`archive_reason IS NULL`) survive forever — do not
relax this filter or wrap it in an `or_`.

`segment_overridden` exempts a tender from `archive_candidates_query()` (and
therefore from the sweep). This is what makes `POST /tenders/{id}/restore`
stick: restore sets `segment_overridden = True`, so the next sweep tick does
not re-archive the row and restart its purge clock.

### Rule 3 — Self-healing seeders + schema drift

**New columns require Alembic AND a drift-fix entry.** New system agents require an idempotent seeder called from `main.py`.

- **Refusal trigger.** Refuse a PR adding a column via Alembic only — Railway deployments roll forward without running `alembic upgrade head` in lock-step, so a missing drift-fix entry breaks `_add_missing_columns` self-healing.
- **Anchors.** `_apply_schema_drift_fixes()` at [app/main.py:103](app/main.py); `_add_missing_columns()` at [app/main.py:394](app/main.py).
- **Agent seeders.** Idempotent only — they must re-sync `system_prompt`/`tools` only when `is_user_customized=False` (see [seed_costing_researcher_agent.py](app/services/seed_costing_researcher_agent.py) pattern).

---

## Architectural Invariants

These are not preferences — they are the load-bearing rules.

1. **File IO only via `storage_service`.** Never write to `./uploads/` or call boto3 from a graph or tool. ([storage_service.py](app/services/storage_service.py))
2. **LLM dispatch only via `llm_factory.get_chat_model`.** Never instantiate `ChatAnthropic` / `ChatOpenAI` / `ChatGoogleGenerativeAI` directly in a graph. ([llm_factory.py:226-310](app/services/langchain/llm_factory.py))
3. **Background entrypoints wrap `run_id_scope`.** See Rule 2 above.
4. **Schema changes are Alembic + drift-fix.** See Rule 3 above.
5. **New agents are idempotent seeders called from `main.py`.** See Rule 3 above.
6. **Tender analyzer v2 stays sequential (`tender_analyzer_max_parallel=1`).** The override risk is documented in [architecture/sop-tender-analyzer-v2.md](architecture/sop-tender-analyzer-v2.md). Changing this requires updating the SOP and tagging the decision in `memory/decisions.md`.
7. **Tender archive/purge invariants.** See "Tender child tables" above: `CHILD_TABLES` and `WORK_ARTIFACT_TABLES` in `tender_archive_service.py` must stay in sync with the schema, only `archive_reason='past_due'` is ever auto-purged, and `segment_overridden` is the sole mechanism that makes Restore stick.

---

## B.L.A.S.T. Phase Outputs

| Phase | Output |
|---|---|
| **B — Blueprint** | North Star, integrations, source of truth, payload, behavioral rules — captured in [memory/task_plan.md](memory/task_plan.md). |
| **L — Link** | Probe scripts under [execution/probes/](execution/probes/) — anthropic, openai, google, postgres, redis, r2. Each exits non-zero on failure and writes a one-line status to [memory/progress.md](memory/progress.md). |
| **A — Architect** | SOPs under [architecture/](architecture/) — one per surface (tender-analyzer-v2, costing-pipeline, agent-runs-rq). LLM dispatch + storage + run-context are infrastructure, not surfaces — they live in this Constitution. |
| **S — Stylize** | Output formatters live in their existing services (`cost_breakdown_service`, `xlsx_generator_tool`, `format_template` system). No new formatting layer added by the System Pilot pass. |
| **T — Trigger** | Web (uvicorn) and worker (`worker.py`) processes on Railway. Triggers are HTTP requests and RQ jobs respectively. Self-healing repair loop: on failure, patch + verify + update the relevant SOP under `architecture/`. |

---

## Pointers

- SOPs: [architecture/README.md](architecture/README.md)
- Memory: [memory/MEMORY.md](memory/) (task_plan, findings, progress, decisions)
- Probes: [execution/probes/](execution/probes/)
- Onboarding: [../CLAUDE.md](../CLAUDE.md)
