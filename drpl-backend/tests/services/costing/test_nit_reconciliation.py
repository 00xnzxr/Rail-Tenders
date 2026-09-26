from app.services.costing.nit_schedule_parser import ParsedNIT, NITSchedule, NITLine
from app.services.costing.nit_reconciliation import reconcile

def _line(code, sr, amt):
    return NITLine(code, sr, str(sr), f"item {sr}", 1, "Nos", amt, amt, 0.0, amt, "AT Par")

def test_pass_when_lines_match_totals():
    sched = NITSchedule("A", "t", 300.0, (_line("A",1,100.0), _line("A",2,200.0)))
    parsed = ParsedNIT(advertised_value=300.0, schedules=(sched,))
    rep = reconcile(parsed)
    assert rep.ok is True
    assert rep.schedules[0].ok is True and rep.schedules[0].delta == 0.0

def test_flag_when_schedule_lines_dont_match_stated_total():
    # stated total 300 but lines only sum to 250 -> flag, no fabrication
    sched = NITSchedule("A", "t", 300.0, (_line("A",1,100.0), _line("A",2,150.0)))
    parsed = ParsedNIT(advertised_value=300.0, schedules=(sched,))
    rep = reconcile(parsed)
    assert rep.ok is False
    assert rep.schedules[0].ok is False
    assert abs(rep.schedules[0].delta - (-50.0)) < 1e-6

def test_flag_when_schedules_dont_sum_to_advertised_value():
    sched = NITSchedule("A", "t", 300.0, (_line("A",1,300.0),))
    parsed = ParsedNIT(advertised_value=999.0, schedules=(sched,))
    rep = reconcile(parsed)
    assert rep.ok is False
    assert abs(rep.grand_delta - (300.0 - 999.0)) < 1e-6

def test_within_one_rupee_tolerance_passes():
    sched = NITSchedule("A", "t", 300.0, (_line("A",1,299.4),))
    parsed = ParsedNIT(advertised_value=300.0, schedules=(sched,))
    assert reconcile(parsed).ok is True


import fitz
from pathlib import Path
from app.services.costing.nit_schedule_parser import parse_nit_text

def test_parel_fixture_reconciles_exactly():
    p = Path(__file__).parent.parent.parent / "fixtures" / "nit_parel_2531.pdf"
    text = "\n".join(pg.get_text() for pg in fitz.open(str(p)))
    rep = reconcile(parse_nit_text(text))
    assert rep.ok is True
    assert rep.grand_line_sum == 60879392.16
