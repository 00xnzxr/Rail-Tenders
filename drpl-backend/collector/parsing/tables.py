"""
DRPL Collector - find a table by what its header SAYS.

The direct replacement for ``selector_configs/ireps.json``. A CSS path like
``td.tender-no`` is a positional bet on markup you do not own, and the old
config lost that bet: the file still says version 1.0.0, lastUpdated
2026-03-14, and every one of its selectors is a guess at class names IREPS
never promised to keep.

Header text is the one thing a results table cannot rename without also
changing what it means to a human reading it. So: locate the table by the
words in its header row, map column names to indices at runtime, then read by
index. When IREPS reorders its columns tomorrow, this still works. When it
renames "Closing Date" to "Bid Due", ``find_results_table`` raises ParserDrift
loudly instead of returning zero rows quietly.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from collector.portals.base import ParserDrift

#: How a header cell is matched to a logical field. First match wins, so order
#: within a list matters: the more specific pattern goes first.
HEADER_PATTERNS: dict[str, list[re.Pattern]] = {
    "tenderId": [
        re.compile(r"tender\s*(no|number|id)\b", re.I),
        re.compile(r"\b(nit|case)\s*no\b", re.I),
        re.compile(r"tender\s*ref", re.I),
    ],
    "title": [
        re.compile(r"tender\s*title", re.I),
        re.compile(r"name\s*of\s*(work|tender)", re.I),
        re.compile(r"\b(title|description|subject|scope)\b", re.I),
    ],
    "department": [re.compile(r"\b(dept|deptt|department|division|branch)\b", re.I)],
    "organisation": [re.compile(r"^\s*(zone|railway|rly|organi[sz]ation)\s*$", re.I),
                     re.compile(r"zonal", re.I)],
    "estimatedValue": [
        re.compile(r"tender\s*value", re.I),
        re.compile(r"\b(value|amount|cost|estimate)\b", re.I),
    ],
    "openingDate": [
        re.compile(r"open(ing)?\s*date", re.I),
        re.compile(r"(publish|start)\s*date", re.I),
    ],
    "closingDate": [
        re.compile(r"clos(e|ing)\s*date", re.I),
        re.compile(r"\b(due|last)\s*date", re.I),
        re.compile(r"\b(deadline|submission\s*end)\b", re.I),
    ],
    "emdAmount": [re.compile(r"\b(emd|earnest\s*money|bid\s*security)\b", re.I)],
    "status": [re.compile(r"\b(status|stage)\b", re.I)],
}

#: A table is a results table only if it can name a tender AND say when it
#: closes. Anything less is a layout table, a filter panel or a legend.
REQUIRED = ("tenderId", "closingDate")


def _cell_text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True) or "").strip()


def map_header(cells: Iterable[str]) -> dict[str, int]:
    """Logical field -> column index, from the header row's text."""
    mapping: dict[str, int] = {}
    for idx, text in enumerate(cells):
        if not text:
            continue
        for field, patterns in HEADER_PATTERNS.items():
            if field in mapping:
                continue
            if any(p.search(text) for p in patterns):
                mapping[field] = idx
                break
    return mapping


def _owns(table, node) -> bool:
    """Does ``node`` belong to ``table`` itself, rather than a table inside it?

    IREPS wraps its whole page in a layout <table>, and BeautifulSoup's find /
    find_all descend through nested tables. Without this check the outer layout
    table appears to have the results table's header, matches first in document
    order, and every row read out of it is garbage -- the same trap the
    extension's isValidTenderRow had to guard against ("outer layout <tr>s that
    wrap the entire content match every T-icon of every real tender row").
    """
    return node.find_parent("table") is table


def _header_cells(table) -> list[str]:
    """The header row's text, from <thead> when there is one, else row 1.

    Only cells this table owns -- see ``_owns``.
    """
    head = table.find("thead")
    if head is not None and _owns(table, head):
        cells = [c for c in head.find_all(["th", "td"]) if _owns(table, c)]
        if cells:
            return [_cell_text(c) for c in cells]
    for row in table.find_all("tr"):
        if not _owns(table, row):
            continue
        return [_cell_text(c) for c in row.find_all(["th", "td"]) if _owns(table, c)]
    return []


def find_results_table(html: str, portal: str = "ireps"):
    """Locate the results table by what its header says, then map its columns.

    Returns ``(table, mapping)``. Raises ParserDrift when no table in the
    document can both identify a tender and state a closing date -- which is
    the loud failure that a selector file gave you as a silent zero.
    """
    soup = BeautifulSoup(html or "", "lxml")
    best = None
    best_score = 0

    for table in soup.find_all("table"):
        cells = _header_cells(table)
        if len(cells) < 3:
            continue
        mapping = map_header(cells)
        if not all(k in mapping for k in REQUIRED):
            continue
        # Prefer the table that resolves the most fields -- on a page with a
        # summary table above the real one, the real one always wins.
        score = len(mapping)
        if score > best_score:
            best, best_score = (table, mapping), score

    if best is None:
        raise ParserDrift(
            f"{portal}: no table whose header names both a tender number and a "
            f"closing date"
        )
    return best


def data_rows(table) -> list:
    """Body rows this table owns, skipping the header when there is no <tbody>."""
    own_rows = [r for r in table.find_all("tr") if _owns(table, r)]
    body = table.find("tbody")
    if body is not None and _owns(table, body):
        rows = [r for r in own_rows if r.find_parent("tbody") is body]
        if rows:
            return rows
    return own_rows[1:]


def row_values(row, mapping: dict[str, int]) -> dict[str, str]:
    """Read one row by index, with the title's ``title=`` attribute preferred.

    IREPS clips long titles in the visible cell text but keeps the full value
    in a ``title=`` attribute -- reading the text alone gives you a name that
    ends in an ellipsis, which drpl-backend's ``is_corrupt_title`` then
    rejects and replaces with a placeholder.
    """
    cells = row.find_all("td")
    if not cells:
        return {}
    out: dict[str, str] = {}
    for field, idx in mapping.items():
        if idx >= len(cells):
            continue
        cell = cells[idx]
        value = ""
        if field == "title":
            anchor = cell.find(attrs={"title": True})
            value = (cell.get("title") or (anchor.get("title") if anchor else "") or "").strip()
        out[field] = value or _cell_text(cell)
    return out


def row_links(row, base_url: str = "") -> list[str]:
    """Every document-ish href in the row, absolutised against ``base_url``."""
    from urllib.parse import urljoin

    out: list[str] = []
    seen = set()
    for a in row.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("javascript:"):
            continue
        looks_like_doc = bool(
            re.search(r"\.(pdf|docx?|xlsx?|zip|rar)(\?|$)", href, re.I)
            or re.search(r"download|attach|getfile|viewdoc|displaydoc|nit", href, re.I)
        )
        if not looks_like_doc:
            continue
        full = urljoin(base_url, href) if base_url else href
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


def find_text(html: str, pattern: str) -> Optional[str]:
    """First regex capture across the document's visible text. Utility."""
    soup = BeautifulSoup(html or "", "lxml")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    m = re.search(pattern, text, re.I)
    return m.group(1).strip() if m and m.groups() else None
