from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.models.platform_setting import PlatformSetting  # noqa: F401
from app.services.auto_scoring_service import resegment_all


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_resegment_sets_segment_and_skips_overridden():
    db = _session()
    a = Tender(portal="gem", tender_id="1", title="A",
               ai_relevance_score=0.80, estimated_value=9_000_000)   # -> to_bid
    b = Tender(portal="gem", tender_id="2", title="B",
               ai_relevance_score=0.30, estimated_value=9_000_000)   # -> discarded
    c = Tender(portal="gem", tender_id="3", title="C",
               ai_relevance_score=0.30, estimated_value=9_000_000,
               segment="to_bid", segment_overridden=True)           # user override -> untouched
    d = Tender(portal="gem", tender_id="4", title="D")               # unscored -> stays None
    db.add_all([a, b, c, d]); db.commit()

    out = resegment_all(db)
    for t in (a, b, c, d):
        db.refresh(t)
    assert a.segment == "to_bid"
    assert b.segment == "discarded"
    assert c.segment == "to_bid"          # override respected
    assert d.segment is None              # unscored untouched
    assert out["resegmented"] >= 2
