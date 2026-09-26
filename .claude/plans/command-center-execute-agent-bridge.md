# Bridge Command Center → execute_agent() for All Specialized Agents

## Context

Diagnosis from this session's exploration, confirmed by the user's observation:

- When an admin runs `tender_doc_analyzer` from the **Agent Builder "Test Agent"** UI, an `AgentExecution` row gets created and shows up in the monitoring page. The user sees the run's input/output/tokens/cost.
- When **Command Center** invokes the *same* agent for the same tender, it goes through `chat_agent_wrappers.py` → `run_document_analysis()` / `run_enhanced_costing_research()` → bespoke LangGraph state machines that **never call `execute_agent()`** and **never create AgentExecution rows**. The user sees no monitoring, and a separate parsing layer downstream substitutes a canned "Scope clarification required before costing" message when the agent's output isn't parseable — leaving the user staring at a synthetic message that bears no resemblance to what the agent actually said.

The user's explicit ask: "I want the command center to directly use these agents — no backup or fallback options. I want just direct usage from the agent to get the output."

The right fix is to make Command Center call `execute_agent(agent_key, input_data)` for every specialized agent — the same entrypoint Agent Builder Test uses — and let the agent's actual output flow through to the UI without canned substitution.

What we already have on our side:
- `execute_agent()` in [agent_execution_service.py](drpl-backend/app/services/agent_execution_service.py) creates the AgentExecution row and routes to `execute_langchain_agent()` for ReAct agents.
- `execute_langchain_agent()` in [langchain_execution_service.py](drpl-backend/app/services/langchain/langchain_execution_service.py) creates the row, resolves prompt + tools via `assemble_agent_context()` (which honors user-edited CustomAgent prompts), runs the ReAct loop, logs tokens/cost.
- `resolve_system_prompt()` already pipes Agent Builder UI edits to runtime (Layer 2 from the prior plan).
- The reliability layer (Layer 1) already auto-continues truncation and retries empty content via the ContextVar callback pipe.

What we need to add:
- Thin shim `chat_*` wrappers that prep the user message + call `execute_agent`.
- Drop the canned-fallback substitution from the costing parser.
- Best-effort structured UI: parse if the agent emitted markers, otherwise render plain markdown.

## Approach

Replace the six bespoke `chat_*` wrappers with thin shims that all share the same shape:

```python
async def chat_<agent>_via_execute_agent(db, message, tender_id, ..., file_metadata, ...):
    # 1. PREP — build the user_message with tender context embedded as text
    user_message = _build_user_message_for_agent(
        agent_key="<agent_key>",
        message=message,
        tender_id=tender_id,
        file_metadata=file_metadata,
    )

    # 2. EXECUTE — go through the standard path. AgentExecution gets created.
    result = await execute_agent(
        db,
        agent_key_or_id="<agent_key>",
        input_data={
            "message": user_message,
            "tender_id": tender_id,
            "session_id": session_id,
        },
        user_id=user_id,
    )

    # 3. POST — best-effort structured parse. No canned fallback.
    raw_output = result.get("output") or ""
    structured = _try_parse_<agent>_output(raw_output)  # returns None if not parseable

    return {
        "output": raw_output,                      # always — UI renders as markdown
        "output_type": "<agent>_output",
        "agent_key": "<agent_key>",
        "structured_data": structured,             # optional — UI uses if present
        "execution_id": result.get("execution_id"),
        "status": result.get("status", "completed"),
        "metrics": {
            "tokens_input": result.get("tokens_input"),
            "tokens_output": result.get("tokens_output"),
            "cost_estimate": result.get("cost_estimate"),
        },
    }
```

Six agents get this treatment:
- `costing_researcher`
- `deep_analyzer` (alias `tender_doc_analyzer`)
- `checklist_generator`
- `proposal_creator`
- `annexure_finder`
- `workspace_manager`

### Per-agent prep logic

Most agents need very similar prep — embed tender PDF text + analysis summary + BOQ items into the user message. This is what the existing wrappers already do; we lift it into shared helpers.

A new `_build_tender_context_block(db, tender_id)` helper produces:
```
TENDER PDF EXTRACTS:
<extracted text up to 30K chars from _extract_tender_pdf_text()>

PRIOR ANALYSIS (if available):
<TenderAnalysisSummary.requirement_summary if it exists>

BOQ ITEMS (if available):
<BOQ rows from analysis_result>
```

Each shim wraps this in agent-specific framing:
- Costing: prefix with "USER REQUEST: …" (current convention)
- Analyzer: prefix with "Analyze this tender end-to-end and produce the 7-section forensic report"
- Checklist: prefix with "Generate the submission checklist…"
- etc.

### Drop the canned fallback

Remove the `empty_stub` / `prose_leak` substitution block from [enhanced_costing_agent.py:1354-1429](drpl-backend/app/services/langchain/graphs/enhanced_costing_agent.py). When the agent emits prose instead of structured JSON, that prose is the answer — render it. The user said: "I want just direct usage from the agent to get the output."

The structured UI (Cost Breakdown table) continues to render when the agent DOES emit valid markers; otherwise the chat shows the agent's prose as markdown — same as Claude.ai.

### Frontend: best-effort structured rendering

[CommandCenterPage.tsx](drpl-frontend/src/pages/CommandCenterPage.tsx) already renders agent outputs based on `output_type`. Two changes:
1. When `result.structured_data` is null but `output` is non-empty → fall through to the markdown renderer (already the default for `output_type: "general"`).
2. The Cost Breakdown / Analysis structured panels keep working when `structured_data` IS present — they render as artifacts via the existing `extract_artifact_from_output` flow.

No new components needed. The fall-through happens naturally because the structured UIs check `structured_data` for truthiness before rendering.

### Reliability + token-budget + router improvements stay

All the prior work continues to apply:
- [Layer 1](drpl-backend/app/services/ai_service.py) reliability events still flow via ContextVar (the new shim doesn't change that).
- [Layer 4](drpl-backend/app/services/langchain/model_limits.py) Token Budget Protocol is in the system prompt that `assemble_agent_context()` pulls — still active.
- [Layer 3](drpl-backend/app/services/langchain/graphs/agent_router_graph.py) router's extended thinking still picks the right agent before this shim runs.

What we're DROPPING is only the bespoke LangGraph state machines and their parse-fallback layers. The agent itself runs through the same `execute_langchain_agent` codepath as Agent Builder Test.

### Feature flag for safe rollout

Add a platform setting `command_center_use_execute_agent` (default `True`). Wraps the dispatcher in [streaming_handler.py](drpl-backend/app/services/langchain/streaming_handler.py):

```python
if get_effective_setting(db, "command_center_use_execute_agent", True):
    handler = chat_<agent>_via_execute_agent
else:
    handler = chat_<agent>  # legacy path
```

Lets us flip back to the legacy path without a code deploy if something regresses in production.

---

## Critical files

- [drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py](drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py) — adds six new `chat_<agent>_via_execute_agent` shims; legacy ones become fallback.
- [drpl-backend/app/services/langchain/graphs/enhanced_costing_agent.py](drpl-backend/app/services/langchain/graphs/enhanced_costing_agent.py) — drop the `empty_stub` / `prose_leak` canned fallback at lines 1354-1429.
- [drpl-backend/app/services/langchain/streaming_handler.py](drpl-backend/app/services/langchain/streaming_handler.py) — dispatcher honors `command_center_use_execute_agent` flag.
- [drpl-backend/app/services/langchain/graphs/_tender_context.py](drpl-backend/app/services/langchain/graphs/_tender_context.py) (new) — shared `_build_tender_context_block` helper.
- [drpl-backend/app/services/settings_service.py](drpl-backend/app/services/settings_service.py) — register `command_center_use_execute_agent` setting (default `True`).

## Reused, not reinvented

- `execute_agent()` and `execute_langchain_agent()` — already create AgentExecution + run ReAct + log tokens.
- `assemble_agent_context()` — already calls `resolve_system_prompt()` for user-edited prompts.
- `_extract_tender_pdf_text()` in [enhanced_costing_agent.py](drpl-backend/app/services/langchain/graphs/enhanced_costing_agent.py) — extracts ~30K chars per tender.
- `extract_artifact_from_output()` — already handles best-effort structured parsing for the artifact card.
- Layer 1 reliability ContextVar — no changes needed; `execute_langchain_agent` runs inside the same `set_streaming_callback(...)` block.

---

## Verification

**Monitoring restored:**
1. Send "help me with the costing for this tender" with a tender PDF attached.
2. Open Admin → Agent Builder → costing_researcher → Monitoring.
3. Confirm a new AgentExecution row appears with status=completed (or failed), tokens_input/output, and cost_estimate populated.

**No canned fallback:**
4. Same request as above. Whatever the agent says — a real cost breakdown OR a prose explanation of what's missing — appears verbatim in the chat. The string "Scope clarification required before costing" should NEVER appear unless the agent itself produced it.

**Structured UI still works on success:**
5. With a tender that has a clear BOQ, confirm the Cost Breakdown table renders with rates and totals (the agent emitted parseable JSON; the artifact panel pulls structured_data).

**Editing in Agent Builder UI flows through:**
6. Open Admin → Agent Builder → costing_researcher → edit the system prompt to add a unique sentinel sentence ("ALWAYS START YOUR RESPONSE WITH THE WORD 'PINEAPPLE'"). Save.
7. Send a costing request from Command Center. The response begins with "PINEAPPLE" — proving Command Center is now using the user's edited prompt.

**Feature flag toggle:**
8. Set `command_center_use_execute_agent=false` in Platform Settings.
9. Send another costing request. The legacy chat_costing_research path runs (no AgentExecution row, canned fallback may appear).
10. Set it back to true.

**Other agents:**
11. Repeat the analyzer/checklist/proposal/annexure/workspace flows — each should produce an AgentExecution row, render the agent's actual output, and not substitute canned messages.

---

## Rollout

Two PRs:

**PR 1 — Costing first (smallest scope to validate the pattern).**
- New `chat_costing_research_via_execute_agent` shim.
- Drop the canned fallback from `enhanced_costing_agent.py:1354-1429`.
- Dispatcher in streaming_handler honors the flag, defaults to new path for costing only.
- Verification steps 1–7 above.

**PR 2 — All other specialized agents.**
- Same pattern for analyzer, checklist, proposal, annexure, workspace.
- Default to new path for all six.
- Verification step 11.
