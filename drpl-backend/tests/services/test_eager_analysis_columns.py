from app.models.tender import Tender


def test_tender_has_eager_analysis_columns():
    t = Tender(portal="ireps", tender_id="X1", title="t",
               eager_analysis_status="queued")
    assert t.eager_analysis_status == "queued"
    assert t.eager_analysis_at is None
