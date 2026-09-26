"""
The run, end to end.

Drives ``collect_task`` with a stub fetcher, a fake Redis and an in-memory
database, so the whole contract is exercised without touching a portal:

  * the AgentRun row goes queued -> running -> completed
  * events land on the right stream key in the right shape, ending in exactly
    one run_done
  * the ledger is written after each POST and never before
  * a cancel flag stops the sweep on a page boundary, not mid-batch
  * a failed ingest leaves those tenders unknown so the next run retries them
  * the concurrency slot is given back however the run ends
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from collector import db as db_mod
from collector import ledger, runbus, tasks
from collector.models import AgentRun, Base, CollectSeen, CollectSweep, create_ledger_tables
from collector.portals.base import Batch
from collector.sink import SinkError, SinkResult


# -- doubles -------------------------------------------------------------


class FakeRedis:
    """Enough Redis for runbus: streams, a cancel flag and a user set."""

    def __init__(self, cancelled: set[str] | None = None):
        self.streams: dict[str, list[dict]] = {}
        self.keys: set[str] = set(cancelled or set())
        self.sets: dict[str, set] = {}
        self.expired: dict[str, int] = {}

    def xadd(self, key, payload):
        self.streams.setdefault(key, []).append(payload)

    def exists(self, key):
        return 1 if key in self.keys else 0

    def delete(self, key):
        self.keys.discard(key)

    def expire(self, key, ttl):
        self.expired[key] = ttl

    def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)

    # test helpers
    def events(self, run_id):
        return [
            (p["event"], json.loads(p["data"]) if "data" in p else {})
            for p in self.streams.get(runbus.stream_key(run_id), [])
        ]

    def names(self, run_id):
        return [e for e, _ in self.events(run_id)]


class StubFetcher:
    """Two pages of two tenders each. No network."""

    portal = "gem"
    version = "gem-stub-v1"
    fill_baseline = {"tenderId": 1.0}

    def __init__(self, pages=2, per_page=2, fail_on=None):
        self.pages, self.per_page, self.fail_on = pages, per_page, fail_on
        self.closed = False

    async def sweep(self, known, params):
        for page in range(1, self.pages + 1):
            if self.fail_on == page:
                from collector.portals.base import PortalUnavailable

                raise PortalUnavailable("stub: portal said no")
            tenders = [
                {
                    "portal": "gem",
                    "tenderId": f"GEM/2026/B/{page}{i}",
                    "title": f"Tender {page}-{i}",
                    "description": f"Tender {page}-{i}",
                }
                for i in range(self.per_page)
            ]
            yield Batch(
                portal="gem",
                tenders=tenders,
                page=page,
                pages_done=page,
                rows_seen=self.per_page + 1,
                rows_skipped=1,
                expected_total=10,
                term="railway",
                fetcher=self.version,
            )

    async def aclose(self):
        self.closed = True


class StubSink:
    def __init__(self, fail_on_call=None):
        self.calls: list[list[dict]] = []
        self.fail_on_call = fail_on_call

    async def post(self, tenders):
        self.calls.append(list(tenders))
        if self.fail_on_call == len(self.calls):
            raise SinkError("ingest 502")
        return SinkResult(received=len(tenders), new=len(tenders), new_ids=[1, 2])

    async def aclose(self):
        pass


# -- fixtures ------------------------------------------------------------


@pytest.fixture()
def wired(monkeypatch):
    """An in-memory DB (with agent_runs), a fake Redis, and stub collaborators."""
    engine = create_engine("sqlite:///:memory:")
    # agent_runs is drpl-backend's own model now (collector.models re-exports
    # it), so its table is created from the backend's metadata.
    from app.core.database import Base as AppBase
    AppBase.metadata.create_all(bind=engine, tables=[AgentRun.__table__])
    Base.metadata.create_all(bind=engine)
    create_ledger_tables(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(tasks, "SessionLocal", Session)
    monkeypatch.setattr(tasks, "ensure_ledger", lambda: None)

    fake = FakeRedis()
    monkeypatch.setattr(runbus, "get_redis", lambda: fake)

    async def no_profile():
        from collector.scope import ScopeProfile

        return ScopeProfile()

    monkeypatch.setattr("collector.scope.fetch_profile", no_profile)

    state = {"redis": fake, "Session": Session, "fetcher": None, "sink": None}

    def install(fetcher, sink):
        state["fetcher"], state["sink"] = fetcher, sink
        monkeypatch.setattr(tasks, "build_fetcher", lambda portal: fetcher)
        monkeypatch.setattr(tasks, "Sink", lambda *a, **k: sink)
        monkeypatch.setattr(tasks, "InProcessSink", lambda *a, **k: sink)

    state["install"] = install
    return state


def make_run(Session, **meta_params) -> str:
    run_id = str(uuid.uuid4())
    s = Session()
    s.add(AgentRun(
        id=run_id, user_id=7, status="queued",
        meta={"kind": "collect", "params": {"portals": ["gem"], **meta_params}},
    ))
    s.commit()
    s.close()
    return run_id


# -- the happy path ------------------------------------------------------


def test_a_run_completes_and_ships_every_page(wired):
    fetcher, sink = StubFetcher(pages=2), StubSink()
    wired["install"](fetcher, sink)
    run_id = make_run(wired["Session"])

    out = tasks.collect_task(run_id)

    assert out["status"] == "completed"
    assert len(sink.calls) == 2, "one POST per page"
    assert fetcher.closed, "the fetcher was closed"

    s = wired["Session"]()
    run = s.get(AgentRun, run_id)
    assert run.status == "completed"
    assert run.started_at and run.finished_at
    assert run.error_message is None
    assert "new" in (run.result_summary or "")
    s.close()


def test_the_stream_carries_the_expected_events(wired):
    wired["install"](StubFetcher(pages=2), StubSink())
    run_id = make_run(wired["Session"])
    tasks.collect_task(run_id)

    names = wired["redis"].names(run_id)
    assert names[0] == "run_started"
    assert names[-1] == "run_done"
    assert names.count("run_done") == 1, "run_done terminates the client stream"
    assert names.count("collect_progress") == 2
    assert names.count("tenders_ingested") == 2
    assert "portal_done" in names


def test_tenders_ingested_carries_what_the_ui_appends(wired):
    wired["install"](StubFetcher(pages=1), StubSink())
    run_id = make_run(wired["Session"])
    tasks.collect_task(run_id)

    payload = next(d for e, d in wired["redis"].events(run_id) if e == "tenders_ingested")
    assert payload["portal"] == "gem"
    assert payload["new"] == 2
    assert payload["ids"] == [1, 2]


def test_the_stream_gets_an_expiry(wired):
    """A stream created without one lives in Redis for good."""
    wired["install"](StubFetcher(pages=1), StubSink())
    run_id = make_run(wired["Session"])
    tasks.collect_task(run_id)
    assert wired["redis"].expired[runbus.stream_key(run_id)] == runbus.STREAM_TTL_SECONDS


def test_the_ledger_records_every_shipped_tender(wired):
    wired["install"](StubFetcher(pages=2), StubSink())
    run_id = make_run(wired["Session"])
    tasks.collect_task(run_id)

    s = wired["Session"]()
    assert s.query(CollectSeen).count() == 4
    sweep = s.query(CollectSweep).filter_by(run_id=run_id).one()
    assert sweep.status == "completed"
    assert sweep.pages_walked == 2
    assert sweep.rows_new == 4
    assert sweep.rows_skipped == 2
    s.close()


def test_a_second_run_ships_nothing_new(wired):
    """The incremental sweep, working. Re-running is cheap and idempotent."""
    wired["install"](StubFetcher(pages=1), StubSink())
    tasks.collect_task(make_run(wired["Session"]))

    sink2 = StubSink()
    wired["install"](StubFetcher(pages=1), sink2)
    tasks.collect_task(make_run(wired["Session"]))

    s = wired["Session"]()
    assert s.query(CollectSeen).count() == 2, "no duplicate ledger rows"
    s.close()


# -- cancellation --------------------------------------------------------


def test_a_run_cancelled_while_queued_never_starts(wired):
    fetcher, sink = StubFetcher(), StubSink()
    wired["install"](fetcher, sink)
    run_id = make_run(wired["Session"])
    wired["redis"].keys.add(runbus.cancel_key(run_id))

    out = tasks.collect_task(run_id)

    assert out["status"] == "cancelled"
    assert sink.calls == [], "not one portal request was spent on it"
    assert wired["redis"].names(run_id) == ["run_done"]


def test_a_row_already_marked_cancelled_is_not_run(wired):
    """drpl-backend's reaper may have closed the row. Either signal is enough."""
    sink = StubSink()
    wired["install"](StubFetcher(), sink)
    run_id = make_run(wired["Session"])
    s = wired["Session"]()
    s.get(AgentRun, run_id).status = "cancelled"
    s.commit()
    s.close()

    assert tasks.collect_task(run_id)["status"] == "cancelled"
    assert sink.calls == []


def test_cancelling_mid_sweep_stops_on_a_page_boundary(wired, monkeypatch):
    """Cooperative, not a kill: the batch in flight is never left half-shipped."""
    sink = StubSink()
    wired["install"](StubFetcher(pages=5), sink)
    run_id = make_run(wired["Session"])

    # The flag is read once before the run starts (the queued check) and then
    # once per page. Letting the first three reads through means the queued
    # check passes and two pages ship; the fourth stops it.
    reads = {"n": 0}

    def cancel_on_the_fourth_read(_r, rid):
        if rid != run_id:
            return False
        reads["n"] += 1
        return reads["n"] > 3

    monkeypatch.setattr(tasks.runbus, "is_cancelled", cancel_on_the_fourth_read)

    out = tasks.collect_task(run_id)

    assert out["status"] == "cancelled"
    assert len(sink.calls) == 2, "stopped between pages, not inside one"
    assert all(len(call) == 2 for call in sink.calls), "no batch was half-shipped"

    s = wired["Session"]()
    assert s.get(AgentRun, run_id).status == "cancelled"
    # What it managed to ship is recorded and stays recorded.
    assert s.query(CollectSeen).count() == 4
    assert s.query(CollectSweep).filter_by(run_id=run_id).one().status == "cancelled"
    s.close()


def test_a_cancelled_run_gives_back_its_slot(wired):
    run_id = make_run(wired["Session"])
    wired["redis"].keys.add(runbus.cancel_key(run_id))
    wired["install"](StubFetcher(), StubSink())
    tasks.collect_task(run_id)
    assert run_id not in wired["redis"].sets.get(runbus.user_active_key(7), {run_id})


def test_a_completed_run_gives_back_its_slot_too(wired):
    wired["install"](StubFetcher(pages=1), StubSink())
    run_id = make_run(wired["Session"])
    tasks.collect_task(run_id)
    assert run_id not in wired["redis"].sets.get(runbus.user_active_key(7), {run_id})


# -- failure -------------------------------------------------------------


def test_a_failed_ingest_leaves_those_tenders_unknown(wired):
    """THE ordering guarantee, at the level of a whole run.

    Page 1 ships; page 2's POST fails. Page 1's tenders are in the ledger,
    page 2's are not -- so the next run finds them again instead of skipping
    them forever.
    """
    wired["install"](StubFetcher(pages=3), StubSink(fail_on_call=2))
    run_id = make_run(wired["Session"])

    out = tasks.collect_task(run_id)

    assert out["status"] == "failed"
    s = wired["Session"]()
    seen = {r.tender_id for r in s.query(CollectSeen).all()}
    assert seen == {"GEM/2026/B/10", "GEM/2026/B/11"}, "only page 1 was recorded"
    s.close()

    names = wired["redis"].names(run_id)
    assert "ingest_failed" in names
    assert names[-1] == "run_done"


def test_a_failing_portal_does_not_take_the_run_down(wired):
    """A parser or portal failure closes that sweep; the run still finishes."""
    wired["install"](StubFetcher(pages=3, fail_on=1), StubSink())
    run_id = make_run(wired["Session"])

    out = tasks.collect_task(run_id)

    assert out["status"] == "completed"
    assert "portal_failed" in wired["redis"].names(run_id)
    s = wired["Session"]()
    assert s.query(CollectSweep).filter_by(run_id=run_id).one().status == "failed"
    s.close()


def test_an_unknown_run_id_is_reported_not_raised(wired):
    wired["install"](StubFetcher(), StubSink())
    assert tasks.collect_task(str(uuid.uuid4()))["reason"] == "not_found"


def test_partial_trace_says_what_the_run_did(wired):
    """What the stopped-run message in drpl-backend renders."""
    wired["install"](StubFetcher(pages=2), StubSink())
    run_id = make_run(wired["Session"])
    tasks.collect_task(run_id)

    s = wired["Session"]()
    trace = s.get(AgentRun, run_id).partial_trace
    s.close()
    assert any(e.get("tool") == "collect:gem" for e in trace)
    assert any(e.get("type") == "observation" for e in trace)


# -- scope ---------------------------------------------------------------


def test_excluded_tenders_are_dropped_before_the_post(wired, monkeypatch):
    """No reason to spend a POST on a batch DRPL will discard anyway."""
    from collector.scope import ScopeProfile

    async def with_exclusions():
        return ScopeProfile(exclusion_terms=["Tender 1-0"])

    monkeypatch.setattr("collector.scope.fetch_profile", with_exclusions)
    sink = StubSink()
    wired["install"](StubFetcher(pages=1, per_page=2), sink)
    tasks.collect_task(make_run(wired["Session"]))

    shipped = [t["tenderId"] for call in sink.calls for t in call]
    assert "GEM/2026/B/10" not in shipped
    assert "GEM/2026/B/11" in shipped


def test_an_unknown_portal_is_ignored_rather_than_crashing(wired):
    wired["install"](StubFetcher(pages=1), StubSink())
    run_id = make_run(wired["Session"], portals=["nonsense"])
    s = wired["Session"]()
    s.get(AgentRun, run_id).meta = {"kind": "collect", "params": {"portals": ["nonsense"]}}
    s.commit()
    s.close()
    # Falls back to gem rather than failing the run.
    assert tasks.collect_task(run_id)["status"] == "completed"


# -- coverage reaches the UI ---------------------------------------------


class _Stats:
    """Stands in for FullSweepStats on the portal_done path."""

    def __init__(self, coverage, complete):
        self.coverage = coverage
        self.is_complete = complete
        self.rows_distinct = 1703
        self.pages_failed = 0
        self.final_total = 1703


def test_portal_done_carries_coverage_when_the_sweep_measured_it(wired):
    fetcher = StubFetcher(pages=1)
    fetcher.stats = _Stats(1.0, True)
    wired["install"](fetcher, StubSink())
    run_id = make_run(wired["Session"])
    tasks.collect_task(run_id)

    payload = next(d for e, d in wired["redis"].events(run_id) if e == "portal_done")
    assert payload["coverage"] == 1.0
    assert payload["complete"] is True
    assert payload["rows_distinct"] == 1703


def test_portal_done_reports_a_partial_sweep_as_partial(wired):
    fetcher = StubFetcher(pages=1)
    fetcher.stats = _Stats(0.62, False)
    wired["install"](fetcher, StubSink())
    run_id = make_run(wired["Session"])
    tasks.collect_task(run_id)

    payload = next(d for e, d in wired["redis"].events(run_id) if e == "portal_done")
    assert payload["coverage"] == 0.62
    assert payload["complete"] is False


def test_portal_done_omits_coverage_when_the_sweep_cannot_claim_one(wired):
    """An incremental sweep stops on known ground and has not measured anything.

    Absent must stay absent: a default of 0, or of True, would both be lies,
    and the UI keys off the absence to say "newest only" instead of claiming
    a coverage figure nobody computed.
    """
    wired["install"](StubFetcher(pages=1), StubSink())   # no .stats at all
    run_id = make_run(wired["Session"])
    tasks.collect_task(run_id)

    payload = next(d for e, d in wired["redis"].events(run_id) if e == "portal_done")
    assert "coverage" not in payload
    assert "complete" not in payload
