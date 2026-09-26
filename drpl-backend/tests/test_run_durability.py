"""Work belongs to the user, not to the window.

A run that survives a closed browser is only half the requirement. The other
half is being able to find it again: durability the user cannot observe is
indistinguishable from a run that died. These cover the three ways that broke.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.agent_run import AgentRun
from app.models.proposal import ProposalSession
from app.services import run_service

OWNER_ID = 1
INTRUDER_ID = 2


def _session(db, owner_id=OWNER_ID):
    s = ProposalSession(created_by=owner_id, status="draft", title="s")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _run(db, session_id, *, status="running", age_minutes=0, user_id=OWNER_ID,
         started_minutes_ago=None):
    """A run row.

    ``age_minutes`` is how long ago it was ENQUEUED; ``started_minutes_ago`` how
    long ago a worker picked it up. They are two different clocks and the gap
    between them is the queue wait — the thing that made a single created_at
    cutoff cancel live work. Defaults to "picked up immediately" so the tests
    that do not care read the same as before.
    """
    now = datetime.now(timezone.utc)
    started = None
    if status == "running":
        started = now - timedelta(
            minutes=age_minutes if started_minutes_ago is None
            else started_minutes_ago
        )
    r = AgentRun(
        user_id=user_id,
        proposal_session_id=session_id,
        prompt="cost this tender",
        status=status,
        created_at=now - timedelta(minutes=age_minutes),
        started_at=started,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ── finding work already in flight ──────────────────────────────────────────


def test_a_live_run_is_discoverable_without_a_local_pointer(db):
    """The whole point: a second device can find the run the first one started."""
    s = _session(db)
    r = _run(db, s.id, status="running")

    found = run_service.find_live_run_for_session(db, OWNER_ID, s.id)

    assert found is not None and found.id == r.id


def test_a_queued_run_counts_as_live(db):
    """Between enqueue and the worker picking it up, the work still exists."""
    s = _session(db)
    r = _run(db, s.id, status="queued")

    assert run_service.find_live_run_for_session(db, OWNER_ID, s.id).id == r.id


def test_a_finished_run_is_not_offered(db):
    """Reattaching to a completed run would replay an answer already on screen."""
    s = _session(db)
    _run(db, s.id, status="completed")

    assert run_service.find_live_run_for_session(db, OWNER_ID, s.id) is None


def test_a_stranded_run_is_not_offered(db):
    """Past the job timeout and its grace: nobody is going to finish this.

    Offering it would reattach the browser to a stream that never emits
    another event — a spinner that turns until the endpoint gives up.
    """
    s = _session(db)
    _run(db, s.id, status="running", age_minutes=40)

    assert run_service.find_live_run_for_session(db, OWNER_ID, s.id) is None


def test_a_run_that_waited_in_the_queue_is_still_offered(db):
    """The two clocks. RQ's job_timeout starts when the worker PICKS THE JOB
    UP; a single created_at cutoff called a run stranded because it queued for
    fifteen minutes first — and then told the user reattaching from another
    device that there was no run at all, which is the failure this endpoint
    exists to fix."""
    s = _session(db)
    _run(db, s.id, status="running", age_minutes=40, started_minutes_ago=5)

    assert run_service.find_live_run_for_session(db, OWNER_ID, s.id) is not None


def test_a_queued_run_behind_a_backlog_is_still_offered(db):
    """Queue depth is a normal operating condition — twelve worker slots and a
    /health/capacity endpoint that exists because they fill."""
    s = _session(db)
    _run(db, s.id, status="queued", age_minutes=40)

    assert run_service.find_live_run_for_session(db, OWNER_ID, s.id) is not None


def test_a_queued_run_nothing_ever_picked_up_is_not_offered(db):
    """A flushed queue or a Redis restart loses the job; the row waits forever."""
    s = _session(db)
    _run(db, s.id, status="queued", age_minutes=70)

    assert run_service.find_live_run_for_session(db, OWNER_ID, s.id) is None


def test_another_users_run_is_not_offered(db):
    s = _session(db)
    _run(db, s.id, status="running", user_id=INTRUDER_ID)

    assert run_service.find_live_run_for_session(db, OWNER_ID, s.id) is None


def test_the_newest_live_run_wins(db):
    s = _session(db)
    _run(db, s.id, status="running", age_minutes=10)
    newest = _run(db, s.id, status="running", age_minutes=0)

    assert run_service.find_live_run_for_session(db, OWNER_ID, s.id).id == newest.id


# ── the endpoint ────────────────────────────────────────────────────────────


def test_active_endpoint_reports_the_live_run(db, auth_client):
    s = _session(db)
    r = _run(db, s.id, status="running")

    body = auth_client.get(f"/api/runs/active?proposal_session_id={s.id}").json()

    assert body["run"]["id"] == r.id
    assert body["run"]["status"] == "running"


def test_active_endpoint_reports_nothing_as_a_normal_answer(db, auth_client):
    """No run in flight is the ordinary case, not an error."""
    s = _session(db)

    r = auth_client.get(f"/api/runs/active?proposal_session_id={s.id}")

    assert r.status_code == 200
    assert r.json()["run"] is None


def test_active_is_not_swallowed_as_a_run_id(db, auth_client):
    """`/runs/active` must be declared before `/runs/{run_id}`.

    FastAPI matches in declaration order, so the wrong order routes this to
    the by-id handler with run_id="active" and answers 404 forever.
    """
    s = _session(db)

    assert auth_client.get(
        f"/api/runs/active?proposal_session_id={s.id}"
    ).status_code == 200


def test_another_users_session_gets_nothing(db, intruder_client):
    s = _session(db)
    _run(db, s.id, status="running")

    assert intruder_client.get(
        f"/api/runs/active?proposal_session_id={s.id}"
    ).json()["run"] is None


# ── reaping what the worker could not ───────────────────────────────────────


def test_reaper_marks_a_stranded_run_cancelled(db):
    """SIGKILL leaves no chance to write a terminal status. Nothing else did.

    The sweep is global, so it counts whatever else the suite has stranded —
    assert on this row, not on the tally.
    """
    s = _session(db)
    r = _run(db, s.id, status="running", age_minutes=40)

    assert run_service.reap_stuck_runs() >= 1

    db.expire_all()
    row = db.query(AgentRun).filter(AgentRun.id == r.id).first()
    assert row.status == "cancelled"
    assert row.finished_at is not None
    # The message must name the state it was stranded in, not the one we just
    # wrote over it.
    assert "'running'" in row.error_message


def test_reaper_leaves_a_run_a_worker_is_still_executing(db):
    """Enqueued 40 minutes ago, running for 5. Cancelling this row out from
    under a live worker is worse than leaving a stale one: the user watches a
    run marked cancelled while it is still spending their budget."""
    s = _session(db)
    r = _run(db, s.id, status="running", age_minutes=40, started_minutes_ago=5)

    run_service.reap_stuck_runs()

    db.expire_all()
    assert db.query(AgentRun).filter(AgentRun.id == r.id).first().status == "running"


def test_reaper_leaves_a_run_that_is_still_within_its_timeout(db):
    s = _session(db)
    r = _run(db, s.id, status="running", age_minutes=5)

    run_service.reap_stuck_runs()

    db.expire_all()
    assert db.query(AgentRun).filter(AgentRun.id == r.id).first().status == "running"


def test_reaper_does_not_touch_finished_runs(db):
    s = _session(db)
    r = _run(db, s.id, status="completed", age_minutes=99)

    run_service.reap_stuck_runs()

    db.expire_all()
    assert db.query(AgentRun).filter(AgentRun.id == r.id).first().status == "completed"


def test_reaper_keeps_an_existing_error_message(db):
    s = _session(db)
    r = _run(db, s.id, status="running", age_minutes=31)
    r.error_message = "provider refused the request"
    db.commit()

    run_service.reap_stuck_runs()

    db.expire_all()
    row = db.query(AgentRun).filter(AgentRun.id == r.id).first()
    assert row.error_message == "provider refused the request"


def test_reaper_is_idempotent(db):
    """A second sweep finds nothing left to do."""
    s = _session(db)
    _run(db, s.id, status="running", age_minutes=31)

    run_service.reap_stuck_runs()
    assert run_service.reap_stuck_runs() == 0
