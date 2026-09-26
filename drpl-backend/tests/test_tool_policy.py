"""Tests for the write-confirmation gate at the tool boundary.

The gate protects every decision_maker surface (popup, Command Center, and
anything built later), so these tests pin down the two properties that make it
trustworthy: reads are never disturbed, and anything not explicitly known to be
a read is gated.
"""

import asyncio

import pytest
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.services.langchain.tool_policy import (
    ApprovalGrant,
    READ,
    WRITE,
    DESTRUCTIVE,
    PendingActionCapture,
    classify_tool,
    wrap_tools_with_policy,
)


class _Input(BaseModel):
    value: str = "x"


def _make_tool(tool_name: str, calls: list) -> BaseTool:
    """A tool that records every execution, so we can assert it never ran."""

    class _T(BaseTool):
        name: str = tool_name
        description: str = "test tool"
        args_schema: type[BaseModel] = _Input

        def _run(self, value: str = "x") -> str:
            return asyncio.run(self._arun(value=value))

        async def _arun(self, value: str = "x") -> str:
            calls.append((tool_name, value))
            return f"executed {tool_name}"

    return _T()


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "inspect_workspace",
        "inspect_tender",
        "inspect_session",
        "list_recent_errors",
        "check_annexure_extraction",
        "web_search",
        "document_reader",
    ],
)
def test_read_tools_classified_as_read(name):
    assert classify_tool(name) == READ


@pytest.mark.parametrize(
    "name",
    [
        "regenerate_checklist",
        "regenerate_annexures",
        "retry_failed_agent_run",
    ],
)
def test_mutating_tools_classified_as_write(name):
    assert classify_tool(name) == WRITE


@pytest.mark.parametrize("name", ["init_workspace_force", "finalize_document"])
def test_irreversible_tools_classified_as_destructive(name):
    assert classify_tool(name) == DESTRUCTIVE


def test_unknown_tool_defaults_to_write():
    """Whitelist semantics: a tool added later must fail safe, not slip through."""
    assert classify_tool("some_tool_invented_next_year") == WRITE


def test_control_tools_are_not_gated():
    """propose_plan / ask_user drive the conversation; gating them would deadlock."""
    assert classify_tool("propose_plan") == READ
    assert classify_tool("ask_user") == READ


# ── wrapper behaviour ───────────────────────────────────────────────────────


def test_read_tool_executes_untouched():
    calls: list = []
    capture = PendingActionCapture()
    wrapped = wrap_tools_with_policy(
        [_make_tool("inspect_tender", calls)], capture=capture
    )

    result = asyncio.run(wrapped[0]._arun(value="42"))

    assert calls == [("inspect_tender", "42")]
    assert "executed inspect_tender" in result
    assert not capture.is_pending()


def test_write_tool_suspends_instead_of_executing():
    calls: list = []
    capture = PendingActionCapture()
    wrapped = wrap_tools_with_policy(
        [_make_tool("regenerate_checklist", calls)], capture=capture
    )

    result = asyncio.run(wrapped[0]._arun(value="412"))

    # The underlying tool must NOT have run.
    assert calls == []
    assert capture.is_pending()
    assert capture["tool"] == "regenerate_checklist"
    assert capture["args"] == {"value": "412"}
    assert capture["tier"] == WRITE
    # The observation must stop the agent rather than invite a retry.
    assert "AWAITING_USER_CONFIRMATION" in result


def test_destructive_tool_suspends_and_records_tier():
    calls: list = []
    capture = PendingActionCapture()
    wrapped = wrap_tools_with_policy(
        [_make_tool("finalize_document", calls)], capture=capture
    )

    asyncio.run(wrapped[0]._arun(value="9"))

    assert calls == []
    assert capture["tier"] == DESTRUCTIVE


def test_only_first_pending_action_is_captured():
    """One confirmation at a time — a second gated call must not overwrite the
    first, or the user would approve a different action than the card showed."""
    calls: list = []
    capture = PendingActionCapture()
    wrapped = wrap_tools_with_policy(
        [
            _make_tool("regenerate_checklist", calls),
            _make_tool("regenerate_annexures", calls),
        ],
        capture=capture,
    )

    asyncio.run(wrapped[0]._arun(value="a"))
    asyncio.run(wrapped[1]._arun(value="b"))

    assert calls == []
    assert capture["tool"] == "regenerate_checklist"


def test_gate_can_be_disabled():
    """enabled=False restores the pre-gate behaviour for a surface that opts out."""
    calls: list = []
    capture = PendingActionCapture()
    wrapped = wrap_tools_with_policy(
        [_make_tool("regenerate_checklist", calls)], capture=capture, enabled=False
    )

    asyncio.run(wrapped[0]._arun(value="1"))

    assert calls == [("regenerate_checklist", "1")]
    assert not capture.is_pending()


def test_wrapper_preserves_tool_identity():
    """The LLM binds to these by name and description; wrapping must not rename."""
    calls: list = []
    original = _make_tool("regenerate_checklist", calls)
    wrapped = wrap_tools_with_policy([original], capture=PendingActionCapture())[0]

    assert wrapped.name == original.name
    assert wrapped.description == original.description
    assert wrapped.args_schema is original.args_schema


def test_summary_is_plain_language():
    """The confirm card is read by non-technical users — no tool jargon."""
    capture = PendingActionCapture()
    wrapped = wrap_tools_with_policy(
        [_make_tool("regenerate_checklist", [])], capture=capture
    )

    asyncio.run(wrapped[0]._arun(value="412"))

    summary = capture["summary"]
    assert summary
    assert "regenerate_checklist" not in summary
    assert summary[0].isupper()


# ── approval grant ──────────────────────────────────────────────────────────


def test_approved_action_executes_for_real():
    calls: list = []
    capture = PendingActionCapture()
    grant = ApprovalGrant("regenerate_checklist", {"value": "412"})
    wrapped = wrap_tools_with_policy(
        [_make_tool("regenerate_checklist", calls)], capture=capture, grant=grant
    )

    result = asyncio.run(wrapped[0]._arun(value="412"))

    assert calls == [("regenerate_checklist", "412")]
    assert "executed regenerate_checklist" in result
    assert not capture.is_pending()


def test_approval_is_single_use():
    """A second identical call must be gated again — the user approved one
    action, not a standing licence for that tool."""
    calls: list = []
    capture = PendingActionCapture()
    grant = ApprovalGrant("regenerate_checklist", {"value": "412"})
    wrapped = wrap_tools_with_policy(
        [_make_tool("regenerate_checklist", calls)], capture=capture, grant=grant
    )

    asyncio.run(wrapped[0]._arun(value="412"))
    second = asyncio.run(wrapped[0]._arun(value="412"))

    assert calls == [("regenerate_checklist", "412")]  # ran exactly once
    assert "AWAITING_USER_CONFIRMATION" in second
    assert capture.is_pending()


def test_approval_does_not_cover_different_args():
    """Approving 'regenerate tender 412' must not authorise tender 999."""
    calls: list = []
    capture = PendingActionCapture()
    grant = ApprovalGrant("regenerate_checklist", {"value": "412"})
    wrapped = wrap_tools_with_policy(
        [_make_tool("regenerate_checklist", calls)], capture=capture, grant=grant
    )

    result = asyncio.run(wrapped[0]._arun(value="999"))

    assert calls == []
    assert "AWAITING_USER_CONFIRMATION" in result
    assert capture["args"] == {"value": "999"}


def test_approval_does_not_cover_a_different_tool():
    calls: list = []
    capture = PendingActionCapture()
    grant = ApprovalGrant("regenerate_checklist", {"value": "412"})
    wrapped = wrap_tools_with_policy(
        [_make_tool("init_workspace_force", calls)], capture=capture, grant=grant
    )

    asyncio.run(wrapped[0]._arun(value="412"))

    assert calls == []
    assert capture["tool"] == "init_workspace_force"


# ── the real catalog ────────────────────────────────────────────────────────


def test_every_real_orchestrator_tool_is_classified_deliberately():
    """Guards against drift between the policy table and the actual tools.

    If a tool is renamed or added, it silently falls through to the
    unknown-defaults-to-write branch. That is safe, but it means an ordinary
    read would start demanding confirmation — so this test fails loudly and
    makes the classification a conscious decision.
    """
    from app.services.langchain.graphs.orchestrator_tools import (
        build_action_tools,
        build_agent_wrapper_tools,
        build_diagnostic_tools,
        build_llm_meta_tools,
    )
    from app.services.langchain.tool_policy import (
        _DESTRUCTIVE_TOOLS,
        _READ_TOOLS,
        _WRITE_TOOLS,
    )

    from app.core.database import SessionLocal
    from app.services.langchain.graphs.platform_tools import build_platform_tools
    from app.services.langchain.graphs.quality_tools import build_quality_tools

    _db = SessionLocal()
    try:
        real = [
            *build_agent_wrapper_tools(
                session_id=None, proposal_session_id=None, user_id=1
            ),
            *build_diagnostic_tools(user_id=1),
            *build_action_tools(user_id=1),
            *build_llm_meta_tools(),
            # master_admin so every role-gated tool is included in the check.
            *build_platform_tools(db=_db, user_id=1, user_role="master_admin"),
            *build_quality_tools(db=_db, user_id=1),
        ]
    finally:
        _db.close()
    known = _READ_TOOLS | _WRITE_TOOLS | _DESTRUCTIVE_TOOLS
    # `call_*` delegation tools are classified by prefix, because the roster
    # now comes from the admin agent registry — the names are not knowable at
    # import time.
    unclassified = sorted(
        t.name
        for t in real
        if t.name not in known and not t.name.startswith("call_")
    )

    assert not unclassified, (
        f"Unclassified tools: {unclassified}. Add each to "
        "app/services/langchain/tool_policy.py."
    )


def test_the_real_repair_tools_are_all_gated():
    """The five repair tools are the ones that can damage a workspace."""
    from app.services.langchain.graphs.orchestrator_tools import build_action_tools

    for tool in build_action_tools(user_id=1):
        assert classify_tool(tool.name) in (WRITE, DESTRUCTIVE), tool.name


def test_the_real_diagnostic_tools_are_never_gated():
    """Diagnosis must stay frictionless, or users learn to click through."""
    from app.services.langchain.graphs.orchestrator_tools import build_diagnostic_tools

    for tool in build_diagnostic_tools(user_id=1):
        assert classify_tool(tool.name) == READ, tool.name


def test_delegation_is_classified_by_prefix_not_by_list():
    """The worker roster comes from the admin registry at runtime, so the
    policy cannot enumerate it. Any call_* is delegation, which is a read —
    the worker's own tools are gated where they actually write."""
    assert classify_tool("call_deep_analyzer") == READ
    assert classify_tool("call_an_agent_added_next_year") == READ
    # The prefix must not be a loophole for real writes.
    assert classify_tool("recall_something") == WRITE
    assert classify_tool("regenerate_checklist") == WRITE
