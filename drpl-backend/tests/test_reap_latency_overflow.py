"""Regression: the startup reaper must not overflow agent_executions.latency_ms.

`reap_stuck_executions` back-fills latency as (now - created_at). For a row
that has sat at status="running" for months, that elapsed value runs to
billions of milliseconds — far past the 2^31-1 ceiling of a 32-bit INTEGER
column. On Postgres the whole batch UPDATE then fails with

    (psycopg2.errors.NumericValueOutOfRange) integer out of range

and *every* stuck row stays "running", so the sweep can never make progress
on any subsequent boot either.
"""
from datetime import datetime, timedelta, timezone

from app.models.agent_builder import AgentExecution
from app.services.agent_execution_finalizer import (
    _INT32_MAX,
    _bounded_latency_ms,
    reap_stuck_executions,
)


def test_bounded_latency_clamps_to_int32_ceiling():
    created = datetime.now(timezone.utc) - timedelta(days=155)  # ~13.4e9 ms
    value = _bounded_latency_ms(created)
    assert value is not None
    assert value == _INT32_MAX, "a months-old row must clamp, not overflow"


def test_bounded_latency_passes_through_normal_durations():
    created = datetime.now(timezone.utc) - timedelta(seconds=90)
    value = _bounded_latency_ms(created)
    assert 89_000 <= value <= 95_000, f"ordinary latency must be exact, got {value}"


def test_bounded_latency_handles_naive_created_at():
    """SQLite hands back naive datetimes; subtracting from an aware now raises."""
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    value = _bounded_latency_ms(created)
    assert value is not None and value > 0


def test_reaper_writes_a_storable_latency_for_an_ancient_row(db):
    ancient = AgentExecution(
        agent_id=1,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(days=155),
    )
    db.add(ancient)
    db.commit()
    row_id = ancient.id
    db.commit()

    assert reap_stuck_executions() >= 1

    db.expire_all()
    reaped = db.query(AgentExecution).filter(AgentExecution.id == row_id).one()
    assert reaped.status == "cancelled"
    assert reaped.latency_ms <= _INT32_MAX, "latency must fit a 32-bit column"
