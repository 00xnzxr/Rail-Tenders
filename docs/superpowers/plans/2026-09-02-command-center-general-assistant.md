# Command Center General Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Command Center's toolless `general_query` fallback with a tool-using general assistant that can search the web, read this tender's stored data, draft files, and hand off to the specialist agents.

**Architecture:** One new LangGraph ReAct agent (`general_assistant`) with an explicit twelve-tool belt, registered in `canonical_registry` and seeded as a `CustomAgent` so its prompt and tools are editable from Agent Builder. Specialist handoff reuses `orchestrator_tools.build_agent_wrapper_tools` rather than re-implementing orchestration. Both existing toolless general paths collapse into its single entrypoint.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, LangChain / LangGraph (`create_react_agent`), pytest; React 18 + TypeScript + Vite on the frontend.

**Spec:** `docs/superpowers/specs/2026-09-02-command-center-general-assistant-design.md`

## Global Constraints

- Tool selection goes through `tool_loader.load_tools_by_keys` — never instantiate a tool class directly.
- Every tool reaching an agent must pass `tool_policy` classification. Do not add keys to `_READ_TOOLS` / `_WRITE_TOOLS` in this change; all twelve belt tools are already classified.
- `test_tool_policy.py` and `test_tool_policy_integration.py` are drift tests the Constitution forbids silencing. They must pass unmodified.
- Model selection goes through `llm_factory.get_chat_model(db, agent_name=...)`. Never hard-code a provider or model ID in a graph.
- The agent runs inside `run_id_scope` so its log lines carry `[run=<first8>]`.
- Specialist routing behaviour must not change: "build the costing" still dispatches `chat_costing_research`.
- Backend commands run from `drpl-backend/` using `.venv/bin/python` and `.venv/bin/pytest`.

---

## File Structure

**Create:**
- `drpl-backend/app/services/langchain/graphs/general_assistant_agent.py` — the prompt, the toolbelt, and `run_general_assistant`.
- `drpl-backend/app/services/seed_general_assistant_agent.py` — idempotent `CustomAgent` seeder.
- `drpl-backend/tests/test_general_assistant_tools.py`
- `drpl-backend/tests/test_general_assistant_routing.py`
- `drpl-backend/tests/test_general_assistant_streaming.py`

**Modify:**
- `app/services/langchain/canonical_registry.py` — `general_assistant` entry.
- `app/services/seed_agent_models.py` — tier assignment.
- `app/services/langchain/graphs/orchestrator_tools.py` — recursion guard.
- `app/services/langchain/graphs/agent_router_graph.py` — `general_response_node`, classifier prompt.
- `app/services/langchain/streaming_handler.py` — general branch.
- `app/main.py` — call the seeder.
- `drpl-frontend/src/pages/CommandCenterPage.tsx`, `src/lib/assistant.ts`, `src/lib/assistant.test.ts` — `token_reset`.

---

### Task 1: The agent module and its toolbelt

**Files:**
- Create: `drpl-backend/app/services/langchain/graphs/general_assistant_agent.py`
- Test: `drpl-backend/tests/test_general_assistant_tools.py`

**Interfaces:**
- Consumes: `tool_loader.load_tools_by_keys`, `tool_policy.classify_tool`.
- Produces: `GENERAL_ASSISTANT_TOOL_KEYS: list[str]`, `GENERAL_ASSISTANT_SYSTEM_PROMPT: str`, `AGENT_KEY = "general_assistant"`, and `async def run_general_assistant(...) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_general_assistant_tools.py
from app.services.langchain.graphs.general_assistant_agent import (
    GENERAL_ASSISTANT_TOOL_KEYS,
)
from app.services.langchain.tool_policy import classify_tool, WRITE


def test_general_assistant_has_web_search():
    """The bug: the general path answered with zero tools and invented sources."""
    assert "web_search" in GENERAL_ASSISTANT_TOOL_KEYS
    assert "web_fetch" in GENERAL_ASSISTANT_TOOL_KEYS


def test_general_assistant_can_read_this_tender():
    for key in ("document_reader", "tender_lookup", "semantic_search"):
        assert key in GENERAL_ASSISTANT_TOOL_KEYS


def test_general_assistant_toolbelt_is_registered():
    from app.services.langchain.tools.tool_loader import get_available_tool_keys
    available = set(get_available_tool_keys())
    assert set(GENERAL_ASSISTANT_TOOL_KEYS) <= available


def test_general_assistant_toolbelt_is_classified():
    for key in GENERAL_ASSISTANT_TOOL_KEYS:
        assert classify_tool(key) is not None, f"{key} is unclassified"
    assert classify_tool("docx_generator") == WRITE
    assert classify_tool("xlsx_generator") == WRITE
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_general_assistant_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: general_assistant_agent`.

- [ ] **Step 3: Write the module**

Define `AGENT_KEY`, `GENERAL_ASSISTANT_TOOL_KEYS` (the twelve keys from the spec), `GENERAL_ASSISTANT_SYSTEM_PROMPT` (capabilities, the citation rule, the "read this tender before answering from memory" rule, the handoff rule), and `run_general_assistant` building a `create_react_agent` over `get_chat_model(db, agent_name=AGENT_KEY)` inside `run_id_scope`.

Return shape must match the specialized-agent contract other callers expect:

```python
{
    "output": str,
    "output_type": "general",
    "agent_key": "general_assistant",
    "tool_calls": list[dict],
    "sources": list[str],
    "status": "completed" | "failed",
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_general_assistant_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/langchain/graphs/general_assistant_agent.py drpl-backend/tests/test_general_assistant_tools.py
git commit -m "feat(command-center): a general assistant that has tools"
```

---

### Task 2: Registry entry, seeder, and model tier

**Files:**
- Modify: `app/services/langchain/canonical_registry.py`, `app/services/seed_agent_models.py`, `app/main.py`
- Create: `app/services/seed_general_assistant_agent.py`
- Test: append to `tests/test_general_assistant_tools.py`

**Interfaces:**
- Consumes: `AGENT_KEY`, `GENERAL_ASSISTANT_SYSTEM_PROMPT`, `GENERAL_ASSISTANT_TOOL_KEYS` from Task 1.
- Produces: `seed_general_assistant_agent(db) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_general_assistant_is_in_the_canonical_registry():
    from app.services.langchain.canonical_registry import CANONICAL_AGENTS
    entry = CANONICAL_AGENTS["general_assistant"]
    assert entry["supports_user_prompt"] is True
    assert entry["supports_user_tools"] is True
    assert set(entry["default_tools"]) == set(GENERAL_ASSISTANT_TOOL_KEYS)


def test_general_assistant_has_a_model_tier():
    from app.services.seed_agent_models import AGENT_TIERS, model_for_agent
    assert "general_assistant" in AGENT_TIERS
    assert model_for_agent("general_assistant", "anthropic")
```

Note: confirm the helper's real name in `seed_agent_models.py` (the module resolves a tier at line ~103) and use it verbatim.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_general_assistant_tools.py -k "registry or tier" -v`
Expected: FAIL — `KeyError: 'general_assistant'`.

- [ ] **Step 3: Implement**

Add the `CANONICAL_AGENTS["general_assistant"]` entry with `prompt_constant_path` pointing at the module constant, `default_tools=GENERAL_ASSISTANT_TOOL_KEYS`, empty placeholder lists, both `supports_user_*` True. Add `"general_assistant": WORKER` to `AGENT_TIERS`. Write the seeder on the `seed_costing_researcher_agent.py` pattern — create when absent, re-sync `system_prompt` + `tools` only while `is_user_customized` is False. Call it from `main.py` beside the other agent seeders.

- [ ] **Step 4: Run to verify it passes, then verify the model resolves**

```bash
.venv/bin/pytest tests/test_general_assistant_tools.py -v
.venv/bin/python -c "
from app.core.database import SessionLocal
from app.services.seed_general_assistant_agent import seed_general_assistant_agent
from app.services.langchain.llm_factory import get_chat_model
db = SessionLocal(); seed_general_assistant_agent(db)
m = get_chat_model(db, agent_name='general_assistant')
print(type(m).__name__, getattr(m, 'model', None) or getattr(m, 'model_name', None))
"
```

Expected: a concrete model ID, **not** `None`. Before this task it returns `FailoverChatModel model=None`.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/langchain/canonical_registry.py drpl-backend/app/services/seed_agent_models.py drpl-backend/app/services/seed_general_assistant_agent.py drpl-backend/app/main.py drpl-backend/tests/test_general_assistant_tools.py
git commit -m "feat(command-center): make the general assistant configurable in Agent Builder"
```

---

### Task 3: Specialist handoff and the recursion guard

**Files:**
- Modify: `app/services/langchain/graphs/orchestrator_tools.py:437`, `app/services/langchain/graphs/general_assistant_agent.py`
- Test: append to `tests/test_general_assistant_tools.py`

**Interfaces:**
- Consumes: `orchestrator_tools.build_agent_wrapper_tools`, `_NON_WORKER_AGENT_KEYS`.
- Produces: `run_general_assistant` toolbelt now includes `call_<agent_key>` tools.

- [ ] **Step 1: Write the failing test**

```python
def test_general_assistant_is_not_its_own_worker():
    """A generalist in its own roster is unbounded recursion with a price tag."""
    from app.services.langchain.graphs.orchestrator_tools import _NON_WORKER_AGENT_KEYS
    assert "general_assistant" in _NON_WORKER_AGENT_KEYS


def test_handoff_tools_are_read_classified():
    from app.services.langchain.tool_policy import classify_tool, READ
    assert classify_tool("call_costing_researcher") == READ
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_general_assistant_tools.py -k "worker or handoff" -v`
Expected: FAIL on the first assertion.

- [ ] **Step 3: Implement**

Add `"general_assistant"` to `_NON_WORKER_AGENT_KEYS` with a comment explaining the recursion. In `run_general_assistant`, extend the belt with `build_agent_wrapper_tools(session_id=..., proposal_session_id=..., user_id=..., conversation_history=..., file_metadata=..., stream_callback=..., db=db)`.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_general_assistant_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/langchain/graphs/orchestrator_tools.py drpl-backend/app/services/langchain/graphs/general_assistant_agent.py drpl-backend/tests/test_general_assistant_tools.py
git commit -m "feat(command-center): let the generalist hand off to the specialists"
```

---

### Task 4: Wire both general paths, with context and persistence

**Files:**
- Modify: `app/services/langchain/graphs/agent_router_graph.py:602-650`, `app/services/langchain/streaming_handler.py:846-901`
- Test: `tests/test_general_assistant_routing.py`

**Interfaces:**
- Consumes: `run_general_assistant` (Task 1).
- Produces: nothing new; both call sites now delegate.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_general_assistant_routing.py
import asyncio
from unittest.mock import AsyncMock, patch


def test_general_query_passes_tender_id(db_session, general_session):
    """The old path passed no tender_id at all, so 'why this costing' had
    only the chat scrollback to answer from."""
    from app.services.langchain import streaming_handler as sh
    captured = {}

    async def fake_run(db, message, **kwargs):
        captured.update(kwargs)
        return {"output": "ok", "output_type": "general",
                "agent_key": "general_assistant", "tool_calls": [],
                "sources": [], "status": "completed"}

    with patch.object(sh, "run_general_assistant", side_effect=fake_run), \
         patch.object(sh, "classify_intent_node",
                      new=AsyncMock(return_value={"intent": "general_query",
                                                  "selected_agents": []})):
        asyncio.run(_drain(sh.stream_agent_response(
            db=db_session, message="what is this tender for?",
            tender_id=301, session_id=general_session)))

    assert captured["tender_id"] == 301


def test_specialist_routing_unchanged(db_session, general_session):
    """The generalist must not be inserted in front of the deterministic
    NIT-schedule costing path."""
    ...  # assert chat_costing_research is dispatched for estimate_costing


def test_general_answer_is_persisted_as_a_proposal_message(db_session, general_session):
    """The general branch never wrote a ProposalMessage, so its answers
    vanished on reload."""
    ...  # drive the branch, then query ProposalMessage for role="assistant"
```

Fill the two elided bodies from the existing fixtures in `tests/conftest.py` and the patterns in `tests/test_chat_stream_first_byte.py`; do not leave them elided in the code.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_general_assistant_routing.py -v`
Expected: FAIL — no `run_general_assistant` attribute on the handler.

- [ ] **Step 3: Implement**

Replace the inline `llm.astream` block in `streaming_handler.py` and the body of `general_response_node` with calls to `run_general_assistant`, passing `tender_id`, `proposal_session_id`, `user_id`, history and `file_metadata`. Add the `ProposalMessage` write next to the existing `save_conversation_turn`, with `metadata_json` carrying `tool_calls` and `sources`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_general_assistant_routing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/langchain/graphs/agent_router_graph.py drpl-backend/app/services/langchain/streaming_handler.py drpl-backend/tests/test_general_assistant_routing.py
git commit -m "fix(command-center): general answers now have tools, context, and a saved message"
```

---

### Task 5: `token_reset` streaming

**Files:**
- Modify: `app/services/langchain/graphs/general_assistant_agent.py`, `drpl-frontend/src/pages/CommandCenterPage.tsx:728`, `drpl-frontend/src/lib/assistant.ts`
- Test: `drpl-backend/tests/test_general_assistant_streaming.py`, `drpl-frontend/src/lib/assistant.test.ts`

**Interfaces:**
- Produces: SSE event `token_reset` with payload `{}`.

- [ ] **Step 1: Write the failing tests**

Backend: feed a fake two-turn agent stream (text "Let me search…" → tool call → final answer) through the streaming path and assert a `token_reset` event precedes the final turn's tokens, and that concatenating post-reset tokens yields the final answer alone.

Frontend, in `src/lib/assistant.test.ts` beside the existing `decision_action` case:

```ts
it('clears accumulated text on token_reset', async () => {
  const events = [
    'event: token\ndata: {"content":"Let me search"}\n\n',
    'event: token_reset\ndata: {}\n\n',
    'event: token\ndata: {"content":"The answer"}\n\n',
  ];
  // ...drive the parser, then:
  expect(result.text).toBe('The answer');
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_general_assistant_streaming.py -v` and `npm test -- assistant` in `drpl-frontend/`.

- [ ] **Step 3: Implement**

Emit `token_reset` from the agent's stream handler when a model turn ends with tool calls. Add the `case 'token_reset':` branch to `CommandCenterPage.tsx` (clear `fullText` and `setStreamingText('')`) and the equivalent in `assistant.ts`.

- [ ] **Step 4: Run to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/langchain/graphs/general_assistant_agent.py drpl-backend/tests/test_general_assistant_streaming.py drpl-frontend/src/pages/CommandCenterPage.tsx drpl-frontend/src/lib/assistant.ts drpl-frontend/src/lib/assistant.test.ts
git commit -m "fix(command-center): pre-tool narration no longer lands in the saved answer"
```

---

### Task 6: Classifier prompt, regression suite, manual verification

**Files:**
- Modify: `app/services/langchain/graphs/agent_router_graph.py:55-165`

- [ ] **Step 1: Reframe `general_query` in the classifier prompt**

Change the `## Available Agents` list and intent category so `general_query` reads as a capability — open-ended questions, web research, drafting emails/letters/notes, explaining prior output — rather than "no specialized agent (respond directly)". Leave every escalation and specialist rule untouched.

- [ ] **Step 2: Run the full regression suite**

```bash
.venv/bin/pytest tests/ -q
```

Expected: PASS, with `test_tool_policy.py` and `test_tool_policy_integration.py` unmodified.

- [ ] **Step 3: Manual verification against the real bug**

Start both servers, open the Command Center session from the bug report, and re-ask:
1. "Can you provide me just the reason for the above costing?"
2. "Can you provide me the web search result from where the costing came?"

Confirm the answer carries a `## Sources` list of real URLs and that the tool call appears in the logs:

```bash
tail -f drpl-backend/logs/backend.log | grep -E "web_search|general_assistant"
```

- [ ] **Step 4: Commit**

```bash
git add drpl-backend/app/services/langchain/graphs/agent_router_graph.py
git commit -m "feat(command-center): general_query is a capability, not a leftover bucket"
```

---

## Self-Review

**Spec coverage:** agent + toolbelt → Task 1; registry/seeder/model → Task 2; handoff + recursion guard → Task 3; wiring, context, `ProposalMessage` → Task 4; `token_reset` → Task 5; classifier + regression + manual → Task 6. Every spec section maps to a task.

**Placeholders:** two test bodies in Task 4 are marked `...` with explicit instructions to fill them from named existing fixtures; every other code block is complete. No "TBD" or "add error handling" steps.

**Type consistency:** `GENERAL_ASSISTANT_TOOL_KEYS`, `GENERAL_ASSISTANT_SYSTEM_PROMPT`, `AGENT_KEY`, and `run_general_assistant`'s return dict are named identically in Tasks 1–4.
