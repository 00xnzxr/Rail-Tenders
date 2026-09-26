from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.tender import Tender
from app.services.eager_analysis_service import reap_stuck_eager_analysis

def _db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e); return sessionmaker(bind=e)()

def test_reaps_stale_running_markers():
    db = _db()
    old = Tender(portal="ireps", tender_id="A", title="a", eager_analysis_status="running",
                 eager_analysis_at=datetime.now(timezone.utc) - timedelta(hours=2))
    fresh = Tender(portal="ireps", tender_id="B", title="b", eager_analysis_status="running",
                   eager_analysis_at=datetime.now(timezone.utc))
    done = Tender(portal="ireps", tender_id="C", title="c", eager_analysis_status="done")
    db.add_all([old, fresh, done]); db.commit()
    n = reap_stuck_eager_analysis(db, older_than_minutes=30)
    assert n == 1
    db.refresh(old); db.refresh(fresh); db.refresh(done)
    assert old.eager_analysis_status is None      # stale -> reset
    assert fresh.eager_analysis_status == "running"  # fresh -> kept
    assert done.eager_analysis_status == "done"      # terminal -> untouched
