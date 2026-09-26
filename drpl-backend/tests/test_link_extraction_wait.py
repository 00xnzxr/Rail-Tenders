"""A chat upload does not stall its analysis for a minute.

`_wait_for_link_extraction` waits while any of the tender's top-level
documents is "pending" or "processing". The Command Center registered chat
PDFs as TenderDocuments without a status, so they sat at the column default
"pending" forever -- link extraction for a chat upload is tracked on its
ChatAttachment -- and every analysis of that tender waited out the full
sixty seconds, in a blocking sleep that also froze the run's progress events.
"""

import re
import uuid

import pytest

from app.models.tender import Tender, TenderDocument


@pytest.fixture
def tender(db):
    tid = 990000 + (uuid.uuid4().int % 9000)
    db.add(Tender(id=tid, portal="command_center", tender_id=f"cc-{uuid.uuid4().hex[:8]}",
                  title="New Session", source_url="x"))
    keys = [f"chat/{tid}/nit.pdf"]
    for k in keys:
        db.add(TenderDocument(tender_id=tid, file_name="nit.pdf", file_path=k,
                              extraction_status="completed"))
    db.commit()
    yield tid, keys
    db.query(TenderDocument).filter(TenderDocument.tender_id == tid).delete()
    db.query(Tender).filter(Tender.id == tid).delete()
    db.commit()


def test_chat_registered_documents_do_not_wait_for_link_extraction():
    import inspect
    from app.services.langchain import streaming_handler as sh

    src = inspect.getsource(sh)
    # Every chat registration of a PDF as a TenderDocument says it is not
    # waiting on a link extraction of its own.
    registrations = re.findall(r"db\.add\(TenderDocument\((.*?)\)\)", src, flags=re.DOTALL)
    assert len(registrations) == 4
    assert all('extraction_status="completed"' in r for r in registrations)


def test_a_long_pending_document_no_longer_stalls_analysis(db, tender):
    """Rows the old registration left "pending" are never processed; waiting
    on them cost a full minute on every analysis of the tender."""
    import time
    from datetime import datetime, timedelta, timezone
    from app.services.langchain.graphs import chat_agent_wrappers as caw

    tid, keys = tender
    doc = db.query(TenderDocument).filter(TenderDocument.file_path == keys[0]).one()
    doc.extraction_status = "pending"
    doc.uploaded_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()
    t0 = time.monotonic()
    caw._wait_for_link_extraction(db, None, tid, timeout=6)
    assert time.monotonic() - t0 < 2


def test_a_fresh_pending_document_is_still_waited_for(db, tender, monkeypatch):
    from app.services.langchain.graphs import chat_agent_wrappers as caw

    tid, keys = tender
    doc = db.query(TenderDocument).filter(TenderDocument.file_path == keys[0]).one()
    doc.extraction_status = "processing"
    db.commit()
    assert caw._recently_uploaded(doc.uploaded_at) is True
