"""
Table finding by header text, and drift detection.

Together these replace ``selector_configs/ireps.json``: one finds the data
without betting on class names, the other notices when the portal has moved
before a human does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from collector.parsing import drift, tables
from collector.portals.base import ParserDrift
from collector.portals.ireps import rows_to_tenders

FIXTURE = Path(__file__).parent / "fixtures" / "ireps_results_page.html"


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# -- finding the table ---------------------------------------------------


def test_finds_the_results_table_not_the_first_table(html):
    """The filter-summary table sits above the real one -- table[0] is wrong."""
    table, mapping = tables.find_results_table(html)
    assert table.get("id") == "resultTable"
    assert "tenderId" in mapping and "closingDate" in mapping


def test_columns_are_resolved_by_header_text(html):
    _table, mapping = tables.find_results_table(html)
    assert mapping["tenderId"] == 1
    assert mapping["title"] == 2
    assert mapping["department"] == 3
    assert mapping["organisation"] == 4
    assert mapping["closingDate"] == 8


def test_reordering_columns_does_not_break_it(html):
    """The point of reading the header rather than betting on a position."""
    swapped = html.replace(
        "<th>Tender No</th>\n        <th>Tender Title</th>",
        "<th>Tender Title</th>\n        <th>Tender No</th>",
    ).replace(
        '<td>12212090</td>\n        <td><a href="/epsn/tenderDetail.do?id=12212090"',
        '<td><a href="/epsn/tenderDetail.do?id=12212090"',
        1,
    )
    _t, mapping = tables.find_results_table(swapped)
    assert mapping["title"] < mapping["tenderId"], "the mapping followed the header"


def test_a_renamed_header_raises_loudly_rather_than_returning_nothing(html):
    """The failure a selector file gives you as a silent zero."""
    broken = html.replace("Closing Date &amp; Time", "Bid Submission Cutoff")
    broken = broken.replace("Tender No", "Reference")
    with pytest.raises(ParserDrift):
        tables.find_results_table(broken)


def test_a_page_with_no_tables_raises(html):
    with pytest.raises(ParserDrift):
        tables.find_results_table("<html><body><p>nothing here</p></body></html>")


# -- reading rows --------------------------------------------------------


def test_full_title_is_read_from_the_title_attribute(html):
    """IREPS clips the visible text and keeps the full value in title=.

    Reading the text alone gives a name ending in an ellipsis, which
    drpl-backend's ``is_corrupt_title`` then rejects and replaces with a
    "ireps #12212090" placeholder.
    """
    rows = rows_to_tenders(html, "https://www.ireps.gov.in")
    first = rows[0]
    assert first["title"].startswith("Supply of Traction Motor Bearings for WAP-7")
    assert "......" not in first["title"]


def test_rows_carry_the_portal_dedupe_key(html):
    rows = rows_to_tenders(html, "https://www.ireps.gov.in")
    assert [r["tenderId"] for r in rows] == ["12212090", "09232145", "77451003", "66120088"]
    assert all(r["portal"] == "ireps" for r in rows)


def test_dates_are_parsed_as_day_first(html):
    """IREPS renders dd/mm/yyyy. Reading 02/09 as February would be nine months out."""
    rows = rows_to_tenders(html, "https://www.ireps.gov.in")
    assert rows[0]["openingDate"].startswith("2026-09-02")
    assert rows[0]["closingDate"].startswith("2026-09-24")


def test_indian_digit_grouping_parses(html):
    rows = rows_to_tenders(html, "https://www.ireps.gov.in")
    assert rows[0]["estimatedValue"] == 12545000.0
    assert rows[0]["emdAmount"] == 250900.0


def test_a_missing_value_is_none_not_zero_and_not_a_rejection(html):
    """None routes downstream to "fetch the detail page and find out".

    Zero would look like a free tender; a rejection would drop exactly the row
    that most needs enrichment.
    """
    rows = rows_to_tenders(html, "https://www.ireps.gov.in")
    row = next(r for r in rows if r["tenderId"] == "77451003")
    assert row["estimatedValue"] is None
    assert row in rows, "a row with no value is still shipped"


def test_document_links_are_absolutised(html):
    rows = rows_to_tenders(html, "https://www.ireps.gov.in")
    links = rows[0]["documentLinks"]
    assert links and all(u.startswith("https://www.ireps.gov.in/") for u in links)
    assert rows[0]["nitDocumentLinks"], "the NIT link should be classified"


def test_a_row_with_no_documents_still_ships(html):
    rows = rows_to_tenders(html, "https://www.ireps.gov.in")
    row = next(r for r in rows if r["tenderId"] == "77451003")
    assert row["documentLinks"] == []


# -- drift ---------------------------------------------------------------


def test_healthy_rows_produce_no_findings():
    rows = [{"tenderId": f"x{i}", "title": "t", "closingDate": "2026-01-01"} for i in range(20)]
    assert drift.check_fill(rows, {"tenderId": 1.0, "title": 0.95}, "gem") == []


def test_a_field_that_collapses_is_reported():
    """A portal that renames bd_category_name returns a valid page of empty
    titles, and nothing throws. Fill rate is the only alarm that fires."""
    rows = [{"tenderId": f"x{i}", "title": ""} for i in range(20)]
    findings = drift.check_fill(rows, {"tenderId": 1.0, "title": 0.95}, "gem", "gem-allbids-v1")
    assert len(findings) == 1
    assert findings[0]["field"] == "title"
    assert findings[0]["fill_rate"] == 0.0
    assert findings[0]["fetcher"] == "gem-allbids-v1"


def test_ordinary_variation_does_not_page_anyone():
    """Some tenders genuinely have no closing date. That is not drift."""
    rows = [{"closingDate": "2026-01-01"} for _ in range(17)] + [{"closingDate": ""}] * 3
    assert drift.check_fill(rows, {"closingDate": 0.95}, "ireps") == []


def test_a_tiny_sample_is_noise_not_a_signal():
    assert drift.check_fill([{"title": ""}], {"title": 1.0}, "gem") == []


def test_whitespace_only_does_not_count_as_populated():
    rows = [{"title": "   "} for _ in range(20)]
    assert drift.check_fill(rows, {"title": 1.0}, "gem")


def test_summarise_is_none_when_nothing_is_wrong():
    assert drift.summarise([]) is None
    assert "title" in drift.summarise(
        [{"field": "title", "fill_rate": 0.1, "baseline": 0.95}]
    )
