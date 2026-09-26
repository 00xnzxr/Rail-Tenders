"""Worker entry points delegate to the batch service; reaper self-reschedules."""
import app.worker.auto_scoring_tasks as tasks


def test_score_new_tenders_delegates(monkeypatch):
    called = {}
    def _fake_score(db, ids):
        called["ids"] = ids
        return {"scored": len(ids)}
    monkeypatch.setattr(tasks, "score_tenders_batch", _fake_score)
    # SessionLocal is opened inside; stub it to a dummy
    class _DummyDB:
        def close(self): pass
    monkeypatch.setattr(tasks, "SessionLocal", lambda: _DummyDB())
    out = tasks.score_new_tenders([1, 2, 3])
    assert called["ids"] == [1, 2, 3]
    assert out["scored"] == 3


def test_reaper_noop_when_no_queue(monkeypatch):
    monkeypatch.setattr(tasks, "get_queue", lambda: None)
    # Even with no queue, it should not raise and should return a disabled marker
    out = tasks.reap_unscored_tenders()
    assert out.get("rescheduled") is False
