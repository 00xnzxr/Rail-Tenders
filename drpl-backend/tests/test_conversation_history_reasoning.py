"""Whether an agent can actually see what was said earlier.

Bug #6 — "when the user requests the agent to access chat history to answer the
question, the process is not proper and output is not as requested". Three
separate defects, each of which alone produces that symptom:

1. `get_conversation_history` ordered ASC and then applied the LIMIT, so it
   returned the *oldest* N turns. Past turn 20 an agent's window stopped
   advancing entirely.
2. `run_decision_maker` — the default chat engine — accepted
   `conversation_history`, passed it to its worker tools, and invoked its own
   graph with a single HumanMessage. The Master had no memory of the
   conversation at all.
3. Nothing could reach a turn older than the window, so an explicit "what did I
   say earlier about X" had no path to an answer and was answered anyway.
"""

import json

import pytest

from app.models.agent_memory import AgentConversationHistory
from app.services.langchain.memory_service import get_conversation_history
from app.services.langchain.tools.conversation_history_tool import (
    ConversationHistorySearchTool,
)


@pytest.fixture
def transcript(db):
    """Thirty turns in one session, oldest first."""
    session_id = "history-test-session"
    db.query(AgentConversationHistory).filter(
        AgentConversationHistory.session_id == session_id
    ).delete()
    db.commit()

    for i in range(30):
        db.add(AgentConversationHistory(
            session_id=session_id,
            agent_key="proposal_router",
            role="user" if i % 2 == 0 else "assistant",
            content=f"turn {i}",
        ))
    db.commit()
    yield session_id
    db.query(AgentConversationHistory).filter(
        AgentConversationHistory.session_id == session_id
    ).delete()
    db.commit()


# ── 1. the window must be the recent end ────────────────────────────────────


def test_the_window_holds_the_most_recent_turns(db, transcript):
    """ASC + LIMIT returned turns 0-19 forever, however long the chat got."""
    turns = get_conversation_history(db, transcript, limit=10)
    assert [t.content for t in turns] == [f"turn {i}" for i in range(20, 30)]


def test_the_window_is_still_chronological(db, transcript):
    """Callers slice it with [-n:] and render it in order."""
    turns = get_conversation_history(db, transcript, limit=5)
    assert [t.id for t in turns] == sorted(t.id for t in turns)


def test_a_short_conversation_comes_back_whole(db, transcript):
    assert len(get_conversation_history(db, transcript, limit=200)) == 30


def test_turns_saved_in_one_request_keep_their_order(db):
    """created_at ties would let an answer sort ahead of its question."""
    from datetime import datetime, timezone

    session_id = "history-tie-session"
    stamp = datetime.now(timezone.utc)
    for role, content in (("user", "the question"), ("assistant", "the answer")):
        db.add(AgentConversationHistory(
            session_id=session_id, agent_key="proposal_router",
            role=role, content=content, created_at=stamp,
        ))
    db.commit()
    try:
        turns = get_conversation_history(db, session_id, limit=10)
        assert [t.content for t in turns] == ["the question", "the answer"]
    finally:
        db.query(AgentConversationHistory).filter(
            AgentConversationHistory.session_id == session_id
        ).delete()
        db.commit()


# ── 2. the Master receives the conversation ─────────────────────────────────


def _roles(messages):
    return ["user" if type(m).__name__ == "HumanMessage" else "assistant"
            for m in messages]


def test_the_master_turns_history_into_messages():
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_messages,
    )

    messages = build_master_messages([
        {"role": "user", "content": "what is the EMD?"},
        {"role": "assistant", "content": "Rs 2,00,000."},
    ], "and the bid validity?")
    assert _roles(messages) == ["user", "assistant", "user"]
    assert messages[1].content == "Rs 2,00,000."
    assert messages[-1].content == "and the bid validity?"


def test_the_history_never_leads_with_an_assistant_turn():
    """A window opening mid-exchange must not 400 the whole request."""
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_messages,
    )

    messages = build_master_messages([
        {"role": "assistant", "content": "…as I was saying"},
        {"role": "user", "content": "go on"},
        {"role": "assistant", "content": "certainly"},
    ], "now cost it")
    assert _roles(messages)[0] == "user"


def test_an_unanswered_turn_does_not_produce_two_user_messages():
    """A failed run leaves a user turn with no answer after it. Gemini rejects
    consecutive same-role messages, so this would break every later message."""
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_messages,
    )

    messages = build_master_messages([
        {"role": "user", "content": "analyse tender 5"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "now cost it"},  # the run that died
    ], "cost it please")

    assert _roles(messages) == ["user", "assistant", "user"]
    assert "now cost it" in messages[-1].content
    assert "cost it please" in messages[-1].content


def test_roles_always_alternate_and_end_with_the_user():
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_messages,
    )

    history = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"},
        {"role": "assistant", "content": "d"},
        {"role": "user", "content": "e"},
    ]
    roles = _roles(build_master_messages(history, "now"))
    assert roles[0] == "user" and roles[-1] == "user"
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_a_long_turn_is_trimmed_and_says_so():
    """An assistant turn stores the whole answer it gave."""
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_messages,
    )

    messages = build_master_messages(
        [{"role": "user", "content": "x" * 9000},
         {"role": "assistant", "content": "ok"}],
        "next", max_chars_per_turn=1000,
    )
    assert len(messages[0].content) < 9000
    assert "omitted" in messages[0].content


def test_only_the_last_n_turns_are_carried():
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_messages,
    )

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
        for i in range(40)
    ]
    messages = build_master_messages(history, "now", max_turns=6)
    assert len(messages) <= 7  # 6 turns plus the current message
    assert "turn 39" in messages[-2].content or "turn 39" in messages[-1].content


def test_empty_history_is_just_the_current_message():
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_messages,
    )

    for history in (None, [], [{"role": "user", "content": "   "}]):
        messages = build_master_messages(history, "hello")
        assert len(messages) == 1
        assert messages[0].content == "hello"


def test_the_master_actually_sends_them():
    """A helper nothing calls is the bug with extra steps."""
    import inspect

    from app.services.langchain.graphs import decision_maker_agent as dm

    source = inspect.getsource(dm.run_decision_maker)
    assert "build_master_messages(conversation_history, human_msg)" in source
    assert '"messages": conversation_messages' in source
    assert '"messages": [HumanMessage(content=human_msg)]' not in source


def test_the_generalist_uses_the_same_builder():
    """It had its own copy of half these rules; two answers is worse than one."""
    import inspect

    from app.services.langchain.graphs import general_assistant_agent as ga

    source = inspect.getsource(ga)
    assert "build_conversation_messages" in source
    assert "conversation_history or [])[-10:]" not in source


# ── 3. reaching past the window ─────────────────────────────────────────────


def test_search_finds_a_turn_that_fell_out_of_the_window(db, transcript):
    tool = ConversationHistorySearchTool(db=db, router_session_id=transcript)
    result = json.loads(tool._run(query="turn 3"))

    assert result["status"] == "completed"
    contents = [t["content"] for t in result["turns"]]
    assert "turn 3" in contents  # turn 3 is 27 turns back


def test_search_pages_further_back(db, transcript):
    tool = ConversationHistorySearchTool(db=db, router_session_id=transcript)
    first = json.loads(tool._run(limit=5))
    assert [t["content"] for t in first["turns"]] == [
        f"turn {i}" for i in range(25, 30)
    ]

    older = json.loads(tool._run(limit=5, before_id=first["oldest_id"]))
    assert [t["content"] for t in older["turns"]] == [
        f"turn {i}" for i in range(20, 25)
    ]


def test_no_match_says_so_rather_than_returning_something_close(db, transcript):
    tool = ConversationHistorySearchTool(db=db, router_session_id=transcript)
    result = json.loads(tool._run(query="escalation clause"))
    assert result["turns"] == []
    assert "note" in result


def test_the_excerpt_is_taken_around_the_match(db):
    """The head of a 20,000-character analysis answers a different question."""
    session_id = "history-excerpt-session"
    content = "filler " * 3000 + "THE ESCALATION CLAUSE IS 5%" + " tail" * 100
    db.add(AgentConversationHistory(
        session_id=session_id, agent_key="proposal_router",
        role="assistant", content=content,
    ))
    db.commit()
    try:
        tool = ConversationHistorySearchTool(db=db, router_session_id=session_id)
        result = json.loads(tool._run(query="ESCALATION CLAUSE"))
        turn = result["turns"][0]
        assert "THE ESCALATION CLAUSE IS 5%" in turn["content"]
        assert turn["content_complete"] is False
        assert turn["total_chars"] == len(content)
    finally:
        db.query(AgentConversationHistory).filter(
            AgentConversationHistory.session_id == session_id
        ).delete()
        db.commit()


def test_the_session_comes_from_the_run_not_the_model(db):
    """There is no argument by which a model could name another conversation."""
    from app.services.langchain.tools.conversation_history_tool import (
        ConversationHistorySearchInput,
    )

    assert "router_session_id" in ConversationHistorySearchTool.model_fields
    assert not set(ConversationHistorySearchInput.model_fields) & {
        "session_id", "router_session_id",
    }


def test_an_unbound_run_refuses_rather_than_reading_everything(db):
    tool = ConversationHistorySearchTool(db=db, router_session_id=None)
    result = json.loads(tool._run(query="anything"))
    assert result["status"] == "failed"


def test_it_is_a_registered_read_the_loader_binds():
    from app.services.langchain.capability_registry import CAPABILITIES, READ
    from app.services.langchain.tools.tool_loader import get_available_tool_keys

    assert CAPABILITIES["conversation_history_search"].tier == READ
    assert "conversation_history_search" in get_available_tool_keys()


def test_the_loader_injects_the_session(db):
    from app.services.langchain.tools.tool_loader import load_tools_by_keys

    tools = load_tools_by_keys(
        db, ["conversation_history_search"], router_session_id="sess-abc",
    )
    assert tools
    assert tools[0].router_session_id == "sess-abc"


# ── 4. the prompt stays bounded ─────────────────────────────────────────────


def test_the_history_block_has_a_total_ceiling():
    """The per-turn cap does not bound the prompt: twelve turns at four
    thousand characters is fifty thousand characters before the request is
    even read."""
    from app.services.langchain.chat_messages import build_conversation_messages

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 4000}
        for i in range(12)
    ]
    messages = build_conversation_messages(
        history, "now", max_turns=12, max_chars_per_turn=4000,
        max_total_chars=10000,
    )
    history_chars = sum(len(m.content) for m in messages[:-1])
    assert history_chars <= 10000


def test_the_ceiling_drops_the_oldest_turns_first():
    """The window's value is its recency; what falls out is searchable."""
    from app.services.langchain.chat_messages import build_conversation_messages

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "x" * 900}
        for i in range(10)
    ]
    messages = build_conversation_messages(
        history, "now", max_turns=10, max_chars_per_turn=4000,
        max_total_chars=3000,
    )
    kept = " ".join(m.content for m in messages)
    assert "turn 9" in kept
    assert "turn 0" not in kept


def test_the_ceiling_cannot_expose_a_leading_assistant_turn():
    """Dropping the oldest turns can uncover one — the same 400 by another route."""
    from app.services.langchain.chat_messages import build_conversation_messages

    history = [
        {"role": "user", "content": "x" * 5000},
        {"role": "assistant", "content": "y" * 5000},
        {"role": "user", "content": "z" * 100},
        {"role": "assistant", "content": "w" * 100},
    ]
    messages = build_conversation_messages(
        history, "now", max_turns=10, max_chars_per_turn=6000,
        max_total_chars=500,
    )
    assert type(messages[0]).__name__ == "HumanMessage"


def test_the_master_applies_the_ceiling():
    from app.core.config import get_settings
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_messages,
    )

    ceiling = get_settings().master_history_max_total_chars
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 4000}
        for i in range(40)
    ]
    messages = build_master_messages(history, "now")
    assert sum(len(m.content) for m in messages[:-1]) <= ceiling


def test_a_failed_run_still_names_what_completed():
    """A run that analysed a tender and then died costing it has produced
    something. A bare apology throws it away and invites a full re-run."""
    import inspect

    from app.services.langchain.graphs import decision_maker_agent as dm

    source = inspect.getsource(dm.run_decision_maker)
    failure_branch = source[source.index("run failed"):]
    assert "partial_trace" in failure_branch
    assert '"trace": []' not in failure_branch


def test_search_treats_wildcards_as_literal_text(db):
    """A model searching for "50% escalation" must not get a pattern match."""
    session_id = "history-wildcard-session"
    for content in ("escalation is 50% per year", "escalation is 5 percent"):
        db.add(AgentConversationHistory(
            session_id=session_id, agent_key="proposal_router",
            role="assistant", content=content,
        ))
    db.commit()
    try:
        tool = ConversationHistorySearchTool(db=db, router_session_id=session_id)
        hits = json.loads(tool._run(query="50%"))["turns"]
        assert [t["content"] for t in hits] == ["escalation is 50% per year"]

        # A bare wildcard must match nothing, not everything.
        assert json.loads(tool._run(query="5%_"))["turns"] == []
    finally:
        db.query(AgentConversationHistory).filter(
            AgentConversationHistory.session_id == session_id
        ).delete()
        db.commit()


def test_a_failure_does_not_claim_the_run_ran_out_of_time():
    """The timeout renderer is reused to list what completed, but its default
    headline sends a user whose provider errored off to fix the wrong thing."""
    import inspect

    from app.services.langchain.graphs import decision_maker_agent as dm

    trace = [
        {"type": "action", "step": 1, "tool": "call_deep_analyzer"},
        {"type": "observation", "step": 1},
        {"type": "action", "step": 2, "tool": "call_costing_researcher"},
    ]
    text = dm.render_partial_timeout_message(
        trace, 12.0,
        headline="_The request stopped partway through._",
        unfinished="in progress when it stopped",
    )
    assert "ran out of time" not in text
    assert "in progress when it stopped" in text
    assert "`call_deep_analyzer` — completed" in text

    # And the default wording is untouched for the genuine timeout.
    assert "ran out of time" in dm.render_partial_timeout_message(trace, 12.0)

    failure_branch = inspect.getsource(dm.run_decision_maker)
    failure_branch = failure_branch[failure_branch.index("run failed"):]
    assert "headline=" in failure_branch
