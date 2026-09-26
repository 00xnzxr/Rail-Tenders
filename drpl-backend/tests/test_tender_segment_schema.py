"""Tender carries segment + segment_overridden columns."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_segment_defaults():
    db = _session()
    t = Tender(portal="gem", tender_id="1", title="X")
    db.add(t); db.commit(); db.refresh(t)
    assert t.segment is None
    assert t.segment_overridden is False
