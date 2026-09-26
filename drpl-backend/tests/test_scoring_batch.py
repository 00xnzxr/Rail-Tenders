"""Batch-API scoring: result application fans out to duplicates; batch drain dedupes."""
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.models.message_batch import MessageBatch
from app.services import batch_service

# Ensure the models the batch path touches are registered on Base for create_all.
from app.models import platform_setting as _platform_setting  # noqa: F401


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


_counter = 0


def _mk(db, title, score=None, scope="traction spares", value=None):
    global _counter
    _counter += 1
    t = Tender(title=title, portal="ireps", tender_id=f"{title}-{_counter}",
               description=scope, ai_relevance_score=score, estimated_value=value)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_apply_scoring_results_writes_and_fans_out():
    db = _session()
    rep = _mk(db, "rep")
    dup = _mk(db, "dup")           # shares rep's content hash in real use
    fanout = {"h1": [rep.id, dup.id]}
    batch = MessageBatch(
        batch_id="msgbatch_test", batch_type="tender_scoring",
        status="ended", model="claude-haiku-4-5-20251001",
        total_requests=1,
        metadata_json=json.dumps({"fanout": fanout}),
    )
    db.add(batch)
    db.commit()

    # one reply for the representative only
    updated = batch_service.apply_scoring_results(
        db, batch, {rep.id: "MATCH: 82\nREASON: core railway electrical"})
    db.refresh(rep)
    db.refresh(dup)

    assert updated == 2                      # representative + fanned-out duplicate
    assert rep.ai_relevance_score == 0.82
    assert dup.ai_relevance_score == 0.82    # duplicate got the same score, no tokens
    assert dup.fit_reasoning == "core railway electrical"
    assert rep.segment in ("to_bid", "not_bidable", "discarded")


def test_batch_drain_dedupes_before_submit(monkeypatch):
    from app.services import scoring_backlog_service as svc

    db = _session()
    a = _mk(db, "same", scope="supply of traction motors")
    b = _mk(db, "same", scope="supply of traction motors")   # identical -> one rep
    _mk(db, "other", scope="civil bridge works")

    captured = {}

    async def _fake_create(db_, ids, fanout, user_id=None):
        captured["ids"] = ids
        captured["fanout"] = fanout

        class _B:  # minimal stand-in
            batch_id = "msgbatch_fake"
        return _B()

    monkeypatch.setattr("app.services.batch_service.create_tender_scoring_batch", _fake_create)
    out = svc.drain_backlog(db, mode="batch")
    assert out["mode"] == "batch"
    assert out["submitted"] == 2          # a/b collapsed, "other" separate
    assert out["deduped_saved"] == 1
    assert len(captured["ids"]) == 2
    # the a/b pair shares one fanout entry pointing at both ids
    two = [ids for ids in captured["fanout"].values() if len(ids) == 2]
    assert two and sorted(two[0]) == sorted([a.id, b.id])
