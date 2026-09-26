"""Shared helpers for backend probes.

Probes are self-contained scripts that test one external dependency. Each:
- Exits 0 on success, non-zero on failure.
- Appends a one-line status to ../../memory/progress.md.

Run from the drpl-backend/ directory so that `app.*` imports resolve:

    cd drpl-backend
    python execution/probes/anthropic.py
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

# Make `app` importable when run as `python execution/probes/xxx.py` from drpl-backend/.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

_PROGRESS_PATH = _BACKEND_ROOT / "memory" / "progress.md"


def log_progress(probe_name: str, status: str, detail: str = "") -> None:
    """Append a one-line entry to memory/progress.md."""
    ts = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    detail_cell = detail.replace("|", "\\|").replace("\n", " ")[:200]
    line = f"| {ts} | probe | {probe_name} | {status} {('— ' + detail_cell) if detail_cell else ''}|\n"
    try:
        _PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not _PROGRESS_PATH.exists():
            _PROGRESS_PATH.write_text(
                "# Backend — Progress Log\n\n| Date | Actor | Event | Detail |\n|---|---|---|---|\n",
                encoding="utf-8",
            )
        with _PROGRESS_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        # Best-effort — probe shouldn't fail because progress log is unwritable.
        print(f"[probe:{probe_name}] could not write progress: {e}", file=sys.stderr)


def finish(probe_name: str, ok: bool, detail: str = "") -> None:
    status = "OK" if ok else "FAIL"
    log_progress(probe_name, status, detail)
    print(f"[probe:{probe_name}] {status} {detail}".rstrip())
    sys.exit(0 if ok else 1)


def env_flag(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())
