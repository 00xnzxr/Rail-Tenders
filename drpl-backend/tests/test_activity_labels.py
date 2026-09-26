"""Live progress text, shared by both surfaces.

Command Center and the assistant both already rendered `agent_status.message`;
what they rendered was the problem. Only 7 of 25 tools had a human label and the
rest fell through to `name.replace("_", " ")`, so users watched "Using
anonymizing web search" and — for the most common step the Master Agent takes —
"Using call costing researcher".
"""

import asyncio
import time
from uuid import uuid4

import pytest

from app.services.langchain.activity_labels import (
    DELEGATION_ACTIVITY,
    TOOL_ACTIVITY,
    describe_phase,
    describe_tool,
)
from app.services.langchain.callback_handler import _humanize_tool
from app.services.langchain.tools.tool_loader import get_available_tool_keys


# ── coverage ────────────────────────────────────────────────────────────────


def test_every_runnable_tool_has_a_human_label():
    """A tool with no label shows the user a generic phrase while it works."""
    generic = [n for n in get_available_tool_keys() if describe_tool(n) == "Working on it"]
    assert not generic, f"tools with no activity label: {generic}"


def test_callback_handler_uses_the_same_vocabulary():
    """The Master Agent's timeline and a specialist's progress line must
    describe the same work the same way."""
    for name in get_available_tool_keys():
        assert _humanize_tool(name) != "Working on it", name


def test_master_only_tools_are_labelled():
    """Diagnostics, repair and platform tools only the Master reaches."""
    for name in (
        "inspect_tender", "list_recent_errors", "regenerate_checklist",
        "finalize_document", "diagnose_tender_outputs", "update_platform_setting",
    ):
        assert name in TOOL_ACTIVITY, name


# ── phrasing rules ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(TOOL_ACTIVITY))
def test_label_never_leaks_the_tool_name(name):
    """The label is read by people who do not know what a tool is."""
    label = TOOL_ACTIVITY[name]
    assert name not in label
    assert "_" not in label, f"{name}: {label!r} contains snake_case"


@pytest.mark.parametrize("name", sorted(TOOL_ACTIVITY))
def test_label_is_short_and_capitalised(name):
    label = TOOL_ACTIVITY[name]
    assert label[0].isupper(), label
    # These render on one line in a narrow popup.
    assert len(label) <= 60, f"{name}: {len(label)} chars"
    assert not label.endswith("."), label


@pytest.mark.parametrize("key", sorted(DELEGATION_ACTIVITY))
def test_delegation_label_describes_the_work_not_the_handoff(key):
    """It should read as the work being done, not as an internal hand-off.

    A key's word may legitimately appear ("Building the submission checklist"
    for `checklist`); what must not appear is the key as an identifier, or the
    call_ prefix.
    """
    label = DELEGATION_ACTIVITY[key]
    assert "call_" not in label
    assert "_" not in label and "-" not in label, f"{key}: {label!r} echoes the key"
    assert label.lower() != key.replace("_", " ").replace("-", " ").lower()
    assert " " in label, f"{key}: {label!r} is not a phrase"


# ── behaviour ───────────────────────────────────────────────────────────────


def test_delegation_reads_as_work():
    assert describe_tool("call_costing_researcher") == (
        "Researching rates and building the costing"
    )


def test_unknown_agent_still_reads_sensibly():
    """An admin-registered agent we have no phrasing for still beats the raw
    key, because the roster is now driven by the registry."""
    assert describe_tool("call_doc_review_bot") == "Asking the Doc Review Bot to help"


def test_tender_id_makes_the_step_concrete():
    label = describe_tool("inspect_tender", {"tender_id": 412})
    assert "412" in label


def test_unknown_tool_does_not_leak_internals():
    label = describe_tool("some_internal_thing_20260101")
    assert "some_internal_thing" not in label
    assert label == "Working on it"


def test_empty_name_is_handled():
    assert describe_tool("") == "Working on it"
    assert describe_phase("nonsense") == "Working on it"


# ── the Master Agent's timeline ─────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def _streamer():
    from app.services.langchain.graphs.decision_maker_agent import _TimelineStreamer

    events: list = []
    streamer = _TimelineStreamer(
        stream_callback=lambda e, p: events.append((e, p)),
        started_at=time.time(),
    )
    return streamer, events


def test_master_emits_a_plain_language_status():
    """decision_action carries the raw tool name and lands in collapsed
    technical detail, so without this the Master's work was invisible while it
    ran — a spinner and nothing else."""
    streamer, events = _streamer()
    _run(streamer.on_tool_start(
        {"name": "call_annexure_finder"}, '{"tender_id": 7}', run_id=uuid4()
    ))

    statuses = [p for e, p in events if e == "agent_status"]
    assert statuses
    assert statuses[0]["message"] == "Extracting the annexures"
    # The technical event is still emitted for the detail view.
    assert any(e == "decision_action" for e, _ in events)


def test_status_carries_run_id_so_the_pill_can_clear():
    streamer, events = _streamer()
    run_id = uuid4()
    _run(streamer.on_tool_start({"name": "ratecard_lookup"}, "{}", run_id=run_id))

    start = next(p for e, p in events if e == "agent_status")
    assert start["run_id"] == str(run_id)
    assert start["phase"] == "tool_running"
    assert isinstance(start["started_at_ms"], int)


def test_finished_tool_clears_the_pill():
    streamer, events = _streamer()
    run_id = uuid4()
    _run(streamer.on_tool_start({"name": "ratecard_lookup"}, "{}", run_id=run_id))
    _run(streamer.on_tool_end("done", run_id=run_id))

    phases = [p["phase"] for e, p in events if e == "agent_status"]
    assert phases == ["tool_running", "tool_done"]


def test_failed_tool_also_clears_the_pill():
    """Otherwise a failure leaves 'Looking up known rates…' on screen forever."""
    streamer, events = _streamer()
    run_id = uuid4()
    _run(streamer.on_tool_start({"name": "ratecard_lookup"}, "{}", run_id=run_id))
    _run(streamer.on_tool_error(Exception("boom"), run_id=run_id))

    done = [p for e, p in events if e == "agent_status" and p["phase"] == "tool_done"]
    assert done, "a failed tool must dismiss its progress pill"
    assert done[0]["run_id"] == str(run_id)


def test_malformed_tool_input_does_not_break_the_run():
    """Tool input is whatever the model produced; it is not always JSON."""
    streamer, events = _streamer()
    _run(streamer.on_tool_start(
        {"name": "web_search"}, "not json at all", run_id=uuid4()
    ))

    statuses = [p for e, p in events if e == "agent_status"]
    assert statuses[0]["message"] == "Searching the web"
