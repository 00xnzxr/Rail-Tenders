"""A run that stops early leaves something readable.

Streamed text lived only in the Redis event stream, which expires an hour
after the run ends, and the final answer was saved only when a run finished.
A run killed at token 2,900 of 3,000 — a deploy, a crash, a cancel — left a
row saying `cancelled` and nothing a person could read once Redis forgot it.

Three pieces, each pinned here: the worker accumulates what the run has said
and done from the events it already fans out; it writes that to the row every
few seconds and on exit; and a run that ends without finishing has it written
into the session history — the transcript the user reloads into — from the
worker for a cancel or failure, and from the startup reaper for a worker that
was killed outright.
"""

import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.agent_memory import AgentConversationHistory
from app.models.agent_run import AgentRun
from app.models.proposal import ProposalMessage, ProposalSession
from app.services import run_service

OWNER_ID = 1


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# A distinct transcript per test. The clock is not a source of uniqueness
# here — it does not tick once per call — and two tests sharing a
# router_session_id would read each other's conversation turns.
_SESSIONS = itertools.count()


def _session(db):
    s = ProposalSession(created_by=OWNER_ID, status="draft", title="s",
                        router_session_id=f"partial-{next(_SESSIONS)}")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _run(db, session, *, status="running", partial_output=None, partial_trace=None,
         started_minutes_ago=5):
    now = datetime.now(timezone.utc)
    r = AgentRun(
        user_id=OWNER_ID,
        proposal_session_id=session.id,
        router_session_id=session.router_session_id,
        prompt="cost this tender",
        status=status,
        created_at=now - timedelta(minutes=started_minutes_ago + 1),
        started_at=now - timedelta(minutes=started_minutes_ago),
        partial_output=partial_output,
        partial_trace=partial_trace,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ── accumulating from the stream ────────────────────────────────────────────


def test_tokens_accumulate_in_order():
    p = run_service.PartialProgress()
    for piece in ("The EMD ", "is Rs ", "2,00,000."):
        p.note(_sse("token", {"content": piece}))
    assert p.text == "The EMD is Rs 2,00,000."
    assert p.dirty


def test_token_reset_discards_pre_tool_narration():
    """The browser drops what a model turn narrated before it called a tool;
    the persisted copy must be the same text the browser shows."""
    p = run_service.PartialProgress()
    p.note(_sse("token", {"content": "Let me look that up…"}))
    p.note(_sse("token_reset", {}))
    p.note(_sse("token", {"content": "The answer is 42."}))
    assert p.text == "The answer is 42."


def test_master_steps_become_a_trace():
    p = run_service.PartialProgress()
    p.note(_sse("decision_action", {"step": 1, "tool": "call_deep_analyzer", "input_preview": "x"}))
    p.note(_sse("decision_observation", {"step": 1, "result_preview": "ok"}))
    p.note(_sse("decision_action", {"step": 2, "tool": "call_costing_researcher", "input_preview": "y"}))
    assert p.trace == [
        {"type": "action", "step": 1, "tool": "call_deep_analyzer"},
        {"type": "observation", "step": 1, "is_error": False},
        {"type": "action", "step": 2, "tool": "call_costing_researcher"},
    ]


def test_unrelated_and_malformed_events_are_ignored():
    p = run_service.PartialProgress()
    p.note(_sse("agent_status", {"phase": "thinking"}))
    p.note("event: token\ndata: not json\n\n")
    p.note(": keepalive\n\n")
    assert p.text == ""
    assert p.trace == []
    assert not p.dirty


def test_text_is_capped():
    p = run_service.PartialProgress()
    for _ in range(300):
        p.note(_sse("token", {"content": "x" * 1000}))
    assert len(p.text) <= run_service._PARTIAL_OUTPUT_MAX_CHARS


# ── writing it to the row ───────────────────────────────────────────────────


def test_progress_is_written_to_the_row(db):
    run = _run(db, _session(db))
    p = run_service.PartialProgress()
    p.note(_sse("token", {"content": "so far"}))
    p.note(_sse("decision_action", {"step": 1, "tool": "call_deep_analyzer"}))

    assert run_service.record_partial_progress(run.id, p) is True

    db.expire_all()
    row = db.query(AgentRun).filter(AgentRun.id == run.id).first()
    assert row.partial_output == "so far"
    assert row.partial_trace == [{"type": "action", "step": 1, "tool": "call_deep_analyzer"}]
    assert not p.dirty


def test_nothing_new_means_no_write(db):
    run = _run(db, _session(db))
    p = run_service.PartialProgress()
    assert run_service.record_partial_progress(run.id, p) is False


# ── the stopped run leaves its answer in the history ────────────────────────


def test_a_cancelled_run_writes_what_it_had_into_the_session(db):
    session = _session(db)
    run = _run(
        db, session, status="cancelled",
        partial_output="Eligibility: the bidder must hold a Class A licence…",
        partial_trace=[
            {"type": "action", "step": 1, "tool": "call_deep_analyzer"},
            {"type": "observation", "step": 1, "is_error": False},
            {"type": "action", "step": 2, "tool": "call_costing_researcher"},
        ],
    )

    assert run_service.save_partial_turn(db, run, reason="was stopped at your request") is True

    turn = (
        db.query(AgentConversationHistory)
        .filter(AgentConversationHistory.session_id == session.router_session_id,
                AgentConversationHistory.role == "assistant")
        .one()
    )
    assert "was stopped at your request" in turn.content
    assert "Class A licence" in turn.content
    assert "`call_deep_analyzer` — completed" in turn.content
    assert "`call_costing_researcher` — in progress when it stopped" in turn.content
    assert turn.metadata_json["partial"] is True
    assert turn.metadata_json["run_id"] == run.id

    msg = db.query(ProposalMessage).filter(ProposalMessage.session_id == session.id).one()
    assert msg.role == "assistant"
    assert msg.content == turn.content


def test_a_run_that_produced_nothing_writes_nothing(db):
    session = _session(db)
    run = _run(db, session, status="failed")

    assert run_service.save_partial_turn(db, run, reason="stopped because of an error") is False
    assert db.query(AgentConversationHistory).filter(
        AgentConversationHistory.session_id == session.router_session_id
    ).count() == 0


def test_a_run_whose_answer_was_already_saved_is_not_duplicated(db):
    """The pipeline saves its answer before the worker writes the terminal
    status. A run whose status write failed and was later reaped must not get
    its finished answer followed by a "partial" copy of the same thing."""
    session = _session(db)
    run = _run(db, session, status="cancelled", partial_output="the full answer")
    db.add(AgentConversationHistory(
        session_id=session.router_session_id, agent_key="proposal_router",
        role="assistant", content="the full answer",
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()

    assert run_service.save_partial_turn(db, run, reason="was interrupted by a restart") is False
    assert db.query(AgentConversationHistory).filter(
        AgentConversationHistory.session_id == session.router_session_id,
        AgentConversationHistory.role == "assistant",
    ).count() == 1


def test_it_is_idempotent(db):
    session = _session(db)
    run = _run(db, session, status="cancelled", partial_output="partial text")

    assert run_service.save_partial_turn(db, run, reason="was stopped at your request") is True
    assert run_service.save_partial_turn(db, run, reason="was stopped at your request") is False
    assert db.query(AgentConversationHistory).filter(
        AgentConversationHistory.session_id == session.router_session_id
    ).count() == 1


def test_a_run_with_no_session_is_a_no_op(db):
    session = _session(db)
    run = _run(db, session, status="cancelled", partial_output="text")
    run.router_session_id = None
    db.commit()
    assert run_service.save_partial_turn(db, run, reason="x") is False


# ── the reaper: a worker killed outright ────────────────────────────────────


def test_the_reaper_saves_what_a_killed_worker_had_streamed(db):
    """SIGKILL leaves no chance for the worker to write anything at the end.
    The periodic flush is what it leaves behind, and the reaper is the only
    thing left to put it in front of the user."""
    session = _session(db)
    run = _run(db, session, status="running", started_minutes_ago=60,
               partial_output="Three of five schedules priced…")

    assert run_service.reap_stuck_runs() >= 1

    db.expire_all()
    assert db.query(AgentRun).filter(AgentRun.id == run.id).first().status == "cancelled"
    turn = (
        db.query(AgentConversationHistory)
        .filter(AgentConversationHistory.session_id == session.router_session_id,
                AgentConversationHistory.role == "assistant")
        .one()
    )
    assert "interrupted by a restart" in turn.content
    assert "Three of five schedules priced" in turn.content


def test_the_reaper_does_not_invent_an_answer_for_a_silent_run(db):
    session = _session(db)
    _run(db, session, status="running", started_minutes_ago=60)

    run_service.reap_stuck_runs()

    assert db.query(AgentConversationHistory).filter(
        AgentConversationHistory.session_id == session.router_session_id
    ).count() == 0


# ── the worker wires it up ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_pump_records_progress_and_flushes_on_exit(monkeypatch):
    from app.worker import run_tasks

    flushed: list[str] = []

    async def _fake_stream(**kwargs):
        yield _sse("token", {"content": "hello "})
        yield _sse("token", {"content": "world"})

    monkeypatch.setattr(
        "app.services.langchain.streaming_handler.stream_router_response",
        _fake_stream,
    )
    monkeypatch.setattr(run_tasks, "_xadd_event", lambda r, k, **kw: None)
    monkeypatch.setattr(run_tasks.run_service, "cancel_requested", lambda rid: False)
    monkeypatch.setattr(
        run_tasks.run_service, "record_partial_progress",
        lambda rid, progress: flushed.append(progress.text) or True,
    )

    await run_tasks._pump(object(), "stream-key", {}, run_id="r1")

    assert flushed[-1] == "hello world"


def test_the_worker_saves_a_partial_turn_only_for_an_unfinished_run():
    import inspect

    from app.worker import run_tasks

    source = inspect.getsource(run_tasks._run_router_task_inner)
    assert 'final_status in ("cancelled", "failed")' in source
    assert "save_partial_turn" in source
    # Re-read the row first: the pump wrote through its own sessions.
    assert source.index("db.expire_all()") < source.index("save_partial_turn")


# ── schema: Constitution rule 3 ─────────────────────────────────────────────


def test_both_drift_fixes_carry_the_new_columns():
    """Railway rolls forward without `alembic upgrade head`; the in-process
    drift fixes are what actually add the columns to an existing database."""
    import inspect

    from app import main

    pg = inspect.getsource(main._apply_schema_drift_fixes)
    assert "agent_runs ADD COLUMN IF NOT EXISTS partial_output TEXT" in pg
    assert "agent_runs ADD COLUMN IF NOT EXISTS partial_trace JSON" in pg

    lite = inspect.getsource(main._add_missing_columns)
    assert '("agent_runs", "partial_output", "TEXT")' in lite
    assert '("agent_runs", "partial_trace", "JSON")' in lite


def test_the_run_status_exposes_it(db, auth_client):
    session = _session(db)
    run = _run(db, session, status="cancelled", partial_output="so far")

    body = auth_client.get(f"/api/runs/{run.id}").json()

    assert body["has_partial_output"] is True
    assert body["partial_output"] == "so far"
