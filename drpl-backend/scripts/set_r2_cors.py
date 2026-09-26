"""Apply the R2 bucket CORS policy the xlsx preview needs.

DRY-RUN by default (writes nothing). Pass --apply to put the policy.

Why this is needed
------------------
The cost-breakdown DOWNLOAD points an ``<a href>`` at a presigned R2 URL. That
is a navigation, and navigations are not subject to CORS — which is why
downloads work with no bucket policy at all.

The PREVIEW is different. pdf.js fetches the rendered PDF over XHR and then
issues HTTP **Range** requests for anything beyond the first chunk. Those are
cross-origin requests from the frontend origin to
``<account>.r2.cloudflarestorage.com``, so the bucket must allow them.

``StorageService.get_presigned_url`` had no callers anywhere in the codebase
before the preview work, so no browser had ever fetched R2 directly from this
application and no CORS rule has ever existed on the bucket.

Neither the test suite nor the containerised end-to-end run can catch a missing
policy — both use the local storage backend. It fails only in a real browser.

Symptom when it is missing: the Artifacts panel falls back to the flat
line-items table and the browser console shows a CORS error. The download keeps
working, which makes the cause easy to misread.

The policy
----------
- ``GET`` / ``HEAD`` only. The browser never writes to R2; presigned URLs are
  read-only.
- ``Range`` in AllowedHeaders — pdf.js cannot stream without it, and the
  preflight fails on any PDF large enough to need a second request.
- ``Content-Range`` in ExposeHeaders is the matching half: pdf.js reads it to
  learn the object size and to seek. Omitting it breaks streaming even when the
  requests themselves succeed.
- No ``Authorization`` header is needed: a presigned URL carries its signature
  in the query string.

Usage (from drpl-backend/, with the production R2 env vars set):
  python scripts/set_r2_cors.py                          # show current vs desired
  python scripts/set_r2_cors.py --origin https://app.example.com
  python scripts/set_r2_cors.py --origin https://app.example.com --apply

Origins may be repeated or comma-separated. When omitted, CORS_ORIGINS is used
(the same browser origins the API already allows), which is normally correct.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Allow `python scripts/set_r2_cors.py` (direct invocation, as documented above)
# to find the `app` package when sys.path[0] is scripts/ rather than the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.disable(logging.INFO)  # the engine may run with echo=True; this reports via print

ALLOWED_METHODS = ["GET", "HEAD"]
ALLOWED_HEADERS = ["Range"]
EXPOSE_HEADERS = ["Content-Range", "Content-Length", "Accept-Ranges", "ETag"]
MAX_AGE_SECONDS = 3600


def build_rules(origins: list[str]) -> list[dict]:
    return [
        {
            "AllowedOrigins": origins,
            "AllowedMethods": ALLOWED_METHODS,
            "AllowedHeaders": ALLOWED_HEADERS,
            "ExposeHeaders": EXPOSE_HEADERS,
            "MaxAgeSeconds": MAX_AGE_SECONDS,
        }
    ]


def normalize_origin(value: str) -> str:
    """Reduce a pasted URL to the exact string a browser sends in `Origin`.

    A browser sends scheme://host[:port] with NO trailing slash and no path.
    Pasting "https://app.example.com/" straight from the address bar produces a
    policy that applies cleanly and then never matches, so the preview silently
    keeps falling back to the table with nothing to point at. Normalising here
    is worth more than remembering.
    """
    v = value.strip()
    if not v:
        return ""
    if "://" not in v:
        # Bare host: assume https rather than emitting an origin that can never match.
        v = "https://" + v
    scheme, _, rest = v.partition("://")
    host = rest.split("/", 1)[0]          # drop any path, query or trailing slash
    return f"{scheme.lower()}://{host}"


def _resolve_origins(cli_origins: list[str] | None) -> list[str]:
    """CLI origins win; otherwise fall back to CORS_ORIGINS."""
    raw: list[str] = []
    for item in cli_origins or []:
        raw.extend(part.strip() for part in item.split(","))
    if not raw:
        env = os.environ.get("CORS_ORIGINS", "").strip()
        if env and env != "*":
            raw.extend(part.strip() for part in env.split(","))
    # Normalise, preserve order, drop blanks and duplicates.
    seen: set[str] = set()
    origins: list[str] = []
    for o in raw:
        norm = normalize_origin(o)
        if norm and norm not in seen:
            seen.add(norm)
            origins.append(norm)
    return origins


def _covers(current: list[dict], origin: str) -> bool:
    """True when some existing rule already permits a pdf.js range read for `origin`."""
    for rule in current or []:
        allowed = rule.get("AllowedOrigins") or []
        if origin not in allowed and "*" not in allowed:
            continue
        methods = {m.upper() for m in (rule.get("AllowedMethods") or [])}
        if "GET" not in methods:
            continue
        headers = {h.lower() for h in (rule.get("AllowedHeaders") or [])}
        if "range" not in headers and "*" not in headers:
            continue
        exposed = {h.lower() for h in (rule.get("ExposeHeaders") or [])}
        if "content-range" not in exposed:
            continue
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--origin", action="append", default=None,
        help="frontend origin, e.g. https://app.example.com (repeatable or comma-separated). "
             "Defaults to CORS_ORIGINS.",
    )
    parser.add_argument("--apply", action="store_true",
                        help="write the policy (default: dry-run)")
    args = parser.parse_args()

    from app.core.config import get_settings
    from app.services.storage_service import get_storage_service

    settings = get_settings()
    if settings.storage_backend != "r2":
        print(f"[skip] storage_backend={settings.storage_backend!r} - this script only "
              f"applies to R2. Set STORAGE_BACKEND=r2 and the R2_* credentials.")
        return 1

    origins = _resolve_origins(args.origin)
    if not origins:
        print("[error] no origins. Pass --origin https://your-app.vercel.app, or set "
              "CORS_ORIGINS to the browser origin(s) the frontend is served from.")
        print("        A wildcard CORS_ORIGINS='*' is deliberately NOT accepted here: "
              "the bucket serves presigned URLs and should name its callers.")
        return 2

    storage = get_storage_service()
    client = storage._client  # noqa: SLF001 — no public accessor for the raw S3 client
    bucket = settings.r2_bucket_name

    print(f"bucket : {bucket}")
    print(f"account: {settings.r2_account_id[:8]}...")
    print(f"origins: {', '.join(origins)}")

    # Show any rewrite: a silently "fixed" origin is as confusing as a broken one.
    supplied: list[str] = []
    for item in args.origin or []:
        supplied.extend(part.strip() for part in item.split(","))
    for raw in supplied:
        norm = normalize_origin(raw)
        if raw and norm != raw:
            print(f"[note] normalised {raw!r} -> {norm!r} "
                  f"(a browser's Origin header carries no trailing slash or path)")

    try:
        current = client.get_bucket_cors(Bucket=bucket).get("CORSRules", [])
    except Exception as e:  # noqa: BLE001 — botocore raises NoSuchCORSConfiguration here
        if "NoSuchCORSConfiguration" in str(e) or "NoSuchCORSConfig" in str(e):
            current = []
        else:
            print(f"[error] could not read the current CORS policy: {e}")
            return 3

    print("\n--- current policy ---")
    print(json.dumps(current, indent=2) if current else "  (none set)")

    desired = build_rules(origins)
    print("\n--- desired policy ---")
    print(json.dumps(desired, indent=2))

    already = [o for o in origins if _covers(current, o)]
    if already:
        print(f"\n[note] already permitted by the existing policy: {', '.join(already)}")
    if len(already) == len(origins):
        print("[ok] every origin can already do a pdf.js range read. Nothing to change.")
        return 0

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to set the policy.")
        print("NOTE: this REPLACES the bucket's whole CORS policy. If the 'current'")
        print("      block above contains rules you still need, add their origins via")
        print("      --origin so they survive, or merge by hand in the dashboard.")
        return 0

    client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": desired})
    print("\n[ok] policy applied.")

    verify = client.get_bucket_cors(Bucket=bucket).get("CORSRules", [])
    if all(_covers(verify, o) for o in origins):
        print("[ok] verified by re-reading the bucket.")
    else:
        print("[warn] re-read did not confirm every origin - check the dashboard.")
        print(json.dumps(verify, indent=2))
        return 4

    print("\nNext: open a cost-breakdown artifact's preview. If it still shows the "
          "line-items table, check the browser console for a CORS error and confirm "
          "the origin above exactly matches the page's origin (scheme, host, port).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
