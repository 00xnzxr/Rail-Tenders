"""The run must outlive the client.

A Command Center costing run routed correctly, parsed the BOQ and made real LLM
calls — then the browser disconnected ~48s in. Because the assistant's reply is
persisted from inside the run, cancelling the response generator discarded
everything: the work, the API spend, and any record the turn happened. The
session looked exactly as though nothing had been sent.

These tests drive `detached_stream` directly and close the consumer, which is
the only honest way to simulate a disconnect here. Both `TestClient` and
httpx's `ASGITransport` run the app to completion regardless of whether the
caller keeps reading, so neither can express "the client went away" — an
earlier version of this file passed against the broken code because of exactly
that.
"""

import asyncio

import pytest

from app.services import detached_stream as ds


def _run(coro):
    return asyncio.run(coro)


def _slow_source(finished: list, events: int = 3, per_event: float = 0.01):
    """Emits a few events, then records that it reached the end.

    "Reached the end" stands in for the real thing: persisting the assistant
    turn is the last thing the run does.
    """

    async def _source():
        for i in range(events):
            await asyncio.sleep(per_event)
            yield f"chunk{i}"
        await asyncio.sleep(per_event)
        finished.append("persisted")

    return _source


# ── the regression ──────────────────────────────────────────────────────────


def test_run_finishes_after_the_consumer_walks_away():
    """Read one event, close the stream, and the run still completes."""
    finished: list = []

    async def scenario():
        agen = ds.detached_stream(_slow_source(finished), label="probe")
        first = await agen.__anext__()
        assert first == "chunk0"

        # The client is gone.
        await agen.aclose()

        # The run should still be in flight, and should still finish.
        for _ in range(200):
            if finished:
                break
            await asyncio.sleep(0.01)

    _run(scenario())

    assert finished == ["persisted"], (
        "the run died with the client — its reply would never be saved"
    )


def test_closing_the_consumer_does_not_cancel_the_task():
    """The mechanism, stated directly."""

    async def scenario():
        started = asyncio.Event()
        done = asyncio.Event()

        async def _source():
            started.set()
            yield "first"
            await asyncio.sleep(0.05)
            done.set()

        agen = ds.detached_stream(_source, label="probe")
        await agen.__anext__()
        await started.wait()
        await agen.aclose()
        await asyncio.wait_for(done.wait(), timeout=2)
        return True

    assert _run(scenario())


# ── the normal path is unchanged ────────────────────────────────────────────


def test_a_consumer_that_stays_receives_every_event():
    finished: list = []

    async def scenario():
        return [
            event
            async for event in ds.detached_stream(
                _slow_source(finished), label="probe"
            )
        ]

    events = _run(scenario())

    assert events == ["chunk0", "chunk1", "chunk2"]
    assert finished == ["persisted"]


def test_a_failing_run_reports_and_terminates():
    """A raising run must emit its error events and still end the stream — if
    the sentinel were skipped the response would hang open forever."""

    async def _boom():
        yield "partial"
        raise RuntimeError("agent exploded")

    async def scenario():
        return [
            event
            async for event in ds.detached_stream(
                _boom, label="probe", on_error=lambda e: [f"error: {e}"]
            )
        ]

    events = _run(scenario())

    assert events == ["partial", "error: agent exploded"]


def test_a_failing_run_without_a_handler_still_terminates():
    async def _boom():
        raise RuntimeError("immediate")
        yield  # pragma: no cover

    async def scenario():
        return [event async for event in ds.detached_stream(_boom, label="probe")]

    assert _run(scenario()) == []


# ── task bookkeeping ────────────────────────────────────────────────────────


def test_in_flight_runs_are_held_then_released():
    """asyncio holds only weak references to tasks, so an untracked detached
    run can be garbage collected mid-flight — quietly reintroducing the bug."""

    async def scenario():
        before = ds.in_flight_count()

        async def _work():
            await asyncio.sleep(0.02)

        task = asyncio.ensure_future(_work())
        ds.track(task, "probe")
        assert ds.in_flight_count() == before + 1

        await task
        await asyncio.sleep(0)
        assert ds.in_flight_count() == before

    _run(scenario())


def test_a_failed_detached_run_is_logged_not_swallowed(caplog):
    """Nobody is awaiting these tasks, so an unlogged failure is invisible."""

    async def scenario():
        async def _work():
            raise RuntimeError("detached boom")

        task = asyncio.ensure_future(_work())
        ds.track(task, "probe")
        try:
            await task
        except RuntimeError:
            pass
        await asyncio.sleep(0)

    with caplog.at_level("ERROR"):
        _run(scenario())

    assert any("detached boom" in r.message or "detached boom" in str(r.exc_info)
               for r in caplog.records)


# ── the hole in the first version of this fix ───────────────────────────────


def test_work_before_the_first_yield_is_still_detached():
    """Session 297: the attachment phase sat OUTSIDE the detached task.

    Reading two large tender PDFs takes ~20s, and the task was not created
    until after it. A client that disconnected during that window killed the
    generator before the run existed — the message was saved, the attachments
    were linked, and then nothing ran at all: no routing, no reply, no error.

    Everything slow must live inside the source, so closing the consumer at any
    point still leaves the work running.
    """
    reached: list = []

    async def _source():
        # Stands in for attachment extraction: slow, and before any agent work.
        await asyncio.sleep(0.05)
        reached.append("attachments")
        yield "first"
        await asyncio.sleep(0.05)
        reached.append("agent")

    async def scenario():
        agen = ds.detached_stream(_source, label="probe")
        first = await agen.__anext__()
        assert first == "first"
        await agen.aclose()
        for _ in range(200):
            if "agent" in reached:
                break
            await asyncio.sleep(0.01)

    _run(scenario())

    assert reached == ["attachments", "agent"], (
        "work was abandoned when the client left"
    )


def test_a_consumer_that_never_reads_still_lets_the_work_finish():
    """The harshest case: the client vanishes before reading anything."""
    finished: list = []

    async def scenario():
        agen = ds.detached_stream(_slow_source(finished), label="probe")
        # Start the generator (creating the task) then immediately abandon it.
        await agen.__anext__()
        await agen.aclose()
        for _ in range(200):
            if finished:
                break
            await asyncio.sleep(0.01)

    _run(scenario())
    assert finished == ["persisted"]
