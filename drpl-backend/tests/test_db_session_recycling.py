"""The agent run's DB session must not hold a transaction open across tool calls.

A Command Center run takes one request-scoped Session (`get_db`) and hands the
same object to every tool for the whole run. With `autocommit=False`, the first
read opens an implicit transaction that nothing closes, so the connection sits
*idle inside a transaction* while the model thinks. Neon terminates such a
session at `idle_in_transaction_session_timeout` (5 min on this account), and
the next tool call dies with "SSL connection has been closed unexpectedly" —
surfaced to the user as "A temporary database error occurred."

That is what killed session 306: `document_reader` at 09:18:26, a 2m18s model
call plus a 60s wait, then the same session at 09:23:35 — 5m09s later.
"""

import os
import tempfile
from typing import Optional

import pytest
from langchain_core.tools import BaseTool
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.models.tender import Tender, TenderDocument  # noqa: F401 — registers the tables before create_all
from app.models.user import User  # noqa: F401 — same
from app.services.langchain.db_recycle import recycle_session_between_calls


class _ReadingTool(BaseTool):
    """A real tool doing what every read tool does: one query on the shared session.

    It also records the raw connection it ran on, so a test can kill exactly the
    connection the run was using — the way the server does.
    """

    name: str = "reading_tool"
    description: str = "Reads one row using the injected session."
    db: Optional[Session] = None
    used: list = []

    class Config:
        arbitrary_types_allowed = True

    def _run(self) -> str:
        value = str(self.db.execute(text("SELECT 1")).scalar())
        self.used.append(self.db.connection().connection.dbapi_connection)
        return value


@pytest.fixture
def pooled_session():
    """A Session on its own pooled engine, shaped like the production one.

    `pool_pre_ping` matters: it is what lets a released connection be replaced
    when the server killed it. It cannot help a connection that is still
    checked out, which is the whole point of the bug.
    """
    path = os.path.join(tempfile.gettempdir(), "drpl_recycle_test.db")
    if os.path.exists(path):
        os.remove(path)
    engine = create_engine(f"sqlite:///{path}", pool_pre_ping=True)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_tool_call_leaves_no_open_transaction(pooled_session):
    """After a wrapped tool returns, the session holds no transaction.

    No open transaction means no idle-in-transaction session for the server to
    reap, however long the model takes before the next tool call.
    """
    tool = recycle_session_between_calls([_ReadingTool(db=pooled_session)], pooled_session)[0]

    tool._run()

    assert pooled_session.in_transaction() is False


@pytest.mark.asyncio
async def test_async_tool_call_leaves_no_open_transaction(pooled_session):
    """The async path is the one agents actually take, so it must recycle too."""
    tool = recycle_session_between_calls([_ReadingTool(db=pooled_session)], pooled_session)[0]

    await tool._arun()

    assert pooled_session.in_transaction() is False


def test_run_survives_a_connection_killed_between_tool_calls(pooled_session):
    """The regression: the server drops the connection while the model thinks.

    Killing the raw DBAPI connection is what Neon's idle-in-transaction reaper
    does. Without recycling the session is still holding that dead connection
    and the second call raises; with it, the connection went back to the pool
    and `pool_pre_ping` swaps in a live one.
    """
    inner = _ReadingTool(db=pooled_session, used=[])
    tool = recycle_session_between_calls([inner], pooled_session)[0]
    tool._run()

    # The server hangs up on the exact connection that call ran on, while the
    # agent is off doing a long model call. Unrecycled, the session is still
    # holding it. Recycled, it is back in the pool where pre_ping will catch it.
    inner.used[0].close()

    assert tool._run() == "1"


def test_uncommitted_work_is_never_discarded(pooled_session):
    """Recycling must not roll back a caller's pending writes.

    Ending the transaction is only safe when there is nothing in it to lose.
    A session carrying unflushed work is left exactly as it was found.
    """
    pooled_session.execute(text(
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT, "
        "name TEXT, hashed_password TEXT, is_active BOOLEAN, role TEXT, "
        "last_login_at DATETIME, created_at DATETIME, updated_at DATETIME)"
    ))
    pending = User(email="pending@drpl.local", name="Pending", hashed_password="x", role="costing_research")
    pooled_session.add(pending)

    tool = recycle_session_between_calls([_ReadingTool(db=pooled_session)], pooled_session)[0]
    tool._run()

    assert pending in pooled_session.new


# --- The wiring: tools reach agents through two assemblers, and both recycle ---


def test_loader_built_tools_release_the_transaction(db):
    """`tool_loader` is where every specialist agent gets its tools."""
    from app.services.langchain.tools.tool_loader import load_tools_by_keys

    tools = load_tools_by_keys(db, ["tender_lookup"])
    assert tools, "tender_lookup should be loadable"

    tools[0]._run(keyword="nothing-matches-this")

    assert db.in_transaction() is False


def test_master_catalog_tools_release_the_transaction(db):
    """The Master Agent's catalog is assembled separately — and failed first.

    The production failure was `document_reader` in the `decision_maker`
    catalog, not in a specialist's belt, so covering only `tool_loader` would
    have left the actual bug in place.
    """
    from app.services.langchain.graphs.decision_maker_agent import build_master_catalog

    tools = build_master_catalog(
        db, user_id=None, user_role=None, context={},
        session_id=None, proposal_session_id=None,
        conversation_history=None, file_metadata=None, stream_callback=None,
    )
    lookup = next((t for t in tools if t.name == "tender_lookup"), None)
    assert lookup is not None, "the Master catalog should carry tender_lookup"

    lookup._run(keyword="nothing-matches-this")

    assert db.in_transaction() is False


def test_wrapping_does_not_hide_the_tools_injected_attributes(pooled_session):
    """The wrapper must stay transparent.

    `tool_loader` injects `db` (and `agent_key`, `session_id`) onto tool
    instances, and `test_tool_db_injection` asserts a loaded tool still carries
    its session. A wrapper that shadows the inner tool breaks that contract —
    and would hide the next injection bug rather than surface it.
    """
    inner = _ReadingTool(db=pooled_session, used=[])
    wrapped = recycle_session_between_calls([inner], pooled_session)[0]

    assert wrapped.db is pooled_session
