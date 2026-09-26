"""
Task-local run_id propagation for structured logging.

Every log line emitted inside `with run_id_scope(run_id):` gets the
`[run=<first8>]` prefix — LangChain callbacks, SQLAlchemy queries,
tool executions, anything that goes through the stdlib logger. This
makes cross-service grep possible: one search in Railway logs finds
every log line for a single run across web + worker.

Usage:
    from app.core.run_context import run_id_scope
    with run_id_scope(run.id):
        ...  # all logs here get [run=abc12345] stamped on them
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Optional

_current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "drpl_run_id", default=None
)


@contextmanager
def run_id_scope(run_id: str):
    """Bind the current run_id to the context for the duration of the block."""
    token = _current_run_id.set(run_id)
    try:
        yield
    finally:
        _current_run_id.reset(token)


def current_run_id() -> Optional[str]:
    """Return the active run_id, or None if no scope is open.

    Use this in nested entrypoints to avoid shadowing a parent caller's
    run_id with a locally synthesised one — see callers in
    `document_analysis_agent.py` and `costing_agent.py`.
    """
    return _current_run_id.get()


class RunIdFilter(logging.Filter):
    """Logging filter that prefixes log messages with the active run_id, if any.

    Added to the root logger at app startup so every handler picks it up.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        run_id = _current_run_id.get()
        if run_id:
            try:
                record.msg = f"[run={str(run_id)[:8]}] {record.msg}"
            except Exception:
                pass
        return True


_installed = False


def install_run_id_filter() -> None:
    """Install the RunIdFilter on the root logger. Idempotent."""
    global _installed
    if _installed:
        return
    try:
        logging.getLogger().addFilter(RunIdFilter())
        _installed = True
    except Exception:
        pass
