"""
R2 credential smoke test.

Run: python -m scripts.r2_smoke_test

Uploads a small test object, downloads it, deletes it, and prints a success
summary. Confirms that R2 env vars are configured correctly before rolling
the app onto R2 storage.
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    from app.core.config import get_settings
    from app.services.storage_service import get_storage_service

    settings = get_settings()
    if settings.storage_backend != "r2":
        print(f"[skip] storage_backend={settings.storage_backend} — set STORAGE_BACKEND=r2 to test R2")
        return 1

    print(f"[info] bucket={settings.r2_bucket_name} account={settings.r2_account_id[:8]}...")

    storage = get_storage_service()
    key = f"_smoke/smoke_{int(time.time())}.txt"
    payload = b"drpl-r2-smoke-test"

    print(f"[step] uploading {key} ({len(payload)} bytes)")
    storage.upload_file_sync(key, payload, content_type="text/plain")

    assert storage.file_exists_sync(key), "expected object to exist after upload"

    print(f"[step] downloading {key}")
    got = storage.download_file_sync(key)
    assert got == payload, f"payload mismatch: got {got!r}"

    print("[step] generating presigned URL (2 minute TTL)")
    import asyncio
    url = asyncio.run(storage.get_presigned_url(key, expires_in=120))
    print(f"       {url}")

    print(f"[step] deleting {key}")
    storage.delete_file_sync(key)
    assert not storage.file_exists_sync(key), "expected object to be gone after delete"

    print("[ok] R2 smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
