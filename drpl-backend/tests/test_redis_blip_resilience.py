"""A Redis restart is a blip, not an outage of the platform.

Railway's managed Redis was security-patched on 2026-09-12 at 10:29 UTC, ten
minutes into a costing. Three things happened, none of them recoverable
without a redeploy:

1. RQ's work loop gave up ("Redis connection timeout, quitting...") and
   `worker._run_single_worker` returned 0 -- a clean exit. Every replica
   exited 0, Railway's ON_FAILURE policy did not restart them, and the queue
   had no consumers: every run enqueued afterwards sat there forever.
2. The `/runs/{id}/events` stream ended on the first failed XREAD with
   "Event stream error.", though the worker was still running.
3. The in-flight run died with its worker.

The first two are pinned here (the third needs a worker that outlives Redis,
which is the first fix). `worker.serve` waits for Redis and starts a fresh
work loop, exiting non-zero only when Redis never comes back;
`runs._redis_retry_delay` bounds the stream's retries with backoff.
"""

import worker
from app.api.routes import runs


# ── the worker waits for Redis and resumes ──────────────────────────────────


class _Client:
    def __init__(self, failures: int):
        self.failures = failures
        self.pings = 0

    def ping(self):
        self.pings += 1
        if self.pings <= self.failures:
            raise ConnectionError("Connection closed by server.")
        return True


def test_wait_for_redis_backs_off_and_succeeds():
    sleeps = []
    client = _Client(failures=3)
    ok = worker.wait_for_redis(client, max_wait_s=900, sleep=sleeps.append)
    assert ok
    assert sleeps == [1, 2, 4]


def test_wait_for_redis_gives_up_after_the_window():
    sleeps = []
    client = _Client(failures=10_000)
    ok = worker.wait_for_redis(client, max_wait_s=20, sleep=sleeps.append)
    assert not ok
    assert sum(sleeps) >= 20
    assert max(sleeps) == 15  # the backoff is capped


class _FakeWorker:
    def __init__(self, stop_requested: bool):
        self._stop_requested = stop_requested
        self.worked = 0

    def work(self, with_scheduler=False):
        self.worked += 1
        return False


def test_serve_resumes_after_redis_comes_back():
    """RQ quit on a timeout (no stop requested); Redis answers after two
    failed pings; a fresh worker is started; the later signal stop exits 0."""
    made = []

    def factory():
        w = _FakeWorker(stop_requested=(len(made) == 1))
        made.append(w)
        return w

    client = _Client(failures=2)
    rc = worker.serve(factory, client, max_wait_s=900, sleep=lambda s: None)
    assert rc == 0
    assert len(made) == 2
    assert all(w.worked == 1 for w in made)


def test_serve_exits_nonzero_when_redis_never_returns():
    """Non-zero is what Railway's ON_FAILURE policy restarts on."""
    made = []

    def factory():
        w = _FakeWorker(stop_requested=False)
        made.append(w)
        return w

    rc = worker.serve(factory, _Client(failures=10_000), max_wait_s=5, sleep=lambda s: None)
    assert rc == 1
    assert len(made) == 1


def test_serve_returns_zero_on_a_requested_stop():
    rc = worker.serve(lambda: _FakeWorker(stop_requested=True), _Client(0), sleep=lambda s: None)
    assert rc == 0


# ── the events stream retries a failing read ────────────────────────────────


def test_stream_retry_backs_off_then_gives_up():
    delays = []
    attempt = 1
    while True:
        d = runs._redis_retry_delay(attempt)
        if d is None:
            break
        delays.append(d)
        attempt += 1
    assert delays[:5] == [1, 2, 4, 8, 15]
    assert max(delays) == runs._SSE_REDIS_RETRY_MAX_DELAY_S
    assert sum(delays) >= runs._SSE_REDIS_RETRY_TOTAL_S
    assert sum(delays) < runs._SSE_REDIS_RETRY_TOTAL_S + runs._SSE_REDIS_RETRY_MAX_DELAY_S


def test_stream_retry_window_outlasts_a_managed_redis_patch():
    """The patch restart observed in production was well under a minute."""
    assert runs._SSE_REDIS_RETRY_TOTAL_S >= 120


def test_stream_source_keeps_the_cursor_across_a_retry():
    """A retry that reset the cursor would replay or skip events."""
    import inspect
    src = inspect.getsource(runs.run_events_endpoint)
    retry_block = src[src.index("redis_failures += 1"):src.index("continue", src.index("redis_failures += 1"))]
    assert "cursor" not in retry_block
    assert "keepalive" in retry_block
