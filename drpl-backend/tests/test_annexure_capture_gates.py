"""An annexure that is a scanned table must still be read.

The Liluah tender, third report. The bidder's annexures turned out to be
material lists -- `MaterialListPaintAnnexure-VII.pdf` (one scanned page) and
`MaterialListICFtoNMGHSR_1.pdf` (13 pages, 7,076 characters in total: tables
as images under printed headings). The platform costed the NIT schedules and
nothing from either annexure, and its logs did not say why.

Two gates, both silent, both pinned here:

  1. The vision cascade sends a page to vision only when its text layer is
     under `pdf_vision_density_min_chars` (40). A scanned table under a
     printed heading -- tender number, "Annexure-VII", "Material List" --
     clears 40 characters with the heading alone, so the page is tagged
     "text", never sent to vision, and the extractor is handed a title.

  2. An extraction that comes back empty is retried only when the text
     carries the IREPS markers `Description:-` / `Item Code`. A material
     list has neither, so its empty result was final -- and not logged.
"""

import logging

import pytest

from app.services import boq_parser_service as bps

HEADER_ONLY = (
    "EASTERN RAILWAY  Liluah Workshop\n"
    "Tender No. ER/LLH/VanBrake/2026\n"
    "ANNEXURE - VII\n"
    "MATERIAL LIST FOR PAINTING\n"
    "Page 3 of 13\n"
)

TABLE_TEXT = (
    "Sr No | Description of Material | Specification | Qty | Unit\n"
    "1 | Red oxide zinc chromate primer | IS 2074 | 120 | Litre\n"
    "2 | Synthetic enamel paint, signal red | IS 2932 | 80 | Litre\n"
    "3 | Aluminium paint | IS 2339 | 40 | Litre\n"
    "4 | Thinner | RDSO M&C-PCN | 60 | Litre\n"
)

IREPS_TEXT = (
    "Schedule () A-Engine removal and refitment\n"
    "Item Code: A1  Description:- Removal of engine  Item Qty: 4  Unit Rate: 12500.00\n"
)

PROSE = (
    "All the bidders should ensure they are registered vendors of Eastern "
    "Railway and submit the undertaking in the prescribed format.\n"
)


# -- gate 1: a scanned table under a printed heading -------------------------


def test_a_header_only_page_looks_like_an_image_table():
    assert bps._page_looks_like_image_table(HEADER_ONLY)


def test_a_page_with_rows_does_not():
    assert not bps._page_looks_like_image_table(TABLE_TEXT)


def test_a_long_text_page_does_not():
    assert not bps._page_looks_like_image_table(HEADER_ONLY + PROSE * 30)


def test_an_empty_page_is_left_to_the_cascade():
    """The cascade already sends genuinely empty pages to vision."""
    assert not bps._page_looks_like_image_table("")
    assert not bps._page_looks_like_image_table(None)


@pytest.mark.asyncio
async def test_sparse_text_pages_are_given_to_vision(monkeypatch):
    seen: list[int] = []

    async def fake_vision(_path, page_num):
        seen.append(page_num)
        return TABLE_TEXT

    monkeypatch.setattr(bps, "_force_page_vision_text", fake_vision)
    pages = [
        {"page_num": 1, "text": HEADER_ONLY, "method": "text"},
        {"page_num": 2, "text": TABLE_TEXT * 4, "method": "text"},
        {"page_num": 3, "text": HEADER_ONLY, "method": "text"},
        {"page_num": 4, "text": TABLE_TEXT, "method": "vision"},  # already vision
    ]

    out = await bps._recover_image_table_pages("anx.pdf", pages)

    assert seen == [1, 3], "only the sparse text-layer pages should go to vision"
    assert out[0]["text"] == TABLE_TEXT.strip() and out[0]["method"] == "vision"
    assert out[2]["text"] == TABLE_TEXT.strip() and out[2]["method"] == "vision"
    assert out[1]["text"] == TABLE_TEXT * 4 and out[1]["method"] == "text"


@pytest.mark.asyncio
async def test_a_heading_is_never_traded_for_less(monkeypatch):
    """Vision that returns nothing, or less than the text layer had, must not
    overwrite the page."""
    async def worse(_path, _n):
        return "  "

    monkeypatch.setattr(bps, "_force_page_vision_text", worse)
    pages = [{"page_num": 1, "text": HEADER_ONLY, "method": "text"}]

    out = await bps._recover_image_table_pages("anx.pdf", pages)

    assert out[0]["text"] == HEADER_ONLY and out[0]["method"] == "text"


@pytest.mark.asyncio
async def test_vision_recovery_is_capped_per_document(monkeypatch):
    calls: list[int] = []

    async def fake_vision(_path, page_num):
        calls.append(page_num)
        return TABLE_TEXT

    monkeypatch.setattr(bps, "_force_page_vision_text", fake_vision)
    monkeypatch.setattr(bps, "_VISION_RECOVERY_MAX_PAGES", 5)
    pages = [{"page_num": n, "text": HEADER_ONLY, "method": "text"} for n in range(1, 21)]

    await bps._recover_image_table_pages("anx.pdf", pages)

    assert len(calls) == 5


@pytest.mark.asyncio
async def test_load_pdf_pages_recovers_the_reported_annexure(monkeypatch):
    """End to end through `_load_pdf_pages`: the cascade hands back thirteen
    header-only 'text' pages; the extractor must receive tables."""
    def fake_cascade(_path):
        return {"pages": [
            {"page_num": n, "text": HEADER_ONLY, "method": "text"} for n in range(1, 14)
        ], "total_pages": 13}

    async def fake_vision(_path, _n):
        return TABLE_TEXT

    monkeypatch.setattr(
        "app.services.advanced_document_parser.extract_text_from_pdf_advanced", fake_cascade
    )
    monkeypatch.setattr(bps, "_force_page_vision_text", fake_vision)

    pages, carry_in, scheds = await bps._load_pdf_pages(None, "MaterialListICFtoNMGHSR_1.pdf")

    assert len(pages) == 13
    assert all(p["method"] == "vision" and "Litre" in p["text"] for p in pages)
    assert carry_in == [None] * 13, "an annexure has no schedule banner to carry"


@pytest.mark.asyncio
async def test_a_vision_failure_does_not_lose_the_document(monkeypatch):
    def fake_cascade(_path):
        return {"pages": [{"page_num": 1, "text": HEADER_ONLY, "method": "text"}]}

    async def boom(_path, _n):
        raise RuntimeError("vision down")

    monkeypatch.setattr(
        "app.services.advanced_document_parser.extract_text_from_pdf_advanced", fake_cascade
    )
    monkeypatch.setattr(bps, "_force_page_vision_text", boom)

    pages, _, _ = await bps._load_pdf_pages(None, "anx.pdf")

    assert len(pages) == 1 and pages[0]["text"] == HEADER_ONLY


# -- gate 2: an empty extraction on a non-IREPS table ------------------------


def test_ireps_markers_are_still_sufficient():
    assert bps._text_looks_like_schedule(IREPS_TEXT)


def test_a_material_list_header_looks_like_a_schedule():
    assert bps._text_looks_like_schedule(TABLE_TEXT)


def test_a_page_of_prose_does_not():
    assert not bps._text_looks_like_schedule(PROSE)
    assert not bps._text_looks_like_schedule("")


@pytest.mark.asyncio
async def test_an_empty_result_on_a_material_list_is_retried(monkeypatch):
    """First call empty (a too-dense chunk, an overflow, a hiccup); the
    retry succeeds. Before the widened gate the first empty result was
    returned as final for any document without IREPS markers."""
    calls = []

    async def flaky(_db, chunk_pages, _carry):
        calls.append(len(chunk_pages))
        if len(calls) == 1:
            return []
        return [{"sr_no": 1, "description": "Red oxide primer", "quantity": 120,
                 "unit": "Litre", "schedule_name": None}]

    monkeypatch.setattr(bps, "_call_boq_chunk", flaky)
    chunk = [{"page_num": 1, "text": TABLE_TEXT}]

    rows = await bps._extract_chunk_with_retry(None, chunk, None, max_retries=2)

    assert len(calls) == 2, "the empty first result must be retried"
    assert rows and rows[0]["description"] == "Red oxide primer"


@pytest.mark.asyncio
async def test_a_final_zero_is_always_logged(monkeypatch, caplog):
    """Prose pages legitimately yield nothing; a table page yielding nothing
    is the bug. Either way the log must say so -- it used to say nothing for
    any page without IREPS markers."""
    async def nothing(_db, _pages, _carry):
        return []

    monkeypatch.setattr(bps, "_call_boq_chunk", nothing)
    chunk = [{"page_num": 7, "text": PROSE}]

    with caplog.at_level(logging.WARNING, logger=bps.logger.name):
        rows = await bps._extract_chunk_with_retry(None, chunk, None, max_retries=2)

    assert rows == []
    assert any("yielded 0 rows" in r.message and "looked_like_schedule=False" in r.message
               for r in caplog.records)


# -- the prompt knows an annexure table is a line-item source ----------------


def test_the_prompt_treats_an_annexure_table_as_line_items():
    p = bps._BOQ_AI_SYSTEM_PROMPT
    assert "ANNEXURE" in p and "material list" in p
    assert "leave `schedule_name` null" in p
    # The prose exclusions must survive.
    assert "DO NOT extract prose" in p and "eligibility criteria" in p
    assert "both an Item Qty and a Unit Rate" not in p


# -- every document reports what it contributed -----------------------------


def test_each_document_reports_its_row_count():
    """A document that contributes nothing must be visible in the run's own
    log, not inferred from the total."""
    import inspect

    src = inspect.getsource(bps.parse_boq_from_tender)
    assert "-> 0 rows" in src and "contributes nothing to the schedule" in src
    assert "row(s)\"" in src or "row(s)'" in src or "row(s)" in src
