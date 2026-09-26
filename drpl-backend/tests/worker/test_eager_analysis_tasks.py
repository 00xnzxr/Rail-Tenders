from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.tender import Tender
from app.models.document_analysis import DocumentExtractionResult
import app.worker.eager_analysis_tasks as tasks

def _db_factory():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e)
    return sessionmaker(bind=e)

def _mk(SessionLocal, **kw):
    db = SessionLocal(); t = Tender(portal="ireps", tender_id="T1", title="t", **kw)
    db.add(t); db.commit(); tid = t.id; db.close(); return tid

def test_runs_analysis_and_marks_done(monkeypatch):
    SL = _db_factory()
    tid = _mk(SL, ai_relevance_score=0.9)
    monkeypatch.setattr(tasks, "SessionLocal", SL)
    async def _fake(db, tender_id):
        db.add(DocumentExtractionResult(tender_id=tender_id, extraction_type="test")); db.commit(); return object()
    monkeypatch.setattr(tasks, "analyze_all_documents", _fake)
    out = tasks.eager_analyze_tender(tid)
    assert out["status"] == "done"
    db = SL()
    assert db.query(Tender).get(tid).eager_analysis_status == "done"

def test_idempotent_skip_when_already_analyzed(monkeypatch):
    SL = _db_factory(); tid = _mk(SL, ai_relevance_score=0.9)
    db = SL(); db.add(DocumentExtractionResult(tender_id=tid, extraction_type="test")); db.commit(); db.close()
    monkeypatch.setattr(tasks, "SessionLocal", SL)
    called = {"n": 0}
    async def _fake(db, tender_id): called["n"] += 1
    monkeypatch.setattr(tasks, "analyze_all_documents", _fake)
    out = tasks.eager_analyze_tender(tid)
    assert out["status"] == "skipped" and called["n"] == 0

def test_sweep_noop_when_disabled(monkeypatch):
    SL = _db_factory(); monkeypatch.setattr(tasks, "SessionLocal", SL)
    monkeypatch.setattr(tasks, "get_queue", lambda: object())  # queue "present"
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {"eager_analysis_enabled": False})
    out = tasks.enqueue_eager_analysis_sweep()
    assert out["reason"] == "disabled"

def test_sweep_enqueues_candidates_and_marks_queued(monkeypatch):
    SL = _db_factory()
    tid = _mk(SL, ai_relevance_score=0.9)
    monkeypatch.setattr(tasks, "SessionLocal", SL)
    enqueued = []
    class Q:
        count = 0
        def enqueue(self, path, *a, **k): enqueued.append((path, a)); return object()
        def enqueue_in(self, *a, **k): return object()
    monkeypatch.setattr(tasks, "get_queue", lambda: Q())
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {
        "eager_analysis_enabled": True, "eager_analysis_interval_seconds": 180,
        "eager_analysis_daily_cap": 200, "eager_analysis_queue_ceiling": 20})
    monkeypatch.setattr(tasks, "find_eager_analysis_candidates", lambda db: [tid])
    monkeypatch.setattr(tasks, "gather_capacity", lambda db: (0, 0, 0.1))
    out = tasks.enqueue_eager_analysis_sweep()
    assert out["enqueued"] == 1 and enqueued
    db = SL(); assert db.query(Tender).get(tid).eager_analysis_status == "queued"

def test_sweep_skips_on_backpressure(monkeypatch):
    SL = _db_factory(); monkeypatch.setattr(tasks, "SessionLocal", SL)
    monkeypatch.setattr(tasks, "get_queue", lambda: type("Q", (), {"count": 0, "enqueue_in": lambda *a, **k: None})())
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {
        "eager_analysis_enabled": True, "eager_analysis_interval_seconds": 180,
        "eager_analysis_daily_cap": 200, "eager_analysis_queue_ceiling": 20})
    monkeypatch.setattr(tasks, "gather_capacity", lambda db: (99, 0, 0.1))  # queue too deep
    out = tasks.enqueue_eager_analysis_sweep()
    assert out["reason"] == "backpressure" and out["enqueued"] == 0

def test_sweep_skips_at_daily_cap(monkeypatch):
    SL = _db_factory(); monkeypatch.setattr(tasks, "SessionLocal", SL)
    # Pre-set today's daily counter at/above the cap so remaining <= 0.
    db = SL()
    from app.services.eager_analysis_service import bump_daily_count
    bump_daily_count(db, 5)
    db.close()
    monkeypatch.setattr(tasks, "get_queue", lambda: type("Q", (), {"count": 0, "enqueue_in": lambda *a, **k: None})())
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {
        "eager_analysis_enabled": True, "eager_analysis_interval_seconds": 180,
        "eager_analysis_daily_cap": 5, "eager_analysis_queue_ceiling": 20})
    monkeypatch.setattr(tasks, "gather_capacity", lambda db: (0, 0, 0.1))  # healthy capacity
    out = tasks.enqueue_eager_analysis_sweep()
    assert out["reason"] == "daily_cap" and out["enqueued"] == 0

def test_sweep_reschedules_on_every_tick(monkeypatch):
    SL = _db_factory(); monkeypatch.setattr(tasks, "SessionLocal", SL)
    rescheduled = []
    class Q:
        count = 0
        def enqueue(self, path, *a, **k): return object()
        def enqueue_in(self, *a, **k): rescheduled.append((a, k)); return object()
    monkeypatch.setattr(tasks, "get_queue", lambda: Q())
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {"eager_analysis_enabled": False})
    out = tasks.enqueue_eager_analysis_sweep()
    assert out["reason"] == "disabled"
    assert rescheduled, "sweep must reschedule itself (enqueue_in) on every tick"

class _FakeRedisAlwaysFree:
    """set(..., nx=True) always succeeds — simulates a free slot. `eval`
    mimics the real compare-and-delete Lua script (get == argv[0] -> del)
    against an in-memory store so tests genuinely exercise the ownership
    check rather than a bare delete."""
    def __init__(self):
        self.store = {}
        self.deleted = []
    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True
    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)
        return 1
    def eval(self, script, numkeys, key, token):
        if self.store.get(key) == token:
            self.deleted.append(key)
            del self.store[key]
            return 1
        return 0

class _FakeRedisNoSlots:
    """set(..., nx=True) always fails — simulates all slots taken."""
    def set(self, key, value, nx=False, ex=None):
        return False
    def delete(self, key):
        raise AssertionError("delete should not be called when no slot was acquired")
    def eval(self, script, numkeys, key, token):
        raise AssertionError("eval/release should not be called when no slot was acquired")

def test_slot_unavailable_defers_and_resets_status(monkeypatch):
    SL = _db_factory(); tid = _mk(SL, ai_relevance_score=0.9)
    monkeypatch.setattr(tasks, "SessionLocal", SL)
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {"eager_analysis_max_concurrency": 1})
    monkeypatch.setattr(tasks, "get_redis", lambda: _FakeRedisNoSlots())
    called = {"n": 0}
    async def _fake(db, tender_id): called["n"] += 1
    monkeypatch.setattr(tasks, "analyze_all_documents", _fake)

    out = tasks.eager_analyze_tender(tid)

    assert out["status"] == "deferred"
    assert called["n"] == 0
    db = SL(); assert db.query(Tender).get(tid).eager_analysis_status is None

def test_slot_available_analyzes_and_releases(monkeypatch):
    SL = _db_factory(); tid = _mk(SL, ai_relevance_score=0.9)
    monkeypatch.setattr(tasks, "SessionLocal", SL)
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {"eager_analysis_max_concurrency": 1})
    fake_redis = _FakeRedisAlwaysFree()
    monkeypatch.setattr(tasks, "get_redis", lambda: fake_redis)
    called = {"n": 0}
    async def _fake(db, tender_id):
        called["n"] += 1
        db.add(DocumentExtractionResult(tender_id=tender_id, extraction_type="test")); db.commit()
    monkeypatch.setattr(tasks, "analyze_all_documents", _fake)

    out = tasks.eager_analyze_tender(tid)

    assert out["status"] == "done"
    assert called["n"] == 1
    assert fake_redis.deleted, "slot must be released after analysis"
    db = SL(); assert db.query(Tender).get(tid).eager_analysis_status == "done"

def test_acquire_analysis_slot_first_then_none():
    calls = {"n": 0}
    class _Redis:
        def set(self, key, value, nx=False, ex=None):
            calls["n"] += 1
            return calls["n"] == 1  # first call succeeds, subsequent ones fail
    redis = _Redis()
    slot1 = tasks._acquire_analysis_slot(redis, max_concurrency=1)
    assert slot1 is not None
    key1, token1 = slot1
    assert key1 == "eager-analysis-slot-0"
    assert isinstance(token1, str) and token1
    slot2 = tasks._acquire_analysis_slot(redis, max_concurrency=1)
    assert slot2 is None

def test_release_slot_is_token_owned_compare_and_delete():
    """A stale holder (token A) whose TTL expired must NOT delete a slot that
    a different process has since legitimately acquired (token B) — this is
    the concurrency guarantee the slot lock exists to enforce."""
    fake_redis = _FakeRedisAlwaysFree()
    slot = tasks._acquire_analysis_slot(fake_redis, max_concurrency=1)
    assert slot is not None
    slot_key, token_a = slot
    # Simulate: A's TTL expired, and a second process re-acquired the same
    # slot index with a new token B (overwriting the value in the store).
    token_b = "token-b-different-holder"
    fake_redis.store[slot_key] = token_b

    tasks._release_analysis_slot(fake_redis, slot_key, token_a)

    # The stale release (token A) must be a no-op: B's live lock survives.
    assert fake_redis.store.get(slot_key) == token_b
    assert slot_key not in fake_redis.deleted

    # Only the true owner (token B) can release it.
    tasks._release_analysis_slot(fake_redis, slot_key, token_b)
    assert slot_key not in fake_redis.store
    assert slot_key in fake_redis.deleted

def test_sweep_reaps_stale_marker_every_tick(monkeypatch):
    SL = _db_factory(); monkeypatch.setattr(tasks, "SessionLocal", SL)
    monkeypatch.setattr(tasks, "get_queue", lambda: type("Q", (), {"count": 0, "enqueue_in": lambda *a, **k: None})())
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", lambda db: {"eager_analysis_enabled": False})
    reaped = {"n": 0}
    def _fake_reap(db):
        reaped["n"] += 1
        return 0
    monkeypatch.setattr(tasks, "reap_stuck_eager_analysis", _fake_reap)
    tasks.enqueue_eager_analysis_sweep()
    assert reaped["n"] == 1, "sweep must call reap_stuck_eager_analysis every tick"

def test_sweep_reschedules_even_if_body_raises(monkeypatch):
    SL = _db_factory(); monkeypatch.setattr(tasks, "SessionLocal", SL)
    rescheduled = []
    class Q:
        count = 0
        def enqueue(self, path, *a, **k): return object()
        def enqueue_in(self, *a, **k): rescheduled.append((a, k)); return object()
    monkeypatch.setattr(tasks, "get_queue", lambda: Q())
    def _boom(db):
        raise RuntimeError("settings read failed")
    monkeypatch.setattr(tasks, "get_eager_analysis_settings", _boom)
    import pytest
    with pytest.raises(RuntimeError):
        tasks.enqueue_eager_analysis_sweep()
    assert rescheduled, "finally must still reschedule even when the body raises unexpectedly"
