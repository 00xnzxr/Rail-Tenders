import fitz  # PyMuPDF
from pathlib import Path
from app.services.costing.nit_schedule_parser import parse_nit_text, is_ireps_schedule_format

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "nit_parel_2531.pdf"

def _fixture_text() -> str:
    doc = fitz.open(str(FIXTURE))
    return "\n".join(pg.get_text() for pg in doc)

def test_detects_ireps_format():
    assert is_ireps_schedule_format(_fixture_text()) is True

def test_parses_advertised_value():
    parsed = parse_nit_text(_fixture_text())
    assert parsed.advertised_value == 60879392.16

def test_parses_three_schedules_with_stated_totals():
    parsed = parse_nit_text(_fixture_text())
    by_code = {s.code: s for s in parsed.schedules}
    assert set(by_code) == {"A", "B", "C"}
    assert by_code["A"].stated_total == 5975127.60
    assert by_code["B"].stated_total == 53693176.56
    assert by_code["C"].stated_total == 1211088.00

def test_schedule_a_has_exactly_two_lines_no_duplicates():
    parsed = parse_nit_text(_fixture_text())
    a = next(s for s in parsed.schedules if s.code == "A")
    assert len(a.lines) == 2
    line1 = a.lines[0]
    assert line1.sr_no == 1 and line1.item_code == "1"
    assert line1.qty == 120.0 and line1.unit == "Set"
    assert line1.unit_rate == 42398.05 and line1.amount == 5087766.00
    assert "POH maintenance" in line1.description

def test_schedule_c_single_line():
    parsed = parse_nit_text(_fixture_text())
    c = next(s for s in parsed.schedules if s.code == "C")
    assert len(c.lines) == 1
    assert c.lines[0].item_code == "a" and c.lines[0].qty == 120.0
    assert c.lines[0].amount == 1211088.00

def test_line_amounts_sum_to_stated_total_per_schedule():
    parsed = parse_nit_text(_fixture_text())
    for s in parsed.schedules:
        line_sum = round(sum(l.amount for l in s.lines if l.amount is not None), 2)
        assert abs(line_sum - s.stated_total) <= 1.0, f"{s.code}: {line_sum} vs {s.stated_total}"

def test_no_prose_lines_captured():
    parsed = parse_nit_text(_fixture_text())
    all_desc = " ".join(l.description for s in parsed.schedules for l in s.lines).lower()
    assert "i/we the tenderer" not in all_desc
    assert "eligibility" not in all_desc
    assert "gst mandate" not in all_desc


def test_no_page_boilerplate_in_descriptions():
    """FINDING 1: page header/footer boilerplate must not leak into descriptions
    when a line item sits at a page boundary."""
    parsed = parse_nit_text(_fixture_text())
    b = next(s for s in parsed.schedules if s.code == "B")
    line6 = next(l for l in b.lines if l.sr_no == 6 and l.item_code == "b")
    assert "Page" not in line6.description
    assert "Run Date" not in line6.description
    assert "TENDER DOCUMENT" not in line6.description
    assert "Tender No:" not in line6.description
    assert "Closing Date/Time:" not in line6.description
    assert "blower" in line6.description.lower()
    assert "erection" in line6.description.lower()

    for s in parsed.schedules:
        for l in s.lines:
            assert "Page 1 of 7" not in l.description
            assert "Run Date/Time:" not in l.description


def test_schedule_a_line1_escl_and_bidding_unit():
    """FINDING 2: lock current escl/bidding_unit slot behavior for the AT-Par
    fixture rows."""
    parsed = parse_nit_text(_fixture_text())
    a = next(s for s in parsed.schedules if s.code == "A")
    line1 = a.lines[0]
    assert line1.escalation_pct == 0.0
    assert line1.bidding_unit == "AT Par"


def test_schedule_c_title_has_no_export_artifact():
    """FINDING 3: the '1000 more rows at the bottom' IREPS export artifact must
    not land in the schedule title."""
    parsed = parse_nit_text(_fixture_text())
    c = next(s for s in parsed.schedules if s.code == "C")
    assert "1000 more rows" not in c.title
