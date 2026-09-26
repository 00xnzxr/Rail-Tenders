"""The gate as the specialist path actually sees it.

`tests/test_tool_policy.py` covers the policy in isolation. These tests go
through `tool_loader`, the choke point every specialist agent loads its tools
through, because that is where a regression would actually happen: someone adds
a return path to the loader, or a new write tool, and the router's
direct-to-specialist route starts writing unconfirmed again.
"""

import asyncio

import pytest

from app.services.langchain.tool_policy import (
    ApprovalGrant,
    PendingActionCapture,
    classify_tool,
    current_capture,
    policy_scope,
    READ,
    WRITE,
)
from app.services.langchain.tools.tool_loader import (
    get_available_tool_keys,
    load_tools_by_keys,
)


# ── classification covers the real specialist catalog ───────────────────────


def test_every_registered_tool_key_is_classified(db):
    """Same drift guard as the orchestrator catalog, for the specialist tools.

    An unclassified tool defaults to `write`, which is safe but would make an
    ordinary read start demanding confirmation. Fail loudly instead.
    """
    from app.services.langchain.tool_policy import (
        _DESTRUCTIVE_TOOLS,
        _READ_TOOLS,
        _WRITE_TOOLS,
    )

    known = _READ_TOOLS | _WRITE_TOOLS | _DESTRUCTIVE_TOOLS
    tools = load_tools_by_keys(db, get_available_tool_keys(), agent_key="test")
    unclassified = sorted(t.name for t in tools if t.name not in known)

    assert not unclassified, (
        f"Unclassified tool keys: {unclassified}. Add each to "
        "app/services/langchain/tool_policy.py."
    )


def test_document_writing_tools_are_gated(db):
    """These persist a user-visible artifact, so they need a yes."""
    for key in ("workspace_generate_document", "document_generator", "xlsx_generator"):
        tools = load_tools_by_keys(db, [key], agent_key="test")
        if not tools:
            pytest.skip(f"{key} not instantiable in this environment")
        assert classify_tool(tools[0].name) == WRITE, key


def test_lookup_tools_are_not_gated(db):
    """Research and lookup must stay frictionless."""
    for key in ("tender_lookup", "checklist_reader", "ratecard_lookup"):
        tools = load_tools_by_keys(db, [key], agent_key="test")
        if not tools:
            pytest.skip(f"{key} not instantiable in this environment")
        assert classify_tool(tools[0].name) == READ, key


# ── the loader actually applies the gate ────────────────────────────────────


def _one(db, key):
    tools = load_tools_by_keys(db, [key], agent_key="test")
    if not tools:
        pytest.skip(f"{key} not instantiable in this environment")
    return tools[0]


def test_loader_gates_a_write_tool_inside_a_scope(db):
    """The whole point: a specialist's write tool suspends instead of writing."""
    tool = _one(db, "workspace_generate_document")
    capture = PendingActionCapture()

    with policy_scope(capture=capture):
        result = asyncio.run(tool._arun(tender_id=1, document_name="x", content="y"))

    assert capture.is_pending()
    assert capture["tool"] == "workspace_generate_document"
    assert "AWAITING_USER_CONFIRMATION" in str(result)


def test_write_tool_refuses_when_no_scope_is_open(db):
    """Fail closed. With no scope there is no way to ask the user, so executing
    anyway would be exactly the silent ungated write this module exists to
    prevent."""
    tool = _one(db, "workspace_generate_document")

    result = asyncio.run(tool._arun(tender_id=1, document_name="x", content="y"))

    assert "BLOCKED" in str(result)


def test_approved_specialist_write_executes(db):
    """An approval carried into the scope lets exactly that call through.

    We assert on what the gate did, not on what the tool returned: the real
    tool may well fail against a bare test DB, and that is fine — reaching it
    at all is the thing being proved.
    """
    tool = _one(db, "workspace_generate_document")
    args = {"tender_id": 1, "document_name": "x", "content": "y"}
    capture = PendingActionCapture()
    grant = ApprovalGrant("workspace_generate_document", args)

    with policy_scope(capture=capture, grant=grant):
        try:
            result = asyncio.run(tool._arun(**args))
        except Exception:
            # The underlying tool ran and raised — it was not gated, which is
            # exactly what the approval is supposed to achieve.
            result = "executed-and-raised"

    assert "AWAITING_USER_CONFIRMATION" not in str(result)
    assert "BLOCKED" not in str(result)
    assert not capture.is_pending()
    assert grant["consumed"] is True


def test_reads_are_returned_unwrapped(db):
    """A read tool must be the original object, not a wrapper — no added
    latency and nothing to go wrong on the hot path."""
    tools = load_tools_by_keys(db, ["tender_lookup"], agent_key="test")
    if not tools:
        pytest.skip("tender_lookup not instantiable in this environment")
    assert type(tools[0]).__name__ != "_GatedTool"


# ── scope isolation ─────────────────────────────────────────────────────────


def test_scope_is_restored_on_exit(db):
    assert current_capture() is None
    with policy_scope():
        assert current_capture() is not None
    assert current_capture() is None


def test_nested_scopes_do_not_leak(db):
    outer = PendingActionCapture()
    inner = PendingActionCapture()

    with policy_scope(capture=outer):
        with policy_scope(capture=inner):
            assert current_capture() is inner
        assert current_capture() is outer


def test_concurrent_runs_get_separate_captures(db):
    """Two runs in one worker must not see each other's pending actions —
    otherwise one user could approve another user's write."""
    tool = _one(db, "workspace_generate_document")
    seen: dict = {}

    async def run_one(tag, tender_id):
        capture = PendingActionCapture()
        with policy_scope(capture=capture):
            await tool._arun(tender_id=tender_id, document_name=tag, content="c")
            seen[tag] = capture

    async def main():
        await asyncio.gather(run_one("a", 111), run_one("b", 222))

    asyncio.run(main())

    assert seen["a"]["args"]["tender_id"] == 111
    assert seen["b"]["args"]["tender_id"] == 222
    assert seen["a"] is not seen["b"]
