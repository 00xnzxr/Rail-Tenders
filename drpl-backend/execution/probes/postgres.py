"""Probe: SELECT 1 + pool snapshot via the configured database URL."""

from __future__ import annotations

import time

from execution.probes._common import finish


PROBE = "postgres"


def main() -> None:
    try:
        from sqlalchemy import text
        from app.core.database import engine
    except Exception as e:
        finish(PROBE, ok=False, detail=f"import failed: {e}")
        return

    t0 = time.time()
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT 1")).scalar()
    except Exception as e:
        finish(PROBE, ok=False, detail=f"connect/SELECT failed: {e}")
        return

    elapsed_ms = int((time.time() - t0) * 1000)
    if row != 1:
        finish(PROBE, ok=False, detail=f"SELECT 1 returned {row!r}")
        return

    # Best-effort pool snapshot (Postgres only; SQLite has no pool).
    pool_detail = ""
    try:
        pool = engine.pool
        size = getattr(pool, "size", lambda: None)()
        checked_out = getattr(pool, "checkedout", lambda: None)()
        overflow = getattr(pool, "overflow", lambda: None)()
        if size is not None:
            pool_detail = f", pool size={size} checked_out={checked_out} overflow={overflow}"
    except Exception:
        pass

    finish(PROBE, ok=True, detail=f"{elapsed_ms}ms{pool_detail}")


if __name__ == "__main__":
    main()
