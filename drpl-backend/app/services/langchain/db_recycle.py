"""Keep the agent run's database session out of an open transaction.

A Command Center run takes ONE request-scoped `Session` (`get_db`) and hands
that same object to every tool for the whole run — minutes, sometimes tens of
minutes. `SessionLocal` is `autocommit=False`, so the first read opens an
implicit transaction and nothing ever closes it. The connection then sits
*idle inside a transaction* for the entire model call.

Postgres reaps exactly that. Neon ships
`idle_in_transaction_session_timeout = 5min`, and when it fires the next tool
call raises `OperationalError: SSL connection has been closed unexpectedly` —
which `error_utils.format_user_error` reports to the user as "A temporary
database error occurred." Session 306 died this way: a read at 09:18:26, a
2m18s model call plus a 60s wait, then the same session at 09:23:35.

`pool_pre_ping` and `pool_recycle` in `app/core/database.py` cannot help. Both
act at *checkout*; neither can save a connection that was already checked out
when the server hung up. The fix is to not be holding one.

So: after every tool call, end the transaction. The connection goes back to the
pool, no idle-in-transaction session exists for the server to reap, and the next
use checks out a connection that `pool_pre_ping` has just validated.

Two things this is careful about:

**It never discards work.** `rollback()` on a session carrying unflushed
changes would silently lose them. A session with anything pending is left
exactly as found — correctness beats the connection hygiene, and gated writes
commit their own work anyway.

**It never breaks a run.** Releasing is best-effort. If the rollback itself
fails (typically because the connection is already dead) the exception is
swallowed: SQLAlchemy has invalidated the connection by then, which is the
outcome we wanted.

Applied at the same choke points as the write gate, immediately outside it, so
a suspended write releases its transaction too.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain_core.tools import BaseTool
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def release_transaction(db: Optional[Session]) -> None:
    """End ``db``'s open transaction so the connection returns to the pool.

    A no-op when there is nothing to release, and when the session is carrying
    uncommitted work that a rollback would throw away.
    """
    if db is None:
        return
    if db.new or db.dirty or db.deleted:
        logger.debug(
            "db_recycle: session has pending changes (%d new, %d dirty, %d deleted) "
            "— leaving its transaction open rather than discarding them.",
            len(db.new), len(db.dirty), len(db.deleted),
        )
        return
    try:
        db.rollback()
    except Exception as e:  # pragma: no cover - the connection is already gone
        logger.debug("db_recycle: could not release the transaction (%s)", e)


def recycle_session_between_calls(
    tools: list[BaseTool], db: Optional[Session]
) -> list[BaseTool]:
    """Wrap ``tools`` so each call ends by releasing ``db``'s transaction."""
    if db is None:
        return tools
    return [_wrap_one(t, db) for t in tools]


def _wrap_one(inner: BaseTool, db: Session) -> BaseTool:
    # Closure over `inner` rather than a pydantic field, for the same reason
    # tool_policy._wrap_one does it: BaseTool is a BaseModel and holding
    # another tool as a field fights its validation.
    _name = inner.name
    _description = inner.description
    _args_schema = inner.args_schema

    class _RecyclingTool(BaseTool):
        name: str = _name
        description: str = _description
        args_schema: Any = _args_schema

        class Config:
            arbitrary_types_allowed = True

        def __getattr__(self, item):
            # Stay transparent. `tool_loader` injects `db` (and `agent_key`,
            # `session_id`) onto the instances it builds, and callers read them
            # back off the loaded tool — `test_tool_db_injection` asserts
            # exactly that. A wrapper that shadowed the inner tool would break
            # the contract and hide the next injection bug instead of surfacing
            # it.
            try:
                return super().__getattr__(item)  # type: ignore[misc]
            except AttributeError:
                return getattr(inner, item)

        def _run(self, *args, **kwargs):  # type: ignore[override]
            try:
                return inner._run(*args, **kwargs)
            finally:
                release_transaction(db)

        async def _arun(self, *args, **kwargs):  # type: ignore[override]
            try:
                return await inner._arun(*args, **kwargs)
            finally:
                release_transaction(db)

    return _RecyclingTool()
