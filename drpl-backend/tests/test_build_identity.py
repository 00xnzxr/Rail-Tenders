"""A deploy has to be checkable from outside.

The GeM ministry commit went to `main`, Railway deployed it, and there was
still no way to confirm that without reading `/openapi.json` and hoping the
change happened to touch a request model. It did, that time. A fix to a
service function does not, so "is my change live?" was guesswork -- and the
expensive version of that guess is the worker: it is the process that reads
the NIT and prices the schedule, so a build that reaches the web service and
not the worker reads as "the fix changed nothing".

`/health` now states the commit it serves, and `/health/capacity` repeats what
each worker replica published on boot. Both are unauthenticated counters, as
those endpoints already were.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import platform_build
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestPlatformBuild:
    @pytest.mark.parametrize("var", [
        "RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA", "SOURCE_COMMIT",
    ])
    def test_it_reads_the_deploy_provided_sha(self, monkeypatch, var):
        for v in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA", "SOURCE_COMMIT"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv(var, "0a43bef2b1654d1ba2f59fd9169bbc0edfcda497")
        assert platform_build() == "0a43bef"

    def test_it_says_local_rather_than_guessing(self, monkeypatch):
        for v in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA", "SOURCE_COMMIT"):
            monkeypatch.delenv(v, raising=False)
        assert platform_build() == "local"


class TestHealthStatesItsBuild:
    def test_health_carries_the_build(self, client):
        body = client.get("/health").json()
        assert body["status"] == "healthy"
        assert body["build"] == platform_build()

    def test_it_stays_unauthenticated(self, client):
        """The point is that a deploy can be checked without a login."""
        assert client.get("/health").status_code == 200


class TestCapacityReportsTheWorkers:
    def test_it_carries_the_web_build_and_a_workers_field(self, client, monkeypatch):
        import app.core.redis_client as rc

        monkeypatch.setattr(rc, "get_redis", lambda: None)
        body = client.get("/health/capacity").json()
        assert body["build"] == platform_build()
        # No Redis: no worker has reported, and that is stated rather than
        # filled in with something reassuring.
        assert body["workers"] == {}

    @staticmethod
    def _fake_redis(fields):
        class FakeRedis:
            def hgetall(self, key):
                assert key == "drpl:worker:build"
                return fields

            def __getattr__(self, _name):
                # The endpoint's other Redis reads are guarded; make them inert.
                def _noop(*a, **k):
                    raise RuntimeError("not part of this test")
                return _noop

        return FakeRedis()

    @staticmethod
    def _fake_rq(live):
        """Stand in for rq.Worker.all(). `live` is a list of (host, pid)."""
        class W:
            def __init__(self, host, pid):
                self.hostname, self.pid = host, pid

        class Worker:
            @staticmethod
            def all(connection=None):
                return [W(h, p) for h, p in live]

        return Worker

    def test_it_repeats_what_each_replica_published(self, client, monkeypatch):
        import app.core.redis_client as rc
        import rq

        fake = self._fake_redis({b"host-a:11": b"0a43bef", b"host-b:12": b"0a43bef"})
        monkeypatch.setattr(rc, "get_redis", lambda: fake)
        monkeypatch.setattr(rq, "Worker", self._fake_rq([("host-a", 11), ("host-b", 12)]))
        body = client.get("/health/capacity").json()
        assert body["workers"] == {"host-a:11": "0a43bef", "host-b:12": "0a43bef"}
        assert body["worker_builds"] == {"0a43bef": 2}

    def test_a_replaced_container_stops_being_reported(self, client, monkeypatch):
        """The defect this endpoint shipped with: the hash is written once at
        boot and never refreshed, so the first deploy after it went live
        reported 24 children on two builds -- twelve of them already gone.
        RQ heartbeats its own registry, so that is the liveness signal."""
        import app.core.redis_client as rc
        import rq

        monkeypatch.setattr(rc, "get_redis", lambda: self._fake_redis({
            b"dead-host:2": b"47a9802",     # replaced container, still in the hash
            b"dead-host:3": b"47a9802",
            b"live-host:2": b"420789b",
            b"live-host:3": b"420789b",
        }))
        monkeypatch.setattr(rq, "Worker", self._fake_rq([("live-host", 2), ("live-host", 3)]))
        body = client.get("/health/capacity").json()
        assert body["workers"] == {"live-host:2": "420789b", "live-host:3": "420789b"}
        assert body["worker_builds"] == {"420789b": 2}, "one build, not two"

    def test_a_rollout_in_flight_shows_both_builds(self, client, monkeypatch):
        """Two entries in the tally is the signal worth seeing, not a bug."""
        import app.core.redis_client as rc
        import rq

        monkeypatch.setattr(rc, "get_redis", lambda: self._fake_redis({
            b"old:2": b"47a9802", b"new:2": b"420789b", b"new:3": b"420789b",
        }))
        monkeypatch.setattr(rq, "Worker", self._fake_rq(
            [("old", 2), ("new", 2), ("new", 3)]
        ))
        body = client.get("/health/capacity").json()
        assert body["worker_builds"] == {"47a9802": 1, "420789b": 2}

    def test_an_empty_rq_registry_does_not_hide_every_worker(self, client, monkeypatch):
        """If the intersection empties the map, the registry is what is wrong.
        Losing a worker from this list is worse than showing one too many."""
        import app.core.redis_client as rc
        import rq

        monkeypatch.setattr(rc, "get_redis", lambda: self._fake_redis(
            {b"host-a:11": b"420789b"}
        ))
        monkeypatch.setattr(rq, "Worker", self._fake_rq([]))
        body = client.get("/health/capacity").json()
        assert body["workers"] == {"host-a:11": "420789b"}

    def test_an_rq_failure_falls_back_to_the_unfiltered_map(self, client, monkeypatch):
        import app.core.redis_client as rc
        import rq

        class Boom:
            @staticmethod
            def all(connection=None):
                raise RuntimeError("registry unavailable")

        monkeypatch.setattr(rc, "get_redis", lambda: self._fake_redis(
            {b"host-a:11": b"420789b"}
        ))
        monkeypatch.setattr(rq, "Worker", Boom)
        body = client.get("/health/capacity").json()
        assert body["workers"] == {"host-a:11": "420789b"}

    def test_a_dropped_connection_does_not_break_the_endpoint(self, client, monkeypatch):
        """The real failure mode: `get_redis` hands back a live client and the
        call on it fails. An observability endpoint that 500s when Redis
        hiccups is worse than one that reports less."""
        import app.core.redis_client as rc

        class FlakyRedis:
            def hgetall(self, key):
                raise ConnectionError("connection reset by peer")

            def __getattr__(self, _name):
                def _noop(*a, **k):
                    raise RuntimeError("not part of this test")
                return _noop

        monkeypatch.setattr(rc, "get_redis", lambda: FlakyRedis())
        r = client.get("/health/capacity")
        assert r.status_code == 200
        assert r.json()["workers"] == {}
        assert r.json()["build"] == platform_build()


class TestWorkerPublishesItsBuild:
    def test_the_worker_writes_the_key_the_endpoint_reads(self):
        """Read off the source: the key name is the contract between two
        processes that never import each other."""
        import inspect

        import worker as worker_mod

        src = inspect.getsource(worker_mod)
        assert 'hset("drpl:worker:build"' in src
        assert 'expire("drpl:worker:build"' in src, "a departed replica must age out"

    def test_publishing_is_best_effort(self):
        """A worker that cannot write this still has to start."""
        import inspect

        import worker as worker_mod

        src = inspect.getsource(worker_mod)
        i = src.index('hset("drpl:worker:build"')
        window = src[max(0, i - 400):i + 400]
        assert "try:" in window and "except Exception" in window
