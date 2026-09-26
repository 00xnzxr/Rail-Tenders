# Global Assistant + Write-Confirmation Gate

Date: 2026-09-02
Status: implemented

## Goal

Make the existing `decision_maker` master agent reachable from every page of the
platform as a popup assistant, and make its write actions safe by requiring an
explicit human confirmation at the tool boundary.

This is sub-project **A + D** of a four-part decomposition:

- **A. Global assistant surface** — this spec
- **B. Broad tool + parameter access** — later spec
- **C. Self-diagnosis of generation output** — later spec
- **D. Non-technical guidance mode** — this spec

## Background: what already exists

`app/services/langchain/graphs/decision_maker_agent.py` is already an autonomous
ReAct orchestrator sitting above six specialist agents. It can call any of them
as a sub-tool, inspect platform state, repair broken data, choose an LLM per
subtask, and stream Thought/Action/Observation to `DecisionMakerTimeline.tsx`.

The governing agent is therefore **not new**. What is missing:

1. It is reachable only when `classify_intent` escalates to it inside the
   Command Center or `AgentChatPage`. There is no platform-wide surface.
2. It runs autonomously (`decision_maker_autonomous=True`) with no gate on
   write actions.
3. Output assumes a technical reader.

## Non-goals

- Widening the tool surface toward all 307 API routes (that is sub-project B).
- Replacing or restructuring `decision_maker`'s reasoning loop.
- Changing the intent router's classification behaviour.

## Design

### 1. Global thread — no schema change

One `ProposalSession` per user, created lazily on first popup open:

- `tender_id = NULL` (column is already nullable)
- `mode = "standalone"`
- `agent_type = "global_assistant"` (the discriminator)
- `status = "draft"` (pinned)

Looked up by `created_by + agent_type`. History, artifacts and attachments all
key off `session_id`, so they work unchanged.

**Invariant:** the global session MUST be excluded from `list_sessions` and
`bulk_delete_sessions` in `app/api/routes/command_center.py`. Without this it
appears as a phantom row in the Command Center sidebar and can be deleted,
which would silently orphan every user's assistant history.

**Invariant:** `chat_stream` rejects sessions whose `status` is not `draft` or
`revision_requested`. The global session's status must never be advanced.

### 2. Page context envelope

The popup sends `page_context` alongside `message`:

```json
{
  "route": "/tenders/412/workspace/checklist",
  "tender_id": 412,
  "workspace_tab": "checklist",
  "visible_entity": {"type": "checklist_item", "id": 88},
  "last_error": "..."
}
```

`chat_stream` accepts an optional `page_context` field and:

- resolves `tender_id` from it when the session itself has none;
- prepends a short structured preamble to `enriched_message`, using the same
  mechanism `file_context` already uses.

This is what makes "why is this wrong?" resolvable without the user naming the
entity they are looking at.

### 3. Confirm gate at the tool boundary

New module `app/services/langchain/tool_policy.py`:

- `TOOL_POLICY: dict[str, str]` classifying every known tool as
  `read` | `write` | `destructive`.
- `classify_tool(name) -> str` — **whitelist semantics: an unknown tool is
  treated as `write`**, so a tool added later fails safe rather than silently
  bypassing the gate.
- `wrap_tools_with_policy(tools, capture, stream_callback, enabled)` returning
  wrapped tools.

A gated tool's `_arun` does not execute. It records the pending call in a
`PendingActionCapture` and raises a sentinel that unwinds the agent loop.

Resume mirrors the plan-approval pattern that already works:

- park the call in `pipeline_state["pending_action"]`
- emit `event: confirm_required` carrying a plain-language summary
- resume via `POST /sessions/{id}/action/respond` with
  `{action: "approve" | "deny", ...}`, returning SSE in the identical format to
  `/decision/respond` so the frontend reuses its existing event handler.

The gate is applied at **two** choke points, because there are two ways a tool
reaches an agent:

1. `run_decision_maker` (`decision_maker_agent.py`), after the 4-family catalog
   is built — covers the orchestrator's own tools.
2. `tool_loader._apply_policy`, applied to both `load_tools_for_agent` and
   `load_tools_by_keys` — covers the specialist agents' own tools. Without
   this, the router's direct-to-specialist path (`classify_intent` picking
   `checklist_generator` rather than `decision_maker`) writes unconfirmed while
   the orchestrator path is gated. One safety model with a hole in it is worse
   than none, because it is the hole nobody remembers.

Binding the two together is `policy_scope`, a ContextVar-based run scope opened
by `streaming_handler` around each execution point. The specialist agents build
their tools deep inside `chat_agent_wrappers` (~194KB) with no seam to thread a
capture through, so tools bind late to whatever scope is open when they run. A
ContextVar rather than a global means concurrent runs in one worker cannot see
each other's pending actions — one user can never approve another's write.

**A write tool called with no scope open refuses to execute.** There would be
no way to ask the user, so running anyway would be precisely the silent ungated
write this module exists to prevent.

### 4. Behaviour change (accepted)

Gating at the tool boundary changes existing Command Center behaviour: a
multi-step run that today silently regenerates a checklist will now pause for a
confirmation. This was explicitly chosen over a popup-only gate to avoid two
divergent safety models. `decision_maker_autonomous` stays `True` — the agent
still plans and acts on its own; it cannot *write* without a yes.

### 5. Adaptive plain language (D)

Answers lead with plain language and a concrete next step. Technical detail —
traces, tool names, stack traces, agent reasoning — moves behind a
"Show technical detail" expander in the popup rather than being removed. One
surface serves both audiences with no setting to discover.

## Frontend

- `GlobalAssistant` mounted once in `components/layout/AppLayout.tsx`, so it
  covers every protected route.
- Floating launcher bottom-right; `Cmd+K` / `Ctrl+K` toggles.
- Streams from the existing `/chat/stream` SSE contract.
- `ConfirmActionCard` renders `confirm_required`, following the existing
  `DecisionPlanCard` pattern.
- Page context is derived from `useLocation()` plus route params.

## Logging

`streaming_handler` opens a `run_id_scope` at the same three execution points as
`policy_scope`, so every log line in a run carries `[run=...]`
(Constitution Rule 2). It is deliberately NOT wrapped around the async
generators themselves: a scope spanning a `yield` can be resumed in a different
context and raise on token reset.

## Testing

- `test_tool_policy.py` — classification, unknown-tool-defaults-to-write,
  suspend-instead-of-execute, reads untouched, single-use approval grants, and
  a drift guard that classifies the *real* orchestrator catalog.
- `test_tool_policy_integration.py` — the same through `tool_loader`, plus
  fail-closed with no scope, scope restore/nesting, and concurrent-run
  isolation.
- `test_confirm_gate_e2e.py` — drives `run_decision_maker` with a scripted
  model: the LLM emits a tool call, LangGraph dispatches it, the gate
  intercepts, the run ends `needs_confirmation`, and an approval replays it.
  Stubbed rather than live so it is deterministic and free; the gate is what is
  under test, not the model's judgement.
- `test_global_assistant.py` — thread creation/reuse/isolation, page context.
- Frontend (`vitest`): SSE parsing including chunk-split frames and malformed
  data, page-context derivation, and the confirm card's plain-language and
  destructive-vs-ordinary treatment.

## Verification

    cd drpl-backend && .venv/bin/pytest tests/ -q     # 557 passed, 1 skipped
    cd drpl-frontend && npm test                      # 22 passed
    cd drpl-frontend && npm run build                 # tsc -b clean
