"""Report what the archive sweep WOULD do. READ-ONLY: writes nothing, ever.

Run this against production before enabling archive_sweep_enabled:

    cd drpl-backend && DATABASE_URL="<neon prod url>" ./venv/Scripts/python.exe scripts/archive_sweep_dryrun.py

Never run without an explicit DATABASE_URL you have verified — the local
.env may point at a live database.

This script only ever SELECTs / counts via archive_candidates_query and
purge_candidates_query (Task 4). It never calls run_archive_pass,
run_purge_pass, or delete_tenders_deep, never assigns to ORM attributes,
and never calls db.commit().
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python scripts/archive_sweep_dryrun.py` (direct invocation, as
# documented above) to find the `app` package when sys.path[0] is scripts/
# rather than the repo root. No-op when already importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.services.auto_scoring_settings import get_scoring_settings
from app.services.tender_archive_service import (
    archive_candidates_query,
    purge_candidates_query,
)


def main() -> None:
    db = SessionLocal()
    try:
        cfg = get_scoring_settings(db)
        now = datetime.now(timezone.utc)
        grace, purge = cfg["archive_grace_days"], cfg["archive_purge_days"]

        arch_q = archive_candidates_query(db, now=now, grace_days=grace)
        purge_q = purge_candidates_query(db, now=now, purge_days=purge)
        arch_n, purge_n = arch_q.count(), purge_q.count()

        print(f"enabled          : {cfg['archive_sweep_enabled']}")
        purge_label = "NEVER (archive-only)" if purge <= 0 else f"{purge}d"
        print(f"grace / purge    : {grace}d / {purge_label}")
        print(f"WOULD ARCHIVE    : {arch_n}")
        for t in arch_q.limit(20).all():
            print(f"   #{t.id:<7} closed={t.closing_date}  {(t.title or '')[:60]}")
        print(f"WOULD DELETE     : {purge_n}")
        for t in purge_q.limit(20).all():
            print(f"   #{t.id:<7} archived={t.archived_at}  {(t.title or '')[:60]}")
        if arch_n > 20 or purge_n > 20:
            print("(showing first 20 of each)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
