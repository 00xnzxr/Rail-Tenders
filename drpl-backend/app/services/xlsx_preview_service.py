"""Render a cost-breakdown workbook to a previewable PDF.

One `soffice` pass over the WHOLE workbook. Converting sheet-by-sheet is not an
option: the workbook uses cross-sheet formulas and named ranges
(`xlsx_generator_tool.py:767-812`), so isolating a sheet turns those references
into #REF!. A single pass also lets LibreOffice compute the totals, which
openpyxl never wrote cached values for.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid

logger = logging.getLogger(__name__)


def build_manifest(sheet_names: list[str], toc: list, page_count: int) -> dict:
    """Map worksheet tab order onto PDF start pages.

    `sheet_names` comes from openpyxl and is authoritative for the tab labels.
    `toc` is PyMuPDF's `Document.get_toc()` — `[level, title, page]` rows.

    Names are taken from openpyxl rather than the bookmark text because Excel
    truncates sheet names to 31 characters, so the two can differ. Position is
    the reliable key.

    When the outline is absent or has fewer top-level entries than there are
    sheets, the mapping cannot be trusted, so we emit a single tab spanning the
    document. The preview is then less navigable but still correct.
    """
    if not sheet_names:
        return {"sheets": [], "page_count": page_count}

    tops = [row for row in (toc or []) if row and len(row) >= 3 and row[0] == 1]

    def _single_tab_fallback(reason: str) -> dict:
        logger.warning(
            "[xlsx preview] %s — falling back to a single tab", reason,
        )
        return {
            "sheets": [{"name": sheet_names[0], "start_page": 1}],
            "page_count": page_count,
        }

    if len(tops) < len(sheet_names):
        if not tops:
            return _single_tab_fallback(
                f"outline has 0 top-level entries for {len(sheet_names)} sheet(s)"
            )
        return _single_tab_fallback(
            f"outline has {len(tops)} top-level entries for {len(sheet_names)} sheet(s)"
        )

    # PyMuPDF returns -1 for a destination it cannot resolve, and nothing
    # otherwise guarantees the outline's page numbers are in document order.
    # Either produces a broken tab (start > end -> silently empty container,
    # or getPage(-1) -> throws), so validate before trusting the mapping.
    start_pages = [int(tops[i][2]) for i in range(len(sheet_names))]

    if any(p < 1 or p > page_count for p in start_pages):
        return _single_tab_fallback(
            f"outline has an out-of-range page number in {start_pages} "
            f"(page_count={page_count})"
        )

    if any(start_pages[i] > start_pages[i + 1] for i in range(len(start_pages) - 1)):
        return _single_tab_fallback(
            f"outline page numbers are not non-decreasing: {start_pages}"
        )

    return {
        "sheets": [
            {"name": name, "start_page": start_pages[i]}
            for i, name in enumerate(sheet_names)
        ],
        "page_count": page_count,
    }


SOFFICE_BIN = os.environ.get("SOFFICE_BIN", "soffice")


class PreviewRenderError(RuntimeError):
    """The workbook could not be rendered. Never fatal to a costing run."""


def soffice_available() -> bool:
    """True when the LibreOffice binary is on PATH."""
    return shutil.which(SOFFICE_BIN) is not None


def convert_to_pdf(src_path: str, out_dir: str, timeout_s: int = 180) -> str:
    """Convert a workbook to PDF with headless LibreOffice.

    Each call gets a private ``-env:UserInstallation`` profile: LibreOffice takes
    an exclusive lock on its profile directory, so concurrent worker replicas
    sharing one would have all but the first silently exit.
    """
    profile = os.path.join(tempfile.gettempdir(), f"lo_profile_{uuid.uuid4().hex}")
    cmd = [
        SOFFICE_BIN,
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--norestore",
        "--convert-to", "pdf:calc_pdf_Export",
        "--outdir", out_dir,
        src_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise PreviewRenderError(
            f"soffice timed out after {timeout_s}s converting {os.path.basename(src_path)}"
        )
    except FileNotFoundError:
        raise PreviewRenderError(
            f"{SOFFICE_BIN} not found — is libreoffice-calc installed in this image?"
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    if proc.returncode != 0:
        raise PreviewRenderError(
            f"soffice exited {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:300]}"
        )

    stem = os.path.splitext(os.path.basename(src_path))[0]
    pdf_path = os.path.join(out_dir, f"{stem}.pdf")
    if not os.path.exists(pdf_path):
        raise PreviewRenderError(f"soffice reported success but {pdf_path} is missing")
    return pdf_path
