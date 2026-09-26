"""Title search accepts multiple case-insensitive keywords."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tender import Tender
from app.services.tender_service import count_tenders, get_tenders


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_title_search_matches_every_keyword_case_insensitively():
    db = _session()
    try:
        db.add_all([
            Tender(portal="ireps", tender_id="search-1", title="Electrical work for railway station", status="open"),
            Tender(portal="ireps", tender_id="search-2", title="Electrical maintenance contract", status="open"),
            Tender(portal="ireps", tender_id="search-3", title="Railway platform repairs", status="open"),
        ])
        db.commit()

        rows = get_tenders(db, search="RAILWAY electrical", limit=50, offset=0)
        assert [row.tender_id for row in rows] == ["search-1"]
        assert count_tenders(db, search="railway electrical") == 1
    finally:
        db.close()
