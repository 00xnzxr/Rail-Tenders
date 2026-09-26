from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.services.tender_service import get_tenders, count_tenders


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    db.add_all([
        Tender(portal="gem", tender_id="1", title="A", segment="to_bid",
               ai_relevance_score=0.9, estimated_value=9_000_000, eligibility_status="eligible"),
        Tender(portal="gem", tender_id="2", title="B", segment="not_bidable",
               ai_relevance_score=0.5, estimated_value=1_000_000, eligibility_status="unknown"),
        Tender(portal="gem", tender_id="3", title="C", segment="discarded",
               ai_relevance_score=0.2, estimated_value=9_000_000, eligibility_status="not_eligible"),
    ])
    db.commit()


def test_segment_filter_and_count_match():
    db = _session(); _seed(db)
    items = get_tenders(db, segment="to_bid")
    assert {t.tender_id for t in items} == {"1"}
    assert count_tenders(db, segment="to_bid") == 1


def test_score_range_filter():
    db = _session(); _seed(db)
    items = get_tenders(db, score_min=0.6)
    assert {t.tender_id for t in items} == {"1"}
    assert count_tenders(db, score_min=0.6) == 1


def test_eligibility_and_value_filters_count_matches_items():
    db = _session(); _seed(db)
    for kw in ({"eligibility_status": "eligible"}, {"value_min": 5_000_000}, {"segment": "discarded"}):
        assert count_tenders(db, **kw) == len(get_tenders(db, **kw))
