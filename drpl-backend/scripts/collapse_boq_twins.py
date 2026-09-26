"""Collapse already-persisted value-redundant BOQ twin rows.

DRY-RUN by default (writes nothing). Pass --apply to delete flagged rows.
Reuses boq_parser_service._collapse_value_redundant_twins so cleanup and the
forward fix share one predicate. Fixes boq_items only; existing CostBreakdown
versions are untouched — re-run costing for a clean Excel.

Usage (from drpl-backend/):
  venv/Scripts/python.exe scripts/collapse_boq_twins.py            # dry-run, all affected tenders
  venv/Scripts/python.exe scripts/collapse_boq_twins.py --tender 3750
  venv/Scripts/python.exe scripts/collapse_boq_twins.py --apply
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

# Allow `python scripts/collapse_boq_twins.py` (direct invocation, as documented
# above) to find the `app` package when sys.path[0] is scripts/ rather than the
# repo root. No-op when already importable (e.g. `python -m scripts.collapse_boq_twins`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.costing_template import BOQItem
from app.services.boq_parser_service import (
    _collapse_value_redundant_twins,
    _row_value,
)

EPS = 0.05  # per-schedule value-sum drift tolerance (rupees)


def _row_to_dict(r: BOQItem) -> dict:
    return {
        "id": r.id,
        "schedule_name": r.schedule_name,
        "sr_no": r.sr_no,
        "item_code": r.item_code,
        "quantity": r.quantity,
        "unit": r.unit,
        "basic_value": r.basic_value,
        "estimated_rate": r.estimated_rate,
        "is_tax_line": r.is_tax_line,
    }


def _affected_tender_ids(db) -> list[int]:
    from sqlalchemy import text
    rows = db.execute(text("""
        SELECT tender_id FROM boq_items WHERE sr_no IS NOT NULL
        GROUP BY tender_id
        HAVING EXISTS (
          SELECT 1 FROM (
            SELECT schedule_name, sr_no FROM boq_items b2
            WHERE b2.tender_id = boq_items.tender_id AND b2.sr_no IS NOT NULL
            GROUP BY schedule_name, sr_no
            HAVING COUNT(DISTINCT item_code) > 1
          ) g
        )
        ORDER BY tender_id
    """)).fetchall()
    return [r[0] for r in rows]


def _sched_distinct_sums(rows: list[dict]) -> dict:
    """Per-schedule sum counting each distinct (sr_no, rounded value) ONCE.

    Identical-value twins (same physical line captured twice) must not inflate
    the sum — otherwise collapsing them to one survivor looks like value loss.
    This is the value-preservation reference for the --apply guardrail: it
    changes ONLY when a genuinely distinct value is added or removed.
    """
    seen: dict = defaultdict(set)  # sched -> set of (sr_no, rounded_value)
    for it in rows:
        if it.get("is_tax_line"):
            continue
        v = _row_value(it)
        if v is None:
            continue
        sched = (it.get("schedule_name") or "").upper()
        seen[sched].add((it.get("sr_no"), round(v, 2)))
    out: dict = defaultdict(float)
    for sched, pairs in seen.items():
        out[sched] = sum(val for (_sr, val) in pairs)
    return out


def process_tender(db, tid: int, apply: bool) -> tuple[int, list]:
    rows = db.query(BOQItem).filter(BOQItem.tender_id == tid).all()
    dicts = [_row_to_dict(r) for r in rows]
    survivors, dropped, unresolvable = _collapse_value_redundant_twins(dicts)
    survivor_ids = {d["id"] for d in survivors}
    to_delete = [d for d in dicts if d["id"] not in survivor_ids]

    print(f"\n===== tender {tid}: {len(rows)} rows -> drop {dropped} =====")
    for d in to_delete:
        print(f"  DELETE id={d['id']} sch={d['schedule_name']!r} sr={d['sr_no']} "
              f"code={d['item_code']!r} qty={d['quantity']} value={_row_value(d)}")
    for g in unresolvable:
        print(f"  UNRESOLVED sch={g['schedule']!r} sr={g['sr_no']} "
              f"codes={g['codes']} values={g['values']} (left untouched)")

    if apply and to_delete:
        before, after = _sched_distinct_sums(dicts), _sched_distinct_sums(survivors)
        for sched, bsum in before.items():
            if abs(bsum - after.get(sched, 0.0)) > EPS:
                print(f"  !! ABORT tender {tid}: schedule {sched!r} value-sum "
                      f"drift {bsum:.2f} -> {after.get(sched, 0.0):.2f} > {EPS}")
                db.rollback()
                return 0, unresolvable
        if not survivor_ids:
            raise RuntimeError(
                f"tender {tid}: refusing to delete — would leave 0 survivors"
            )
        del_ids = [d["id"] for d in to_delete]
        db.query(BOQItem).filter(BOQItem.id.in_(del_ids)).delete(
            synchronize_session=False)
        db.commit()
        print(f"  APPLIED: deleted {len(del_ids)} row(s).")

    return len(to_delete), unresolvable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="delete flagged rows")
    ap.add_argument("--tender", type=int, help="limit to one tender_id")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        tids = [args.tender] if args.tender else _affected_tender_ids(db)
        print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}; tenders: {tids}")
        total, unresolved = 0, 0
        for tid in tids:
            n, unres = process_tender(db, tid, args.apply)
            total += n
            unresolved += len(unres)
        print(f"\nTotal rows {'deleted' if args.apply else 'to delete'}: "
              f"{total}; unresolved groups: {unresolved}")
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
