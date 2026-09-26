from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.models.platform_setting import PlatformSetting  # noqa: F401
import app.worker.scheduled_tasks as st


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _mk(db, tid, **kw):
    t = Tender(portal="gem", tender_id=tid, title=tid, **kw)
    db.add(t); db.commit()
    old = datetime.now(timezone.utc) - timedelta(days=10)
    db.query(Tender).filter(Tender.id == t.id).update({Tender.created_at: old})
    db.commit(); db.refresh(t)
    return t


def test_archives_only_stale_discarded(monkeypatch):
    db = _session()
    monkeypatch.setattr(st, "SessionLocal", lambda: db)
    # eligible: discarded, old, workflow new, not overridden
    a = _mk(db, "a", segment="discarded", workflow_status="new")
    # protected: overridden
    b = _mk(db, "b", segment="discarded", workflow_status="new", segment_overridden=True)
    # protected: workflow moved
    c = _mk(db, "c", segment="discarded", workflow_status="in_progress")
    # not discarded
    d = _mk(db, "d", segment="not_bidable", workflow_status="new")

    out = st._run_discard_cleanup(db)  # pure-ish worker body, no queue
    for t in (a, b, c, d): db.refresh(t)
    assert a.is_archived is True
    assert b.is_archived is False
    assert c.is_archived is False
    assert d.is_archived is False
    assert out["archived"] == 1
