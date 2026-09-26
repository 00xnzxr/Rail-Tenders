"""The deterministic IREPS parser against the layouts a real NIT actually prints.

`parse_nit_text` reads the "2. SCHEDULE" token stream verbatim, and its result
wins outright over the AI path -- so a row it cannot read is a row costing
never sees, with nothing to reconcile against. Three variants it did not
expect, each found on a real tender:

  1. An Item Code that wraps onto two lines ("CONVERSION" / "MAT"). The fixed
     eight-token run shifted by one, failed the numeric checks, and the row
     was skipped: ten of the Liluah NIT's sixteen rows, including the two
     that cite the material-list annexures, and both schedules' totals.
  2. A unit and the rate beside it rendered as ONE line ("Set 223465.00"):
     five of NIT 5374229's ninety-seven rows.
  3. An Item Code cell pushed past a page break, so the serial is followed by
     the quantity and the code turns up after the page boilerplate (row 90
     of the same NIT).

And the guard behind them all: `_deterministic_parse_gap` declares a
deterministic parse incomplete when it has fewer rows than the text prints
"Description:-" lines, or when a schedule's summed amounts miss its printed
total, so a fourth variant fails over to the AI path instead of winning
half-read.
"""
from pathlib import Path

import fitz
import pytest

from app.services.boq_parser_service import (
    _deterministic_parse_gap,
    build_boq_items_from_text,
)
from app.services.costing.nit_schedule_parser import parse_nit_text

FIXTURES = Path(__file__).parent.parent.parent / "fixtures"


def _text(name: str) -> str:
    doc = fitz.open(str(FIXTURES / name))
    try:
        return "\n".join(pg.get_text() for pg in doc)
    finally:
        doc.close()


# ── the Liluah NIT: wrapped codes and a trailer between banner and total ────


@pytest.fixture(scope="module")
def liluah():
    return parse_nit_text(_text("nit_liluah_vanbrake_2026.pdf"))


def test_liluah_reads_every_row(liluah):
    by_code = {s.code: s for s in liluah.schedules}
    assert set(by_code) == {"A", "B"}
    assert len(by_code["A"].lines) == 11
    assert len(by_code["B"].lines) == 5


def test_liluah_wrapped_item_codes_are_joined(liluah):
    a = {l.sr_no: l for l in next(s for s in liluah.schedules if s.code == "A").lines}
    assert a[3].item_code == "CONVERSION MAT"
    assert a[9].item_code == "CONVERSION LAB"
    assert a[11].item_code == "ELECTRICAL LAB"
    assert a[10].item_code == "Air Brake"
    b = {l.sr_no: l for l in next(s for s in liluah.schedules if s.code == "B").lines}
    assert b[4].item_code == "PAINT MATERIAL"


def test_liluah_stated_totals_are_read_past_the_trailer_line(liluah):
    by_code = {s.code: s for s in liluah.schedules}
    assert by_code["A"].stated_total == 20378311.80
    assert by_code["B"].stated_total == 52942828.80
    assert liluah.advertised_value == 73321140.60


def test_liluah_sums_to_the_paisa(liluah):
    for s in liluah.schedules:
        assert round(sum(l.amount for l in s.lines), 2) == s.stated_total


def test_liluah_annexure_parents_are_present(liluah):
    a = {l.sr_no: l for l in next(s for s in liluah.schedules if s.code == "A").lines}
    assert "Annexure-II" in a[3].description and a[3].unit_rate == 816051.00
    b = {l.sr_no: l for l in next(s for s in liluah.schedules if s.code == "B").lines}
    assert "Annexure-VII" in b[4].description and b[4].unit_rate == 22547.44


# ── NIT 5374229: merged unit/rate cells and a code pushed past a page break ─


@pytest.fixture(scope="module")
def nit_5374229():
    return parse_nit_text(_text("nit_5374229.pdf"))


def test_5374229_reads_every_row(nit_5374229):
    (a,) = nit_5374229.schedules
    assert a.code == "A" and len(a.lines) == 97
    assert a.stated_total == 10466691.00
    assert round(sum(l.amount for l in a.lines), 2) == a.stated_total


def test_5374229_merged_unit_and_rate_are_split(nit_5374229):
    (a,) = nit_5374229.schedules
    r22 = next(l for l in a.lines if l.sr_no == 22)
    assert r22.unit == "Set" and r22.unit_rate == 223465.00 and r22.qty == 1.0


def test_5374229_code_pushed_past_a_page_break_is_recovered(nit_5374229):
    (a,) = nit_5374229.schedules
    r90 = next(l for l in a.lines if l.sr_no == 90)
    assert r90.item_code == "90"
    assert r90.qty == 11.0 and r90.unit == "Numbers" and r90.amount == 6820.00
    assert r90.description.startswith("Supply and wiring to light/fan points")
    assert "Run Date" not in r90.description


# ── the Parel NIT is read exactly as before ──────────────────────────────────


def test_parel_is_unchanged():
    p = parse_nit_text(_text("nit_parel_2531.pdf"))
    counts = {s.code: len(s.lines) for s in p.schedules}
    assert counts == {"A": 2, "B": 81, "C": 1}
    for s in p.schedules:
        assert round(sum(l.amount for l in s.lines), 2) == s.stated_total


# ── synthetic token streams, so a regression reads as a sentence ─────────────


def _stream(rows: str, banner_trailer: str = "") -> str:
    return (
        "2. SCHEDULE\nS.No.\nItem Code\nItem Qty\nQty Unit\nUnit Rate\nBasic Value\n"
        "Escl.(%)\nAmount\nBidding\nUnit\n"
        "Schedule () A-Test schedule\n" + banner_trailer + "1000.00\n" + rows +
        "4. ELIGIBILITY CONDITIONS\n"
    )


def test_a_two_line_item_code_is_one_code():
    text = _stream("1\nCONVERSION\nMAT\n15.00\nCoach Set\n40.00\n600.00\nAT Par\n600.00\nRs.\n"
                   "Description:- Material cost\n"
                   "2\nLAB\n15.00\nCoach Set\n26.67\n400.00\nAT Par\n400.00\nRs.\n"
                   "Description:- Labour\n")
    (a,) = parse_nit_text(text).schedules
    assert [(l.sr_no, l.item_code, l.qty, l.amount) for l in a.lines] == [
        (1, "CONVERSION MAT", 15.0, 600.0), (2, "LAB", 15.0, 400.0),
    ]


def test_a_merged_unit_rate_cell_is_two_cells():
    text = _stream("1\n01\n1.00\nSet 1000.00\n1000.00\nAT Par\n1000.00\nRs.\nDescription:- Panel\n")
    (a,) = parse_nit_text(text).schedules
    assert a.lines[0].unit == "Set" and a.lines[0].unit_rate == 1000.0 and a.lines[0].amount == 1000.0


def test_a_code_after_the_page_break_belongs_to_its_row():
    text = _stream("1\n11.00\nNumbers\n50.00\n550.00\nAT Par\n550.00\nRs.\n"
                   "Page 7 of 12\nRun Date/Time: 05/05/2026 16:01:39\nTENDER DOCUMENT\n"
                   "1\nDescription:- Supply and wiring\n"
                   "2\n02\n9.00\nMetre\n50.00\n450.00\nAT Par\n450.00\nRs.\nDescription:- Next row\n")
    (a,) = parse_nit_text(text).schedules
    assert [(l.sr_no, l.item_code, l.description) for l in a.lines] == [
        (1, "1", "Supply and wiring"), (2, "02", "Next row"),
    ]


def test_the_stated_total_survives_a_trailer_line():
    text = _stream("1\nX\n1.00\nNos\n1000.00\n1000.00\nAT Par\n1000.00\nRs.\nDescription:- Only row\n",
                   banner_trailer="(INCLUSIVE OF ALL TAXES AND CHARGES)\n")
    (a,) = parse_nit_text(text).schedules
    assert a.stated_total == 1000.0 and len(a.lines) == 1


def test_a_schedule_with_no_printed_total_does_not_take_a_row_amount_as_one():
    text = ("2. SCHEDULE\nItem Qty\nSchedule () A-No total printed\n"
            "1\nX\n1.00\nNos\n1000.00\n1000.00\nAT Par\n1000.00\nRs.\nDescription:- Only row\n"
            "4. ELIGIBILITY CONDITIONS\n")
    (a,) = parse_nit_text(text).schedules
    assert a.stated_total is None and len(a.lines) == 1


def test_an_unreadable_row_does_not_swallow_the_next_one():
    """A row whose fields the parser cannot read must be skipped alone; the
    code search must not reach across its description into the next row."""
    text = _stream("1\nX\nbroken\nrow\n"
                   "Description:- Transportation, repair and rewinding of blower motor\n"
                   "2\na\n360.00\nNumbers\n2338.49\n841856.40\nAT Par\n841856.40\nRs.\nDescription:- Real row\n")
    (a,) = parse_nit_text(text).schedules
    assert [(l.sr_no, l.item_code) for l in a.lines] == [(2, "a")]


# ── the guard ────────────────────────────────────────────────────────────────


def test_gap_names_rows_the_text_prints_but_the_parse_lacks():
    text = _text("nit_liluah_vanbrake_2026.pdf")
    items, totals = build_boq_items_from_text(text)
    assert _deterministic_parse_gap(text, items, totals) is None
    assert _deterministic_parse_gap(text, items[:6], totals) == (
        "6 row(s) parsed but the text prints 16 Description:- lines"
    )


def test_gap_names_a_schedule_short_of_its_printed_total():
    items = [{"schedule_name": "A", "amount": 400.0, "description": "x"}]
    totals = [{"schedule_code": "A", "stated_total": 1000.0}]
    text = "Description:- x\n"
    assert "schedule A sums to 400.00" in _deterministic_parse_gap(text, items, totals)
    items[0]["amount"] = 1000.0
    assert _deterministic_parse_gap(text, items, totals) is None


def test_gap_ignores_summary_placeholder_descriptions():
    text = ("Description:- Please see Item Breakup for details.\n"
            "Description:- Real row\n")
    items = [{"schedule_name": "A", "amount": 10.0, "description": "Real row"}]
    assert _deterministic_parse_gap(text, items, []) is None


# ── the Mid-Life NIT: a serial displaced past a page break ─────────────────
#
# Schedule R row 6 prints its fields ("Elect 2.00 Numbers 76700.00 ...") at
# the foot of page 9 and its serial "6" at the top of page 10, just before
# its description. Row 5's description read the fields as text, row 6 was
# lost, and 285 of 286 sent the whole NIT to the AI extractor -- which paired
# rates with the wrong descriptions (a 4.5 kW RBC unit at Rs 883.82).


@pytest.fixture(scope="module")
def midlife_text():
    return _text("nit_liluah_midlife_2026.pdf")


def test_midlife_recovers_the_displaced_serial_row(midlife_text):
    r = {ln.sr_no: ln for s in parse_nit_text(midlife_text).schedules if s.code == "R" for ln in s.lines}
    assert sorted(r) == [1, 2, 3, 4, 5, 6, 7, 8]
    six = r[6]
    assert (six.item_code, six.qty, six.unit, six.unit_rate, six.amount) == (
        "Elect", 2.0, "Numbers", 76700.0, 153400.0)
    assert six.description.startswith("Labour cost for")
    assert six.description.endswith('required for SBC."')
    # Row 5's description no longer carries row 6's cells.
    assert "76700" not in r[5].description and r[5].description.endswith("HT compertment of SBC.")


def test_midlife_is_whole_and_sums_to_the_advertised_value(midlife_text):
    items, totals = build_boq_items_from_text(midlife_text)
    assert len(items) == 286
    assert _deterministic_parse_gap(midlife_text, items, totals) is None
    assert round(sum(i["amount"] for i in items), 2) == 173151893.83
