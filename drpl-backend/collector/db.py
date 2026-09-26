"""
DRPL Collector - database access.

The collector runs INSIDE drpl-backend now (same image, same worker), so it
uses the backend's engine and session factory rather than an engine of its
own: one connection pool, one DATABASE_URL, and the ``agent_runs`` model is
the backend's own class (see models.py). The four ``collect_*`` ledger tables
are still declared here and created by ``ensure_ledger`` at first use.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def get_engine():
    from app.core.database import engine
    return engine


def get_session_factory():
    from app.core.database import SessionLocal as _factory
    return _factory


def SessionLocal() -> Session:
    """A new session on the backend's engine. Caller closes it."""
    return get_session_factory()()


@contextmanager
def session_scope():
    """Short-lived session, committed on clean exit and always closed."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_ledger() -> None:
    """Create the collector's own tables if they are missing.

    Safe to call at every run: ``create_ledger_tables`` names its four tables
    explicitly, so this never touches ``agent_runs`` or any backend table.
    """
    from collector.models import create_ledger_tables

    try:
        create_ledger_tables(get_engine())
    except Exception as e:  # noqa: BLE001 -- a failed create must not block a run
        logger.warning("ensure_ledger: could not create ledger tables: %s", e)


def reset_for_tests() -> None:
    """Kept for the collector's tests; the engine is the backend's."""
