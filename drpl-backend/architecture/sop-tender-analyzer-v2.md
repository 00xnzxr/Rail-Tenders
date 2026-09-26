# SOP — Tender Analyzer v2 (vision-first PDF path)

## Goal

Extract structured analysis from every PDF attached to a tender, then synthesize a single tender-level analysis. The v2 path sends each PDF as a Claude vision document block to Haiku for per-doc extraction, then a Sonnet pass synthesizes across docs.

`tender_analyzer_v2_enabled=True` is the default (config flag — see [../app/core/config.py](../app/core/config.py)).

## Inputs

- **Trigger.** `_ensure_tender_analysis(tender_id)` at [../app/services/langchain/chat_agent_wrappers.py:66-220](../app/services/langchain/chat_agent_wrappers.py). Called as a prerequisite by any agent that needs tender analysis (auto-chained).
- **Entrypoint.** `run_document_analysis(db, tender_id, ...)` at [../app/services/langchain/graphs/document_analysis_agent.py:241](../app/services/langchain/graphs/document_analysis_agent.py).
- **Payload.** Tender row + every TenderDocument row attached to that tender. PDFs are read from R2/local via `storage_service.as_local_file()`.

## Outputs

- **Persisted.** `tender.analysis_result` (JSONB) — the synthesized analysis dict. Per-doc extractions are persisted on each `TenderDocument` row.
- **Side effects.** Cost aggregation rolled into `tender.pipeline_state["analysis_cost"]`. Failure modes write a "degraded" analysis dict — never null.

## Determinism boundary

| Decision | Owner |
|---|---|
| Which PDFs to send to Haiku | Deterministic (every `TenderDocument` row with status=ready and is_pdf=True) |
| Per-PDF extraction content | LLM (Haiku — vision document block) |
| Per-PDF cost accounting | Deterministic (`call_ai_with_documents` returns token counts) |
| Cross-document synthesis | LLM (Sonnet) |
| Synthesis cost accounting | Deterministic |
| Final structured fields (tender title, value, dates) | LLM — **acceptable** because each is a verbatim copy from the PDF, not a derived computation |

The LLM is **not** doing arithmetic here, so the Rule 1 violation found in the costing pipeline does not apply.

## Tool contract

No `BaseTool` invocations in this graph — the LLM is called directly via `call_ai_with_documents` ([document_analysis_agent.py:934-943](../app/services/langchain/graphs/document_analysis_agent.py)) and `call_ai` ([document_analysis_agent.py:1080-1087](../app/services/langchain/graphs/document_analysis_agent.py)).

- **Per-doc model:** `settings.tender_analyzer_per_doc_model` (Haiku 4.5).
- **Synthesis model:** `settings.tender_analyzer_synthesis_model` (Sonnet 4.6).
- **Page cap per doc:** `settings.tender_analyzer_per_doc_max_pages=200` (Anthropic native PDF supports ~100 standard / ~600 on the document API beta).

## Edge cases

1. **Sequential by design.** `tender_analyzer_max_parallel=1`. **Do not raise this without reading the failure-mode block below.**
2. **Page cap.** Documents over 200 pages are rejected at [document_analysis_agent.py:909-917](../app/services/langchain/graphs/document_analysis_agent.py) — the per-doc validation. Larger PDFs should be pre-split.
3. **Auto-chain timeout.** `_ensure_tender_analysis` runs the analysis inline with a hard 480s ceiling ([chat_agent_wrappers.py:168-171](../app/services/langchain/chat_agent_wrappers.py)). At ~60s per PDF, this fits ~3 PDFs in the prerequisite path. Tenders with more docs must be pre-analyzed via a foreground run.
4. **Scanned PDFs.** Claude vision handles these natively — no separate OCR step.

## Failure modes

The v2 path has four documented swallow points. Each is labeled below as **intentional** (degraded but acceptable) or **defect** (needs fixing).

| Anchor | What happens | Classification |
|---|---|---|
| [document_analysis_agent.py:271-275](../app/services/langchain/graphs/document_analysis_agent.py) | V2 catches Exception → falls back to v1 (logged, not raised) | **Intentional.** v1 fallback is the safety net while v2 stabilizes. |
| [document_analysis_agent.py:1616-1633](../app/services/langchain/graphs/document_analysis_agent.py) | Synthesis exception → degraded analysis dict (no exception raised) | **Intentional.** Better to ship a partial analysis than fail the whole tender pipeline. Log line must include the stack trace. |
| [document_analysis_agent.py:1650-1684](../app/services/langchain/graphs/document_analysis_agent.py) | Empty-markdown synthesis → fallback report | **Defect to monitor.** This is the silent failure mode that `tender_analyzer_max_parallel=1` exists to prevent. If this fires under `max_parallel=1`, it is a real defect — file a ticket. |
| [document_analysis_agent.py:1691-1701](../app/services/langchain/graphs/document_analysis_agent.py) | Cost aggregation errors → defaults to zeros | **Intentional.** Cost reporting is observability, not correctness. |

### The silent empty-synthesis failure (THE reason `max_parallel=1`)

Documented in [../app/core/config.py:74-98](../app/core/config.py):

> Three PDFs simultaneously occasionally produced empty synthesis output even though each individual call succeeded. Root cause: memory pressure + SQLAlchemy session contention in the parallel path. The chosen tradeoff is sequential analysis to avoid silent per-doc failures under parallel load.

**Override risk.** Setting `TENDER_ANALYZER_MAX_PARALLEL > 1` re-introduces the failure mode. If the override is necessary for a 5+ PDF tender, the change must be:

1. Tagged in [../memory/decisions.md](../memory/decisions.md) with date and rationale.
2. Gated behind a feature flag, not a global env-var bump.
3. Monitored — every empty-synthesis instance in [../logs/costing_run.log](../logs/) is a regression.

## Verification

```bash
# 1. Trigger a tender analysis run (set TENDER_ID to a real tender with ≥1 PDF)
cd drpl-backend
python -c "
import asyncio
from app.core.database import SessionLocal
from app.services.langchain.graphs.document_analysis_agent import run_document_analysis
db = SessionLocal()
result = asyncio.run(run_document_analysis(db, tender_id=TENDER_ID))
print(result['status'])
"

# 2. Verify run_id stamps in logs (every line should carry [run=...])
grep -c "\[run=" logs/costing_run.log

# 3. Verify per-doc extractions persisted
psql -c "SELECT id, status, jsonb_path_exists(analysis_result, '$.summary') FROM tender_documents WHERE tender_id = TENDER_ID;"
```

## Open work

- `run_document_analysis` entrypoint is being wrapped in `run_id_scope` as part of the System Pilot pass (2026-05-18). See [../memory/progress.md](../memory/progress.md).
- The empty-synthesis defect class needs a counter at `/health/capacity` so we can detect it without grepping logs. Deferred.
