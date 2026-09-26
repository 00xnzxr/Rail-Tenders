"""An IREPS schedule banner says what its rows price, and the capture keeps it.

The Liluah Mid-Life NIT lists "Web to Drg. No. LE11185" twice: in schedule A,
"(Mechanical) Cost of Material ...", at Rs 279.66 -- the web itself -- and in
schedule B, "COST OF LABOUR ...", at Rs 2,239.21 -- the labour to cut the
corroded one out and weld the new one in. Both banners end "(INCLUSIVE OF
ALL TAXES AND CHARGES)". The capture kept only the letter, so nothing
downstream could tell a material row from a labour row, or a rate with GST
in it from one without. `NITSchedule.full_title` reads the banner with its
continuation lines, the capture stores it as `BOQScheduleTotal.title`, and
`schedule_context` says what it means.
"""
import itertools
from pathlib import Path

from app.models.costing_template import BOQScheduleTotal
from app.services.costing import schedule_context as sc

_ids = itertools.count(996000)
FIXTURES = Path(__file__).parent / "fixtures"


def _midlife_text() -> str:
    import fitz
    doc = fitz.open(str(FIXTURES / "nit_liluah_midlife_2026.pdf"))
    try:
        return "".join(p.get_text() for p in doc)
    finally:
        doc.close()


def test_the_mid_life_banners_say_what_each_schedule_prices():
    from app.services.costing.nit_schedule_parser import schedule_titles

    titles = schedule_titles(_midlife_text())
    assert len(titles) == 19
    assert sc.classify_title(titles["B"]) == (sc.LABOUR, True)       # COST OF LABOUR ... INCLUSIVE
    assert sc.classify_title(titles["A"]) == (sc.MATERIAL, True)     # (Mechanical) Cost of Material
    assert sc.classify_title(titles["O"]) == (sc.LABOUR, True)       # stripping of coaches
    assert sc.classify_title(titles["P"]) == (sc.MATERIAL, None)     # Electrical Materials, taxes unstated
    assert sc.classify_title(titles["R"]) == (sc.MATERIAL_AND_LABOUR, None)
    assert sc.classify_title(titles["T"])[0] == sc.LABOUR
    # The banner continues over four lines; all of it is read, and the
    # instruction IREPS repeats in every banner is not.
    assert titles["B"].endswith("(INCLUSIVE OF ALL TAXES AND CHARGES)")
    assert "15 LWACCN, 09 LWACCW, 05 LWFAC, 05 LWCBAC & 03 LWLRRM" in titles["B"]
    assert "Tenderer should submit" not in titles["B"]


def test_the_row_count_is_unchanged_by_reading_the_banners():
    from app.services.boq_parser_service import _deterministic_parse_gap, build_boq_items_from_text

    text = _midlife_text()
    items, totals = build_boq_items_from_text(text)
    assert len(items) == 286
    assert _deterministic_parse_gap(text, items, totals) is None
    assert {t["schedule_code"]: t["title"] for t in totals}["B"].startswith("COST OF LABOUR")


def test_the_capture_stores_each_banner(db):
    from app.services.boq_parser_service import _persist_schedule_totals, build_boq_items_from_text

    tid = next(_ids)
    _items, totals = build_boq_items_from_text(_midlife_text())
    _persist_schedule_totals(db, tid, totals)
    ctx = sc.schedule_contexts(db, tid, read_documents=False)
    assert ctx["B"].work == sc.LABOUR and ctx["B"].taxes_inclusive is True
    assert ctx["C"].work == sc.MATERIAL
    assert "labour" in ctx["B"].plain_work()


def test_a_capture_without_banners_reads_them_from_its_document_once(db, monkeypatch):
    tid = next(_ids)
    db.add(BOQScheduleTotal(tender_id=tid, schedule_code="B", stated_total=None))
    db.commit()
    reads = []

    def _read(_db, _tid):
        reads.append(_tid)
        return {"B": "COST OF LABOUR: corrosion work (INCLUSIVE OF ALL TAXES AND CHARGES)"}

    monkeypatch.setattr(sc, "_titles_from_documents", _read)
    first = sc.schedule_contexts(db, tid)
    assert first["B"].work == sc.LABOUR and first["B"].taxes_inclusive is True
    second = sc.schedule_contexts(db, tid)
    assert second["B"].title.startswith("COST OF LABOUR")
    assert reads == [tid], "the banners are stored after the first read"
