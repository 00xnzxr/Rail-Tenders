"""Backfill `cost_breakdowns.created_by` from the session that produced them.

`created_by` was added when the per-user firewall landed (app/core/ownership.py).
Every row written before that has NULL, which the firewall reads as "predates the
wall" — master_admin only, never guessed onto a user.

A breakdown produced from the Command Center carries `session_id`, and that
session knows who owns it. That is real provenance, not a guess, so those rows
can be attributed. Rows with no session, or whose session is gone or ownerless,
are left NULL: inventing an owner could hand one user another's work, which is
the exact failure the firewall exists to prevent.

Run with --apply to write. Without it, this is read-only and commits nothing.

    python scripts/backfill_cost_breakdown_owners.py            # dry run
    python scripts/backfill_cost_breakdown_owners.py --apply    # write

Every change is printed as an id → owner mapping, and --apply writes an undo
file next to the script so the whole batch can be reverted.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.disable(logging.INFO)

from sqlalchemy import text  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402

CANDIDATES = text("""
    SELECT cb.id AS breakdown_id, ps.created_by AS owner_id, u.email AS owner_email
    FROM cost_breakdowns cb
    JOIN proposal_sessions ps ON ps.id = cb.session_id
    JOIN users u ON u.id = ps.created_by
    WHERE cb.created_by IS NULL
      AND ps.created_by IS NOT NULL
    ORDER BY cb.id
""")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = db.execute(CANDIDATES).fetchall()
        remaining = db.execute(text(
            "SELECT count(*) FROM cost_breakdowns WHERE created_by IS NULL"
        )).scalar()

        if not rows:
            print("Nothing to backfill.")
            return 0

        by_owner: dict[str, int] = {}
        for _bid, owner_id, email in rows:
            by_owner[f"{owner_id} ({email})"] = by_owner.get(f"{owner_id} ({email})", 0) + 1

        print(f"{'APPLYING' if args.apply else 'DRY RUN —'} "
              f"{len(rows)} cost breakdowns can be attributed from their session:")
        for owner, count in sorted(by_owner.items(), key=lambda kv: -kv[1]):
            print(f"  user {owner}: {count}")
        print(f"\n{remaining - len(rows)} rows stay NULL (no session, or no owner "
              f"on it) and remain master_admin-only.")

        if not args.apply:
            print("\nRead-only. Re-run with --apply to write.")
            return 0

        undo = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "Set created_by back to NULL for these ids to revert.",
            "breakdown_ids": [int(r.breakdown_id) for r in rows],
        }
        undo_path = Path(__file__).with_suffix(".undo.json")
        undo_path.write_text(json.dumps(undo, indent=2))
        print(f"\nUndo written to {undo_path}")

        for row in rows:
            db.execute(
                text("UPDATE cost_breakdowns SET created_by = :owner WHERE id = :id "
                     "AND created_by IS NULL"),
                {"owner": row.owner_id, "id": row.breakdown_id},
            )
        db.commit()

        still_null = db.execute(text(
            "SELECT count(*) FROM cost_breakdowns WHERE created_by IS NULL"
        )).scalar()
        print(f"Done. {len(rows)} rows attributed; {still_null} still NULL.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
