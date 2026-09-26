# Command Center General Assistant

**Date:** 2026-09-02
**Status:** Approved for implementation
**Scope:** `drpl-backend` (primary), `drpl-frontend` (one SSE event)

## Problem

A user asked the Command Center two ordinary questions about a costing it had
just produced:

1. "Can you provide me just the reason for the above costing?"
2. "Can you provide me the web search result from where the costing came?"

The first was answered rhetorically — plausible reasoning reconstructed from the
chat scrollback rather than read from the stored cost breakdown. The second
produced three source domains (`infralens.in`, `tendertiger.com`,
`asiantender.com`) followed by an admission that they "were illustrative
examples ... not actual search results I retrieved."

The model was not being evasive. It had no tools.

### Root cause

`agent_router_graph.general_response_node:602` and its inline duplicate at
`streaming_handler.py:846-880` both answer with a bare `llm.ainvoke` /
`llm.astream`: no tools, no `tender_id`, and a three-to-four line system prompt.
Every message the intent classifier labels `general_query` lands there.

The failure is the inverse of how it feels from the UI. The *specialist* agents
are richly tooled; it is the conversational fallback — where every ad-hoc
question goes — that is blind. Because `general_query` is also the classifier's
fallback for anything it cannot place (`agent_router_graph.py:414`), the least
capable path is the widest one.

### Supporting findings

Established by inspection of the running system, not assumed:

- `web_search` is registered and functional. `google_api_key` is set in
  `PlatformSetting` (Gemini search grounding, the primary provider).
  `tavily_api_key` is empty; DuckDuckGo remains the last fallback.
- The general branch calls `save_conversation_turn` but **never writes a
  `ProposalMessage`**, unlike every other branch. General-path answers therefore
  disappear on page reload. Global-assistant session 293 shows two user
  messages and no assistant reply, while conversation history proves a reply was
  sent.
- The Master Agent (`decision_maker`, `claude-opus-5`) is functional — verified
  by direct invocation (12.9s, 476 output tokens) — but has produced **3
  conversation turns ever** (2026-06-15, 06-28, 07-31), the last of which
  exceeded its time budget. Ctrl+K does not force it; the global assistant goes
  through the same router, so ordinary messages reach the toolless path.
- `get_chat_model(db, agent_name="general_assistant")` currently returns
  `FailoverChatModel` with `model=None`. An unknown agent name has no model
  config.

## Goals

- Any ad-hoc request in the Command Center — research, drafting an email or
  letter, explaining prior output, a factual question about this tender — is
  handled by an agent that can actually act.
- A factual claim about external prices or sources is either backed by a real
  search observation with URLs, or explicitly labelled as unverified.
- New user needs are met by adding a **tool**, not a new agent and a new router
  rule.
- Nothing about the deterministic costing path changes.

## Non-goals

- Sending email. Drafting only; `resend_api_key` is empty and outbound send is
  out of scope.
- Changing the Ctrl+K global assistant, which stays on `decision_maker`.
- Fixing the `deep_analyzer` registry/code tool drift, or the
  `resolve_tool_keys` intersection that silently drops UI-assigned tools. Both
  are real, both are logged below as follow-ups, neither is touched here.

## Design

### 1. The agent

New module `app/services/langchain/graphs/general_assistant_agent.py`:

```python
GENERAL_ASSISTANT_SYSTEM_PROMPT: str

async def run_general_assistant(
    db, message, *, tender_id, session_id, proposal_session_id,
    user_id, conversation_history, file_metadata, stream_callback,
) -> dict
```

A `create_react_agent` over `get_chat_model(db, agent_name="general_assistant")`
— provider and failover resolve through the factory, never hard-coded. The whole
run is wrapped in `run_id_scope` so tool calls and DB writes are correlatable in
the logs.

**Toolbelt** — explicit keys through `tool_loader.load_tools_by_keys`, not the
whole repo. The full catalog is ~7,900 tokens of schema and large tool sets
measurably degrade tool selection:

`web_search`, `web_fetch`, `document_reader`, `tender_lookup`,
`semantic_search`, `ratecard_lookup`, `cost_calculator`, `memory_store`,
`memory_retrieve`, `clarify`, `docx_generator`, `xlsx_generator`

All twelve are already classified in `tool_policy`: ten READ, and
`docx_generator` / `xlsx_generator` are WRITE, so generating a file still raises
the confirmation card. **No new tool-policy entries, no drift-test changes.**

**Citation rule (prompt-level).** Any claim about market rates, prices, or
external sources must come from a `web_search` / `web_fetch` observation, and
the answer ends with a `## Sources` list of the URLs those observations actually
returned. When no search was run, the answer says the figure is unverified. When
a search fails, the answer reports the failure rather than smoothing it into a
confident claim.

**Registry presence.** A `general_assistant` entry in
`canonical_registry.CANONICAL_AGENTS` (`supports_user_prompt: True`,
`supports_user_tools: True`), a `seed_general_assistant_agent.py` following the
existing seeder pattern (re-syncs prompt + tools only while
`is_user_customized=False`), called from `main.py`, and a model-config row in
`seed_agent_models.py` so `get_chat_model` resolves a real model. Prompt and
tools become editable from Agent Builder with no deploy.

### 2. Specialist handoff

`run_general_assistant` calls `orchestrator_tools.build_agent_wrapper_tools(...)`
— reuse, not a second orchestrator. It reads the roster from `custom_agents`
and yields one `call_<agent_key>` tool per enabled agent.

- **Costing keeps its canonical path.** `_BUILTIN_AGENT_HANDLERS["costing_researcher"]`
  is `chat_costing_research`, the same handler `streaming_handler` dispatches
  today, which auto-parses the NIT bidding schedule and prices every row in
  batches. No row can be summarised away by this change.
- **An agent added in Agent Builder becomes reachable from chat** with no code
  change, because the roster is the registry.
- **Delegation is not double-confirmed.** `classify_tool` matches `call_*` by
  prefix as READ; the worker's own writes are gated inside `tool_loader`. This
  is the existing deliberate design.
- **Recursion guard:** `general_assistant` is added to
  `_NON_WORKER_AGENT_KEYS`. Without it the generalist's own seeded row appears
  in its own roster and it can call itself.

**Context is passed, not inferred.** `tender_id`, `proposal_session_id` and
`user_id` reach the agent, and the prompt directs it to `tender_lookup` /
`document_reader` before answering anything about *this* tender from memory.

### 3. Wiring

- `general_response_node` and the inline copy in `streaming_handler` both
  collapse into `run_general_assistant`. The duplication is how this stayed
  broken in two places at once.
- The general branch gains the missing **`ProposalMessage` write**, matching
  every other branch. Metadata carries `tool_calls` and source URLs so citations
  survive a reload.
- The intent-classifier prompt reframes `general_query` as an explicit
  capability (open-ended questions, research, drafting, explaining prior output)
  rather than the leftover bucket it reads as today. The
  `agent not in valid_agents` fallback at line 414 still lands there — now the
  most capable path rather than the blindest.
- **Specialist routing is unchanged.** A clear "build the costing" still goes
  straight to `costing_researcher`.

### 4. Streaming

`CommandCenterPage.tsx:728` accumulates `token` events into `fullText` and saves
that as the message. A ReAct agent has several model turns, so pre-tool
narration would be glued onto the final answer.

A new **`token_reset`** SSE event is emitted when a model turn ends in a tool
call; the frontend clears `fullText` and `streamingText`. Live token streaming is
preserved and the saved message is clean, without depending on whether the model
stays silent before calling a tool. Handled in `CommandCenterPage.tsx` and
`src/lib/assistant.ts` (~5 lines each).

Tool activity emits the existing `agent_status` events
(`phase: "tool_running" | "tool_done"`), already rendered as the working pill at
`CommandCenterPage.tsx:743`. No new contract for progress.

## Testing

TDD: each test written failing first.

**`tests/test_general_assistant_tools.py`**
1. `test_general_assistant_has_web_search` — the resolved toolbelt contains
   `web_search` and `web_fetch`. Fails on `main` today, where the general path
   resolves zero tools. This is the bug in one assertion.
2. `test_general_assistant_toolbelt_is_classified` — every belt key returns a
   non-`None` class from `tool_policy.classify_tool`; the two generators
   classify as WRITE.
3. `test_general_assistant_is_not_its_own_worker` — `general_assistant` is in
   `_NON_WORKER_AGENT_KEYS` and absent from `list_worker_agents()`.

**`tests/test_general_assistant_routing.py`**
4. `test_general_query_reaches_the_generalist` — with a stubbed classifier
   returning `general_query`, `run_general_assistant` is invoked with the
   session's `tender_id`, not `None`.
5. `test_specialist_routing_unchanged` — "build the full costing for this
   tender" still dispatches `chat_costing_research`.
6. `test_general_answer_is_persisted_as_proposal_message` — the general branch
   writes a `ProposalMessage`, so the answer survives a reload.

**`tests/test_general_assistant_streaming.py`**
7. `test_token_reset_discards_pre_tool_narration` — a fake two-turn stream
   (narration → tool call → final answer) yields `token_reset`, and the
   accumulated text is the final answer alone.

**Frontend:** a `token_reset` case added to `src/lib/assistant.test.ts`,
asserting accumulated text is cleared.

**Regression suite that must stay green** (this change touches their subject
matter): `test_tool_policy.py`, `test_tool_policy_integration.py` — the two
drift tests the Constitution forbids silencing — plus `test_confirm_gate_e2e.py`,
`test_global_assistant.py`, `test_effective_tools.py`,
`test_agent_tool_sync.py`, `test_chat_stream_first_byte.py`.

**Manual verification:** with both servers running, re-ask the two questions
from the bug report and confirm the answer carries real URLs from a real
`web_search` observation, with the tool call visible in the logs.

**Known limit, stated rather than hidden:** no unit test can prove the *model
chooses* to search. The tests prove the tools are present, classified,
reachable, and context-fed. Prompt quality is verified by the manual run.

## Follow-ups (not in this change)

1. `deep_analyzer` runs with 4 tools while `canonical_registry` declares 6 —
   `web_search` and `memory_store` are declared but never passed
   (`document_analysis_agent.py:430`).
2. `resolve_tool_keys` intersects user-assigned tools with the graph's hardcoded
   defaults (`canonical_registry.py:487`), so a tool assigned in Agent Builder
   can be silently dropped at runtime. The UI implies an assignment the runtime
   discards.
3. The Master Agent is effectively unreachable — 3 turns ever, the last one
   timing out — and Ctrl+K does not route to it. Worth deciding deliberately
   whether that is the intent.
