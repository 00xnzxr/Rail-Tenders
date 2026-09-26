"""Stopping a run has to stop the run.

The Stop button aborted the browser's fetch and nothing else. The browser went
quiet; the worker carried on to completion, still calling models, still billing
against the user's monthly cap for work they had explicitly told it to stop. On
a platform that meters per user and enforces a budget, that is not a missing
convenience — it is the one control a user has over their own spend.

Cancellation is cooperative rather than a kill. The run executes in a worker
process and the request asking it to stop is in the web process; RQ can kill a
work horse, but that stops the pipeline mid-write with no `finally`, and this
one writes artifacts, messages and usage rows as it goes. So the request sets a
flag and the worker reads it between the events it emits.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.agent_run import AgentRun
from app.models.proposal import ProposalSession
from app.services import run_service

OWNER_ID = 1
INTRUDER_ID = 2


class FakeRedis:
    """Enough Redis for the cancellation path, and it records what it was told."""

    def __init__(self):
        self.keys: dict[str, str] = {}
        self.streams: dict[str, list] = {}
        self.sets: dict[str, set] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key, value, ex=None):
        self.keys[key] = value

    def exists(self, key):
        return 1 if key in self.keys else 0

    def delete(self, *keys):
        n = 0
        for k in keys:
            n += 1 if self.keys.pop(k, None) is not None else 0
        return n

    def xadd(self, key, payload):
        self.streams.setdefault(key, []).append(payload)

    def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)

    def expire(self, key, seconds):
        self.ttls[key] = seconds
        return 1


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: r)
    return r


def _session(db):
    s = ProposalSession(created_by=OWNER_ID, status="draft", title="s")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _run(db, session_id, *, status="running", user_id=OWNER_ID, rq_job_id=None):
    now = datetime.now(timezone.utc)
    r = AgentRun(
        user_id=user_id,
        proposal_session_id=session_id,
        prompt="cost this tender",
        status=status,
        created_at=now,
        started_at=now if status == "running" else None,
        rq_job_id=rq_job_id,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ── cancelling before anything started ──────────────────────────────────────


def test_a_queued_run_is_stopped_outright(db, fake_redis):
    """Nothing has started, so nothing has to be asked. No model call is spent."""
    run = _run(db, _session(db).id, status="queued")

    result = run_service.request_cancel(db, run)

    assert result["outcome"] == "cancelled"
    assert result["status"] == "cancelled"
    db.refresh(run)
    assert run.status == "cancelled"
    assert run.finished_at is not None


def test_a_queued_cancel_closes_the_event_stream(db, fake_redis):
    """Otherwise an attached browser keepalives at a dead run for 40 minutes."""
    from app.core.redis_client import run_stream_key

    run = _run(db, _session(db).id, status="queued")
    run_service.request_cancel(db, run)

    events = fake_redis.streams.get(run_stream_key(run.id), [])
    assert [e["event"] for e in events] == ["run_done"]
    assert json.loads(events[0]["data"])["status"] == "cancelled"


def test_a_queued_cancel_gives_the_slot_back(db, fake_redis):
    """The worker releases the slot when a run ends. There is no worker here."""
    fake_redis.sets[f"drpl:user:{OWNER_ID}:active_runs"] = {"other"}
    run = _run(db, _session(db).id, status="queued")
    fake_redis.sets[f"drpl:user:{OWNER_ID}:active_runs"].add(run.id)

    run_service.request_cancel(db, run)

    assert fake_redis.sets[f"drpl:user:{OWNER_ID}:active_runs"] == {"other"}


def test_a_queued_cancel_leaves_the_flag_for_a_worker_that_already_dequeued(db, fake_redis):
    """RQ cannot pull back a job a worker has already picked up. That worker's
    start-of-job check is what stops it, and it reads this flag — clearing it
    here would race that check and run the job we just reported cancelled."""
    run = _run(db, _session(db).id, status="queued")

    run_service.request_cancel(db, run)

    assert run_service.cancel_requested(run.id) is True


def test_a_queued_cancel_gives_the_stream_key_an_expiry(db, fake_redis):
    """The web process may be the first thing to write to this key. A stream
    created without a TTL outlives everything else about the run."""
    from app.core.redis_client import RUN_STREAM_TTL_SECONDS, run_stream_key

    run = _run(db, _session(db).id, status="queued")
    run_service.request_cancel(db, run)

    assert fake_redis.ttls.get(run_stream_key(run.id)) == RUN_STREAM_TTL_SECONDS


# ── cancelling something already executing ──────────────────────────────────


def test_a_running_run_is_asked_not_told(db, fake_redis):
    """The row keeps saying `running` because it still is. Reporting it as
    cancelled the moment the flag is set would be the same lie as the button
    that only closed the browser's connection."""
    run = _run(db, _session(db).id, status="running")

    result = run_service.request_cancel(db, run)

    assert result["outcome"] == "cancelling"
    db.refresh(run)
    assert run.status == "running"
    assert run_service.cancel_requested(run.id) is True


def test_the_flag_outlives_the_queue_wait(db, fake_redis):
    """A run cancelled while queued behind a backlog must still be stopped by
    the worker that eventually picks it up."""
    run = _run(db, _session(db).id, status="running")
    run_service.request_cancel(db, run)

    assert run_service.cancel_requested(run.id) is True
    run_service.clear_cancel_flag(run.id)
    assert run_service.cancel_requested(run.id) is False


def test_cancelling_a_finished_run_is_not_an_error(db, fake_redis):
    """Two clicks, a slow network, a reload — all of them land here."""
    run = _run(db, _session(db).id, status="completed")

    result = run_service.request_cancel(db, run)

    assert result["outcome"] == "already_finished"
    assert result["status"] == "completed"
    assert run_service.cancel_requested(run.id) is False


def test_a_redis_outage_refuses_rather_than_lying(db, monkeypatch):
    """Redis is how the web process reaches the worker. Without it we cannot
    stop the run, and saying we did is worse than saying we could not."""
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: None)
    run = _run(db, _session(db).id, status="running")

    with pytest.raises(RuntimeError):
        run_service.request_cancel(db, run)


def test_cancel_requested_is_false_when_redis_is_down(monkeypatch):
    """A Redis blip must not cancel work the user is waiting for. The opposite
    failure — a cancel that does not take — is visible and retryable."""
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: None)
    assert run_service.cancel_requested("any-run") is False


# ── the endpoint ────────────────────────────────────────────────────────────


def test_endpoint_cancels_the_users_own_run(db, fake_redis, auth_client):
    run = _run(db, _session(db).id, status="queued")

    body = auth_client.post(f"/api/runs/{run.id}/cancel").json()

    assert body["outcome"] == "cancelled"


def test_endpoint_will_not_cancel_someone_elses_run(db, fake_redis, intruder_client):
    run = _run(db, _session(db).id, status="running")

    assert intruder_client.post(f"/api/runs/{run.id}/cancel").status_code == 404
    db.refresh(run)
    assert run.status == "running"
    assert run_service.cancel_requested(run.id) is False


def test_endpoint_404s_an_unknown_run(db, fake_redis, auth_client):
    assert auth_client.post("/api/runs/does-not-exist/cancel").status_code == 404


def test_endpoint_503s_when_it_cannot_reach_the_worker(db, monkeypatch, auth_client):
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: None)
    run = _run(db, _session(db).id, status="running")

    assert auth_client.post(f"/api/runs/{run.id}/cancel").status_code == 503


# ── the worker actually stops ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_pump_stops_at_the_next_event_boundary(monkeypatch, fake_redis):
    """It must stop between events, not mid-write, and it must not swallow the
    cancellation into a plain completion."""
    from app.worker import run_tasks

    emitted: list[str] = []
    closed: list[bool] = []

    async def _fake_stream(**kwargs):
        try:
            for i in range(100):
                yield f"event: token\ndata: {{\"i\": {i}}}\n\n"
        finally:
            closed.append(True)

    monkeypatch.setattr(
        "app.services.langchain.streaming_handler.stream_router_response",
        _fake_stream,
    )
    monkeypatch.setattr(run_tasks, "_xadd_event",
                        lambda r, k, **kw: emitted.append(kw.get("event")))
    # Cancelled from the third event onward.
    monkeypatch.setattr(run_tasks.run_service, "cancel_requested",
                        lambda rid: len(emitted) >= 3)
    # The probe is throttled to once a second in production; this test is
    # about the boundary, not the throttle, so check on every event.
    monkeypatch.setattr(run_tasks, "_CANCEL_CHECK_INTERVAL_S", 0.0)

    with pytest.raises(run_tasks.RunCancelled):
        await run_tasks._pump(fake_redis, "stream-key", {}, run_id="r1")

    assert len(emitted) == 3, "it consumed events after the cancel was seen"
    assert closed == [True], "the generator's finally never ran"


@pytest.mark.asyncio
async def test_the_pump_runs_to_completion_when_nothing_cancels(monkeypatch, fake_redis):
    from app.worker import run_tasks

    emitted: list[str] = []

    async def _fake_stream(**kwargs):
        for i in range(5):
            yield f"event: token\ndata: {{\"i\": {i}}}\n\n"

    monkeypatch.setattr(
        "app.services.langchain.streaming_handler.stream_router_response",
        _fake_stream,
    )
    monkeypatch.setattr(run_tasks, "_xadd_event",
                        lambda r, k, **kw: emitted.append(kw.get("event")))
    monkeypatch.setattr(run_tasks.run_service, "cancel_requested", lambda rid: False)

    await run_tasks._pump(fake_redis, "stream-key", {}, run_id="r1")

    assert len(emitted) == 5


def test_a_cancelled_run_is_not_reported_as_a_failure():
    """`cancelled` is not `failed`: nothing went wrong, and the user should not
    get a "your run failed" notification for something they just did."""
    import inspect

    from app.worker import run_tasks

    source = inspect.getsource(run_tasks._run_router_task_inner)
    assert "except RunCancelled:" in source
    assert 'final_status = "cancelled"' in source
    # The notification is still gated on a genuine failure.
    assert 'final_status == "failed" and run2.user_id' in source


def test_the_worker_honours_a_row_already_marked_cancelled():
    """Two signals, either sufficient. The request may have closed the row
    before the worker's flag read; the reaper closes rows with no flag at all.
    A row that says cancelled is not run, whoever said it."""
    import inspect

    from app.worker import run_tasks

    source = inspect.getsource(run_tasks._run_router_task_inner)
    assert 'or run.status == "cancelled"' in source


def test_the_pump_probe_is_throttled():
    """One Redis round trip per streamed token is a cost nobody asked for."""
    from app.worker import run_tasks

    assert run_tasks._CANCEL_CHECK_INTERVAL_S >= 0.5


def test_a_run_cancelled_while_queued_never_starts():
    """RQ's own cancel is best-effort — a job already handed to a worker cannot
    be pulled back — so the worker's own check is what guarantees it."""
    import inspect

    from app.worker import run_tasks

    source = inspect.getsource(run_tasks._run_router_task_inner)
    start = source.index("cancel_requested")
    assert source.index('run.status = "running"') > start, (
        "the cancel check must come before the run is marked running"
    )
