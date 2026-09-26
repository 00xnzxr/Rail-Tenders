# SOP — Costing Pipeline

## Goal

Produce a structured cost estimate for a tender. The agent (a) decomposes scope into manpower + resources, (b) sources rates from training data → web search → derived estimates, and (c) emits a JSON cost statement that the backend persists and renders.

This SOP is also **the canonical reference for Rule 1 (determinism boundary)** in the Backend Constitution. The recent fix is described in the **Determinism boundary** section below.

## Inputs

- **Entry points.**
  - Pipeline mode: `run_costing_research(db, tender_id, analysis_result, user_id)` at [../app/services/langchain/graphs/costing_agent.py:423](../app/services/langchain/graphs/costing_agent.py) → delegates to `run_enhanced_costing_research(..., mode="pipeline")` in [enhanced_costing_agent.py](../app/services/langchain/graphs/enhanced_costing_agent.py).
  - Chat mode: invoked via the canonical_registry `costing_researcher` agent, exposed through [chat_agent_wrappers.py](../app/services/langchain/chat_agent_wrappers.py).
- **Canonical registry entry.** [canonical_registry.py:45-65](../app/services/langchain/canonical_registry.py) — defines default tools, required prompt placeholders (`<overhead_percent>`, `<margin_percent>`, `<gst_percent>`).
- **Payload.** Tender row + analysis result from the v2 analyzer + (optional) user message.

## Outputs

- **Persisted.** Cost statement JSON written via `cost_breakdown_service.persist_from_agent_output()` — line items, totals, strategic_summary, cost_assumptions, manpower_resource_analysis.
- **Side effects.** Optional XLSX generation via `xlsx_generator_tool`. Cost telemetry to [../logs/costing_run.log](../logs/).

## Determinism boundary

### THE rule

LLMs do not compute money. **Every `amount` field on every line item MUST be the value returned by `cost_calculator`, not arithmetic the LLM wrote in JSON.**

### The fix (2026-05-18)

`costing_agent.py` previously instructed the LLM to compute `amount = quantity × rate` directly in JSON output. The 2026-05-18 change:

1. **Promoted the `cost_calculator` sanity-check call from optional to mandatory.** Every costing run MUST invoke `cost_calculator` once with `(overhead_percent=0, margin_percent=0, gst_percent=0)` before emitting the final JSON. The tool returns precise per-line `amount_expected` values.
2. **Required transcription.** Each line item's `amount` field MUST equal the corresponding `cost_calculator.line_amounts[*].amount_expected`. The LLM transcribes; it does not compute.
3. **Documented the backend rejection rule** (to be enforced in `cost_breakdown_service` — currently advisory): if `abs(amount - quantity*rate) > 0.5` for any priced line, reject the response and re-prompt.

The cost_calculator tool already handles all the deterministic work — see [../app/services/langchain/tools/cost_calculator_tool.py:125-173](../app/services/langchain/tools/cost_calculator_tool.py) (`_resolve_amounts`).

### What the LLM still owns

| Decision | Why it's OK |
|---|---|
| Which line items to emit | Scope decomposition — qualitative judgement informed by the tender PDF |
| `quantity` | Read from the BOQ verbatim (Rule 3 in the costing prompt — verbatim preservation) |
| `rate` | Sourced from training data / web search / derived estimate — the LLM picks the rate, the tool multiplies |
| `category`, `description`, `source_ref`, `cost_buildup_note`, `confidence` | Pure description, no arithmetic |
| `strategic_summary`, `key_observations`, `assumptions` | Qualitative analysis |

The LLM continues to compute **none of**: amounts, subtotals, overhead, margin, GST, grand totals, margin_amount, margin_pct.

## Tool contract

The costing_researcher agent has these tools (registered via [canonical_registry.py:47-54](../app/services/langchain/canonical_registry.py)):

| Tool | Owner module | Purpose |
|---|---|---|
| `costing_training_retrieval` | [costing_training_retrieval_tool.py](../app/services/langchain/tools/costing_training_retrieval_tool.py) | Primary rate source — searches assigned DRPL training datasets |
| `anonymizing_web_search` | [anonymizing_web_search_tool.py](../app/services/langchain/tools/anonymizing_web_search_tool.py) | Secondary rate source — internet rates with company/tender ID stripped |
| `memory_store` / `memory_retrieve` | Memory tools | Cross-session rate recall |
| `cost_calculator` | [cost_calculator_tool.py](../app/services/langchain/tools/cost_calculator_tool.py) | **Mandatory deterministic compute** — every line amount, every total |
| `xlsx_generator_tool` | [xlsx_generator_tool.py](../app/services/langchain/tools/xlsx_generator_tool.py) | XLSX export of the final cost statement |
| `delegate_travel_research`, `clarify` | Sub-agents | Specialised research / user clarification |

### `cost_calculator` invocation (mandatory)

```python
cost_calculator(
    line_items=[{...} for each line],   # with quantity + rate (no amount)
    overhead_percent=0,
    margin_percent=0,
    gst_percent=0,
)
```

Returns `line_amounts[*].amount_expected` — copy these into the line items' `amount` field.

## Edge cases

1. **Training data covers ≥60% of scope.** The LLM MUST emit priced line items, not `needs_user_input` rows. See the failure-mode block in the costing prompt at [costing_agent.py:170-186](../app/services/langchain/graphs/costing_agent.py).
2. **BOQ in a separate file.** Not a refusal trigger. The LLM derives line items from the scope description in the PDF extracts. See [costing_agent.py:332-339](../app/services/langchain/graphs/costing_agent.py).
3. **Margin-analysis mode.** When tender lines carry a stated `tender_rate`, the LLM emits both `rate` (DRPL's cost) and `tender_rate` (tender's revenue). `cost_calculator` computes `margin_amount = tender_amount - amount` per line and rolls up `margin_totals`.
4. **`profit_pct` is always `null` on line items.** The user sets margin in the editor. The LLM does not pick margin.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `parse_error` in cost statement | LLM emitted prose instead of JSON between markers | Re-prompt; see `_parse_costing_response` at [costing_agent.py:487-528](../app/services/langchain/graphs/costing_agent.py) for the fallback chain |
| `line_items` contains only `needs_user_input` | Training data not consulted, or scope decomposition skipped | Check `costing_training_retrieval` was called; verify `manpower_resource_analysis` is non-empty |
| `amount ≠ quantity × rate` on any line | LLM bypassed `cost_calculator` — Rule 1 violation | Re-prompt; future: enforce in `cost_breakdown_service` |
| Empty `manpower_resource_analysis` on AMC / EPC tender | Decomposition step skipped | Re-prompt with stricter system message — `manpower_resource_analysis` is required for these tender types |
| Costing run hangs > 5 min | Web search rate-limiting | Check `anonymizing_web_search_tool` retry config; `failover_model` 429 handling at [failover_model.py:24-41](../app/services/langchain/failover_model.py) |

## Verification

```bash
cd drpl-backend

# 1. Tail the dedicated costing log
tail -f logs/costing_run.log &

# 2. Trigger a costing run on a tender with completed analysis
# (replace TENDER_ID with a real id whose analysis_result is populated)
python -c "
import asyncio
from app.core.database import SessionLocal
from app.services.langchain.graphs.costing_agent import run_costing_research
db = SessionLocal()
tender = db.execute(\"SELECT id, analysis_result FROM tenders WHERE id = TENDER_ID\").first()
result = asyncio.run(run_costing_research(db, tender_id=tender.id, analysis_result=tender.analysis_result))
print(result['status'])
"

# 3. Verify the determinism rule: every line's amount == quantity * rate (±0.5)
psql -c "
SELECT id, jsonb_array_elements(costing -> 'line_items') ->> 'description' AS desc,
       (jsonb_array_elements(costing -> 'line_items') ->> 'amount')::numeric AS amount,
       (jsonb_array_elements(costing -> 'line_items') ->> 'quantity')::numeric AS qty,
       (jsonb_array_elements(costing -> 'line_items') ->> 'rate')::numeric AS rate
FROM cost_breakdowns WHERE tender_id = TENDER_ID;"
# All rows should satisfy abs(amount - qty*rate) < 0.5

# 4. Confirm cost_calculator was invoked in the log
grep "cost_calculator" logs/costing_run.log
```

## Open work

- Enforce the `|amount - qty*rate| < 0.5` rejection rule in `cost_breakdown_service.persist_from_agent_output` (advisory only as of 2026-05-18 — the prompt-level fix is the first half).
- Wrap `run_costing_research` and `run_enhanced_costing_research` entrypoints in `run_id_scope` — being done in this pass.
