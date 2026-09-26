"""Clear `is_tax_line` on already-persisted BOQ rows that are really work items.

DRY-RUN by default (writes nothing). Pass --apply to update rows.

Background: `_detect_tax_line` used to match a bare `\\bGST\\b` anywhere in the
description, so a work item quoting a GST-inclusive rate ("Supply & Fitment SS
Metal RMPU Trough … (Rates are inclusive of GST @ 18%)") was flagged as a tax
row. The batched costing node drops tax rows from `cost_rows`, so a tender whose
only row carried that qualifier got ZERO costable rows and silently returned a
"[NEEDS RATE]" skeleton (Command Center session 285 / tender 3809).

The forward fix is in `boq_parser_service._detect_tax_line`, which this script
reuses so cleanup and the parser share one predicate. It only affects NEW
parses, and `ensure_boq_parsed` is a no-op when a schedule already exists — so
already-captured tenders stay broken until this runs.

Deliberately conservative: a row is corrected ONLY when the fixed detector says
it is not a tax row AND it carries a pricing signal (positive quantity, rate or
basic value). Compliance prose ("Contractor should have GST, EPF, ESI
registration certificate.") also trips the old detector, but such rows entered
`boq_items` only because the tax keyword doubled as a row-ACCEPTANCE signal in
`_is_boq_row`. They carry no qty/rate, are not priceable, and unflagging them
would hand the costing agent a sentence to price — so they keep their flag and
stay excluded from costing.

Usage (from drpl-backend/):
  .venv/Scripts/python.exe scripts/fix_mislabelled_tax_lines.py            # dry-run, all tenders
  .venv/Scripts/python.exe scripts/fix_mislabelled_tax_lines.py --tender 3809
  .venv/Scripts/python.exe scripts/fix_mislabelled_tax_lines.py --apply
"""
import argparse
import logging
import sys
from pathlib import Path

# Allow direct invocation (sys.path[0] is scripts/) to find the `app` package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.costing_template import BOQItem
from app.services.boq_parser_service import _detect_tax_line

# The app engine is built with echo=settings.debug (app/core/database.py:25).
# SQLAlchemy binds that choice at construction time, so raising the logger level
# afterwards does nothing — disable INFO records outright, or the SQL firehose
# buries this script's report. This script reports via print(), not logging.
logging.disable(logging.INFO)


def _positive(value) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _has_pricing_signal(row: BOQItem) -> bool:
    """True when the row is a priced schedule line rather than prose."""
    return (
        _positive(row.quantity)
        or _positive(row.estimated_rate)
        or _positive(row.basic_value)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tender", type=int, default=None,
                        help="restrict to a single tender id")
    parser.add_argument("--apply", action="store_true",
                        help="write the corrections (default: dry-run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        q = db.query(BOQItem).filter(BOQItem.is_tax_line.is_(True))
        if args.tender is not None:
            q = q.filter(BOQItem.tender_id == args.tender)
        flagged = q.order_by(BOQItem.tender_id, BOQItem.id).all()

        correct, skipped_prose, still_tax = [], [], []
        for row in flagged:
            if _detect_tax_line(row.description):
                still_tax.append(row)
            elif _has_pricing_signal(row):
                correct.append(row)
            else:
                skipped_prose.append(row)

        print(f"Scanned {len(flagged)} row(s) flagged is_tax_line=True"
              + (f" for tender {args.tender}" if args.tender else ""))
        print(f"  still tax rows (unchanged):        {len(still_tax)}")
        print(f"  prose, no pricing signal (kept):   {len(skipped_prose)}")
        print(f"  MISLABELLED work items (correct):  {len(correct)}")

        if skipped_prose:
            print("\nKept flagged - no qty/rate, not priceable:")
            for r in skipped_prose:
                desc = " ".join((r.description or "").split())[:90]
                print(f"  tender {r.tender_id} id={r.id}  {desc!r}")

        if correct:
            print("\nWould clear is_tax_line on:" if not args.apply
                  else "\nClearing is_tax_line on:")
            for r in correct:
                desc = " ".join((r.description or "").split())[:90]
                print(f"  tender {r.tender_id} id={r.id}  qty={r.quantity} "
                      f"rate={r.estimated_rate} value={r.basic_value}")
                print(f"      {desc!r}")

        # Tenders left with zero costable rows are the silent-failure shape.
        affected = sorted({r.tender_id for r in correct})
        for tid in affected:
            total = db.query(BOQItem).filter(BOQItem.tender_id == tid).count()
            costable_now = db.query(BOQItem).filter(
                BOQItem.tender_id == tid, BOQItem.is_tax_line.is_(False)
            ).count()
            freed = sum(1 for r in correct if r.tender_id == tid)
            flag = "  <-- was silently un-costable" if costable_now == 0 else ""
            print(f"\n  tender {tid}: {total} row(s), costable "
                  f"{costable_now} -> {costable_now + freed}{flag}")

        if not args.apply:
            print("\nDRY RUN - nothing written. Re-run with --apply to correct.")
            return 0

        if not correct:
            print("\nNothing to correct.")
            return 0

        for row in correct:
            row.is_tax_line = False
        db.commit()
        print(f"\nUpdated {len(correct)} row(s).")
        print("Re-run costing for the affected tenders to rebuild the breakdown "
              "(existing CostBreakdown versions are untouched).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
