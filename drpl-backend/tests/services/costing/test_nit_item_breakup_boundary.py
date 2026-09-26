"""The "Schedule of Items" boundary must not fire on a summary row.

Regression: Command Center session 290 (tender 3822, BE-TLAC-RMPU-CMC-2026-28).
An IREPS NIT's section `2. SCHEDULE` is a SUMMARY: one row per schedule whose
description is literally "Please see Item Breakup for details." carrying the
schedule total. The real line items live in section `3. ITEM BREAKUP`.

`_SCHEDULE_END_RE` matched the bare words "Item Breakup", so `_load_pdf_pages`
truncated the NIT at the FIRST summary row on page 1 and broke out of the loop,
discarding pages 2-12 -- the entire Item Breakup section. Schedule D's ~19
priced line items were never extracted; the user reported the gap on 2026-08-07.

"ITEM BREAKUP" is not a terminator at all: it is the START of the region we
want. Only a genuine numbered/anchored section heading may end the schedule.
"""
from app.services.boq_parser_service import _SCHEDULE_END_RE


# Verbatim from tender 3822's NIT (occurs 5x before the real heading).
SUMMARY_ROW = "Please see Item Breakup for details."


def test_summary_row_does_not_end_the_schedule_section():
    assert _SCHEDULE_END_RE.search(SUMMARY_ROW) is None


def test_item_breakup_heading_does_not_end_the_schedule_section():
    # It STARTS the line-item region -- keeping it would drop every real row.
    assert _SCHEDULE_END_RE.search("3. ITEM BREAKUP") is None


def test_real_section_headings_still_end_the_schedule_section():
    for heading in [
        "4. ELIGIBILITY CONDITIONS",
        "5. COMPLIANCE",
        "Special Financial Criteria",
        "Special Technical Criteria",
        "General Instructions",
        "Undertakings",
        "Documents attached with tender",
    ]:
        assert _SCHEDULE_END_RE.search(heading) is not None, heading


def test_prose_mentioning_a_heading_word_does_not_end_the_section():
    # A work item that merely mentions compliance is not a section boundary.
    assert _SCHEDULE_END_RE.search(
        "Supply of RDSO compliance-tested thermostat for Non-LHB coaches"
    ) is None
