# Backend — Findings

Code anchors from the 2026-05-18 exploration pass that informed the SOPs. Update as new anchors are discovered.

## Tender analyzer v2

- Graph: `app/services/langchain/graphs/document_analysis_agent.py`
  - `_analyze_single_document_native` (lines 852-986) — per-doc Haiku extraction
  - `_run_synthesis` (lines 989-1092) — Sonnet synthesis
  - `run_document_analysis` (line 241) — entrypoint, NOT wrapped in `run_id_scope` as of pass start
- Auto-chain: `app/services/langchain/chat_agent_wrappers.py:66-220` — `_ensure_tender_analysis`, 480s timeout
- Config: `app/core/config.py:74-98` documents the `tender_analyzer_max_parallel=1` rationale
- Swallow points: lines 271-275 (v1 fallback), 1616-1633 (synthesis exception), 1650-1684 (empty markdown fallback), 1691-1701 (cost aggregation)

## Costing pipeline

- Graphs: `app/services/langchain/graphs/costing_agent.py` + `enhanced_costing_agent.py`
- Canonical registry: `app/services/langchain/canonical_registry.py:45-65`
- Tools: `cost_calculator_tool.py`, `costing_training_retrieval_tool.py`, `anonymizing_web_search_tool.py`, `xlsx_generator_tool.py`
- **Determinism violation (LLM does arithmetic):** `costing_agent.py:20-29` — `amount = quantity × rate` instructed in the prompt
- Dedicated logger: `app/main.py:20-42` → `logs/costing_run.log`

## Agent runs + RQ

- Enqueue: `app/api/routes/runs.py:138-301` (wraps `run_id_scope` at 290-294)
- RQ entry: `app/worker/run_tasks.py:45-50` (wraps `run_id_scope` at line 49 — gold-standard reference)
- SSE: `app/api/routes/runs.py:338-415`
- Capacity: `app/main.py:560-638` (`GET /health/capacity`)
- Reaper: `app/main.py:379-390` (`_reap_stuck_executions_on_startup`)
- Swallow points: `run_tasks.py:98-101` (pump exception → failed), 111-114 (status-write swallow), 129-134 (slot release swallow); `runs.py:173-178` (capacity pre-check soft-fail)

## Cross-cutting infrastructure

- `app/core/run_context.py`:
  - `run_id_scope(run_id)` context manager (lines 28-35)
  - `RunIdFilter` (lines 38-51), `install_run_id_filter()` (57-66)
- `app/services/storage_service.py`:
  - `upload_file` / `download_file` / `delete_file` / `file_exists` (async + sync pairs, lines 84-190)
  - `get_presigned_url` (192-208), `as_local_file` (210-243)
  - Backend selected by `settings.storage_backend` (line 37)
- LLM dispatch:
  - `llm_factory.get_chat_model()` (lines 226-310)
  - `provider_config.detect_provider()` (108-119), `build_fallback_chain()` (177-253)
  - `failover_model.FailoverChatModel` — auto-advances on 429 (lines 44-256)
- Alembic: 31 revisions; most recent `20260515_tender_analysis_structured.py`, `20260515_pdf_max_pages_200.py`
- Schema drift: `app/main.py:103` (`_apply_schema_drift_fixes`), `app/main.py:394` (`_add_missing_columns`)

## Tests

- `tests/test_api.py` only — 4 smoke tests (`test_health`, `test_root`, `test_extension_config_requires_auth`, `test_tender_upload_requires_auth`)
- Load test: `scripts/load_test_command_center.py` exercises enqueue + SSE + `/health/capacity`
