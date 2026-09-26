import fitz
from pathlib import Path
from app.services.boq_parser_service import build_boq_items_from_text, _dedup_boq_rows  # new helper

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "nit_parel_2531.pdf"

def _text():
    return "\n".join(pg.get_text() for pg in fitz.open(str(FIXTURE)))

def test_deterministic_pass_yields_one_row_per_line_no_dupes():
    items, sched_totals = build_boq_items_from_text(_text())
    # Schedule A: 2 lines, C: 1 line; B: many. No duplicates: (schedule, sr_no,
    # item_code) is unique across the whole set.
    keys = [(it["schedule_name"], it["sr_no"], it["item_code"]) for it in items]
    assert len(keys) == len(set(keys)), "duplicate BOQ rows present"
    a = [it for it in items if it["schedule_name"] == "A"]
    assert len(a) == 2
    # estimated_rate carries the NIT unit rate verbatim
    assert any(abs(it["estimated_rate"] - 42398.05) < 1e-6 for it in a)

def test_schedule_totals_captured():
    _items, sched_totals = build_boq_items_from_text(_text())
    by = {s["schedule_code"]: s for s in sched_totals}
    assert by["A"]["stated_total"] == 5975127.60
    assert by["B"]["stated_total"] == 53693176.56
    assert by["A"]["advertised_value"] == 60879392.16

def test_dedup_collapses_cross_document_duplicate_schedule():
    # Simulate the same schedule being printed twice across two documents
    # (e.g. an original NIT + a corrigendum re-printing Schedule A) — the
    # parse_boq_from_tender safety-net calls _dedup_boq_rows on the
    # concatenated per-doc items before persisting. Prove it collapses back
    # down to one row per (schedule, sr_no, item_code).
    items, _sched_totals = build_boq_items_from_text(_text())
    doubled = items + items
    deduped = _dedup_boq_rows(doubled)
    assert len(deduped) == len(items)
    keys = [(it["schedule_name"], it["sr_no"], it["item_code"]) for it in deduped]
    assert len(keys) == len(set(keys)), "duplicate BOQ rows survived dedup"
