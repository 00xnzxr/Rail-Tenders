"""The unscored filter returns only tenders with a NULL ai_relevance_score."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.services.tender_service import get_tenders, count_tenders


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


_n = 0


def _mk(db, score=None, below=False):
    global _n
    _n += 1
    t = Tender(portal="ireps", tender_id=f"U{_n}", title=f"u{_n}",
               ai_relevance_score=score, below_threshold=below)
    db.add(t); db.commit()
    return t


def test_unscored_returns_only_null_scores():
    db = _session()
    _mk(db, score=0.8)
    _mk(db, score=None)                 # unscored
    _mk(db, score=None, below=False)    # unscored
    _mk(db, score=0.2)

    items = get_tenders(db, unscored=True, include_below_threshold=True, limit=50, offset=0)
    assert len(items) == 2
    assert all(t.ai_relevance_score is None for t in items)
    assert count_tenders(db, unscored=True, include_below_threshold=True) == 2
