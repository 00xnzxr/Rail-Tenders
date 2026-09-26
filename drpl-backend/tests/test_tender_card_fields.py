from app.core.database import Base, engine, SessionLocal
from app.models.tender import Tender


def test_tender_has_card_columns():
    """The four new card fields persist on the Tender model."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        t = Tender(
            portal="tendertiger", tender_id="TEST-CARD-1", title="Card test",
            location="Saran, Bihar, India", bid_type="NCB",
            source_portal="gem", category="Railways Transport Services",
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        assert t.location == "Saran, Bihar, India"
        assert t.bid_type == "NCB"
        assert t.source_portal == "gem"
        assert t.category == "Railways Transport Services"
    finally:
        db.query(Tender).filter(Tender.tender_id == "TEST-CARD-1").delete()
        db.commit()
        db.close()


def test_tender_response_serializes_card_fields():
    from app.schemas import TenderResponse
    from datetime import datetime, timezone

    class _Row:
        id = 1; portal = "tendertiger"; tender_id = "X1"; title = "t"
        department = None; organisation = None; estimated_value = None
        emd_amount = 200000.0; closing_date = None; status = "open"
        ai_relevance_score = 0.9; ai_summary = "fit"; priority = "medium"
        workflow_status = "new"; assigned_to = None; eligibility_status = None
        created_at = datetime.now(timezone.utc)
        location = "Saran, Bihar, India"; bid_type = "NCB"
        source_portal = "gem"; category = "Railways Transport Services"

    out = TenderResponse.model_validate(_Row())
    assert out.location == "Saran, Bihar, India"
    assert out.bid_type == "NCB"
    assert out.source_portal == "gem"
    assert out.category == "Railways Transport Services"
    assert out.emd_amount == 200000.0
    assert out.ai_summary == "fit"


def test_ingest_maps_card_fields():
    from app.core.database import SessionLocal
    from app.schemas import TenderInput
    from app.services.tender_service import ingest_tender_batch, get_tenders
    from app.models.tender import Tender

    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.tender_id == "ING-1").delete(); db.commit()
        ti = TenderInput(
            portal="tendertiger", tenderId="ING-1", title="Goods transport",
            location="Saran, Bihar, India", bidType="NCB",
            sourcePortal="gem", category="Railways Transport Services",
        )
        res = ingest_tender_batch(db, [ti], user_id=1)
        assert res.new == 1
        row = db.query(Tender).filter(Tender.tender_id == "ING-1").first()
        assert row.location == "Saran, Bihar, India"
        assert row.bid_type == "NCB"
        assert row.source_portal == "gem"
        assert row.category == "Railways Transport Services"
    finally:
        db.query(Tender).filter(Tender.tender_id == "ING-1").delete(); db.commit(); db.close()


def test_get_tenders_relevance_sort_puts_nulls_last():
    from app.core.database import SessionLocal
    from app.models.tender import Tender
    from app.services.tender_service import get_tenders

    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.tender_id.in_(["REL-HI", "REL-NULL"])).delete(); db.commit()
        db.add(Tender(portal="gem", tender_id="REL-HI", title="hi", ai_relevance_score=0.95))
        db.add(Tender(portal="gem", tender_id="REL-NULL", title="null", ai_relevance_score=None))
        db.commit()
        rows = get_tenders(db, sort_by="relevance", limit=200)
        ids = [r.tender_id for r in rows]
        assert ids.index("REL-HI") < ids.index("REL-NULL")
    finally:
        db.query(Tender).filter(Tender.tender_id.in_(["REL-HI", "REL-NULL"])).delete(); db.commit(); db.close()


def test_ingest_truncates_overlong_card_fields():
    """Data-loss guard: on Postgres an over-length VARCHAR insert is rejected and
    the per-tender SAVEPOINT silently drops the tender. Card fields must be
    truncated to their column widths at ingest so the row is never lost."""
    from app.core.database import SessionLocal
    from app.schemas import TenderInput
    from app.services.tender_service import ingest_tender_batch
    from app.models.tender import Tender

    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.tender_id == "TRUNC-1").delete(); db.commit()
        overlong_bid_type = "X" * 200
        overlong_location = "Y" * 400
        overlong_source_portal = "Z" * 200
        overlong_category = "W" * 400
        ti = TenderInput(
            portal="tendertiger", tenderId="TRUNC-1", title="Overlong fields test",
            location=overlong_location, bidType=overlong_bid_type,
            sourcePortal=overlong_source_portal, category=overlong_category,
        )
        res = ingest_tender_batch(db, [ti], user_id=1)
        assert res.new == 1
        row = db.query(Tender).filter(Tender.tender_id == "TRUNC-1").first()
        assert row is not None
        assert len(row.bid_type) <= 50
        assert len(row.source_portal) <= 50
        assert len(row.location) <= 255
        assert len(row.category) <= 255
    finally:
        db.query(Tender).filter(Tender.tender_id == "TRUNC-1").delete(); db.commit(); db.close()


def test_relevance_sort_secondary_created_at():
    """Most rows share a NULL ai_relevance_score; ordering must fall back to
    created_at desc so pagination is stable rather than DB-order-dependent."""
    from datetime import datetime, timedelta, timezone
    from app.core.database import SessionLocal
    from app.models.tender import Tender
    from app.services.tender_service import get_tenders

    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.tender_id.in_(["REL-OLDER", "REL-NEWER"])).delete(); db.commit()
        older = datetime.now(timezone.utc) - timedelta(days=2)
        newer = datetime.now(timezone.utc) - timedelta(minutes=1)
        # Insert older row first so any DB-default insertion order would put
        # it ahead of newer if the secondary sort key were absent/wrong.
        db.add(Tender(portal="gem", tender_id="REL-OLDER", title="older", ai_relevance_score=None, created_at=older))
        db.commit()
        db.add(Tender(portal="gem", tender_id="REL-NEWER", title="newer", ai_relevance_score=None, created_at=newer))
        db.commit()
        rows = get_tenders(db, sort_by="relevance", limit=200)
        ids = [r.tender_id for r in rows]
        assert ids.index("REL-NEWER") < ids.index("REL-OLDER")
    finally:
        db.query(Tender).filter(Tender.tender_id.in_(["REL-OLDER", "REL-NEWER"])).delete(); db.commit(); db.close()
