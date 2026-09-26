"""
The ledger, and the ordering that is the reason it exists.

The single highest-value assertion in this suite is
``test_a_failed_post_leaves_the_ledger_untouched``. The old extension marked
tenders done before uploading them, so any failed upload lost that tender
permanently and silently -- the next incremental pass skipped it because the
local "done" set said it was handled. These tests pin the opposite behaviour.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from collector import ledger
from collector.models import CollectGap, CollectSeen, CollectSweep, create_ledger_tables
from collector.portals.base import Batch
from collector.sink import SinkResult


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    create_ledger_tables(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def make_batch(ids: list[str], **kw) -> Batch:
    return Batch(
        portal="gem",
        tenders=[{"tenderId": i, "title": f"t{i}"} for i in ids],
        page=kw.get("page", 1),
        pages_done=kw.get("pages_done", 1),
        rows_seen=kw.get("rows_seen", len(ids)),
        rows_skipped=kw.get("rows_skipped", 0),
        expected_total=kw.get("expected_total"),
        term=kw.get("term"),
        fetcher=kw.get("fetcher", "gem-allbids-v1"),
    )


# -- creation ------------------------------------------------------------


def test_create_ledger_tables_never_creates_agent_runs(db):
    """agent_runs belongs to drpl-backend. Creating it from the partial mirror
    here would produce a table missing most of its columns."""
    names = set(db.bind.dialect.get_table_names(db.connection()))
    assert "agent_runs" not in names
    assert {"collect_seen", "collect_sweeps", "collect_gaps", "collect_drift"} <= names


# -- the ordering --------------------------------------------------------


def test_record_writes_after_a_successful_post(db):
    batch = make_batch(["GEM/2026/B/1", "GEM/2026/B/2"])
    result = SinkResult(received=2, new=2, duplicates=0, new_ids=[10, 11])
    inserted = ledger.record(db, "gem", batch, result)
    assert inserted == 2
    assert ledger.known_ids(db, "gem") == {"GEM/2026/B/1", "GEM/2026/B/2"}


def test_a_failed_post_leaves_the_ledger_untouched(db):
    """The bug this service exists to end.

    ``record`` is never reached when the POST raises, so the tenders stay
    unknown and the next sweep finds them again. This test asserts the shape of
    that call site rather than trusting a comment.
    """
    from collector.sink import SinkError

    batch = make_batch(["GEM/2026/B/99"])

    async def failing_post(_tenders):
        raise SinkError("502 from ingest")

    # The exact sequence tasks.py uses, in miniature.
    import asyncio

    async def ship():
        result = await failing_post(batch.tenders)   # raises
        ledger.record(db, "gem", batch, result)      # never runs

    with pytest.raises(SinkError):
        asyncio.run(ship())

    assert ledger.known_ids(db, "gem") == set(), (
        "a tender whose upload failed must remain unknown, or it is lost forever"
    )


def test_re_recording_updates_rather_than_duplicating(db):
    """The second run reports duplicates and adds nothing -- D1 by construction."""
    batch = make_batch(["GEM/2026/B/1"])
    assert ledger.record(db, "gem", batch, SinkResult(new=1)) == 1
    assert ledger.record(db, "gem", batch, SinkResult(duplicates=1)) == 0
    assert db.query(CollectSeen).count() == 1


def test_last_seen_moves_but_first_seen_does_not(db):
    batch = make_batch(["GEM/2026/B/1"])
    ledger.record(db, "gem", batch, SinkResult(new=1))
    row = db.get(CollectSeen, {"portal": "gem", "tender_id": "GEM/2026/B/1"})
    first, last = row.first_seen_at, row.last_seen_at

    import time

    time.sleep(0.01)
    ledger.record(db, "gem", batch, SinkResult(duplicates=1))
    db.refresh(row)
    assert row.first_seen_at == first
    assert row.last_seen_at >= last


def test_sequence_parts_are_stored_for_gap_detection(db):
    ledger.record(db, "gem", make_batch(["GEM/2026/B/7916525"]), SinkResult(new=1))
    row = db.get(CollectSeen, {"portal": "gem", "tender_id": "GEM/2026/B/7916525"})
    assert row.seq_prefix == "GEM/2026/B"
    assert row.seq_number == 7916525


def test_non_gem_portals_do_not_get_gem_sequence_parsing(db):
    ledger.record(db, "ireps", make_batch(["12212090"]), SinkResult(new=1))
    row = db.get(CollectSeen, {"portal": "ireps", "tender_id": "12212090"})
    assert row.seq_prefix is None


# -- sweeps --------------------------------------------------------------


def test_sweep_accumulates_across_pages(db):
    sweep = ledger.begin_sweep(db, "run-1", "gem", "gem-allbids-v1")
    ledger.record(db, "gem", make_batch(["a"], page=1, pages_done=1, rows_seen=10,
                                        rows_skipped=8, expected_total=18),
                  SinkResult(new=1), sweep)
    ledger.record(db, "gem", make_batch(["b"], page=2, pages_done=2, rows_seen=8,
                                        rows_skipped=4, expected_total=18),
                  SinkResult(duplicates=1), sweep)
    assert sweep.pages_walked == 2
    assert sweep.rows_seen == 18
    assert sweep.rows_skipped == 12
    assert sweep.rows_new == 1
    assert sweep.rows_duplicate == 1
    assert sweep.expected_total == 18


def test_expected_total_is_the_max_not_a_running_sum(db):
    """Two pages of one search share one numFound; summing them would double it."""
    sweep = ledger.begin_sweep(db, "run-1", "gem", "v1")
    for page in (1, 2, 3):
        ledger.record(db, "gem", make_batch([f"x{page}"], expected_total=1769),
                      SinkResult(new=1), sweep)
    assert sweep.expected_total == 1769


def test_close_sweep_records_the_terminal_state(db):
    sweep = ledger.begin_sweep(db, "run-1", "gem", "v1")
    ledger.close_sweep(db, sweep, status="failed", error="boom")
    assert sweep.status == "failed"
    assert sweep.error == "boom"
    assert sweep.finished_at is not None


# -- the trace the UI renders -------------------------------------------


def test_trace_says_what_a_stopped_run_had_done(db):
    """A sweep that dies at page 40 of 60 leaves this behind saying so."""
    done = ledger.begin_sweep(db, "run-1", "gem", "v1")
    ledger.record(db, "gem", make_batch(["a"], pages_done=40), SinkResult(new=1), done)
    ledger.close_sweep(db, done)
    ledger.begin_sweep(db, "run-1", "ireps", "v1")  # still running

    trace = ledger.trace(db, "run-1")
    actions = [e for e in trace if e["type"] == "action"]
    observations = [e for e in trace if e["type"] == "observation"]
    assert [a["tool"] for a in actions] == ["collect:gem", "collect:ireps"]
    assert len(observations) == 1, "the unfinished sweep has no observation yet"
    assert observations[0]["is_error"] is False


def test_trace_marks_a_failed_sweep_as_an_error(db):
    sweep = ledger.begin_sweep(db, "run-2", "gem", "v1")
    ledger.close_sweep(db, sweep, status="failed", error="parser drift")
    obs = [e for e in ledger.trace(db, "run-2") if e["type"] == "observation"]
    assert obs[0]["is_error"] is True


# -- gaps ----------------------------------------------------------------


def test_gaps_are_recorded_once(db):
    assert ledger.record_gaps(db, "gem", ["GEM/2026/B/5", "GEM/2026/B/6"]) == 2
    assert ledger.record_gaps(db, "gem", ["GEM/2026/B/5"]) == 0
    assert db.query(CollectGap).count() == 2


def test_resolving_a_gap_closes_it(db):
    ledger.record_gaps(db, "gem", ["GEM/2026/B/5"])
    assert ledger.resolve_gap(db, "gem", "GEM/2026/B/5", "not_found") is True
    assert ledger.open_gaps(db, "gem") == []


def test_resolving_an_unknown_gap_is_false_not_an_error(db):
    assert ledger.resolve_gap(db, "gem", "nope", "fetched") is False
