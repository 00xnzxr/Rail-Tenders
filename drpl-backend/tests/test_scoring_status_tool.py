"""The tender_scoring_status agent tool reports the backlog and can drain it."""
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.services.langchain.tools.scoring_status_tool import ScoringStatusTool

# Register models the backlog path touches so create_all builds their tables.
from app.models import platform_setting as _platform_setting  # noqa: F401
from app.models import message_batch as _message_batch  # noqa: F401


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


_counter = 0


def _mk(db, title, score=None):
    global _counter
    _counter += 1
    t = Tender(title=title, portal="ireps", tender_id=f"{title}-{_counter}",
               description="x", ai_relevance_score=score)
    db.add(t)
    db.commit()
    return t


def test_status_tool_reports_backlog():
    db = _session()
    _mk(db, "scored", score=0.7)
    _mk(db, "pending")
    tool = ScoringStatusTool(db=db)
    out = json.loads(tool._run(action="status"))
    assert out["pending"] == 1
    assert out["scored"] == 1


def test_status_tool_can_drain(monkeypatch):
    db = _session()
    from app.services.langchain.tools import scoring_status_tool as mod
    monkeypatch.setattr(mod, "drain_backlog",
                        lambda db, mode, limit=None: {"mode": mode, "scored": 5, "failed": 0})
    tool = ScoringStatusTool(db=db)
    out = json.loads(tool._run(action="drain", mode="live"))
    assert out["scored"] == 5


def test_tool_is_registered():
    from app.services.langchain.tools import tool_loader
    tool_loader._ensure_registry()
    assert "tender_scoring_status" in tool_loader._TOOL_CLASS_REGISTRY
