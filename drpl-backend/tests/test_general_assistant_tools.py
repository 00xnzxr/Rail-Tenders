"""The general assistant's toolbelt.

The bug this file exists for: a user asked the Command Center where a costing's
numbers came from, and got three source domains the assistant had never
retrieved, followed by an admission that they "were illustrative examples ...
not actual search results I retrieved."

The model was not being evasive. `general_response_node` answered with a bare
`llm.ainvoke` — no tools, no tender_id. These tests pin the toolbelt so the
conversational path can never silently lose its hands again.
"""

from app.services.langchain.graphs.general_assistant_agent import (
    AGENT_KEY,
    GENERAL_ASSISTANT_SYSTEM_PROMPT,
    GENERAL_ASSISTANT_TOOL_KEYS,
)
from app.services.langchain.tool_policy import READ, WRITE, classify_tool


def test_general_assistant_has_web_search():
    """The exact failure from the bug report, as one assertion."""
    assert "web_search" in GENERAL_ASSISTANT_TOOL_KEYS
    assert "web_fetch" in GENERAL_ASSISTANT_TOOL_KEYS


def test_general_assistant_can_read_this_tender():
    """"Why did the costing come out this way" must be answerable from the
    stored breakdown, not reconstructed from the chat scrollback."""
    for key in ("document_reader", "tender_lookup", "semantic_search", "ratecard_lookup"):
        assert key in GENERAL_ASSISTANT_TOOL_KEYS, key


def test_general_assistant_can_produce_files():
    for key in ("docx_generator", "xlsx_generator"):
        assert key in GENERAL_ASSISTANT_TOOL_KEYS, key


def test_general_assistant_toolbelt_is_registered():
    """Every key must resolve to a real tool class, or it is silently dropped
    at load time and the agent quietly loses a capability."""
    from app.services.langchain.tools.tool_loader import get_available_tool_keys

    available = set(get_available_tool_keys())
    missing = set(GENERAL_ASSISTANT_TOOL_KEYS) - available
    assert not missing, f"unregistered tool keys: {sorted(missing)}"


def test_general_assistant_toolbelt_is_classified():
    """An unclassified tool is gated as a write — which would make the
    generalist demand a confirmation for an ordinary web search."""
    for key in GENERAL_ASSISTANT_TOOL_KEYS:
        assert classify_tool(key) is not None, f"{key} is unclassified"


def test_file_generation_still_requires_confirmation():
    assert classify_tool("docx_generator") == WRITE
    assert classify_tool("xlsx_generator") == WRITE


def test_reads_are_not_gated():
    for key in ("web_search", "web_fetch", "document_reader", "tender_lookup"):
        assert classify_tool(key) == READ, key


def test_agent_key_is_stable():
    """Seeder, registry, model tier and router all key off this string."""
    assert AGENT_KEY == "general_assistant"


def test_prompt_demands_real_sources():
    """The prompt is the only thing standing between the user and another
    plausible-looking list of sites nobody visited."""
    prompt = GENERAL_ASSISTANT_SYSTEM_PROMPT.lower()
    assert "web_search" in prompt
    assert "sources" in prompt


def test_general_assistant_is_in_the_canonical_registry():
    """Registry presence is what makes the prompt and tools editable in Agent
    Builder instead of frozen in code."""
    from app.services.langchain.canonical_registry import CANONICAL_AGENTS

    entry = CANONICAL_AGENTS[AGENT_KEY]
    assert entry["supports_user_prompt"] is True
    assert entry["supports_user_tools"] is True
    assert set(entry["default_tools"]) == set(GENERAL_ASSISTANT_TOOL_KEYS)


def test_canonical_prompt_resolves_to_this_module():
    from app.services.langchain.canonical_registry import get_canonical_prompt

    assert get_canonical_prompt(AGENT_KEY) == GENERAL_ASSISTANT_SYSTEM_PROMPT


def test_general_assistant_has_a_model_tier():
    """Without a tier the factory hands back FailoverChatModel(model=None)."""
    from app.services.seed_agent_models import AGENT_TIERS, target_model

    assert AGENT_KEY in AGENT_TIERS
    assert target_model(AGENT_KEY, "anthropic")


def test_general_assistant_is_on_the_roster_for_the_master():
    """Under chat_engine=master the generalist is the cheap delegate, so it
    must NOT be excluded from the worker roster any more. Its self-call guard
    moved into its own _build_tools — pinned by
    test_scoring_agents_are_not_offered_as_handoffs below."""
    from app.services.langchain.graphs.orchestrator_tools import (
        _NON_WORKER_AGENT_KEYS,
    )

    assert AGENT_KEY not in _NON_WORKER_AGENT_KEYS


def test_handoff_to_a_specialist_is_not_double_confirmed():
    """The worker's own writes are gated inside tool_loader. Gating the
    hand-off too made the orchestrator suspend on its first delegation."""
    from app.services.langchain.tool_policy import classify_tool

    assert classify_tool("call_costing_researcher") == READ
    assert classify_tool("call_deep_analyzer") == READ


def test_costing_handoff_uses_the_canonical_path():
    """chat_costing_research parses the NIT schedule and prices every row. The
    generic ReAct path collapses dense spares schedules into one summary line."""
    from app.services.langchain.graphs.orchestrator_tools import (
        _BUILTIN_AGENT_HANDLERS,
    )

    assert _BUILTIN_AGENT_HANDLERS["costing_researcher"] == "chat_costing_research"


def test_scoring_agents_are_not_offered_as_handoffs(db):
    """relevance / risk / classifier / summary are pipeline internals. As chat
    tools they are meaningless as a request and pure schema cost."""
    from app.services.langchain.graphs.general_assistant_agent import _build_tools

    names = {
        t.name
        for t in _build_tools(
            db,
            proposal_session_id=None,
            session_id="test",
            user_id=None,
            conversation_history=None,
            file_metadata=None,
            stream_callback=None,
        )
    }
    for key in ("relevance", "risk", "classifier", "summary", "costing_scope_extractor"):
        assert f"call_{key}" not in names, key
    assert "call_general_assistant" not in names
    assert "web_search" in names
