"""Below-threshold tenders are hidden by default, shown when included."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.services.tender_service import get_tenders


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_below_threshold_hidden_by_default():
    db = _session()
    db.add_all([
        Tender(portal="gem", tender_id="1", title="big", below_threshold=False),
        Tender(portal="gem", tender_id="2", title="small", below_threshold=True),
    ])
    db.commit()
    tids = {t.tender_id for t in get_tenders(db)}
    assert "1" in tids
    assert "2" not in tids


def test_below_threshold_shown_when_included():
    db = _session()
    db.add(Tender(portal="gem", tender_id="2", title="small", below_threshold=True))
    db.commit()
    tids = {t.tender_id for t in get_tenders(db, include_below_threshold=True)}
    assert "2" in tids
