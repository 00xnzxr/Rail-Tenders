from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
from app.services import scoring_backlog_service as svc


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


_counter = 0


def _mk(db, title, score=None, archived=False, attempts=0, scope="rail traction spares"):
    global _counter
    _counter += 1
    t = Tender(title=title, portal="ireps", tender_id=f"{title}-{_counter}",
               description=scope, ai_relevance_score=score,
               is_archived=archived, scoring_attempts=attempts)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_backlog_stats_counts_match_reaper_filter():
    db = _session()
    _mk(db, "scored", score=0.8)
    _mk(db, "pending-a")
    _mk(db, "pending-b")
    _mk(db, "archived-pending", archived=True)
    _mk(db, "retry-capped", attempts=99)  # above default max_retries

    stats = svc.backlog_stats(db)
    assert stats["scored"] == 1
    # pending excludes the archived one, includes the retry-capped one
    assert stats["pending"] == 3
    # drainable excludes retry-capped
    assert stats["drainable"] == 2
    assert stats["stuck_at_cap"] == 1
    assert "est_cost_inr" in stats
    assert isinstance(stats["enabled"], bool)


def test_dedupe_by_content_hash_collapses_identical_scope():
    db = _session()
    a = _mk(db, "Dup tender", scope="supply of traction motors")
    b = _mk(db, "Dup tender", scope="supply of traction motors")
    c = _mk(db, "Different", scope="civil bridge works")
    unique, fanout = svc.dedupe_by_content_hash([a, b, c])
    assert len(unique) == 2
    # the two identical ones share a hash pointing at both ids
    groups = [ids for ids in fanout.values() if len(ids) == 2]
    assert groups and sorted(groups[0]) == sorted([a.id, b.id])


def test_drain_live_scores_all_pending(monkeypatch):
    db = _session()
    from app.services import auto_scoring_service as ass

    _mk(db, "p1")
    _mk(db, "p2")

    # stub the LLM call: mark each scored tender with a fixed score
    def _fake_score_batch(db_, ids):
        for t in db_.query(Tender).filter(Tender.id.in_(ids)).all():
            t.ai_relevance_score = 0.7
        db_.commit()
        return {"scored": len(ids), "flagged_below_threshold": 0, "failed": 0}

    monkeypatch.setattr(svc, "score_tenders_batch", _fake_score_batch)

    out = svc.drain_backlog(db, mode="live")
    assert out["mode"] == "live"
    assert out["scored"] == 2
    # idempotent: nothing left
    assert svc.backlog_stats(db)["pending"] == 0
    assert svc.drain_backlog(db, mode="live")["scored"] == 0
