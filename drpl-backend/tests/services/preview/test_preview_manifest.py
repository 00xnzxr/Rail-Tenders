"""Mapping the PDF outline onto the worksheet tab list.

LibreOffice Calc emits one PDF bookmark per worksheet (confirmed by the
2026-08-02 spike: 3 entries with start pages 1 / 4 / 13). The manifest turns
that into the preview's tab bar.
"""
from app.services.xlsx_preview_service import build_manifest


def test_maps_each_sheet_to_its_start_page():
    toc = [
        [1, "1. Summary", 1],
        [1, "2. Cost Assumptions", 4],
        [1, "3. All Schedules", 13],
    ]
    m = build_manifest(["1. Summary", "2. Cost Assumptions", "3. All Schedules"], toc, 52)
    assert m["page_count"] == 52
    assert m["sheets"] == [
        {"name": "1. Summary", "start_page": 1},
        {"name": "2. Cost Assumptions", "start_page": 4},
        {"name": "3. All Schedules", "start_page": 13},
    ]


def test_empty_outline_falls_back_to_one_tab():
    """A workbook whose outline is missing still previews -- just less navigable."""
    m = build_manifest(["1. Summary", "2. Cost Assumptions"], [], 9)
    assert m["sheets"] == [{"name": "1. Summary", "start_page": 1}]
    assert m["page_count"] == 9


def test_partial_outline_falls_back_to_one_tab():
    """Fewer bookmarks than sheets means the mapping is untrustworthy."""
    m = build_manifest(["A", "B", "C"], [[1, "A", 1]], 6)
    assert m["sheets"] == [{"name": "A", "start_page": 1}]


def test_outline_order_wins_over_name_matching():
    """Excel truncates sheet names to 31 chars, so bookmark text may not match
    the openpyxl title exactly. Position is the reliable key, not the string."""
    long_a = "A" * 40
    long_b = "B" * 40
    toc = [[1, "A" * 31, 1], [1, "B" * 31, 5]]
    m = build_manifest([long_a, long_b], toc, 8)
    # Names come from openpyxl (authoritative), pages from the outline.
    assert m["sheets"] == [
        {"name": long_a, "start_page": 1},
        {"name": long_b, "start_page": 5},
    ]


def test_nested_outline_entries_are_ignored():
    """Only top-level (level 1) bookmarks are sheets."""
    toc = [[1, "A", 1], [2, "A sub-heading", 2], [1, "B", 5]]
    m = build_manifest(["A", "B"], toc, 8)
    assert m["sheets"] == [
        {"name": "A", "start_page": 1},
        {"name": "B", "start_page": 5},
    ]


def test_no_sheets_yields_no_tabs():
    assert build_manifest([], [], 0)["sheets"] == []


def test_malformed_outline_row_does_not_crash():
    """A row with no page number must not IndexError; a partial outline that
    results falls back to a single tab rather than mis-aligning tabs to pages."""
    toc = [[1, "A", 1], [1, "B"]]  # second row missing its page
    m = build_manifest(["A", "B"], toc, 8)
    assert m["sheets"] == [{"name": "A", "start_page": 1}]


def test_unresolved_destination_page_falls_back_to_one_tab():
    """PyMuPDF returns -1 for a bookmark it cannot resolve to a page. Trusting
    it would produce getPage(-1), which throws in pdf.js."""
    toc = [[1, "A", 1], [1, "B", -1]]
    m = build_manifest(["A", "B"], toc, 8)
    assert m["sheets"] == [{"name": "A", "start_page": 1}]


def test_out_of_range_page_falls_back_to_one_tab():
    """A start page beyond page_count would produce start > end for the
    previous tab, silently rendering an empty container."""
    toc = [[1, "A", 1], [1, "B", 99]]
    m = build_manifest(["A", "B"], toc, 8)
    assert m["sheets"] == [{"name": "A", "start_page": 1}]


def test_decreasing_page_sequence_falls_back_to_one_tab():
    """Nothing in the outline format guarantees ascending order; a
    decreasing sequence must not be trusted as a page-range mapping."""
    toc = [[1, "A", 5], [1, "B", 2]]
    m = build_manifest(["A", "B"], toc, 8)
    assert m["sheets"] == [{"name": "A", "start_page": 1}]
