"""Probe: round-trip put → get → delete on a _probe/ key via storage_service.

Only runs when storage_backend=r2. Local backend writes are unnecessary
to verify — the filesystem either works or it doesn't.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid

from execution.probes._common import finish, log_progress


PROBE = "r2"


async def _run() -> tuple[bool, str]:
    try:
        from app.core.config import settings
        from app.services.storage_service import (
            upload_file,
            download_file,
            delete_file,
        )
    except Exception as e:
        return False, f"import failed: {e}"

    backend = getattr(settings, "storage_backend", "local")
    if backend != "r2":
        log_progress(PROBE, "SKIP", f"storage_backend={backend}, R2 probe inert")
        print(f"[probe:{PROBE}] SKIP — storage_backend={backend}")
        sys.exit(0)

    key = f"_probe/{uuid.uuid4().hex}.txt"
    payload = b"probe-ok"

    t0 = time.time()
    try:
        await upload_file(key, payload, "text/plain")
    except Exception as e:
        return False, f"upload failed: {e}"

    try:
        fetched = await download_file(key)
    except Exception as e:
        return False, f"download failed: {e}"

    if fetched != payload:
        return False, f"round-trip mismatch: got {len(fetched)} bytes"

    try:
        await delete_file(key)
    except Exception as e:
        return False, f"delete failed (object persisted): {e}"

    return True, f"put+get+delete in {int((time.time() - t0) * 1000)}ms"


def main() -> None:
    try:
        ok, detail = asyncio.run(_run())
    except Exception as e:
        finish(PROBE, ok=False, detail=f"unhandled: {e}")
        return
    finish(PROBE, ok=ok, detail=detail)


if __name__ == "__main__":
    main()
