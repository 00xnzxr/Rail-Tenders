"""Upload dispatches scoring to RQ when a queue exists, else inline."""
import app.api.routes.extension as ext


def test_dispatch_uses_queue_when_available(monkeypatch):
    enqueued = {}
    class _Q:
        def enqueue(self, fn, *args, **kwargs):
            enqueued["fn"] = fn; enqueued["args"] = args
    monkeypatch.setattr(ext, "get_queue", lambda: _Q())
    added = []
    class _BG:
        def add_task(self, fn, *a): added.append((fn, a))
    ext._dispatch_scoring([10, 11], _BG())
    assert enqueued["args"] == ([10, 11],)
    assert added == []   # not inline when queue present


def test_dispatch_falls_back_inline_without_queue(monkeypatch):
    monkeypatch.setattr(ext, "get_queue", lambda: None)
    added = []
    class _BG:
        def add_task(self, fn, *a): added.append((fn, a))
    ext._dispatch_scoring([12], _BG())
    assert len(added) == 1   # inline BackgroundTask used
    assert added[0][1] == ([12],)


def test_dispatch_noop_when_scoring_disabled(monkeypatch):
    # get_settings() is @lru_cache'd (a true singleton), so patching the
    # attribute on the cached instance affects every subsequent call to
    # get_settings() -- including the fresh one _dispatch_scoring() makes
    # internally. This genuinely exercises the disabled branch.
    from app.core.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "auto_scoring_enabled", False)
    enqueued = []
    class _Q:
        def enqueue(self, *a, **k): enqueued.append(a)
    monkeypatch.setattr(ext, "get_queue", lambda: _Q())
    added = []
    class _BG:
        def add_task(self, fn, *a): added.append((fn, a))
    ext._dispatch_scoring([1, 2], _BG())
    assert enqueued == []   # no RQ enqueue
    assert added == []      # no inline task
