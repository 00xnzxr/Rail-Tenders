"""
Single source of truth for marking AgentExecution rows as terminal.

Every agent execution path (LangChain ReAct, chain_of_thought, OpenAI Agents,
the auto-analyzer sidecar, the tender pipeline) has the same problem: the
request `Session` may be poisoned (PendingRollbackError) by the time we want
to record the run's outcome — typically because a tool's failed DB query
left the session unable to commit. The historical defenses (commit on the
same session, or roll back and try again) all fail in that case, leaving
the monitoring row stuck at `status="running"` forever.

`finalize_execution_row` opens a fresh `SessionLocal()` (its own connection
+ transaction) and updates the row by primary key. It always succeeds when
the database is reachable, regardless of what state the caller's session
is in. Best-effort — never raises.

Also exposes `reap_stuck_executions` for a startup-time sweep that resets
any rows older than the RQ job timeout to `cancelled`.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_FINALIZE_ATTEMPTS = 3
_FINALIZE_RETRY_DELAY_S = 0.5


def finalize_execution_row(
    execution_id: Optional[int],
    *,
    status: str,
    output_summary: Optional[str] = None,
    error_message: Optional[str] = None,
    latency_ms: Optional[int] = None,
    tokens_input: Optional[int] = None,
    tokens_output: Optional[int] = None,
    cost_estimate: Optional[float] = None,
    metadata_json: Optional[dict] = None,
) -> bool:
    """Update an AgentExecution row to a terminal state via a fresh DB session.

    Returns True on success, False otherwise. Never raises.
    """
    if not execution_id:
        return False
    from app.core.database import SessionLocal
    from app.models.agent_builder import AgentExecution

    last_err: Optional[Exception] = None
    for attempt in range(_MAX_FINALIZE_ATTEMPTS):
        fresh = SessionLocal()
        try:
            row = fresh.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
            if row is None:
                logger.debug(f"finalize_execution_row: execution {execution_id} not found")
                return False
            row.status = status
            if output_summary is not None:
                row.output_summary = output_summary[:2000]
            if error_message is not None:
                row.error_message = error_message[:1000]
            if latency_ms is not None:
                row.latency_ms = latency_ms
            if tokens_input is not None:
                row.tokens_input = tokens_input
            if tokens_output is not None:
                row.tokens_output = tokens_output
            if cost_estimate is not None:
                row.cost_estimate = cost_estimate
            if metadata_json is not None:
                row.metadata_json = metadata_json
            fresh.commit()
            return True
        except Exception as e:
            last_err = e
            try:
                fresh.rollback()
            except Exception:
                pass
            if attempt < _MAX_FINALIZE_ATTEMPTS - 1:
                _time.sleep(_FINALIZE_RETRY_DELAY_S)
        finally:
            try:
                fresh.close()
            except Exception:
                pass

    logger.warning(
        f"finalize_execution_row: all {_MAX_FINALIZE_ATTEMPTS} attempts failed to update "
        f"execution {execution_id} → status={status}: "
        f"{type(last_err).__name__}: {last_err}"
    )
    return False


# Default reaper threshold — matches the RQ job timeout in run_service.py
# (30 min). Anything older than this definitely won't complete.
_DEFAULT_REAP_AFTER_MINUTES = 30

# Ceiling of a 32-bit signed INTEGER. `agent_executions.latency_ms` is widened
# to BIGINT on Postgres (see the drift fix in main.py + the Alembic revision),
# but a deployment that has not run either yet still has the narrow column, and
# SQLite has no such widening. Clamping keeps the reaper's batch UPDATE valid
# on every backend: a row stuck for months yields a meaningless duration
# anyway, so a saturated value loses nothing real.
_INT32_MAX = 2_147_483_647


def _bounded_latency_ms(created_at: "datetime") -> Optional[int]:
    """Milliseconds from `created_at` until now, clamped to fit an INTEGER.

    Returns None if the elapsed time can't be computed. Tolerates a naive
    `created_at` (SQLite reads timestamps back without a tzinfo) by assuming
    UTC, since subtracting naive from aware would otherwise raise.
    """
    if not created_at:
        return None
    try:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        elapsed_ms = int((datetime.now(timezone.utc) - created_at).total_seconds() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, min(elapsed_ms, _INT32_MAX))


def reap_stuck_executions(*, older_than_minutes: int = _DEFAULT_REAP_AFTER_MINUTES) -> int:
    """Mark any AgentExecution rows older than `older_than_minutes` and still
    `status="running"` as `cancelled`. Best-effort — never raises.

    Called once at server startup (and safe to call again) to clean up rows
    orphaned by process crashes / SIGKILL where no in-process finally could
    fire. Returns the number of rows updated.
    """
    from app.core.database import SessionLocal
    from app.models.agent_builder import AgentExecution

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    fresh = SessionLocal()
    try:
        stuck = (
            fresh.query(AgentExecution)
            .filter(
                AgentExecution.status == "running",
                AgentExecution.created_at < cutoff,
            )
            .all()
        )
        for row in stuck:
            row.status = "cancelled"
            row.error_message = (
                row.error_message
                or f"Reaped on startup — row was status=running for >{older_than_minutes}m"
            )
            if row.latency_ms is None and row.created_at:
                # Best-effort latency: how long it sat as "running" until reaped.
                # Anchor to created_at since that's the only timestamp we have.
                # Clamped — an ancient row's elapsed time overflows INTEGER and
                # would fail the whole batch UPDATE, stranding every stuck row.
                bounded = _bounded_latency_ms(row.created_at)
                if bounded is not None:
                    row.latency_ms = bounded
        if stuck:
            fresh.commit()
            logger.warning(
                f"reap_stuck_executions: marked {len(stuck)} stuck row(s) as "
                f"cancelled (older than {older_than_minutes}m)"
            )
        return len(stuck)
    except Exception as e:
        logger.warning(
            f"reap_stuck_executions: sweep failed: {type(e).__name__}: {e}"
        )
        try:
            fresh.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            fresh.close()
        except Exception:
            pass
