"""Prepare a workbook copy for faithful PDF rendering.

The generated cost workbook sets no print setup. LibreOffice therefore clips at
the page boundary, and columns past that boundary are dropped entirely — the
spike against prod breakdown 76 lost Estimated Cost, Gross Margin, GM % and the
Reconciliation status columns off the Summary sheet, and truncated the Key
Observations narrative. Excel masks this on screen by overflowing text across
adjacent empty cells; it only appears when the sheet is printed.

This pass sizes columns to their content, switches to landscape, and scales each
sheet to one page wide. Verified to restore every clipped column and to reduce
that workbook from 52 pages to 9.

The source file is never modified — the distributed .xlsx must stay byte-identical
to what costing produced.
"""
from __future__ import annotations

import logging

from openpyxl import load_workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

# Column width bounds, in approximate characters (openpyxl's unit).
MIN_WIDTH = 8
MAX_WIDTH = 60


def apply_print_setup(src_path: str, dest_path: str) -> list[str]:
    """Write a print-ready copy of ``src_path`` to ``dest_path``.

    Returns the worksheet titles in tab order — the authoritative tab list for
    the preview manifest.
    """
    wb = load_workbook(src_path)

    for ws in wb.worksheets:
        widest: dict[str, int] = {}
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if value is None:
                    continue
                text = str(value)
                # A formula's source text is not what prints; its result is, and
                # we cannot know that width here. Let the floor cover it.
                if text.startswith("="):
                    continue
                longest = max((len(seg) for seg in text.split("\n")), default=0)
                col = cell.column_letter
                if longest > widest.get(col, 0):
                    widest[col] = longest

        for col, longest in widest.items():
            ws.column_dimensions[col].width = max(
                MIN_WIDTH, min(MAX_WIDTH, longest + 2)
            )
            if longest > MAX_WIDTH:
                # Too wide to fit: wrap instead of letting it run off the page.
                for cell in ws[col]:
                    ws[cell.coordinate].alignment = Alignment(
                        horizontal=cell.alignment.horizontal,
                        vertical=cell.alignment.vertical,
                        wrap_text=True,
                    )

        # Columns with only formulas never entered `widest`; give them the floor
        # so they are not left at openpyxl's default.
        for idx, col_cells in enumerate(ws.iter_cols(), start=1):
            # ``col_cells[0]`` may be a MergedCell (non-anchor cell of a merged
            # range), which has no ``column_letter``. Derive the letter from
            # the column index instead so merged sheets don't crash here.
            letter = get_column_letter(idx)
            if letter not in widest and any(c.value is not None for c in col_cells):
                ws.column_dimensions[letter].width = MIN_WIDTH

        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_margins.left = 0.3
        ws.page_margins.right = 0.3
        ws.page_margins.top = 0.4
        ws.page_margins.bottom = 0.4

    wb.save(dest_path)
    names = [ws.title for ws in wb.worksheets]
    wb.close()
    logger.info(
        "[xlsx print setup] fitted %d sheet(s): %s", len(names), ", ".join(names)
    )
    return names
