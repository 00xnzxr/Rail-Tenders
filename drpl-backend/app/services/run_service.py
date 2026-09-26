"""
DRPL Backend - Agent Run service.

Thin layer between the API and the AgentRun table + RQ queue. Two jobs:

1. ``create_run`` allocates an AgentRun row upfront so the client gets a
   run_id to subscribe to before the worker has even picked the job up.
2. ``enqueue_run`` pushes the row's id onto the RQ queue. Returns None
   when Redis is off so callers can 503 cleanly; the UI then falls back
   to the legacy inline SSE endpoint.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)


_RUN_JOB_TIMEOUT_SECONDS = 60 * 30   # 30 min hard cap per run
_RUN_RESULT_TTL_SECONDS = 60 * 60    # keep the RQ result around for 1h


def create_run(
    db: Session,
    *,
    user_id: int,
    prompt: str,
    display_message: Optional[str] = None,
    proposal_session_id: Optional[int] = None,
    router_session_id: Optional[str] = None,
    tender_id: Optional[int] = None,
    file_metadata: Optional[dict] = None,
    selected_agents: Optional[list[str]] = None,
) -> AgentRun:
    """Persist a fresh AgentRun row in the 'queued' state."""
    run = AgentRun(
        id=str(uuid.uuid4()),
        user_id=user_id,
        proposal_session_id=proposal_session_id,
        router_session_id=router_session_id,
        tender_id=tender_id,
        prompt=prompt,
        display_message=display_message,
        status="queued",
        selected_agents=selected_agents,
        meta={"file_metadata": file_metadata} if file_metadata else {},
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def enqueue_run(run: AgentRun, db: Optional[Session] = None) -> Optional[str]:
    """Push a run onto RQ. Returns the RQ job id, or None when the queue is off."""
    from app.core.redis_client import get_queue
    q = get_queue()
    if q is None:
        return None

    # Lazy import so the worker module isn't loaded into the web server
    # process unless we're actually enqueueing (cheap, but keeps imports
    # honest).
    from app.worker.run_tasks import run_router_task

    try:
        job = q.enqueue(
            run_router_task,
            run.id,
            job_timeout=_RUN_JOB_TIMEOUT_SECONDS,
            result_ttl=_RUN_RESULT_TTL_SECONDS,
            failure_ttl=_RUN_RESULT_TTL_SECONDS,
        )
    except Exception as e:
        logger.error(f"enqueue_run[{run.id}]: RQ enqueue failed: {e}", exc_info=True)
        return None

    if db is not None:
        try:
            run.rq_job_id = job.id
            db.commit()
        except Exception:
            db.rollback()
    return job.id


def get_run_for_user(db: Session, run_id: str, user_id: int) -> Optional[AgentRun]:
    """Fetch a run, enforcing user scoping. Returns None on miss or wrong owner."""
    return (
        db.query(AgentRun)
        .filter(AgentRun.id == run_id, AgentRun.user_id == user_id)
        .first()
    )


def list_runs_for_user(
    db: Session,
    user_id: int,
    *,
    proposal_session_id: Optional[int] = None,
    limit: int = 50,
) -> list[AgentRun]:
    """Most-recent-first list of the user's runs, optionally scoped to a session."""
    q = db.query(AgentRun).filter(AgentRun.user_id == user_id)
    if proposal_session_id is not None:
        q = q.filter(AgentRun.proposal_session_id == proposal_session_id)
    return q.order_by(AgentRun.created_at.desc()).limit(limit).all()


#: A run cannot outlive the RQ job timeout, so anything still claiming to be
#: queued or running past it is a row nobody is going to finish.
LIVE_STATUSES = ("queued", "running")

#: Grace on top of the job timeout before a RUNNING row is called stranded.
#: RQ's death penalty fires at the timeout; the worker then needs a moment to
#: unwind and write a terminal status, and clocks between the web and worker
#: containers are not the same clock.
_REAP_GRACE_SECONDS = 5 * 60

#: How long a QUEUED row may wait before it is called stranded. Deliberately
#: generous and measured from creation, because a queue depth is a normal
#: operating condition here — four worker replicas at WORKER_CONCURRENCY=3 is
#: twelve slots, and `/health/capacity` exists because they fill. Only a job
#: that was actually lost (a flushed queue, a Redis restart) should reach it.
_REAP_QUEUED_AFTER_SECONDS = _RUN_JOB_TIMEOUT_SECONDS * 2


def _stale_run_clause():
    """SQL for "this row claims to be live and nothing is going to finish it".

    Staleness has to be measured from the clock each status actually runs on,
    and this measured both from ``created_at``. RQ's ``job_timeout`` starts
    when the worker PICKS THE JOB UP, not when it was enqueued — so a run that
    waited ten minutes in the queue and then ran for twenty-five was 35 minutes
    old while sitting comfortably inside its 30-minute timeout, and the reaper
    would cancel it out from under a worker that was still executing it. The
    Master's own wall-clock budget is the full 1800s, so any queue wait at all
    put a legitimate run past the old cutoff.

    Worse than the cancel: ``find_live_run_for_session`` shared the predicate,
    so the same run was reported as "no active run" to a user reattaching from
    another device — which is the exact failure the durability work set out to
    end. Both callers use this one clause so the two answers cannot drift.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    running_cutoff = now - timedelta(
        seconds=_RUN_JOB_TIMEOUT_SECONDS + _REAP_GRACE_SECONDS
    )
    queued_cutoff = now - timedelta(seconds=_REAP_QUEUED_AFTER_SECONDS)

    # The redundant `status IN (...)` is for the query planner: it is a plain
    # index predicate on an indexed column, so Postgres narrows to the handful
    # of live rows before evaluating the coalesce. Without it the OR-of-ANDs
    # invites a sequential scan of every run ever recorded, on a query that
    # runs at web boot.
    return and_(
        AgentRun.status.in_(LIVE_STATUSES),
        or_(
            and_(
                AgentRun.status == "running",
                # A row that reached "running" without a timestamp is
                # malformed; fall back to creation rather than treating it as
                # immortal.
                func.coalesce(AgentRun.started_at, AgentRun.created_at)
                < running_cutoff,
            ),
            and_(
                AgentRun.status == "queued",
                AgentRun.created_at < queued_cutoff,
            ),
        ),
    )


def find_live_run_for_session(
    db: Session, user_id: int, proposal_session_id: int
) -> Optional[AgentRun]:
    """The newest run for this session that is still queued or running.

    This is how a client that has no local pointer finds its way back to work
    already in flight — a different browser, a second device, a cleared cache.
    Tying reattachment to `localStorage` alone meant the run was durable but
    only reachable from the one machine that started it.

    Rows stranded by a killed worker are excluded here as well as being reaped
    at startup, so a browser can never reattach to a stream that will never
    produce another event.
    """
    return (
        db.query(AgentRun)
        .filter(
            AgentRun.user_id == user_id,
            AgentRun.proposal_session_id == proposal_session_id,
            AgentRun.status.in_(LIVE_STATUSES),
            ~_stale_run_clause(),
        )
        .order_by(AgentRun.created_at.desc())
        .first()
    )


def reap_stuck_runs() -> int:
    """Mark AgentRun rows stranded in queued/running as ``cancelled``.

    A worker killed by SIGKILL, a container restart or a deploy has no chance
    to write a terminal status, and nothing else ever did: `AgentExecution` had
    a startup reaper from the beginning, `AgentRun` never got one, so those
    rows claimed to be running forever. A client reattaching to one waits on a
    Redis stream that will never emit `run_done` — a spinner that turns until
    the endpoint's own 40-minute wall clock gives up.

    Best-effort, mirrors `agent_execution_finalizer.reap_stuck_executions`.
    Returns the number of rows updated.
    """
    from datetime import datetime, timezone

    from app.core.database import SessionLocal

    fresh = SessionLocal()
    try:
        stuck = fresh.query(AgentRun).filter(_stale_run_clause()).all()
        for row in stuck:
            # Read the stale status before overwriting it — the message is the
            # only record of which state the row was stranded in.
            was = row.status
            row.status = "cancelled"
            row.finished_at = datetime.now(timezone.utc)
            row.error_message = row.error_message or (
                f"Reaped on startup — row was status={was!r} past the run "
                f"timeout with no worker to finish it"
            )
        if stuck:
            fresh.commit()
            logger.info(
                f"reap_stuck_runs: marked {len(stuck)} stranded run(s) as "
                f"cancelled"
            )
            # A worker killed outright wrote nothing at the end; whatever it
            # had streamed is on the row. Put it where the user will look.
            # Per row, so one bad row cannot cost the others their answer.
            for row in stuck:
                try:
                    save_partial_turn(
                        fresh, row, reason="was interrupted by a restart"
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"reap_stuck_runs: partial turn for {row.id} failed: {e}"
                    )
                    try:
                        fresh.rollback()
                    except Exception:
                        pass
        return len(stuck)
    except Exception as e:  # noqa: BLE001 — a failed sweep must not block boot
        logger.warning(f"reap_stuck_runs: sweep failed: {type(e).__name__}: {e}")
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


# ── Cancellation ─────────────────────────────────────────────────────────
#
# A run spends the user's money until it stops, and until now nothing could
# stop one. The UI's Stop button aborted the client's fetch: the browser
# stopped listening and the worker carried on to completion, still calling
# models, still billing against the monthly cap. On a platform that meters
# per user and enforces a budget, "you cannot stop it" is not a missing
# convenience.
#
# Cancellation is cooperative, not a kill. The run executes in a worker
# process; the request asking it to stop is in the web process. RQ can kill a
# work horse, but that stops the run mid-statement — mid-write, mid-commit,
# with no `finally` — and this pipeline writes artifacts, messages and usage
# rows as it goes. So the request sets a flag, and the worker reads it between
# the events it emits and stops on a boundary of its own choosing.

#: How long the cancel flag lives. Longer than the job timeout so a run that
#: is cancelled while queued behind a backlog still sees it when it starts.
_CANCEL_FLAG_TTL_SECONDS = _RUN_JOB_TIMEOUT_SECONDS * 3


def cancel_requested(run_id: str) -> bool:
    """Has somebody asked this run to stop? False when Redis is unreachable.

    Failing to False is deliberate: a Redis blip must not cancel work the user
    is waiting for. The opposite failure — a cancel that does not take — is
    visible and retryable.
    """
    from app.core.redis_client import get_redis, run_cancel_key

    client = get_redis()
    if client is None:
        return False
    try:
        return bool(client.exists(run_cancel_key(run_id)))
    except Exception as e:
        logger.debug(f"cancel_requested[{run_id}]: {e}")
        return False


def clear_cancel_flag(run_id: str) -> None:
    """Drop the flag once the run has stopped. Best-effort; it has a TTL."""
    from app.core.redis_client import get_redis, run_cancel_key

    client = get_redis()
    if client is None:
        return
    try:
        client.delete(run_cancel_key(run_id))
    except Exception as e:
        logger.debug(f"clear_cancel_flag[{run_id}]: {e}")


def _release_user_slot(user_id: Optional[int], run_id: str) -> None:
    """Give the user back their concurrency slot.

    The worker does this when a run ends. A run cancelled before a worker ever
    picked it up has no worker to do it, and without this the slot stays taken
    until the admission key's own hour-long expiry.
    """
    from app.core.redis_client import get_redis

    if user_id is None:
        return
    client = get_redis()
    if client is None:
        return
    try:
        client.srem(f"drpl:user:{user_id}:active_runs", run_id)
    except Exception as e:
        logger.debug(f"_release_user_slot[{run_id}]: {e}")


def _emit_run_done(run_id: str, status: str, note: str) -> None:
    """Close the event stream so an attached browser stops waiting.

    Without this the SSE endpoint keeps sending keepalives at a run that has
    already stopped, until its own 40-minute deadline.
    """
    import json as _json

    from app.core.redis_client import (
        RUN_STREAM_TTL_SECONDS,
        get_redis,
        run_stream_key,
    )

    client = get_redis()
    if client is None:
        return
    key = run_stream_key(run_id)
    try:
        client.xadd(
            key,
            {
                "event": "run_done",
                "data": _json.dumps(
                    {"run_id": run_id, "status": status, "note": note}
                ),
            },
        )
        # This may be the first write to the key — a run cancelled while
        # queued has had no worker to create the stream — and a key created
        # here without an expiry outlives everything else about the run.
        client.expire(key, RUN_STREAM_TTL_SECONDS)
    except Exception as e:
        logger.debug(f"_emit_run_done[{run_id}]: {e}")


def request_cancel(db: Session, run: AgentRun) -> dict:
    """Ask a run to stop. Idempotent, and safe to call on a finished run.

    Returns ``{"run_id", "status", "outcome"}`` where outcome is:

    - ``"cancelled"``   — it is stopped now. Nothing had started yet.
    - ``"cancelling"``  — a worker is executing it and has been asked to stop;
      it will end at its next event boundary.
    - ``"already_finished"`` — it had already reached a terminal state.

    The two live cases are genuinely different and the caller should not have
    to pretend otherwise. Reporting a running job as "cancelled" the moment the
    flag is set would be the same lie as the Stop button that only closed the
    browser's connection.
    """
    from app.core.redis_client import get_redis, run_cancel_key

    if run.status not in LIVE_STATUSES:
        return {"run_id": run.id, "status": run.status, "outcome": "already_finished"}

    client = get_redis()
    if client is None:
        raise RuntimeError("Redis unavailable — cannot signal a running worker.")

    # Set the flag first, whatever the status. A run that is queued right now
    # may be picked up by a worker between this line and the commit below, and
    # the flag is what that worker reads at start-up.
    client.set(run_cancel_key(run.id), "1", ex=_CANCEL_FLAG_TTL_SECONDS)

    if run.status == "queued":
        # Nothing has started. Take the job out of the queue so a worker never
        # spends a model call on it, then close the row and the stream here —
        # there is no worker that will do it.
        if run.rq_job_id:
            try:
                from rq.job import Job

                Job.fetch(run.rq_job_id, connection=client).cancel()
            except Exception as e:
                # Not fatal: the start-of-job flag check stops it anyway.
                logger.info(
                    "cancel[%s]: RQ job %s not cancellable (%s); the worker's "
                    "own flag check will stop it.", run.id, run.rq_job_id, e,
                )

        run.status = "cancelled"
        run.finished_at = datetime.now(timezone.utc)
        run.error_message = run.error_message or "Cancelled before it started."
        db.commit()

        _release_user_slot(run.user_id, run.id)
        _emit_run_done(run.id, "cancelled", "Cancelled before it started.")
        # The flag deliberately stays (it has a TTL). RQ's cancel cannot pull
        # back a job a worker has already dequeued, and that worker's own
        # start-of-job check is what stops it — clearing the flag here would
        # race it: a worker checking a moment after this line would find
        # nothing and run the job we just told the user was cancelled. The
        # worker also honours the row's `cancelled` status, so either signal
        # is enough; keeping both is the point.
        logger.info("cancel[%s]: cancelled while queued", run.id)
        return {"run_id": run.id, "status": "cancelled", "outcome": "cancelled"}

    # Running. The worker owns the terminal write — the row keeps saying
    # `running` because it still is, until it actually stops.
    logger.info("cancel[%s]: stop requested while running", run.id)
    return {"run_id": run.id, "status": run.status, "outcome": "cancelling"}


# ── Partial output ───────────────────────────────────────────────────────
#
# Streamed text lived only in the Redis event stream, which expires an hour
# after the run ends, and the final answer was saved only when a run finished.
# A run killed at token 2,900 of 3,000 — a deploy, a crash, a cancel — left a
# row saying `cancelled` and nothing a person could read once Redis forgot it.
#
# Three pieces, in the order they happen. `PartialProgress` is what the worker
# accumulates from the events it fans out; `record_partial_progress` writes it
# to the row every few seconds and on exit; `save_partial_turn` turns it into
# an assistant message in the session history when a run ends without
# finishing — from the worker for a cancel or a failure, and from the startup
# reaper for a worker that was killed outright and wrote nothing.

#: Ceiling on stored text. The final answers already stored on messages are
#: unbounded; this is generous for anything that streams and bounds the row.
_PARTIAL_OUTPUT_MAX_CHARS = 200_000
#: Ceiling on stored trace entries — a step list, not an event log.
_PARTIAL_TRACE_MAX_ENTRIES = 200


class PartialProgress:
    """What a run has said and done so far, rebuilt from its own SSE events.

    Mirrors how the browser assembles the answer: `token` appends, `token_reset`
    discards what a model turn narrated before it called a tool, so the text is
    the reply and nothing else. The Master's `decision_action` /
    `decision_observation` events become the compact step list the stopped-run
    message renders — which tool ran, and whether its step came back.
    """

    def __init__(self) -> None:
        self._text: list[str] = []
        self._chars = 0
        self._trace: list[dict] = []
        self._dirty = False

    @staticmethod
    def parse(chunk: str) -> tuple[Optional[str], dict]:
        """(event, data) from one SSE-formatted chunk; data is {} if unparseable."""
        import json as _json

        event: Optional[str] = None
        data: dict = {}
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    parsed = _json.loads(line[5:].strip())
                    data = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    data = {}
        return event, data

    def note(self, chunk: str) -> None:
        event, data = self.parse(chunk)
        if event == "token":
            content = data.get("content")
            if (
                isinstance(content, str)
                and content
                and self._chars < _PARTIAL_OUTPUT_MAX_CHARS
            ):
                self._text.append(content)
                self._chars += len(content)
                self._dirty = True
        elif event == "token_reset":
            if self._text:
                self._text = []
                self._chars = 0
                self._dirty = True
        elif event == "decision_action":
            self._trace.append({
                "type": "action",
                "step": data.get("step"),
                "tool": data.get("tool"),
            })
            self._dirty = True
        elif event == "decision_observation":
            self._trace.append({
                "type": "observation",
                "step": data.get("step"),
                "is_error": bool(data.get("is_error")),
            })
            self._dirty = True

    @property
    def text(self) -> str:
        return "".join(self._text)[:_PARTIAL_OUTPUT_MAX_CHARS]

    @property
    def trace(self) -> list[dict]:
        return self._trace[-_PARTIAL_TRACE_MAX_ENTRIES:]

    @property
    def dirty(self) -> bool:
        return self._dirty

    def mark_flushed(self) -> None:
        self._dirty = False


def record_partial_progress(run_id: str, progress: PartialProgress) -> bool:
    """Write the streamed-so-far text and steps onto the row. Never raises.

    Its own short session per write, committed and closed at once: the pump's
    run session is held for the whole run, and this must not open a
    transaction on it that nothing closes until the run ends.
    """
    if not progress.dirty:
        return False
    from app.core.database import SessionLocal

    text = progress.text
    trace = progress.trace
    db = SessionLocal()
    try:
        db.query(AgentRun).filter(AgentRun.id == run_id).update(
            {
                "partial_output": text or None,
                "partial_trace": trace or None,
            },
            synchronize_session=False,
        )
        db.commit()
        progress.mark_flushed()
        return True
    except Exception as e:
        logger.debug(f"record_partial_progress[{run_id}]: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            db.close()
        except Exception:
            pass


def _render_partial_answer(run: AgentRun, reason: str) -> Optional[str]:
    """The message a stopped run leaves behind, or None if it had nothing.

    Headline first, so a reader knows before the first line that this is not
    a finished answer; then the text as streamed; then the step list when the
    Master's timeline recorded one. Deliberately not the timeout renderer in
    `decision_maker_agent` — that module pulls in the whole LangChain stack,
    and this also runs from the startup reaper in the web process.
    """
    text = (run.partial_output or "").strip()
    trace = [e for e in (run.partial_trace or []) if isinstance(e, dict)]
    if not text and not trace:
        return None

    lines = [
        f"_This run {reason} before it finished. Here is what it had produced:_",
        "",
    ]
    if text:
        lines += [text, ""]
    if trace:
        observed = {
            e.get("step") for e in trace
            if e.get("type") == "observation" and not e.get("is_error")
        }
        steps = []
        for e in trace:
            if e.get("type") != "action":
                continue
            tool = e.get("tool") or "unknown_tool"
            mark = (
                "completed" if e.get("step") in observed
                else "in progress when it stopped"
            )
            steps.append(f"- `{tool}` — {mark}")
        if steps:
            lines += ["**Steps:**", "", *steps, ""]
    lines.append(
        "_Anything marked completed is saved — say **Continue** and I will "
        "pick up from where this stopped rather than redo it._"
    )
    return "\n".join(lines).strip()


def save_partial_turn(db: Session, run: AgentRun, reason: str) -> bool:
    """Put what a stopped run had produced into the session history.

    This is where the user already looks — the transcript that survives a
    reload — rather than a column nobody's screen reads. Written for a run
    that ended in `cancelled` or `failed`; a completed run's answer was saved
    by the pipeline itself.

    Idempotent: nothing is written when an assistant turn already exists for
    the session since the run started. The pipeline saves its answer before
    the worker writes the terminal status, so a run whose status write failed
    and was later reaped would otherwise get its finished answer followed by a
    "partial" copy of the same thing.
    """
    if not run.router_session_id:
        return False
    content = _render_partial_answer(run, reason)
    if not content:
        return False

    from app.models.agent_memory import AgentConversationHistory

    since = run.started_at or run.created_at
    already = (
        db.query(AgentConversationHistory.id)
        .filter(
            AgentConversationHistory.session_id == run.router_session_id,
            AgentConversationHistory.role == "assistant",
            AgentConversationHistory.created_at >= since,
        )
        .first()
    )
    if already is not None:
        return False

    meta: dict = {
        "partial": True,
        "run_id": run.id,
        "run_status": run.status,
        "reason": reason,
        "routing": {"agents_used": ["decision_maker"]},
    }
    try:
        from app.services.run_cost_service import attach_run_cost

        attach_run_cost(db, meta, run.id)
    except Exception as e:  # noqa: BLE001 — the cost is a detail, the text is the point
        logger.debug(f"save_partial_turn[{run.id}]: run cost unavailable: {e}")

    from app.services.langchain.memory_service import save_conversation_turn

    turn = save_conversation_turn(
        db, run.router_session_id, "proposal_router", "assistant", content,
        metadata=meta, output_type="general", routed_from="decision_maker",
    )
    if turn is None:
        return False

    if run.proposal_session_id:
        from app.models.proposal import ProposalMessage

        try:
            db.add(ProposalMessage(
                session_id=run.proposal_session_id,
                role="assistant",
                content=content,
                message_type="text",
                metadata_json=meta,
            ))
            db.commit()
        except Exception as e:  # noqa: BLE001 — the history row already landed
            logger.warning(f"save_partial_turn[{run.id}]: ProposalMessage failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass
    logger.info("save_partial_turn[%s]: wrote partial answer (%s)", run.id, reason)
    return True
