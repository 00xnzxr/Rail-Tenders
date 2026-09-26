"""What one agent run cost, derived from the ledger.

Spend is **derived**, never counted separately — the same rule
`budget_service` follows. `APIUsageLog` already carries one row per LLM call
with its tokens and priced cost; `run_id` gives those rows a second axis, so a
run's cost is a `SUM` over the rows that name it rather than a counter someone
has to remember to increment.

A run is many calls: the Master, every worker it delegates to, every retry,
every failed call that still produced tokens. All of them count. Excluding
failures would under-report exactly the runs a user complains about.
"""

from __future__ import annotations

import logging
from typing import NamedTuple, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.api_usage import APIUsageLog

logger = logging.getLogger(__name__)


class RunCost(NamedTuple):
    """One run's spend. Zero-valued for a run with no ledger rows."""

    run_id: Optional[str]
    calls: int
    tokens_input: int
    tokens_output: int
    cost_usd: float
    user_id: Optional[int] = None
    agent_name: Optional[str] = None
    last_call_at: Optional[object] = None

    @property
    def tokens_total(self) -> int:
        return self.tokens_input + self.tokens_output


_EMPTY_COLUMNS = (
    func.count(APIUsageLog.id),
    func.coalesce(func.sum(APIUsageLog.tokens_input), 0),
    func.coalesce(func.sum(APIUsageLog.tokens_output), 0),
    func.coalesce(func.sum(APIUsageLog.cost_estimate), 0.0),
)


def run_cost(db: Session, run_id: str) -> RunCost:
    """Total spend for one run.

    Returns a zero-valued `RunCost` for a run with no rows rather than raising:
    the Command Center asks for this on every finished run, and a run whose
    calls never reached the ledger must render as zero, not as an error.
    """
    if not run_id:
        return RunCost(run_id=run_id, calls=0, tokens_input=0, tokens_output=0, cost_usd=0.0)

    row = (
        db.query(*_EMPTY_COLUMNS)
        .filter(APIUsageLog.run_id == run_id)
        .one()
    )
    calls, tin, tout, cost = row
    return RunCost(
        run_id=run_id,
        calls=int(calls or 0),
        tokens_input=int(tin or 0),
        tokens_output=int(tout or 0),
        cost_usd=float(cost or 0.0),
    )


def run_cost_payload(db: Session, run_id: str) -> dict:
    """`run_cost` as the small dict the UI stores on a message and renders."""
    cost = run_cost(db, run_id)
    return {
        "run_id": cost.run_id,
        "calls": cost.calls,
        "tokens_input": cost.tokens_input,
        "tokens_output": cost.tokens_output,
        "tokens_total": cost.tokens_total,
        "cost_usd": round(cost.cost_usd, 6),
    }


def run_costs_for(
    db: Session,
    *,
    user_id: Optional[int] = None,
    agent_name: Optional[str] = None,
    limit: int = 50,
) -> list[RunCost]:
    """Recent runs, newest first — one row per run, not per call.

    Rows with no `run_id` are excluded: they are real spend, but they belong to
    no run and grouping them would invent one giant phantom run out of every
    seeder and scheduled job the platform has ever executed.
    """
    q = (
        db.query(
            APIUsageLog.run_id,
            *_EMPTY_COLUMNS,
            func.max(APIUsageLog.user_id),
            func.max(APIUsageLog.agent_name),
            func.max(APIUsageLog.created_at),
        )
        .filter(APIUsageLog.run_id.isnot(None))
    )
    if user_id is not None:
        q = q.filter(APIUsageLog.user_id == user_id)
    if agent_name is not None:
        q = q.filter(APIUsageLog.agent_name == agent_name)

    rows = (
        q.group_by(APIUsageLog.run_id)
        # `id` breaks ties: two runs whose last calls land in the same clock
        # tick have equal `created_at`, and without a second key their order
        # is whatever the planner felt like — a listing that reshuffles
        # between refreshes, and a test that fails one run in twenty.
        .order_by(
            func.max(APIUsageLog.created_at).desc(),
            func.max(APIUsageLog.id).desc(),
        )
        .limit(limit)
        .all()
    )
    return [
        RunCost(
            run_id=r[0],
            calls=int(r[1] or 0),
            tokens_input=int(r[2] or 0),
            tokens_output=int(r[3] or 0),
            cost_usd=float(r[4] or 0.0),
            user_id=r[5],
            agent_name=r[6],
            last_call_at=r[7],
        )
        for r in rows
    ]


def attach_run_cost(db: Session, meta: dict, run_id: Optional[str] = None) -> dict:
    """Stamp what the run cost onto an assistant message's metadata.

    Stored on the message rather than streamed. The Command Center page
    concatenates SSE events into the string it saves, so a cost that only ever
    existed as a live event would vanish on reload; in `metadata_json` the
    number survives a refresh and needs no extra endpoint.

    ``run_id`` defaults to the ambient `run_id_scope` — two of the save sites
    have no run-id local, only the scope their caller opened.

    Never raises. A message that fails to save is a message the user loses;
    costing it is decoration and must not be able to take the message down.
    """
    try:
        from app.core.run_context import current_run_id

        resolved = run_id or current_run_id()
        if not resolved:
            return meta
        meta["run_cost"] = run_cost_payload(db, resolved)
    except Exception as e:
        logger.debug("attach_run_cost: skipped (%s)", e)
    return meta
