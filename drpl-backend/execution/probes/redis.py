"""Probe: Redis PING + queue length read."""

from __future__ import annotations

import os
import sys
import time

from execution.probes._common import finish, log_progress


PROBE = "redis"


def main() -> None:
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        log_progress(PROBE, "SKIP", "REDIS_URL not set — local dev path")
        print(f"[probe:{PROBE}] SKIP — REDIS_URL not set (local dev)")
        sys.exit(0)

    try:
        import redis as redis_lib
    except Exception as e:
        finish(PROBE, ok=False, detail=f"redis lib import failed: {e}")
        return

    try:
        client = redis_lib.Redis.from_url(redis_url, socket_timeout=5)
    except Exception as e:
        finish(PROBE, ok=False, detail=f"from_url raised: {e}")
        return

    t0 = time.time()
    try:
        pong = client.ping()
    except Exception as e:
        finish(PROBE, ok=False, detail=f"PING failed: {e}")
        return

    if not pong:
        finish(PROBE, ok=False, detail="PING returned falsy")
        return

    # RQ queue depth (best-effort — won't fail the probe if introspection breaks).
    detail = f"PING ok in {int((time.time() - t0) * 1000)}ms"
    try:
        from rq import Queue  # type: ignore
        for queue_name in ("default", "runs"):
            depth = Queue(queue_name, connection=client).count
            detail += f", queue={queue_name}:{depth}"
    except Exception:
        pass

    finish(PROBE, ok=True, detail=detail)


if __name__ == "__main__":
    main()
