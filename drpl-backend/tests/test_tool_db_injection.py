"""Tools must actually receive the database session the loader means to give them.

`tool_loader` injects `db` only when the tool class wants one, and it decided
that with `hasattr(tool_cls, "db")`. Under Pydantic v2 a declared field is not
a class attribute, so that test is False for every tool in the registry — and
every tool loaded through the shared loader has been running with `db=None`.

The visible symptom was weak web search. `web_search` resolves its API keys
from PlatformSetting first and `.env` second; with no session it can only read
`.env`, so a platform whose keys live in the database silently falls past
Gemini grounding and Tavily to DuckDuckGo on every call. The user reported the
results as poor. They were poor: it was the free fallback, every time.
"""

import pytest

import app.models  # noqa: F401
from app.core.database import Base, engine
from app.services.langchain.tools.tool_loader import (
    _ensure_registry,
    _TOOL_CLASS_REGISTRY,
    load_tools_by_keys,
)


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


DB_BACKED_TOOLS = [
    "web_search",
    "tender_lookup",
    "ratecard_lookup",
    "semantic_search",
    "document_reader",
    "checklist_reader",
]


def test_registry_declares_db_as_a_field_not_an_attribute():
    """The condition the loader used to depend on. Pinning it so the reason the
    old check failed stays legible."""
    _ensure_registry()
    cls = _TOOL_CLASS_REGISTRY["web_search"]

    assert "db" in cls.model_fields
    assert not hasattr(cls, "db"), "if this ever becomes True, hasattr was never the bug"


@pytest.mark.parametrize("key", DB_BACKED_TOOLS)
def test_db_backed_tools_get_a_session(db, key):
    loaded = load_tools_by_keys(db, [key], agent_key="test")

    assert loaded, f"{key} did not load at all"
    assert loaded[0].db is not None, f"{key} was constructed with db=None"


def test_web_search_can_reach_platform_settings(db):
    """The concrete consequence: without a session, API keys stored in the
    database are invisible and every search drops to DuckDuckGo."""
    from app.services.langchain.tools.web_search_tool import _get_search_config

    tool = load_tools_by_keys(db, ["web_search"], agent_key="test")[0]

    # The call must not raise, and must be reading through the tool's session.
    assert tool.db is not None
    _get_search_config(tool.db)
