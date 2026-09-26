from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.tender import Tender
from app.models.costing_template import BOQItem  # noqa: F401  (ensure metadata loaded)
from app.models.document_analysis import DocumentExtractionResult
from app.models.tender import TenderDocument
from app.models.platform_setting import PlatformSetting
from app.services.eager_analysis_service import (
    find_eager_analysis_candidates,
    capacity_ok,
    daily_count,
    bump_daily_count,
    gather_capacity,
)

def _db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e)
    return sessionmaker(bind=e)()

_n = 0
def _tender(db, score=0.9, eligible=False, archived=False, dup=False, status=None):
    global _n; _n += 1
    t = Tender(portal="ireps", tender_id=f"T{_n}", title=f"t{_n}",
               ai_relevance_score=score, is_eligible_indicator=eligible,
               is_archived=archived, is_duplicate=dup, eager_analysis_status=status)
    db.add(t); db.commit(); return t

def _doc(db, t):
    d = TenderDocument(tender_id=t.id, file_name=f"d{t.id}.pdf", file_path=f"p/{t.id}")
    db.add(d); db.commit(); return d

def test_selects_only_valid_in_scope_with_docs_unanalyzed():
    db = _db()
    good = _tender(db, score=0.9); _doc(db, good)                 # candidate
    elig = _tender(db, score=0.1, eligible=True); _doc(db, elig)  # eligible-flagged
    low = _tender(db, score=0.5); _doc(db, low)                   # below 0.70, not eligible
    nodocs = _tender(db, score=0.9)                               # no documents
    arch = _tender(db, score=0.9, archived=True); _doc(db, arch)  # archived
    inflight = _tender(db, score=0.9, status="running"); _doc(db, inflight)  # in-flight
    analyzed = _tender(db, score=0.9); _doc(db, analyzed)         # already analyzed
    db.add(DocumentExtractionResult(tender_id=analyzed.id, extraction_type="requirements")); db.commit()

    ids = find_eager_analysis_candidates(db)
    assert set(ids) == {good.id, elig.id}

def test_excludes_duplicate_tenders():
    db = _db()
    good = _tender(db, score=0.9); _doc(db, good)                   # candidate
    dup = _tender(db, score=0.9, dup=True); _doc(db, dup)            # duplicate, excluded

    ids = find_eager_analysis_candidates(db)
    assert set(ids) == {good.id}
    assert dup.id not in ids

def test_failed_status_is_reincluded_but_running_and_done_are_not():
    db = _db()
    failed = _tender(db, score=0.9, status="failed"); _doc(db, failed)      # retryable, included
    running = _tender(db, score=0.9, status="running"); _doc(db, running)   # in-flight, excluded
    done = _tender(db, score=0.9, status="done"); _doc(db, done)            # already terminal, excluded

    ids = find_eager_analysis_candidates(db)
    assert failed.id in ids
    assert running.id not in ids
    assert done.id not in ids

def test_eligible_archived_tender_still_excluded():
    db = _db()
    elig_archived = _tender(db, score=0.1, eligible=True, archived=True)
    _doc(db, elig_archived)

    ids = find_eager_analysis_candidates(db)
    assert elig_archived.id not in ids

def test_capacity_ok_pure():
    assert capacity_ok(queue_depth=5, rate_limits_5m=0, pool_util=0.3, ceiling=20) is True
    assert capacity_ok(queue_depth=25, rate_limits_5m=0, pool_util=0.3, ceiling=20) is False   # queue too deep
    assert capacity_ok(queue_depth=5, rate_limits_5m=1, pool_util=0.3, ceiling=20) is False     # recent 429
    assert capacity_ok(queue_depth=5, rate_limits_5m=0, pool_util=0.85, ceiling=20) is False    # pool saturated

def test_daily_count_roundtrip():
    db = _db()
    assert daily_count(db) == 0
    bump_daily_count(db, 3)
    assert daily_count(db) == 3
    bump_daily_count(db, 2)
    assert daily_count(db) == 5

def test_capacity_ok_inclusive_boundaries_pass():
    # queue_depth == ceiling and pool_util == 0.80 are both inclusive edges that should PASS
    assert capacity_ok(queue_depth=20, rate_limits_5m=0, pool_util=0.80, ceiling=20) is True

def test_daily_count_resets_on_day_rollover():
    db = _db()
    db.add(PlatformSetting(
        key="eager_analysis_daily_count",
        value="2000-01-01:99",
        value_type="string",
        category="eager_analysis",
    ))
    db.commit()

    assert daily_count(db) == 0  # stale date -> treated as no count yet today

    bump_daily_count(db, 1)
    assert daily_count(db) == 1  # fresh count for today, not 100

def test_gather_capacity_fails_closed_on_queue_read_error(monkeypatch):
    def _boom():
        raise RuntimeError("redis unavailable")

    import app.core.redis_client as redis_client
    monkeypatch.setattr(redis_client, "get_queue", _boom)

    db = _db()
    queue_depth, rate_limits, pool_util = gather_capacity(db)

    assert queue_depth == 10**9
    # The queue-read failure alone must be enough to defer the sweep.
    assert capacity_ok(queue_depth, rate_limits, pool_util, ceiling=20) is False

def test_gather_capacity_fails_closed_on_all_signal_errors(monkeypatch):
    def _boom_queue():
        raise RuntimeError("redis unavailable")

    def _boom_rate_limits(minutes=5):
        raise RuntimeError("metrics store unavailable")

    import app.core.redis_client as redis_client
    import app.core.llm_metrics as llm_metrics
    import app.core.database as database

    monkeypatch.setattr(redis_client, "get_queue", _boom_queue)
    monkeypatch.setattr(llm_metrics, "count_recent_rate_limits", _boom_rate_limits)

    class _BoomPool:
        def size(self):
            raise RuntimeError("pool unavailable")

    class _BoomEngine:
        pool = _BoomPool()

    monkeypatch.setattr(database, "engine", _BoomEngine())

    db = _db()
    queue_depth, rate_limits, pool_util = gather_capacity(db)

    assert queue_depth == 10**9
    assert rate_limits == 1
    assert pool_util == 1.0
    assert capacity_ok(queue_depth, rate_limits, pool_util, ceiling=20) is False
