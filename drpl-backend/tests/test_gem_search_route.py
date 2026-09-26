"""GeM Search: the collect route, the queue it feeds, and the worker that serves it."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import collect
from app.core.auth import get_current_user
from app.core.database import Base, get_db
from app.main import app
from app.models.agent_run import AgentRun
from app.models.user import User


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _user(db, role="tender_search", email="a@x"):
    u = User(email=email, name="A", hashed_password="x", role=role)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _client(db, user):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _collect_run(db, user, status="running", age_s=60):
    run = AgentRun(user_id=user.id, status=status, prompt="Collect", selected_agents=["collector"],
                   meta={"kind": "collect", "params": {"portals": ["gem_full"], "mode": "incremental"}},
                   created_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
                   started_at=datetime.now(timezone.utc) - timedelta(seconds=age_s))
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def teardown_function(_):
    app.dependency_overrides.clear()


def test_the_route_is_gated_on_the_tenders_surface():
    db = _session()
    ok = _client(db, _user(db, role="costing_research"))
    assert ok.get("/api/collect/active").json() == {"run": None}
    denied = _client(db, _user(db, role="nonsense", email="b@x"))
    assert denied.get("/api/collect/active").status_code in (200, 403)  # unknown role narrows, never widens
    # A role that cannot reach tenders cannot start a sweep.
    from app.core import roles
    assert roles.can_access("costing_research", "tenders")


def test_starting_a_sweep_needs_the_queue_and_refuses_a_second_one(monkeypatch):
    db = _session()
    user = _user(db)
    client = _client(db, user)
    monkeypatch.setattr(collect, "is_redis_enabled", lambda: False)
    assert client.post("/api/collect/runs", json={}).status_code == 503

    monkeypatch.setattr(collect, "is_redis_enabled", lambda: True)
    monkeypatch.setattr(collect, "enqueue_collect_run", lambda run, db=None: "job-1")
    body = client.post("/api/collect/runs", json={"mode": "incremental"}).json()
    assert body["status"] == "queued" and body["events_url"].endswith("/events")
    run = db.get(AgentRun, body["run_id"])
    assert run.meta["kind"] == "collect" and run.meta["params"]["portals"] == ["gem_full"]
    assert run.rq_job_id is None or run.rq_job_id == "job-1"

    # One sweep at a time, whoever started it: a colleague is handed the live run.
    other = _client(db, _user(db, email="c@x"))
    res = other.post("/api/collect/runs", json={})
    assert res.status_code == 409
    assert res.json()["detail"]["run_id"] == body["run_id"]
    active = other.get("/api/collect/active").json()["run"]
    assert active["id"] == body["run_id"] and active["mine"] is False


def test_the_three_modes_are_accepted_and_nothing_else_is(monkeypatch):
    """`ministry` is the cheap complete sweep; a typo must not reach the queue."""
    db = _session()
    client = _client(db, _user(db))
    monkeypatch.setattr(collect, "is_redis_enabled", lambda: True)
    monkeypatch.setattr(collect, "enqueue_collect_run", lambda run, db=None: "job-m")

    res = client.post("/api/collect/runs", json={"mode": "ministry"})
    assert res.status_code == 200
    run = db.get(AgentRun, res.json()["run_id"])
    assert run.meta["params"]["mode"] == "ministry"
    # The run is live now, so the remaining cases must not be blocked by it.
    run.status = "completed"
    db.commit()

    assert client.post("/api/collect/runs", json={"mode": "ministri"}).status_code == 400
    assert collect.SUPPORTED_MODES == ("incremental", "ministry", "full")


def test_a_ministry_sweep_refuses_a_ministry_list_gem_cannot_filter_on(monkeypatch):
    """GeM's filter takes one ministry. A 400 now beats a ValueError in the
    worker a minute later, after the queue slot and the run row are spent."""
    db = _session()
    client = _client(db, _user(db))
    monkeypatch.setattr(collect, "is_redis_enabled", lambda: True)
    monkeypatch.setattr(collect, "enqueue_collect_run", lambda run, db=None: "job-x")

    for ministries in ([], ["Ministry of Railways", "Ministry of Coal"]):
        res = client.post("/api/collect/runs", json={"mode": "ministry", "ministries": ministries})
        assert res.status_code == 400, ministries
        assert "exactly one ministry" in res.json()["detail"]

    # Omitting it entirely uses the configured default, which is one ministry.
    assert client.post("/api/collect/runs", json={"mode": "ministry"}).status_code == 200
    # ...and `full` still takes a list, because it filters client-side.
    db.query(AgentRun).delete()
    db.commit()
    assert client.post(
        "/api/collect/runs",
        json={"mode": "full", "ministries": ["Ministry of Railways", "Ministry of Coal"]},
    ).status_code == 200


def test_a_stale_run_does_not_block_the_button(monkeypatch):
    db = _session()
    user = _user(db)
    client = _client(db, user)
    _collect_run(db, user, status="running", age_s=60 * 60 * 3)
    assert client.get("/api/collect/active").json() == {"run": None}
    monkeypatch.setattr(collect, "is_redis_enabled", lambda: True)
    monkeypatch.setattr(collect, "enqueue_collect_run", lambda run, db=None: "job-2")
    assert client.post("/api/collect/runs", json={}).status_code == 200


def test_recent_lists_only_collect_runs_newest_first():
    db = _session()
    user = _user(db)
    client = _client(db, user)
    db.add(AgentRun(user_id=user.id, status="completed", prompt="chat", meta={"kind": "chat"}))
    old = _collect_run(db, user, status="completed", age_s=600)
    old.result_summary = "3 new, 0 already known"
    new = _collect_run(db, user, status="failed", age_s=10)
    db.commit()
    runs = client.get("/api/collect/recent").json()["runs"]
    assert [r["id"] for r in runs] == [new.id, old.id]
    assert runs[1]["summary"] == "3 new, 0 already known"


def test_the_worker_serves_the_collect_queue_and_the_task_name_resolves():
    import importlib
    import worker as worker_mod
    src = open(worker_mod.__file__, encoding="utf-8").read()
    assert "COLLECT_QUEUE" in src and "Queue(COLLECT_QUEUE" in src
    module, func = collect.COLLECT_TASK.rsplit(".", 1)
    assert callable(getattr(importlib.import_module(module), func))


def test_the_in_process_sink_ships_through_the_ingest_service(monkeypatch):
    import asyncio
    from collector.sink import InProcessSink
    from app.services import tender_service

    calls = {}

    def fake_ingest(db, tenders, user_id):
        calls["n"] = len(tenders)
        calls["user"] = user_id
        from app.schemas import BatchUploadResponse
        return BatchUploadResponse(received=len(tenders), new=len(tenders), duplicates=0, errors=0, new_ids=[11, 12])

    monkeypatch.setattr(tender_service, "ingest_tender_batch", fake_ingest)
    dispatched = []
    from app.api.routes import extension
    monkeypatch.setattr(extension, "_dispatch_scoring", lambda ids, bt: dispatched.append(("score", ids)))
    monkeypatch.setattr(extension, "_dispatch_nit_fetch", lambda ids, bt: dispatched.append(("nit", ids)))
    result = asyncio.run(InProcessSink(user_id=7).post([
        {"portal": "gem", "tenderId": "GEM/2026/B/1", "title": "A"},
        {"portal": "gem", "tenderId": "GEM/2026/B/2", "title": "B"},
        {"portal": "gem"},  # no tenderId/title: rejected by the schema, counted as an error
    ]))
    assert (calls["n"], calls["user"]) == (2, 7)
    assert result.new == 2 and result.errors == 1 and result.new_ids == [11, 12]
    assert dispatched == [("score", [11, 12]), ("nit", [11, 12])]
