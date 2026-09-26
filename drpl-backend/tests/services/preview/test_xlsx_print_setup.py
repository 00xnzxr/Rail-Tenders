"""The print-setup pass is what keeps columns on the page.

Without it LibreOffice clips at the page boundary: the spike against prod
breakdown 76 lost four columns off the Summary sheet (Estimated Cost, Gross
Margin, GM %, and the Reconciliation status columns) and truncated the
narrative. Excel hides this on screen by overflowing text across empty cells.
"""
import os

from openpyxl import Workbook, load_workbook

from app.services.xlsx_print_setup import apply_print_setup, MAX_WIDTH, MIN_WIDTH


def _workbook(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "1. Summary"
    ws["A1"] = "Schedule ELECTRICAL AND PNEUMATIC SPARES FOR DETC"
    ws["B1"] = 1234.5
    ws["C1"] = "x" * 200          # longer than the clamp
    ws["D1"] = "=SUM(B1:B1)"      # formula: its text must not drive width
    second = wb.create_sheet("2. Cost Assumptions")
    second["A1"] = "short"
    src = str(tmp_path / "src.xlsx")
    wb.save(src)
    return src


def test_returns_sheet_titles_in_tab_order(tmp_path):
    src = _workbook(tmp_path)
    names = apply_print_setup(src, str(tmp_path / "out.xlsx"))
    assert names == ["1. Summary", "2. Cost Assumptions"]


def test_source_workbook_is_never_modified(tmp_path):
    src = _workbook(tmp_path)
    before = open(src, "rb").read()
    apply_print_setup(src, str(tmp_path / "out.xlsx"))
    assert open(src, "rb").read() == before


def test_column_width_fits_content(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    ws = load_workbook(dest)["1. Summary"]
    # 48-char label + padding, under the clamp.
    assert ws.column_dimensions["A"].width >= len(
        "Schedule ELECTRICAL AND PNEUMATIC SPARES FOR DETC"
    )
    assert ws.column_dimensions["A"].width <= MAX_WIDTH


def test_overlong_cell_is_clamped_and_wrapped(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    ws = load_workbook(dest)["1. Summary"]
    assert ws.column_dimensions["C"].width == MAX_WIDTH
    assert ws["C1"].alignment.wrap_text is True


def test_narrow_column_gets_a_floor(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    ws = load_workbook(dest)["1. Summary"]
    assert ws.column_dimensions["B"].width >= MIN_WIDTH


def test_formula_text_does_not_drive_width(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    ws = load_workbook(dest)["1. Summary"]
    # "=SUM(B1:B1)" is 11 chars; the column must not be sized to the formula
    # source, only floored to MIN_WIDTH.
    assert ws.column_dimensions["D"].width == MIN_WIDTH


def test_fit_to_width_is_set_on_every_sheet(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    wb = load_workbook(dest)
    for ws in wb.worksheets:
        assert ws.page_setup.fitToWidth == 1
        assert ws.page_setup.fitToHeight == 0
        assert ws.sheet_properties.pageSetUpPr.fitToPage is True
        assert ws.page_setup.orientation == "landscape"


def test_survives_merged_cells(tmp_path):
    """Non-anchor cells of a merged range are MergedCell, which has no
    column_letter. The formula-only-column floor-width loop indexes
    col_cells[0] from ws.iter_cols() and must not assume it is a plain Cell.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "1. Summary"
    ws["A1"] = "Header spanning three columns"
    ws.merge_cells("A1:C1")
    ws["D1"] = "=SUM(B1:B1)"  # formula-only column, drives the buggy loop
    src = str(tmp_path / "src_merged.xlsx")
    wb.save(src)

    names = apply_print_setup(src, str(tmp_path / "out_merged.xlsx"))
    assert names == ["1. Summary"]


def test_real_fixture_workbook(tmp_path):
    """Pure-openpyxl call against a real production workbook with merged
    header blocks — no LibreOffice involved, so it runs anywhere.
    """
    fixture = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "fixtures",
        "spike_workbook.xlsx",
    )
    dest = str(tmp_path / "out_fixture.xlsx")
    names = apply_print_setup(fixture, dest)
    assert names == ["1. Summary", "2. Cost Assumptions", "3. All Schedules"]
