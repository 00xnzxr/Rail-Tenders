"""
DRPL Backend - RQ worker entrypoint.

Run this as a separate Railway service (Phase C4) or locally alongside
the web server:

    python worker.py

Concurrency
-----------
Each RQ worker process pulls one job at a time. To get N parallel runs
per container, we fork N subprocesses and supervise them. Tune via:

    WORKER_CONCURRENCY=3   # default; forks 3 child workers per replica

With Railway `numReplicas=4` and `WORKER_CONCURRENCY=3` that's 12 parallel
agent runs across the fleet. SIGTERM is relayed to children so Railway's
rolling-deploy graceful shutdown still works.

Loads the same settings/DB/Redis config as the web app, ensures all
SQLAlchemy models are imported (so each worker's own SessionLocal has
full mappings), then blocks on the RQ queue forever.

No-ops cleanly when ``REDIS_URL`` is empty — prints a one-liner and
exits so `docker-compose up` without Redis doesn't crash-loop.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from multiprocessing import Process

#: How long a worker waits for Redis to come back before giving up and
#: exiting non-zero (so Railway restarts the replica). Railway's managed Redis
#: security patch restarted the server for well under a minute; a full outage
#: is what this bounds.
REDIS_RECONNECT_WAIT_S = int(os.getenv("WORKER_REDIS_RECONNECT_WAIT_S", "900"))
#: Pause before a work loop that ended for a reason other than Redis (an
#: unhandled exception inside RQ) is started again, so it cannot spin.
_RESUME_PAUSE_S = 1.0


def wait_for_redis(client, *, max_wait_s: float, sleep=time.sleep, log=None) -> bool:
    """Block until `client.ping()` succeeds, backing off 1, 2, 4 ... 15 s.

    True when Redis answered; False once `max_wait_s` has been spent waiting.
    """
    waited = 0.0
    attempt = 0
    while True:
        try:
            client.ping()
            return True
        except Exception as e:  # noqa: BLE001 — any failure means "not yet"
            if waited >= max_wait_s:
                if log:
                    log.error(f"Redis still unreachable after {waited:.0f}s: {e}")
                return False
            delay = min(2 ** attempt, 15)
            attempt += 1
            if log:
                log.warning(f"Redis unreachable ({e}); retrying in {delay}s")
            sleep(delay)
            waited += delay


def serve(worker_factory, client, *, max_wait_s: float = REDIS_RECONNECT_WAIT_S,
          sleep=time.sleep, log=None) -> int:
    """Run RQ work loops until a stop is requested; survive Redis going away.

    `Worker.work()` returns, rather than raising, when RQ gives up on Redis
    ("Redis connection timeout, quitting...") or hits an unhandled exception.
    This used to be treated as a clean exit: the subprocess returned 0, the
    replica exited 0, and Railway's ON_FAILURE policy left it down -- so a
    Redis security patch that restarted the server for seconds took every
    queue consumer with it, and every run enqueued afterwards sat in the
    queue with nothing listening. Now the loop waits for Redis to answer and
    starts a fresh worker (the old one registered its death in teardown); it
    returns 0 only for a signal-requested stop and 1 when Redis never comes
    back, which is the exit Railway restarts on.
    """
    while True:
        worker = worker_factory()
        worker.work(with_scheduler=True)
        if getattr(worker, "_stop_requested", False):
            return 0
        if log:
            log.warning("RQ work loop ended without a stop request -- checking Redis")
        if not wait_for_redis(client, max_wait_s=max_wait_s, sleep=sleep, log=log):
            return 1
        sleep(_RESUME_PAUSE_S)
        if log:
            log.warning("Redis answered -- resuming with a fresh worker")


def _silence_sql_logging(touch_engine: bool = True) -> None:
    """Keep SQLAlchemy's per-statement SQL out of the worker's stdout.

    With DEBUG on, the engine is created with ``echo=True`` and logs every
    statement and every bound parameter set. A costing run issues thousands
    of them, Railway drops log lines past 500/s, and the lines that matter
    (batch progress, the timeout) went with them. ``echo`` bypasses the
    logger's level (SQLAlchemy's InstanceLogger compares against its own
    echo level), so the engine's echo is switched off as well as the levels
    raised. WARNING and above still pass, so connection errors are not
    hidden. Runs in the parent before the fork and again in each child
    after the engine module is imported, so it holds whichever creates the
    engine first. The parent only raises the levels (`touch_engine=False`):
    the engine belongs to the children, each of which creates its own after
    the fork."""
    for name in (
        "sqlalchemy.engine", "sqlalchemy.engine.Engine",
        "sqlalchemy.pool", "sqlalchemy.orm", "sqlalchemy.dialects",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
    if not touch_engine:
        return
    try:
        from app.core import database as _database
        _engine = getattr(_database, "engine", None)
        if _engine is not None and getattr(_engine, "echo", False):
            _engine.echo = False
    except Exception as e:  # never keep the worker from starting
        logging.getLogger("drpl.worker").debug(f"[startup] sql echo off failed: {e}")


def _probe_soffice() -> None:
    """Diagnose a missing LibreOffice at worker boot rather than as a mystery
    preview failure hours later. worker.py never imports app.main, so this is
    the only process that actually runs LibreOffice and the only place this
    probe is meaningful — mirrors the same probe in app/main.py for the web
    process (which never touches soffice itself). Wrapped so a probe failure
    can never prevent the worker from starting."""
    log = logging.getLogger("drpl.worker")
    try:
        from app.services.xlsx_preview_service import soffice_available, SOFFICE_BIN

        if soffice_available():
            log.info(f"[startup] {SOFFICE_BIN} present — xlsx previews enabled")
        else:
            log.warning(
                f"[startup] {SOFFICE_BIN} NOT found — xlsx previews will fail; "
                f"install libreoffice-calc in the image"
            )
    except Exception as e:
        log.warning(f"[startup] soffice probe failed: {e}")


def _run_single_worker() -> int:
    """One RQ worker process. Called directly (single mode) or in a subprocess."""
    log = logging.getLogger("drpl.worker")

    # Import all models so mappers are registered for this process's
    # SessionLocal (matches what app/main.py does for the web process).
    # Done inside the worker function so forked children re-import cleanly.
    import app.models  # noqa: F401
    _silence_sql_logging()
    from app.core.redis_client import get_redis, get_queue
    from app.core.config import get_settings
    from app.core.run_context import install_run_id_filter

    install_run_id_filter()  # [run=xxxxxxxx] log prefix inside run_id_scope
    _probe_soffice()

    from app.core.config import platform_build
    _build = platform_build()
    log.info("DRPL worker starting: build %s", _build)

    settings = get_settings()
    if not settings.redis_url:
        log.warning("REDIS_URL is empty — worker has nothing to listen on. Exiting.")
        return 0

    r = get_redis()
    q = get_queue()
    if r is None or q is None:
        log.error("Could not initialise Redis/RQ. Check REDIS_URL and connectivity.")
        return 1

    # Announce which build this child is running, so `/health/capacity` can
    # answer "did my fix reach the worker?" without a Railway login. One field
    # per forked child, keyed `host:pid` -- the same pair RQ names its own
    # worker registration with, which is how the endpoint tells a live child
    # from a dead one. The field is written once, at boot, and never refreshed:
    # a container that goes away leaves its fields behind until the hash's TTL,
    # so the READER must prune against RQ's live registry rather than trust
    # this hash on its own. Best-effort: a worker that cannot write this still
    # works.
    try:
        import os as _os
        import socket as _socket
        r.hset("drpl:worker:build", f"{_socket.gethostname()}:{_os.getpid()}", _build)
        r.expire("drpl:worker:build", 60 * 60 * 24)
    except Exception as e:  # noqa: BLE001 -- never block a worker from starting
        log.warning("could not publish worker build: %s", e)

    from rq import Worker

    def _make_worker():
        # The GeM collector's queue (`app.api.routes.collect.COLLECT_QUEUE`):
        # its own name so a sweep is visible as such and capped to one at a
        # time by the route, and served by this same worker so no second
        # service is needed. The collector's GeM path is httpx-only.
        from rq import Queue
        from app.api.routes.collect import COLLECT_QUEUE
        queues = [q, Queue(COLLECT_QUEUE, connection=r)]
        worker = Worker(queues, connection=r, name=None)
        log.info(f"Worker pid={os.getpid()} listening on queues "
                 f"'{settings.rq_queue_name}', '{COLLECT_QUEUE}'.")
        return worker

    # RQ handles SIGTERM natively — finishes current job then exits cleanly.
    # with_scheduler=True: needed for the notification center's scheduled jobs
    # (daily digest, closing-date scan) which use Queue.enqueue_at/enqueue_in.
    # `serve` restarts the loop after a Redis outage instead of exiting 0.
    return serve(_make_worker, r, log=log)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("drpl.worker")
    _silence_sql_logging(touch_engine=False)

    concurrency = int(os.getenv("WORKER_CONCURRENCY", "3"))
    if concurrency <= 1:
        log.info("WORKER_CONCURRENCY=1 — running single in-process worker.")
        return _run_single_worker()

    log.info(f"Forking {concurrency} worker subprocesses (WORKER_CONCURRENCY={concurrency}).")

    procs: list[Process] = []
    for i in range(concurrency):
        p = Process(target=_run_single_worker, name=f"rq-worker-{i}", daemon=False)
        p.start()
        procs.append(p)
        log.info(f"Spawned worker #{i} pid={p.pid}")

    # Relay SIGTERM / SIGINT to children so Railway's graceful shutdown
    # (sends SIGTERM, waits ~10s, then SIGKILL) reaches them. RQ's own
    # SIGTERM handler drains the current job before exiting.
    def _relay(signum, _frame):
        log.info(f"Received signal {signum} — relaying to {len(procs)} children.")
        for p in procs:
            if p.is_alive():
                try:
                    os.kill(p.pid, signum)
                except Exception as e:
                    log.warning(f"Could not signal pid={p.pid}: {e}")

    signal.signal(signal.SIGTERM, _relay)
    signal.signal(signal.SIGINT, _relay)

    # Wait for all children. If any crashes, exit non-zero so Railway
    # restarts the whole replica (simpler than partial recovery).
    exit_code = 0
    for p in procs:
        p.join()
        if p.exitcode not in (0, None):
            log.error(f"Worker pid={p.pid} exited with code {p.exitcode}.")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
