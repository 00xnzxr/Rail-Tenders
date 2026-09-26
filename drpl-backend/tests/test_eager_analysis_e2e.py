from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.tender import Tender
from app.models.tender import TenderDocument
from app.models.document_analysis import DocumentExtractionResult
import app.worker.eager_analysis_tasks as tasks
from app.services.eager_analysis_service import find_eager_analysis_candidates

def _SL():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e); return sessionmaker(bind=e)

def test_in_scope_with_docs_is_selected_then_analyzed(monkeypatch):
    SL = _SL(); db = SL()
    t = Tender(portal="ireps", tender_id="E1", title="e", ai_relevance_score=0.9)
    db.add(t); db.commit(); tid = t.id
    db.add(TenderDocument(tender_id=tid, file_name="d.pdf", file_path="p")); db.commit()

    assert find_eager_analysis_candidates(db) == [tid]      # selected

    monkeypatch.setattr(tasks, "SessionLocal", SL)
    async def _fake(db, tender_id):
        db.add(DocumentExtractionResult(tender_id=tender_id, extraction_type="requirements")); db.commit()
    monkeypatch.setattr(tasks, "analyze_all_documents", _fake)
    out = tasks.eager_analyze_tender(tid)
    assert out["status"] == "done"

    db2 = SL()
    assert db2.query(Tender).get(tid).eager_analysis_status == "done"
    assert find_eager_analysis_candidates(db2) == []        # now excluded (analyzed)
