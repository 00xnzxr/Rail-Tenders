"""A costing request finishes in one pass, and the workbook it made is announced.

The Liluah tender, seventh report. The user attached four PDFs, asked for the
costing "properly and completely", and got "Something went wrong while
processing your request" after fourteen minutes and exactly twenty-five tool
calls -- tender_lookup, document_reader seven times, semantic_search six,
memory_retrieve three, call_deep_analyzer twice -- and never the costing.
"Continue" then ran the costing in one pass and ended by offering to
"generate the Excel workbook", which the costing worker had already written.

Four defects, each pinned here:

1. Twenty-five is `master_agent_max_iterations`. LangGraph raised its
   recursion error; `format_user_error` did not know it, so completed work
   was shown under a generic apology.
2. The model was never told its budget was running out -- `decision_budget`
   is a UI event -- so it read into the wall. A `pre_model_hook` now puts the
   notice in the conversation.
3. The delegation doctrine said costing is the worker's job but not that the
   worker is the FIRST call; reading first spends the budget and feeds the
   worker nothing.
4. The costing worker's `xlsx_artifact` was dropped from the payload the
   Master reads and never emitted as `artifact_created` on the ReAct path.
"""

from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

from app.services.langchain import error_utils
from app.services.langchain.capability_registry import DELEGATION_DOCTRINE
from app.services.langchain.graphs import decision_maker_agent as dm
from app.services.langchain.graphs import orchestrator_tools as ot


# ── 1. a spent step budget is named, not "something went wrong" ─────────────


def test_recursion_error_is_explained_not_generic():
    msg = error_utils.format_user_error(
        GraphRecursionError("Recursion limit of 52 reached without hitting a stop condition")
    )
    assert "Something went wrong" not in msg
    assert "budget" in msg.lower()
    assert "continue" in msg.lower()


def test_budget_exhausted_headline_names_the_budget():
    assert "25-step" in dm.budget_exhausted_headline(25)


# ── 2. the model is told before the wall ────────────────────────────────────


def test_no_nudge_while_budget_is_comfortable():
    assert dm.build_budget_nudge(step=3, max_iterations=25) is None


def test_nudge_when_few_calls_remain_names_the_delegation():
    msg = dm.build_budget_nudge(step=25 - dm.BUDGET_NUDGE_REMAINING, max_iterations=25)
    assert msg is not None
    assert "call_<agent>" in msg
    assert str(dm.BUDGET_NUDGE_REMAINING) in msg


def test_last_call_nudge_says_stop_calling_tools():
    msg = dm.build_budget_nudge(step=24, max_iterations=25)
    assert msg is not None
    assert "no further tool calls" in msg


def test_hook_appends_a_human_message_and_leaves_state_alone():
    """Anthropic rejects a mid-list SystemMessage; the graph's own history
    must not carry the notice either, or it would be re-read every turn."""
    streamer = dm._TimelineStreamer(stream_callback=None, started_at=0.0)
    streamer._step = 22
    hook = dm.make_budget_hook(streamer, 25)
    history = [HumanMessage(content="cost this tender")]
    out = hook({"messages": history})
    assert "messages" not in out
    llm_input = out["llm_input_messages"]
    assert llm_input[:-1] == history
    assert isinstance(llm_input[-1], HumanMessage)
    assert "budget notice" in llm_input[-1].content
    assert len(history) == 1  # not mutated


def test_hook_is_silent_early():
    streamer = dm._TimelineStreamer(stream_callback=None, started_at=0.0)
    streamer._step = 1
    out = dm.make_budget_hook(streamer, 25)({"messages": [HumanMessage(content="x")]})
    assert len(out["llm_input_messages"]) == 1


# ── 3. the doctrine puts the costing call first ─────────────────────────────


def test_doctrine_says_costing_call_comes_first():
    assert "FIRST tool call" in DELEGATION_DOCTRINE
    assert "call_costing_researcher" in DELEGATION_DOCTRINE
    assert "document_reader" in DELEGATION_DOCTRINE


# ── 4. the workbook travels with the worker's result ────────────────────────


def _costing_result():
    return {
        "status": "completed",
        "agent_key": "costing_researcher",
        "output_type": "cost_breakdown",
        "output": "Costing complete.",
        "structured_data": {"line_items": []},
        "cost_breakdown_id": 77,
        "xlsx_artifact": {
            "artifact_id": 315,
            "artifact_type": "cost_breakdown_xlsx",
            "title": "Cost Breakdown — Tender #5153",
            "version": 1,
            "file_name": "cost_tender_5153.xlsx",
        },
    }


def test_master_payload_carries_the_workbook():
    payload = ot._worker_result_payload("costing_researcher", _costing_result(), None)
    cards = payload["artifacts"]
    assert cards[0]["artifact_id"] == 315
    assert cards[0]["artifact_type"] == "cost_breakdown_xlsx"
    assert cards[0]["cost_breakdown_id"] == 77
    assert "ALREADY" in payload["artifacts_note"]


def test_payload_without_a_file_has_no_artifacts_key():
    payload = ot._worker_result_payload(
        "deep_analyzer", {"status": "completed", "output": "x"}, None
    )
    assert "artifacts" not in payload


def test_workbook_is_announced_live():
    events = []
    ot.announce_worker_artifacts(_costing_result(), lambda ev, data: events.append((ev, data)))
    assert events == [("artifact_created", {
        "artifact_id": 315,
        "artifact_type": "cost_breakdown_xlsx",
        "title": "Cost Breakdown — Tender #5153",
        "version": 1,
        "file_name": "cost_tender_5153.xlsx",
        "cost_breakdown_id": 77,
    })]


def test_announce_survives_a_failing_callback():
    def boom(ev, data):
        raise RuntimeError("stream closed")
    ot.announce_worker_artifacts(_costing_result(), boom)  # must not raise


# ── The budget has to fit inside the job that carries it ───────────────────
#
# The eighth failure on the same path, from the production log of 2026-09-11:
#
#   run_router_task[727de58b-...] failed: Task exceeded maximum timeout value
#   (1800 seconds)
#
# fifty-two annexure lines into a costing run. The Master has a graceful
# timeout -- `asyncio.TimeoutError` -> `render_partial_timeout_message`,
# status "partial", the trace of what it actually did -- and it could not
# fire, because `master_agent_max_execution_time_s` (1800) was exactly
# `run_service._RUN_JOB_TIMEOUT_SECONDS` (1800). RQ's death penalty got there
# first, so a run that had produced real work was recorded as *failed* with a
# raw RQ message on it.
#
# Nothing about the work changed. The budget is now clamped to sit inside the
# job timeout, which is the difference between "here is what I got" and
# "Agent run failed".


class TestMasterBudgetFitsInsideTheJob:
    def test_the_master_stops_before_rq_kills_the_job(self):
        from app.services.langchain.graphs.decision_maker_agent import (
            resolve_master_budget, _JOB_TIMEOUT_MARGIN_S,
        )
        from app.services.run_service import _RUN_JOB_TIMEOUT_SECONDS

        _, budget = resolve_master_budget()
        assert budget < _RUN_JOB_TIMEOUT_SECONDS, (
            "the graceful timeout can never fire"
        )
        assert budget <= _RUN_JOB_TIMEOUT_SECONDS - _JOB_TIMEOUT_MARGIN_S

    def test_the_margin_leaves_room_for_the_terminal_writes(self):
        """The partial message, the status write, save_partial_turn, the
        run_done event and the notification all happen after the return."""
        from app.services.langchain.graphs.decision_maker_agent import (
            _JOB_TIMEOUT_MARGIN_S,
        )
        assert _JOB_TIMEOUT_MARGIN_S >= 60

    def test_raising_the_setting_past_the_job_timeout_is_a_no_op(self, monkeypatch):
        """Not a silent regression to a hard kill. To give a run longer, the
        job timeout has to move too."""
        from app.services.langchain.graphs import decision_maker_agent as dm
        from app.services.run_service import _RUN_JOB_TIMEOUT_SECONDS

        class FakeSettings:
            master_agent_max_iterations = 25
            master_agent_max_execution_time_s = 36_000.0

        monkeypatch.setattr(dm, "get_settings", lambda: FakeSettings())
        _, budget = dm.resolve_master_budget()
        assert budget == _RUN_JOB_TIMEOUT_SECONDS - dm._JOB_TIMEOUT_MARGIN_S

    def test_a_deliberately_shorter_budget_is_respected(self, monkeypatch):
        """A deployment answering questions rather than running production
        jobs keeps its own choice -- the clamp only pulls an over-long budget
        back inside."""
        from app.services.langchain.graphs import decision_maker_agent as dm

        class FakeSettings:
            master_agent_max_iterations = 8
            master_agent_max_execution_time_s = 300.0

        monkeypatch.setattr(dm, "get_settings", lambda: FakeSettings())
        iters, budget = dm.resolve_master_budget()
        assert (iters, budget) == (8, 300.0)

    def test_the_costing_graph_still_finishes_inside_the_master(self):
        """Three nested limits, in order: costing < Master < RQ. The costing
        cap is deliberately computed against the job timeout rather than the
        clamped Master budget, so this fix costs the pricing stage nothing."""
        from app.services.langchain.graphs.decision_maker_agent import (
            resolve_master_budget,
        )
        from app.services.langchain.graphs.enhanced_costing_agent import (
            _costing_budget_cap_s,
        )
        from app.services.run_service import _RUN_JOB_TIMEOUT_SECONDS

        _, master = resolve_master_budget()
        costing = _costing_budget_cap_s()
        assert costing < master < _RUN_JOB_TIMEOUT_SECONDS
