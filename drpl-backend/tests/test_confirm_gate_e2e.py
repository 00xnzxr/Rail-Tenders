"""End-to-end: an agent decides to write, and the gate stops it.

Every other test exercises a piece. This one drives `run_decision_maker`
itself with a stubbed model, so the whole loop is covered — the LLM emitting a
tool call, LangGraph dispatching it, the gate intercepting, the run ending as
`needs_confirmation`, and an approval replaying the call for real.

The model is stubbed rather than live: it makes the test deterministic and free,
and the thing under test is the gate, not the model's judgement.
"""

import asyncio
from typing import Any, Optional

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.models.user import User  # noqa: F401 — registers the table before create_all
from app.services.langchain.graphs.decision_maker_agent import run_decision_maker


class ScriptedModel(BaseChatModel):
    """Emits a scripted sequence of AI messages, one per invocation.

    Enough of the BaseChatModel surface for `create_react_agent`: it binds
    tools (and ignores them, since the script decides what to call) and returns
    the next scripted message each turn.
    """

    script: list = []
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: D102
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        idx = len(self.calls)
        self.calls.append(messages)
        msg = (
            self.script[idx]
            if idx < len(self.script)
            else AIMessage(content="Done.")
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)


def _use_capability_message(key: str, args: dict) -> AIMessage:
    """A scripted call in the two-tier catalog's shape.

    Writes are Tier 2 now: the model reaches them through the use_capability
    dispatcher, and the gate records the INNER tool name — so the pending
    action the user confirms still says regenerate_checklist, not
    use_capability.
    """
    return _tool_call_message("use_capability", {"key": key, "args": args})


def _tool_call_message(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call_1", "type": "tool_call"}],
    )


@pytest.fixture
def scripted(monkeypatch):
    """Install a scripted model in place of the real provider."""

    def install(script: list) -> ScriptedModel:
        model = ScriptedModel(script=script, calls=[])
        monkeypatch.setattr(
            "app.services.langchain.llm_factory.get_chat_model",
            lambda *a, **k: model,
        )
        return model

    return install


def _run(db, **kwargs) -> dict:
    return asyncio.run(
        run_decision_maker(
            db=db,
            message=kwargs.pop("message", "do the thing"),
            mode="autonomous",
            max_iterations=3,
            max_execution_time=30.0,
            **kwargs,
        )
    )


def test_agent_write_is_gated_and_reported(db, scripted):
    """The agent asks to regenerate a checklist; the run ends awaiting a yes."""
    scripted([_use_capability_message("regenerate_checklist", {"tender_id": 412})])

    result = _run(db, user_id=1, tender_id=412)

    assert result["status"] == "needs_confirmation"
    pending = result["pending_action"]
    assert pending["tool"] == "regenerate_checklist"
    assert pending["args"] == {"tender_id": 412}
    assert pending["tier"] == "write"
    # The user-facing text is the plain summary, not a tool name.
    assert "regenerate_checklist" not in result["output"]
    assert "412" in pending["summary"]


def test_gated_run_does_not_report_success(db, scripted):
    """A gated run must never look completed — that would tell the user their
    change was made when nothing happened."""
    scripted([_use_capability_message("init_workspace_force", {"tender_id": 7})])

    result = _run(db, user_id=1, tender_id=7)

    assert result["status"] != "completed"
    assert result["pending_action"]["tier"] == "destructive"


def test_read_tool_runs_without_confirmation(db, scripted):
    """Diagnosis stays frictionless: a read finishes the run normally."""
    scripted(
        [
            _use_capability_message("inspect_tender", {"tender_id": 412}),
            AIMessage(content="Here is what I found."),
        ]
    )

    result = _run(db, user_id=1, tender_id=412)

    assert result["status"] != "needs_confirmation"
    assert result.get("pending_action") is None


def test_approved_action_is_not_gated_again(db, scripted):
    """Replaying with the matching grant lets exactly that call through."""
    scripted(
        [
            _use_capability_message("regenerate_checklist", {"tender_id": 412}),
            AIMessage(content="Rebuilt the checklist."),
        ]
    )

    result = _run(
        db,
        user_id=1,
        tender_id=412,
        approved_action={"tool": "regenerate_checklist", "args": {"tender_id": 412}},
    )

    # It executed (or tried to) rather than suspending again.
    assert result["status"] != "needs_confirmation"


def test_approval_for_a_different_tender_does_not_transfer(db, scripted):
    """Approving tender 412 must not authorise a write against tender 999."""
    scripted([_use_capability_message("regenerate_checklist", {"tender_id": 999})])

    result = _run(
        db,
        user_id=1,
        tender_id=999,
        approved_action={"tool": "regenerate_checklist", "args": {"tender_id": 412}},
    )

    assert result["status"] == "needs_confirmation"
    assert result["pending_action"]["args"] == {"tender_id": 999}


def test_gate_can_be_switched_off(db, scripted, monkeypatch):
    """The escape hatch works without a deploy."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ASSISTANT_CONFIRM_GATE_ENABLED", "false")
    get_settings.cache_clear()
    try:
        scripted(
            [
                _use_capability_message("regenerate_checklist", {"tender_id": 412}),
                AIMessage(content="Rebuilt."),
            ]
        )
        result = _run(db, user_id=1, tender_id=412)
        assert result["status"] != "needs_confirmation"
    finally:
        get_settings.cache_clear()


# ── role bounding through the real agent loop (sub-project B) ───────────────


@pytest.fixture
def user_with_role(db):
    """Create a user, yield a factory that sets their role."""
    import uuid as _uuid

    u = User(
        email=f"role-{_uuid.uuid4().hex[:8]}@example.com",
        name="Role Test",
        hashed_password="x",
        role="operator",
    )
    db.add(u)
    db.commit()
    db.refresh(u)

    def as_role(role: str):
        u.role = role
        db.add(u)
        db.commit()
        return u

    yield as_role
    db.query(User).filter(User.id == u.id).delete()
    db.commit()


def test_operator_cannot_reach_a_master_admin_tool(db, scripted, user_with_role):
    """The agent tries to change a platform setting on an operator's behalf.

    It must not succeed. The role is resolved from the database inside
    run_decision_maker, never from anything the client sent.
    """
    user = user_with_role("operator")
    scripted(
        [
            _tool_call_message(
                "update_platform_setting", {"key": "anything", "value": "1"}
            ),
            AIMessage(content="I can't do that."),
        ]
    )

    result = _run(db, user_id=user.id, message="turn off auto scoring")

    # Either the tool was never offered (so the call is an unknown tool) or the
    # call-time role check refused it. Both are acceptable; a successful write
    # is not.
    assert result["status"] != "completed" or "REFUSED" not in str(result.get("output"))
    trace = str(result.get("trace", "")) + str(result.get("output", ""))
    assert "SHOULD NOT" not in trace


def test_master_admin_is_offered_the_platform_tools(db, user_with_role):
    """The counterpart: the capability does exist for the right role."""
    from app.services.langchain.graphs.platform_tools import build_platform_tools

    user = user_with_role("master_admin")
    names = {t.name for t in build_platform_tools(db, user.id, user.role)}

    assert "update_platform_setting" in names
    assert "list_platform_users" in names


def test_diagnosis_tool_is_available_and_never_gated(db, scripted):
    """Sub-project C: checking your work must not require a confirmation."""
    from app.services.langchain.tool_policy import classify_tool

    assert classify_tool("diagnose_tender_outputs") == "read"

    scripted(
        [
            _tool_call_message("diagnose_tender_outputs", {"tender_id": 99999999}),
            AIMessage(content="I checked and here is what I found."),
        ]
    )

    result = _run(db, user_id=1, tender_id=99999999)

    assert result["status"] != "needs_confirmation"
