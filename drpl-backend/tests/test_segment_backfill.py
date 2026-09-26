from app.models.tender import Tender
from app.services.auto_scoring_service import resegment_all


def _seed(db, **kw):
    t = Tender(portal="gem", **kw)
    db.add(t); db.commit(); db.refresh(t)
    return t


def test_resegment_populates_scored_non_overridden(db):
    scored = _seed(db, tender_id="SEG-SCORED", title="scored",
                   ai_relevance_score=0.9, estimated_value=1000000.0,
                   segment=None, segment_overridden=False)
    overridden = _seed(db, tender_id="SEG-OVR", title="ovr",
                       ai_relevance_score=0.1, estimated_value=1000000.0,
                       segment="to_bid", segment_overridden=True)
    unscored = _seed(db, tender_id="SEG-UNSCORED", title="unscored",
                     ai_relevance_score=None, segment=None,
                     segment_overridden=False)

    result = resegment_all(db)

    db.refresh(scored); db.refresh(overridden); db.refresh(unscored)
    assert result["resegmented"] >= 1
    assert scored.segment is not None            # scored -> segment populated
    assert overridden.segment == "to_bid"        # override respected
    assert unscored.segment is None              # unscored left alone
