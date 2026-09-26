"""
DRPL Annexure Finder Agent

Identifies every annexure / schedule / proforma / declaration in a tender's
PDF documents via Claude Vision and materializes each one as a paired
(ChecklistItem, DocumentWorkspace) row the user can edit and export as DOCX.

v1 design decisions (see plan):
  - User-initiated only (no auto-trigger after analysis).
  - Literal `[Fill: hint]` placeholders in markdown (no DOCX form fields).
  - Merge-by-identifier idempotency. Rows in review_status in_review/approved
    are never overwritten.

Reuses:
  - ai_service.call_ai_with_documents (native Claude Vision document blocks).
  - storage_service.as_local_file (R2 key -> tempfile).
  - docx_generation_service.generate_docx_from_markdown (downstream DOCX export
    happens via workspace_service.finalize_document; we only produce markdown).
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import ExitStack
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _md_to_html(md_text: str) -> str:
    """Convert extracted annexure markdown to HTML for the editor."""
    try:
        import markdown
        import re as _re
        fixed = _re.sub(
            r"\s*(\|[\s:]*[-:]{2,}[\s:]*(?:\|[\s:]*[-:]{2,}[\s:]*)+\|)",
            r"\n\1\n", md_text,
        )
        fixed = _re.sub(r"\|\s+\|(?!\s*[-:])", "|\n|", fixed)
        return markdown.markdown(fixed, extensions=["tables", "nl2br", "fenced_code"])
    except ImportError:
        html = md_text.replace("\n\n", "</p><p>")
        html = html.replace("\n", "<br/>")
        html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
        html = re.sub(r"^### (.+)", r"<h3>\1</h3>", html, flags=re.MULTILINE)
        html = re.sub(r"^## (.+)", r"<h2>\1</h2>", html, flags=re.MULTILINE)
        html = re.sub(r"^# (.+)", r"<h1>\1</h1>", html, flags=re.MULTILINE)
        return f"<p>{html}</p>"


ANNEXURE_DISCOVERY_PROMPT = """You are an expert at parsing Indian tender \
documents. Your ONLY job on this pass is to LOCATE every fillable form the \
bidder must submit — annexures, schedules, appendices, proformas, formats, \
declarations, certificates, undertakings.

Do NOT transcribe the body of any form. A separate pass does that. Emitting
body text here is a mistake — it wastes the budget you need to find every form.

Return a JSON array. For each form emit an object with exactly these keys:

{
  "identifier": "Annexure-1.3",       // exact reference as printed in the doc
  "title": "PRESCRIBE FORMAT FOR WORK EXPERIENCE",  // exact heading as printed
  "page_range": [18, 19],             // [first_page, last_page] 1-indexed, INCLUSIVE
  "filled_by": "Bidder",              // Bidder | Bank | CA | Auditor | Authorized Signatory
  "purpose": "Past similar work experience certification",
  "orientation": "portrait"           // "portrait" | "landscape"
}

## Page range accuracy is the most important thing on this pass

The next pass re-reads ONLY the pages you list here. If `page_range` is wrong or
too narrow, that annexure gets transcribed incomplete and the bidder submits a
truncated form. Therefore:

- `page_range[0]` = the page where the form's heading/identifier appears.
- `page_range[1]` = the page where the form ENDS (its signature/seal block, or
  the last numbered clause). A 4-page bank guarantee bond starting on page 34 is
  `[34, 37]` — NOT `[34, 34]`.
- When a form continues past a page break (numbered clauses running on, a table
  spilling over, "Contd..." markers), extend `page_range[1]` to cover it.
- When genuinely unsure where it ends, err on the side of ONE page too many.

## Orientation guidance

Set `"orientation": "landscape"` when the original is clearly wider than tall —
inspection tables with 7+ columns, or schedules printed rotated 90° on the
source page. Otherwise `"portrait"`.

## Scope rules
- INCLUDE every form whose layout expects bidder input (blanks, dotted rules,
  signature lines, tables with empty columns, tick-box selections).
- EXCLUDE narrative clauses, instruction text, and purely descriptive annexures
  with no blanks.
- If the same form is referenced multiple times under different identifiers,
  return one entry per identifier.

Return ONLY a valid JSON array. No prose before or after. No markdown fences \
around the array itself."""


ANNEXURE_TRANSCRIPTION_PROMPT = """You are a TRANSCRIBER working on an Indian \
tender document. You are NOT an editor, NOT a summariser, and NOT a form \
designer.

The attached pages contain ONE form the bidder must submit. Reproduce it in
markdown so that a person holding the original page and a printout of your
output would find them identical in wording and structure.

The bidder submits your output to a government tender authority. Wording that
differs from the prescribed format is grounds for the bid being rejected. Every
word you change, drop, tidy, or invent is a real risk to a real bid.

## The three failures that ruin this task

**1. Paraphrasing.** Reproduce every clause word-for-word, character-for-
character — same wording, punctuation, capitalisation, abbreviations, numbers,
currency symbols and units. If the source says "Sl. No." do not write "Sr. No.";
if it says "Rs." do not write "INR". Never re-order, "clean up", correct
spelling/grammar, translate, or normalise spacing. If a sentence runs 90 words
with three sub-clauses, your version runs the same 90 words.

**2. Dropping content.** Reproduce EVERY numbered clause (1., 2., 3. …), every
sub-clause, footnote, marginal note, instruction line, and caption — including
lines starting "Note:", "N.B.", "*", "(i)", parentheticals like "(to be filled
by the bidder)", "(On non-judicial stamp paper)", "duly attested", and any
"to be submitted on letterhead" instruction. A 5-clause form must come back with
5 clauses. These are the most frequently dropped items — do not drop them.

**3. Inventing content.** Emit ONLY what is printed on the page. Do NOT add a
signature block, date line, seal line, "Name:", "Designation:", or any other
field the source does not show. Do NOT add headings, styling, horizontal rules,
CSS, or emojis. Do NOT complete a form the tender left open.

## Blanks — reproduce the mark that is actually printed

This is where transcriptions most often go wrong. Match the ORIGINAL:

| Printed on the page | Emit |
|---|---|
| Dotted leader `...............` | the same run of dots, similar length |
| Underscore rule `__________` | the same run of underscores, similar length |
| Blank space after a label | `____________________` |
| Empty cell in a bordered table | `[Fill: <short hint>]` |
| A tick-box `☐` | `☐` followed by its option text |

NEVER write a descriptive placeholder like `[Name and Address of the Bidder]`,
`[Insert Bank Name]`, or `[Bidder's Address]` where the source shows a dotted or
underscored rule. Those brackets are your words, not the tender's — they are the
single most common defect in this task. The ONLY bracketed marker you may emit is
`[Fill: ...]`, and ONLY inside an empty cell of a bordered data table.

Exception: when the printed page ITSELF contains bracketed instruction text —
e.g. it literally prints `[Insert name of the Bidder]` between dotted rules —
transcribe that text verbatim, because it is part of the prescribed format.
Reproduce what is printed; never substitute your own description for a rule.

## Never fill in values

Blanks stay blank. If the tender's cover page states the contract value, the
tender number, or the bidder's name, do NOT carry those into this form's blanks —
even when you can infer them from the surrounding pages. A blank the bidder must
complete is reproduced as an empty rule. Carrying a value in creates a form that
looks filled but is unverified and legally wrong.

## Layout — reproduce, don't redesign

Choose the markdown that matches the printed layout. Do NOT convert everything
into a table.

- **Inline-blank paragraphs** (`Name of Bidder: ________`): plain paragraph with
  the rule inline. Do NOT convert to a two-column table.
- **Bordered data tables** (column headers + empty rows): markdown pipe table
  with a `|---|---|` separator row; `[Fill: <hint>]` per empty cell. Keep every
  cell on ONE line — the DOCX renderer does not wrap inside cells.
- **Tick-box lists**: `☐ ` + option text, one per line.
- **Clauses / declarations / addressing blocks / "Subject:" lines**: verbatim
  paragraphs and numbered lists. Never wrap these in tables.
- **Signature / seal / date blocks** — ONLY if the source shows one: reproduce
  verbatim as inline-blank paragraphs.

Preserve the source's original numbering (`1.`, `1.1`, `a)`, `(i)`), its bold
and italic emphasis (markdown `**bold**` / `*italic*`), and a single blank line
between distinct blocks so the exported form keeps the original's separation.
Preserve right-aligned or centred header lines (page numbers, `{Annexure-VIA of
GCC}`, `Page 34 of 40`) as their own lines in the position they appear.

## Output format

Emit plain text in EXACTLY this shape — no JSON, no markdown code fences:

IDENTIFIER: <identifier as printed>
TITLE: <title as printed>
ORIENTATION: portrait
CLAUSE_COUNT: <number of top-level numbered clauses you transcribed, 0 if none>
REACHED_END: <yes|no — did you reach this form's actual end?>
NOTES: <any page you could not fully read, else leave blank>
---BEGIN TRANSCRIPTION---
<the verbatim transcription>
---END TRANSCRIPTION---

Everything between the BEGIN and END markers is reproduced exactly as-is, so you
do NOT need to escape quotes, backslashes, or any other character. Write the form
the way it is printed. Legal forms are full of quoted defined terms like
"the Bidder" and "The Railway" — write them as ordinary double quotes.

If the attached pages do not contain the requested form at all, emit exactly:
NOT_FOUND"""


ANNEXURE_EXTRACTION_PROMPT = """You are an expert at parsing Indian tender \
documents. Find EVERY fillable form the bidder must submit — annexures, \
schedules, appendices, proformas, formats, declarations, certificates, \
undertakings — and return a JSON array. For each form emit an object with \
these exact keys:

{
  "identifier": "Annexure-1.3",                 // exact reference from the doc
  "title": "PRESCRIBE FORMAT FOR WORK EXPERIENCE",
  "page_range": [18, 18],                       // [start, end] 1-indexed
  "filled_by": "Bidder",                        // Bidder | Bank | CA | Auditor | Authorized Signatory
  "purpose": "Past similar work experience certification",
  "orientation": "portrait",                    // "portrait" or "landscape" — pick landscape when the original table is too wide to fit on a portrait page (7+ columns or obviously rotated on the source page)
  "markdown_template": "# Annexure-1.3 — ..."   // see rules below
}

## CRITICAL: preserve the ORIGINAL LAYOUT — do not redesign

The goal is a template that looks as close to the printed form as markdown
allows. Different forms use different layouts and you MUST choose the right
markdown representation for each — do NOT convert everything into a table.

### Layout type 1 — Inline-blank paragraph forms
The printed form has labels followed by a blank / dotted / underscored line
on the same row (e.g. `Name of Bidder: ____________________`). Reproduce this
verbatim as a plain paragraph with a long underscore run in place of the blank.

  PRINTED:  Name of Bidder / Manufacturer:  ________________________
  EMIT:     Name of Bidder / Manufacturer: ________________________

DO NOT convert these to a two-column table. DO NOT replace the underscores
with `[Fill: Name]` markers for this layout — keep the underscores so the
printed form's visual character is preserved. Use 20-40 underscores depending
on how long the original blank is.

### Layout type 2 — Actual data tables
The printed form has a bordered grid with column headers and empty rows/cells
intended for bidder entries (e.g. a Work Experience table with Sr.No, Project,
Client, Value columns). Reproduce as a markdown pipe table with a header row
and, for each empty cell, write `[Fill: <hint>]`.

  | Sr.No | Project Name     | Client           | Contract Value |
  |-------|------------------|------------------|----------------|
  | 1     | [Fill: Project]  | [Fill: Client]   | [Fill: Value]  |

### Layout type 3 — Tick-box / check-box lists
The printed form has `☐` boxes next to options. Reproduce each as `☐` followed
by the option text. Do NOT convert to a table unless the source was a table.

  ☐ Class-I Local Supplier (Local content ≥ 50%)
  ☐ Class-II Local Supplier (Local content ≥ 20% but < 50%)
  ☐ Non-Local Supplier (Local content < 20%)

### Layout type 4 — Declarations, clauses, instructions, headings
Paragraphs, numbered lists, bullet points, footnotes, "To be printed on
Company Letterhead" instructions, addressing blocks, "Subject:" lines — keep
verbatim as plain text / numbered lists / blockquotes as appropriate. Never
wrap these in tables.

### Layout type 5 — Signature / date / seal lines at the bottom
Standard form footers like:

  Signature: ____________________
  Name: ____________________
  Date: __________
  (Seal)

Reproduce as inline-blank paragraphs (type 1 style). Use `____________________`
(around 20 underscores) for each blank — not `[Fill: Signature]`.

## Universal rules

- TRANSCRIBE word-for-word, character-for-character. Reproduce EVERY static
  label, column header, paragraph, clause, footnote, note, and caption EXACTLY
  as printed — same wording, punctuation, capitalisation, abbreviations,
  numbers, currency symbols and units. Do NOT paraphrase, summarise, translate,
  re-order, "clean up", correct spelling/grammar, or normalise spacing. If the
  source says "Sl. No." do not write "Sr. No."; if it says "Rs." do not write
  "INR". You are a transcriber, not an editor.
- Reproduce EVERY footnote, instruction line and marginal note exactly — lines
  beginning with "Note:", "N.B.", "*", "**", "(i)", parenthetical instructions
  like "(to be filled by the bidder)", and any "to be submitted on letterhead"
  / "duly attested" caption. These are frequently dropped — do not drop them.
- Preserve the blank-line spacing between distinct blocks (a heading, a
  paragraph group, a table, a signature block) with a single empty line so the
  exported form keeps the original's visual separation. Do not collapse
  everything into one dense block.
- ALWAYS include the closing signature / seal / date block when the source form
  shows one (e.g. `Signature: ____  Name: ____  Designation: ____  Date: ____
  (Seal)`), reproduced verbatim as inline-blank paragraphs, so it round-trips
  into the editor and the exported PDF/DOCX.
- Preserve the original section numbering ("1.", "1.1", "a)", "(i)", etc.).
- Preserve bold / italic emphasis from the source using markdown `**bold**`
  and `*italic*`.
- Tables use pipe syntax with a header separator row (`|---|---|`). Keep every
  table cell on a single line (the DOCX renderer does not wrap inside cells —
  use multiple rows instead of line breaks inside a cell).
- Multi-page forms: merge into ONE markdown document in page order.
- Do NOT wrap the markdown in code fences.
- Do NOT add your own styling, CSS, horizontal-rule dividers, or decorative
  emojis that weren't in the source.

## Orientation guidance

Set `"orientation": "landscape"` when the original annexure is clearly wider
than tall — for example, inspection tables with 7+ columns, or schedules that
are printed rotated 90° on the source page. Otherwise use `"portrait"`.

## Scope rules
- INCLUDE every form whose layout expects bidder input (blanks, signature
  lines, tables with empty columns, tick-box selections).
- EXCLUDE narrative clauses, instruction text, and purely descriptive
  annexures with no blanks.
- If the same form is referenced multiple times under different identifiers,
  return one entry per identifier.

Return ONLY a valid JSON array. No prose before or after. No markdown fences \
around the array itself."""


def _load_tender_pdfs(db: Session, tender_id: int) -> list[str]:
    """Return StorageService keys for every PDF TenderDocument on the tender."""
    from app.models.tender import TenderDocument

    docs = (
        db.query(TenderDocument)
        .filter(
            TenderDocument.tender_id == tender_id,
            TenderDocument.extraction_status != "failed",
        )
        .all()
    )
    return [
        d.file_path
        for d in docs
        if d.file_path and d.file_path.lower().endswith(".pdf")
    ]


def _parse_annexures(response_text: str) -> list[dict]:
    """Tolerant JSON parser.

    Strips ```json fences and leading/trailing prose before extracting the
    first JSON array from the text. Raises ValueError if no array is present.
    """
    t = (response_text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.DOTALL).strip()
    m = re.search(r"\[[\s\S]*\]", t)
    if not m:
        raise ValueError(f"No JSON array in response: {(response_text or '')[:400]}")
    return json.loads(m.group(0))


def _upsert_annexure(
    db: Session,
    tender_id: int,
    annexure: dict,
    format_template_id: Optional[int],
) -> dict:
    """Create or merge-update a (ChecklistItem, DocumentWorkspace) pair.

    Idempotency key: ChecklistItem.source_section == f"annexure_finder:{identifier}".

    Returns a per-annexure summary dict containing:
      identifier, status, checklist_item_id?, workspace_id?, lock_reason?
    where status is one of: created, updated, repaired, skipped_incomplete,
    skipped_locked.
    """
    from app.models.checklist import ChecklistItem
    from app.models.workspace import DocumentWorkspace

    identifier = (annexure.get("identifier") or "").strip()
    if annexure.get("_reuse_existing"):
        # Discovered from a PDF whose bytes have not changed, and its form
        # already has a body: nothing to rewrite.
        existing = (
            db.query(ChecklistItem)
            .filter(
                ChecklistItem.tender_id == tender_id,
                ChecklistItem.source_section == f"annexure_finder:{identifier}",
            )
            .first()
        )
        ws = (
            db.query(DocumentWorkspace)
            .filter(DocumentWorkspace.checklist_item_id == existing.id)
            .first()
        ) if existing is not None else None
        return {
            "identifier": identifier,
            "status": "skipped_unchanged",
            "checklist_item_id": existing.id if existing else None,
            "workspace_id": ws.id if ws else None,
        }
    title = (annexure.get("title") or "").strip()
    markdown = annexure.get("markdown_template")
    orientation = (annexure.get("orientation") or "portrait").strip().lower()
    if orientation not in ("portrait", "landscape"):
        orientation = "portrait"
    if not identifier or not title or not markdown:
        return {"identifier": identifier or "?", "status": "skipped_incomplete"}

    source_key = f"annexure_finder:{identifier}"
    item_name = f"{identifier} — {title}"

    page_range = annexure.get("page_range") or []
    if len(page_range) == 2 and page_range[0] != page_range[1]:
        pages_str = f"pages {page_range[0]}-{page_range[1]}"
    elif page_range:
        pages_str = f"page {page_range[0]}"
    else:
        pages_str = "unknown page"
    description = (
        f"Fillable form extracted from tender PDF ({pages_str}). "
        f"Filled by: {annexure.get('filled_by', 'Bidder')}. "
        f"Purpose: {annexure.get('purpose', '') or '—'}"
    )

    existing = (
        db.query(ChecklistItem)
        .filter(
            ChecklistItem.tender_id == tender_id,
            ChecklistItem.source_section == source_key,
        )
        .first()
    )

    # Secondary: adopt items created by other agents (e.g. checklist_generator)
    # that share the same identifier, rather than creating a duplicate entry.
    if existing is None:
        existing = (
            db.query(ChecklistItem)
            .filter(
                ChecklistItem.tender_id == tender_id,
                ChecklistItem.item_name.ilike(f"{identifier} —%"),
                ChecklistItem.source_section != source_key,
            )
            .first()
        )
        if existing is not None:
            existing.source_section = source_key
            existing.agent_key = "annexure_finder"

    if existing is None:
        item = ChecklistItem(
            tender_id=tender_id,
            item_name=item_name[:500],
            item_description=description,
            is_required=True,
            item_category="generated",
            source_section=source_key,
            agent_key="annexure_finder",
            workspace_status="drafting",
        )
        db.add(item)
        db.flush()
        ws = DocumentWorkspace(
            checklist_item_id=item.id,
            tender_id=tender_id,
            agent_key="annexure_finder",
            format_template_id=format_template_id,
            draft_content_markdown=markdown,
            draft_content_html=_md_to_html(markdown),
            review_status="drafting",
            conversation_session_id=str(uuid.uuid4()),
            content_version=1,
            page_orientation=orientation,
        )
        db.add(ws)
        db.flush()
        return {
            "identifier": identifier,
            "status": "created",
            "checklist_item_id": item.id,
            "workspace_id": ws.id,
        }

    ws = (
        db.query(DocumentWorkspace)
        .filter(DocumentWorkspace.checklist_item_id == existing.id)
        .first()
    )
    if ws is None:
        # Race / prior-failure recovery: checklist row exists without its workspace.
        ws = DocumentWorkspace(
            checklist_item_id=existing.id,
            tender_id=tender_id,
            agent_key="annexure_finder",
            format_template_id=format_template_id,
            draft_content_markdown=markdown,
            draft_content_html=_md_to_html(markdown),
            review_status="drafting",
            conversation_session_id=str(uuid.uuid4()),
            content_version=1,
            page_orientation=orientation,
        )
        db.add(ws)
        db.flush()
        return {
            "identifier": identifier,
            "status": "repaired",
            "checklist_item_id": existing.id,
            "workspace_id": ws.id,
        }

    if ws.review_status in ("in_review", "approved"):
        return {
            "identifier": identifier,
            "status": "skipped_locked",
            "checklist_item_id": existing.id,
            "workspace_id": ws.id,
            "lock_reason": ws.review_status,
        }

    existing.item_name = item_name[:500]
    existing.item_description = description
    existing.workspace_status = "drafting"
    ws.draft_content_markdown = markdown
    ws.draft_content_html = _md_to_html(markdown)
    ws.review_status = "drafting"
    ws.content_version = (ws.content_version or 1) + 1
    ws.page_orientation = orientation
    return {
        "identifier": identifier,
        "status": "updated",
        "checklist_item_id": existing.id,
        "workspace_id": ws.id,
    }


_MAX_PAGES_PER_BATCH = 30  # pages per LLM call for large PDFs


def _get_effective_system_prompt(db) -> str:
    """Return the system prompt for annexure_finder, resolving through the
    canonical_registry so user customizations from Agent Builder UI take
    effect when (a) `is_user_customized=True` AND (b) validation passes.

    Falls back to ANNEXURE_EXTRACTION_PROMPT (the canonical code constant)
    when the agent is not user-customized OR the user prompt fails
    validation. This pre-existed Phase 3d as a direct CustomAgent read; now
    it gets the same fail-safe + is_user_customized respect as every other
    agent.
    """
    if db is None:
        return ANNEXURE_EXTRACTION_PROMPT
    try:
        from app.services.langchain.canonical_registry import resolve_system_prompt
        prompt, source = resolve_system_prompt(
            db, "annexure_finder",
            canonical_builder=lambda: ANNEXURE_EXTRACTION_PROMPT,
        )
        logger.debug(f"[annexure_finder] system prompt source={source}")
        return prompt or ANNEXURE_EXTRACTION_PROMPT
    except Exception as e:
        logger.debug(f"[annexure_finder] resolver failed (non-fatal): {e}")
        try:
            db.rollback()
        except Exception:
            pass
    return ANNEXURE_EXTRACTION_PROMPT


def _get_effective_discovery_prompt(db) -> str:
    """System prompt for the pass-1 locator.

    Resolves through canonical_registry under `annexure_finder` so an existing
    Agent Builder customization still applies to discovery. A customization
    written against the OLD combined prompt keeps working: it will emit bodies
    that pass 2 then overwrites with verbatim ones.
    """
    if db is None:
        return ANNEXURE_DISCOVERY_PROMPT
    try:
        from app.services.langchain.canonical_registry import resolve_system_prompt
        prompt, source = resolve_system_prompt(
            db, "annexure_finder",
            canonical_builder=lambda: ANNEXURE_DISCOVERY_PROMPT,
        )
        logger.debug(f"[annexure_finder] discovery prompt source={source}")
        return prompt or ANNEXURE_DISCOVERY_PROMPT
    except Exception as e:
        logger.debug(f"[annexure_finder] discovery resolver failed (non-fatal): {e}")
        try:
            db.rollback()
        except Exception:
            pass
    return ANNEXURE_DISCOVERY_PROMPT


async def _extract_from_pdfs(
    local_paths: list[str],
    db,
    tender_id: int,
    stats: Optional[dict] = None,
) -> list[dict]:
    """Extract annexures from local PDF files, batching large files by page range.

    Returns a deduplicated list of annexure dicts. ``stats`` (if provided) is
    populated with drop accounting (``dropped_batches``, ``synthesized_ids``,
    ``deduped``) so the caller can surface a shortfall to the user.
    """
    from app.services.ai_service import call_ai_with_documents

    settings = get_settings()
    two_pass = settings.annexure_two_pass_enabled
    # Pass 1 discovers only (identifier/title/pages) when two-pass is on; the
    # legacy combined prompt is used when it's off so the flag is a true
    # rollback to the previous behaviour.
    system_prompt = (
        _get_effective_discovery_prompt(db) if two_pass
        else _get_effective_system_prompt(db)
    )
    all_annexures: list[dict] = []
    seen_keys: set[tuple] = set()
    # identifier -> source PDF, so pass 2 can re-slice the right file.
    path_by_key: dict = {}

    from app.services.analysis_reuse import (
        annexure_bodies_exist,
        file_sha1,
        find_cached_annexure_discovery,
        reuse_enabled,
        store_annexure_discovery,
    )
    _discovery_model = settings.annexure_finder_model
    _reuse = reuse_enabled(db)

    for path in local_paths:
        path_by_key[path] = path
        try:
            import pdfplumber
            with pdfplumber.open(path) as _pdf:
                total_pages = len(_pdf.pages)
        except Exception as e:
            logger.warning(f"[annexure_finder] pdfplumber count failed for {path}: {e}")
            total_pages = 0

        logger.info(f"[annexure_finder] processing {path} — {total_pages} pages")
        batch: list[dict] = []

        # An unchanged PDF (same bytes, model, prompt) was already discovered:
        # reuse pass 1. When every form it holds already has a body, pass 2
        # is skipped for them as well — the rows would only be rewritten.
        _sha1 = file_sha1(path) if _reuse else None
        _cached = (
            find_cached_annexure_discovery(
                db, tender_id, sha1=_sha1, model=_discovery_model, prompt=system_prompt
            ) if _sha1 else None
        )
        if _cached is not None:
            _idents = [
                (a.get("identifier") or "").strip() for a in _cached
                if (a.get("identifier") or "").strip()
            ]
            _unchanged = bool(_idents) and len(_idents) == len(_cached) and annexure_bodies_exist(
                db, tender_id, _idents
            )
            for ann in _cached:
                if _unchanged:
                    ann["_reuse_existing"] = True
            batch = _cached
            if stats is not None:
                stats["discovery_reused"] = stats.get("discovery_reused", 0) + 1
                if _unchanged:
                    stats["reused_unchanged"] = stats.get("reused_unchanged", 0) + len(_cached)
            logger.info(
                f"[annexure_finder] {path}: unchanged since its last discovery "
                f"(sha1={_sha1[:12]}) — reusing {len(_cached)} discovered form(s)"
                + (", bodies already exist, transcription skipped" if _unchanged else "")
            )
        try:
            if _cached is not None:
                pass  # discovery reused above; no vision call
            elif 0 < total_pages <= _MAX_PAGES_PER_BATCH:
                # Small enough — send as-is
                try:
                    response = await call_ai_with_documents(
                        system_prompt=system_prompt,
                        user_prompt=(
                            f"Extract every fillable form from the attached tender "
                            f"document(s) for tender #{tender_id}. Return only the JSON array."
                        ),
                        document_paths=[path],
                        db=db,
                        agent_name="annexure_finder",
                        # Force Haiku — annexure extraction is a high-volume
                        # per-PDF vision pass; Sonnet is needlessly expensive here.
                        model_override=get_settings().annexure_finder_model,
                        # Raised from 16384 to cut mid-array JSON truncation that
                        # dropped later annexures on dense PDFs (Haiku ceiling 32K).
                        max_tokens_override=24576,
                        thinking_mode_override="disabled",
                        # Extraction needs to be deterministic — same tender, same
                        # annexures across re-runs. Default 0.7 caused the model to
                        # occasionally drop or hallucinate annexure rows.
                        temperature_override=0.1,
                    )
                    batch = _safe_parse(response)
                    logger.info(f"[annexure_finder] single-shot returned {len(batch)} annexures")
                except Exception as e:
                    logger.warning(f"[annexure_finder] single-shot call failed ({total_pages}pp): {e}", exc_info=True)
                    # Fall back to batching even for "small" PDFs when the LLM rejects the input
                    batch = await _extract_in_batches(path, total_pages or _MAX_PAGES_PER_BATCH, db, tender_id, system_prompt, stats)
            else:
                # Large PDF (or unknown size) — process in page-range batches and merge
                batch = await _extract_in_batches(path, total_pages or _MAX_PAGES_PER_BATCH, db, tender_id, system_prompt, stats)
        except Exception as e:
            logger.error(f"[annexure_finder] extraction failed for {path}: {e}", exc_info=True)
            print(f"[DRPL ERROR] annexure extraction failed: {type(e).__name__}: {e}", flush=True)
            batch = []

        if _cached is None and _sha1 and batch:
            store_annexure_discovery(
                db, tender_id, sha1=_sha1,
                model=_discovery_model, prompt=system_prompt, annexures=batch,
            )

        for ann in batch:
            identifier = (ann.get("identifier") or "").strip()
            title = (ann.get("title") or "").strip()
            if not identifier:
                # Don't drop a form the model couldn't label — synthesize a
                # stable identifier from its title (or a running sequence) so it
                # still gets created instead of silently vanishing.
                base = re.sub(r"\W+", "-", title).strip("-")[:40] if title else ""
                identifier = f"{base or 'Form'}-{len(all_annexures) + 1}"
                ann["identifier"] = identifier
                if stats is not None:
                    stats["synthesized_ids"] = stats.get("synthesized_ids", 0) + 1
            # Dedup on (identifier, title) so a genuinely different form that
            # happens to reuse an identifier across PDFs is NOT discarded.
            dedup_key = (identifier.lower(), title.lower())
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                # Remember which PDF this form came from so pass 2 slices the
                # right file when a tender has several documents.
                ann["_source_path"] = path
                all_annexures.append(ann)
            elif stats is not None:
                stats["deduped"] = stats.get("deduped", 0) + 1

    if two_pass and all_annexures:
        _to_transcribe = [a for a in all_annexures if not a.get("_reuse_existing")]
        _kept_as_is = [a for a in all_annexures if a.get("_reuse_existing")]
        if _to_transcribe:
            _to_transcribe = await _transcribe_annexures(
                _to_transcribe, path_by_key, db, tender_id, stats
            )
        all_annexures = _kept_as_is + _to_transcribe
        # Any annexure pass 2 could not transcribe keeps whatever pass 1 gave it
        # (usually nothing, since discovery no longer emits bodies). Drop the ones
        # left with no body rather than creating an empty workspace row — the
        # caller surfaces the shortfall via stats.
        kept: list[dict] = []
        for ann in all_annexures:
            if ann.get("_reuse_existing") or (ann.get("markdown_template") or "").strip():
                kept.append(ann)
            else:
                logger.warning(
                    f"[annexure_finder] dropping {ann.get('identifier')} — no body "
                    f"after transcription (status={ann.get('transcription_status')})"
                )
                if stats is not None:
                    stats["dropped_no_body"] = stats.get("dropped_no_body", 0) + 1
        all_annexures = kept

    return all_annexures


async def _extract_in_batches(
    path: str,
    total_pages: int,
    db,
    tender_id: int,
    system_prompt: str = ANNEXURE_EXTRACTION_PROMPT,
    stats: Optional[dict] = None,
) -> list[dict]:
    """Split a large PDF into _MAX_PAGES_PER_BATCH-page chunks and extract each."""
    from app.services.ai_service import call_ai_with_documents

    try:
        from PyPDF2 import PdfReader, PdfWriter
    except ImportError:
        from pypdf import PdfReader, PdfWriter  # newer name fallback

    try:
        reader = PdfReader(path)
        actual_pages = len(reader.pages)
    except Exception as e:
        logger.error(f"[annexure_finder] PdfReader failed for {path}: {e}", exc_info=True)
        print(f"[DRPL ERROR] PdfReader failed: {type(e).__name__}: {e}", flush=True)
        return []

    # Trust PyPDF2's actual page count — pdfplumber may have failed or lied
    if actual_pages > 0:
        total_pages = actual_pages
    logger.info(f"[annexure_finder] batching {path}: {total_pages} pages, batch size {_MAX_PAGES_PER_BATCH}")

    results: list[dict] = []

    async def _call_with_overload_retry(user_prompt: str, doc_path: str) -> str:
        """Call Anthropic with sustained-overload retries.

        ai_service._call_anthropic already retries transient 5xx 3x with
        1s/2s backoff — but during sustained Anthropic overload windows
        (real ones can last 30-120s) all 3 retries hit Overloaded and the
        exception propagates up. Without this outer retry the batch is
        silently dropped, producing zero annexures for the affected pages.

        Outer retries use longer backoff (30s, 60s) since we know the
        inner fast retries already failed.
        """
        _OVERLOAD_BACKOFFS = (30, 60)  # two extra attempts at 30s / 60s
        last_exc: Exception = Exception("(no attempts)")
        for attempt in range(len(_OVERLOAD_BACKOFFS) + 1):
            try:
                return await call_ai_with_documents(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    document_paths=[doc_path],
                    db=db,
                    agent_name="annexure_finder",
                    # Force Haiku for the batched path too (see config note).
                    model_override=get_settings().annexure_finder_model,
                    # Raised from 16384 to reduce mid-array JSON truncation.
                    max_tokens_override=24576,
                    thinking_mode_override="disabled",
                    temperature_override=0.1,
                )
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                is_overload = (
                    "overloaded" in msg
                    or "529" in msg
                    or "rate_limit" in msg
                    or "rate limit" in msg
                )
                if not is_overload or attempt >= len(_OVERLOAD_BACKOFFS):
                    raise
                wait_s = _OVERLOAD_BACKOFFS[attempt]
                logger.warning(
                    f"[annexure_finder] Anthropic overloaded — outer retry "
                    f"{attempt + 1}/{len(_OVERLOAD_BACKOFFS)} in {wait_s}s "
                    f"(err: {str(exc)[:120]})"
                )
                await asyncio.sleep(wait_s)
        raise last_exc

    async def _run_batch(batch_start: int, batch_end: int) -> list[dict]:
        """Extract one page-range batch. Raises on a hard failure (so the caller
        can record it for a later retry); returns [] only for genuinely-empty
        ranges after the schema-nudge retry."""
        writer = PdfWriter()
        for i in range(batch_start, batch_end):
            writer.add_page(reader.pages[i])

        fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as fh:
                writer.write(fh)

            response = await _call_with_overload_retry(
                user_prompt=(
                    f"Extract every fillable form from pages {batch_start + 1}–{batch_end} "
                    f"(of {total_pages} total) for tender #{tender_id}. "
                    f"Return only the JSON array."
                ),
                doc_path=tmp_path,
            )
            batch_items = _safe_parse(response)
            # Retry once with a simpler, more explicit prompt when a batch
            # returned nothing. _safe_parse swallowing a JSON parse error used
            # to silently drop all annexures in this page range — accounting
            # for the run-to-run annexure-count variance users see. We only
            # retry on EMPTY results so successful batches don't pay the cost.
            if not batch_items:
                logger.warning(
                    f"[annexure_finder] batch pages {batch_start + 1}-{batch_end} "
                    f"returned 0 annexures — retrying once with explicit schema nudge"
                )
                response_retry = await _call_with_overload_retry(
                    user_prompt=(
                        f"The previous extraction for pages {batch_start + 1}-{batch_end} "
                        f"of tender #{tender_id} did not return parseable JSON. "
                        f"Re-extract every fillable form on these pages. "
                        f"Respond with ONLY a JSON array (no markdown fences, no commentary) "
                        f"of objects: each must have at least `identifier`, `title`, "
                        f"and `page` (the 1-based page number this form starts on). "
                        f"If you find nothing on these pages, return an empty array `[]`."
                    ),
                    doc_path=tmp_path,
                )
                batch_items = _safe_parse(response_retry)
                logger.info(
                    f"[annexure_finder] batch pages {batch_start + 1}-{batch_end} "
                    f"retry recovered {len(batch_items)} annexures"
                )
            return batch_items
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    failed_ranges: list[tuple[int, int]] = []
    for batch_start in range(0, total_pages, _MAX_PAGES_PER_BATCH):
        batch_end = min(batch_start + _MAX_PAGES_PER_BATCH, total_pages)
        try:
            results.extend(await _run_batch(batch_start, batch_end))
            logger.info(
                f"[annexure_finder] batch pages {batch_start + 1}-{batch_end}: "
                f"{len(results)} annexures so far"
            )
        except Exception as e:
            logger.warning(
                f"[annexure_finder] batch pages {batch_start + 1}-{batch_end} failed: {e}"
            )
            failed_ranges.append((batch_start, batch_end))

    # Retry batches that threw (vs. legitimately empty) once more — a single
    # mid-run overload/timeout used to silently drop every annexure on those
    # pages. Count any still-unrecovered batches so the caller can surface the
    # shortfall instead of pretending extraction was complete.
    for batch_start, batch_end in failed_ranges:
        try:
            recovered = await _run_batch(batch_start, batch_end)
            results.extend(recovered)
            logger.info(
                f"[annexure_finder] failed batch {batch_start + 1}-{batch_end} "
                f"recovered {len(recovered)} annexures on retry"
            )
        except Exception as e:
            logger.error(
                f"[annexure_finder] batch pages {batch_start + 1}-{batch_end} "
                f"failed again after retry: {e}"
            )
            if stats is not None:
                stats["dropped_batches"] = stats.get("dropped_batches", 0) + 1

    return results


def _slice_pdf_pages(src_path: str, first_page: int, last_page: int) -> Optional[str]:
    """Write pages [first_page, last_page] (1-indexed, inclusive) of ``src_path``
    to a new temp PDF and return its path. Returns None if the range is unusable.

    Caller owns the returned file and must unlink it.
    """
    try:
        from PyPDF2 import PdfReader, PdfWriter
    except ImportError:
        from pypdf import PdfReader, PdfWriter

    try:
        reader = PdfReader(src_path)
        total = len(reader.pages)
    except Exception as e:
        logger.warning(f"[annexure_finder] slice: PdfReader failed for {src_path}: {e}")
        return None

    start = max(1, int(first_page or 1))
    end = min(total, int(last_page or start))
    if start > total or end < start:
        return None

    writer = PdfWriter()
    for i in range(start - 1, end):
        writer.add_page(reader.pages[i])

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as fh:
            writer.write(fh)
        return tmp_path
    except Exception as e:
        logger.warning(f"[annexure_finder] slice: write failed: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return None


_TRANSCRIPTION_BODY_RE = re.compile(
    r"---BEGIN TRANSCRIPTION---\s*\n(.*?)\n?\s*---END TRANSCRIPTION---",
    re.DOTALL,
)


def _parse_transcription(response_text: str) -> dict:
    """Parse the pass-2 response into the same dict shape the caller expects.

    The body is delimiter-fenced rather than JSON-encoded on purpose. Verbatim
    tender text is full of unescaped double quotes ("the Bidder", "The Railway")
    and backslashes, which the model routinely fails to escape inside a JSON
    string — that discarded otherwise-perfect transcriptions. Delimiters remove
    the escaping burden entirely.

    A JSON object is still accepted as a fallback so a prompt customized against
    the older format keeps working.
    """
    t = (response_text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json|text)?\s*|\s*```$", "", t, flags=re.DOTALL).strip()

    if re.match(r"^NOT_FOUND\b", t):
        return {"error": "not_found"}

    body_match = _TRANSCRIPTION_BODY_RE.search(t)
    if body_match:
        header = t[: body_match.start()]

        def _field(name: str) -> str:
            m = re.search(rf"^{name}:[ \t]*(.*)$", header, re.MULTILINE | re.IGNORECASE)
            return (m.group(1).strip() if m else "")

        clause_raw = _field("CLAUSE_COUNT")
        clause_digits = re.search(r"\d+", clause_raw)
        reached = _field("REACHED_END").lower()

        return {
            "identifier": _field("IDENTIFIER"),
            "title": _field("TITLE"),
            "orientation": _field("ORIENTATION").lower() or "portrait",
            "markdown_template": body_match.group(1).strip(),
            "completeness": {
                "clause_count": int(clause_digits.group(0)) if clause_digits else None,
                "ends_with_source_end": reached.startswith("y") if reached else None,
                "notes": _field("NOTES"),
            },
        }

    # Fallback: legacy JSON-object shape.
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Transcription response was neither delimiter-fenced nor valid "
                f"JSON ({e}): {t[:300]}"
            ) from e

    raise ValueError(f"Unparseable transcription response: {t[:300]}")


# Any bracketed phrase that is not the sanctioned `[Fill: ...]` table marker.
_BRACKETED_RE = re.compile(r"\[(?!Fill:)([^\]\n]{1,100})\]")

# Indian tender formats genuinely print instruction markers inside brackets —
# "[Insert name of the Bidder]", "[Name in Block letters]", "[Designation with
# Code No.]", "[to be filled by the bidder]". Transcribing those is CORRECT, so
# they must not be reported as defects. They are recognisable by imperative or
# instructional phrasing.
_SOURCE_INSTRUCTION_RE = re.compile(
    r"^\s*(?:"
    r"insert|enter|specify|state|mention|fill|give|indicate|strike|delete|attach"
    r"|as\s+applicable|to\s+be\b|in\s+block|block\s+letters|name\s+in\b"
    r"|designation\s+with|signature\s+of\s+the\s+(?:bank|bidder)\b"
    r")",
    re.IGNORECASE,
)

# The defect shape: a bare noun-phrase description of the field, written by the
# model in place of the rule that is actually printed — e.g.
# "WHEREAS, [Name and Address of the Bidder] ____" or "[Bidder Name Here]".
_FIELD_NOUN_RE = re.compile(
    r"^\s*(?:the\s+)?(?:name|address|amount|value|date|designation|company|firm"
    r"|bidder|bank|place|tender|contract|signature|seal)\b",
    re.IGNORECASE,
)


def _placeholder_defects(markdown: str) -> list[str]:
    """Return distinct descriptive placeholders the MODEL appears to have written
    in place of a printed blank.

    Bracketed instruction text that the source page itself prints (e.g.
    "[Insert name of the Bidder]", "[Name in Block letters]") is genuine
    transcription and is deliberately NOT reported — flagging it would cause
    pointless retries on correct output.
    """
    if not markdown:
        return []
    seen: list[str] = []
    for m in _BRACKETED_RE.finditer(markdown):
        inner = m.group(1)
        if _SOURCE_INSTRUCTION_RE.search(inner):
            continue          # printed instruction marker — correct to keep
        if not _FIELD_NOUN_RE.search(inner):
            continue          # not a field description (e.g. a citation) — ignore
        frag = m.group(0)
        if frag not in seen:
            seen.append(frag)
        if len(seen) >= 8:
            break
    return seen


async def _transcribe_one_annexure(
    ann: dict,
    src_path: str,
    db,
    tender_id: int,
    system_prompt: str,
) -> dict:
    """Pass 2 — re-read ONLY this annexure's pages and transcribe it verbatim.

    Mutates and returns ``ann`` with ``markdown_template`` (and a refined
    orientation) set. On failure the annexure is returned unchanged so the
    caller can fall back to whatever pass 1 produced.
    """
    from app.services.ai_service import call_ai_with_documents

    settings = get_settings()
    identifier = ann.get("identifier") or "?"
    page_range = ann.get("page_range") or []

    if not page_range:
        logger.warning(f"[annexure_finder] {identifier}: no page_range — cannot transcribe")
        ann["transcription_status"] = "no_page_range"
        return ann

    pad = max(0, settings.annexure_transcription_page_padding)
    first = int(page_range[0]) - pad
    last = int(page_range[1] if len(page_range) > 1 else page_range[0]) + pad

    slice_path = _slice_pdf_pages(src_path, first, last)
    if not slice_path:
        logger.warning(f"[annexure_finder] {identifier}: page slice {first}-{last} failed")
        ann["transcription_status"] = "slice_failed"
        return ann

    user_prompt = (
        f"The attached pages contain the form '{identifier} — {ann.get('title', '')}' "
        f"(printed around pages {page_range[0]}-{page_range[-1]} of the tender). "
        f"Transcribe that form verbatim per your instructions. "
        f"Ignore any other form on these pages — transcribe only this one. "
        f"Use the IDENTIFIER/TITLE/... header followed by the "
        f"---BEGIN TRANSCRIPTION--- / ---END TRANSCRIPTION--- block."
    )

    #: Which model each attempt uses. The second is the escalation -- the
    #: same-model retry is what the nine Liluah reports already disproved.
    first_model = settings.annexure_transcription_model
    retry_model = (settings.annexure_transcription_escalation_model or "").strip() or first_model

    try:
        for attempt in range(2):
            prompt = user_prompt
            if attempt == 1:
                prompt = (
                    user_prompt
                    + "\n\nYour previous attempt was rejected. It either (a) substituted "
                    "descriptive placeholders in square brackets (e.g. [Name of the "
                    "Bidder]) where the printed page shows a dotted or underscored rule, "
                    "(b) omitted printed clauses, or (c) was not in the required output "
                    "format. Redo the transcription: reproduce the actual printed rules "
                    "as dots/underscores, include every numbered clause exactly as "
                    "printed, and emit the IDENTIFIER/TITLE/... header followed by the "
                    "body between ---BEGIN TRANSCRIPTION--- and ---END TRANSCRIPTION--- "
                    "with no JSON encoding and no escaping."
                )
                if retry_model != first_model:
                    logger.info(
                        f"[annexure_finder] {identifier}: escalating retry from "
                        f"{first_model} to {retry_model}"
                    )

            response = await call_ai_with_documents(
                system_prompt=system_prompt,
                user_prompt=prompt,
                document_paths=[slice_path],
                db=db,
                agent_name="annexure_finder",
                model_override=first_model if attempt == 0 else retry_model,
                # Full budget for ONE form over a few pages — this headroom is the
                # point of the two-pass split.
                max_tokens_override=32000,
                thinking_mode_override="disabled",
                # Transcription must be deterministic; any sampling freedom here
                # shows up as reworded clauses.
                temperature_override=0.0,
            )

            try:
                parsed = _parse_transcription(response)
            except ValueError as pe:
                # A malformed response is worth one more shot — the content is
                # usually fine and only the envelope is wrong. Without this, a
                # single bad envelope discards an otherwise-good transcription.
                logger.warning(
                    f"[annexure_finder] {identifier}: unparseable response "
                    f"(attempt {attempt + 1}): {pe}"
                )
                ann["transcription_status"] = "unparseable"
                if attempt == 0:
                    continue
                return ann

            if parsed.get("error") == "not_found":
                logger.warning(
                    f"[annexure_finder] {identifier}: not found on pages {first}-{last}"
                )
                ann["transcription_status"] = "not_found"
                return ann

            markdown = (parsed.get("markdown_template") or "").strip()
            if not markdown:
                logger.warning(f"[annexure_finder] {identifier}: empty transcription")
                ann["transcription_status"] = "empty"
                continue

            defects = _placeholder_defects(markdown)
            if defects and attempt == 0:
                logger.warning(
                    f"[annexure_finder] {identifier}: descriptive placeholders "
                    f"{defects[:4]} — retrying transcription"
                )
                continue

            ann["markdown_template"] = markdown
            orient = (parsed.get("orientation") or ann.get("orientation") or "portrait").lower()
            ann["orientation"] = orient if orient in ("portrait", "landscape") else "portrait"
            completeness = parsed.get("completeness") or {}
            ann["transcription_status"] = "ok" if not defects else "ok_with_placeholders"
            used_model = first_model if attempt == 0 else retry_model
            ann["transcription_meta"] = {
                "clause_count": completeness.get("clause_count"),
                "ends_with_source_end": completeness.get("ends_with_source_end"),
                "notes": completeness.get("notes") or "",
                "placeholder_defects": defects,
                "pages": [first, last],
                "chars": len(markdown),
                # Which model's words these are, and whether the first attempt
                # had to be thrown away to get them. A form that only ever
                # transcribes cleanly on the escalation model is the signal
                # that the cheap tier has stopped being good enough for this
                # work -- reported, not inferred from the bill.
                "model": used_model,
                "attempts": attempt + 1,
                "escalated": attempt > 0 and retry_model != first_model,
            }
            logger.info(
                f"[annexure_finder] {identifier}: transcribed {len(markdown)} chars "
                f"from pages {first}-{last} on {used_model} "
                f"(attempt {attempt + 1}, clauses={completeness.get('clause_count')}, "
                f"defects={len(defects)})"
            )
            return ann

        ann.setdefault("transcription_status", "failed")
        return ann
    except Exception as e:
        logger.warning(
            f"[annexure_finder] {identifier}: transcription failed: {e}", exc_info=True
        )
        ann["transcription_status"] = "error"
        return ann
    finally:
        try:
            os.unlink(slice_path)
        except Exception:
            pass


async def _transcribe_annexures(
    annexures: list[dict],
    path_by_key: dict,
    db,
    tender_id: int,
    stats: Optional[dict] = None,
) -> list[dict]:
    """Run pass 2 over every discovered annexure, bounded-concurrently."""
    settings = get_settings()
    system_prompt = _get_effective_transcription_prompt(db)
    limit = max(1, settings.annexure_transcription_concurrency)
    sem = asyncio.Semaphore(limit)

    escalation = (settings.annexure_transcription_escalation_model or "").strip()
    logger.info(
        f"[annexure_finder] pass 2: transcribing {len(annexures)} annexures "
        f"on {settings.annexure_transcription_model} (concurrency={limit}"
        + (f", rejected retries escalate to {escalation}" if escalation else "")
        + ")"
    )

    async def _guarded(ann: dict) -> dict:
        src = path_by_key.get(ann.get("_source_path"))
        if not src:
            ann["transcription_status"] = "no_source"
            return ann
        async with sem:
            return await _transcribe_one_annexure(ann, src, db, tender_id, system_prompt)

    results = await asyncio.gather(
        *(_guarded(a) for a in annexures), return_exceptions=True
    )

    out: list[dict] = []
    for original, res in zip(annexures, results):
        if isinstance(res, Exception):
            logger.warning(
                f"[annexure_finder] transcription task raised for "
                f"{original.get('identifier')}: {res}"
            )
            original["transcription_status"] = "error"
            out.append(original)
        else:
            out.append(res)

    if stats is not None:
        stats["transcribed"] = sum(
            1 for a in out if a.get("transcription_status", "").startswith("ok")
        )
        stats["transcription_failed"] = sum(
            1 for a in out if not a.get("transcription_status", "").startswith("ok")
        )
        stats["placeholder_defects"] = sum(
            1 for a in out if a.get("transcription_status") == "ok_with_placeholders"
        )
        stats["escalated"] = sum(
            1 for a in out if (a.get("transcription_meta") or {}).get("escalated")
        )

    return out


def _get_effective_transcription_prompt(db) -> str:
    """System prompt for the pass-2 transcriber.

    Resolved through canonical_registry under its own key so an admin can tune
    transcription rules in Agent Builder without touching discovery (and so a
    bad customization of one pass cannot break the other).
    """
    if db is None:
        return ANNEXURE_TRANSCRIPTION_PROMPT
    try:
        from app.services.langchain.canonical_registry import resolve_system_prompt
        prompt, source = resolve_system_prompt(
            db, "annexure_transcriber",
            canonical_builder=lambda: ANNEXURE_TRANSCRIPTION_PROMPT,
        )
        logger.debug(f"[annexure_finder] transcription prompt source={source}")
        return prompt or ANNEXURE_TRANSCRIPTION_PROMPT
    except Exception as e:
        logger.debug(f"[annexure_finder] transcription resolver failed (non-fatal): {e}")
        try:
            db.rollback()
        except Exception:
            pass
    return ANNEXURE_TRANSCRIPTION_PROMPT


def expected_annexure_count(db: Session, tender_id: int) -> Optional[int]:
    """Best-effort count of distinct annexures the tender ANALYSIS identified.

    Reads the latest analysis report markdown (``TenderAnalysisSummary.
    requirement_summary``) and counts distinct annexure/schedule/appendix/
    proforma references. Used purely to RECONCILE against what extraction
    created and surface a shortfall — it is a heuristic, hence the ``~`` the
    caller renders. Returns None when there is no analysis to compare against.
    """
    try:
        text = ""
        # Source 1 — the deep_analyzer's persisted summary (UI "Analyse" path).
        from app.models.document_analysis import TenderAnalysisSummary
        row = (
            db.query(TenderAnalysisSummary)
            .filter(TenderAnalysisSummary.tender_id == tender_id)
            .order_by(TenderAnalysisSummary.created_at.desc())
            .first()
        )
        text = (row.requirement_summary or "") if row else ""

        # Source 2 — the Command Center stores its analysis as an artifact, NOT
        # in TenderAnalysisSummary. Fall back to the latest `analysis` artifact
        # across this tender's sessions so reconciliation works in chat flows.
        if not text.strip():
            try:
                from app.models.artifact import CommandCenterArtifact
                from app.models.proposal import ProposalSession
                sess_ids = [
                    s.id for s in db.query(ProposalSession.id)
                    .filter(ProposalSession.tender_id == tender_id).all()
                ]
                if sess_ids:
                    art = (
                        db.query(CommandCenterArtifact)
                        .filter(
                            CommandCenterArtifact.session_id.in_(sess_ids),
                            CommandCenterArtifact.artifact_type == "analysis",
                        )
                        .order_by(CommandCenterArtifact.created_at.desc())
                        .first()
                    )
                    if art and (art.content or "").strip():
                        text = art.content
            except Exception as _art_err:
                logger.debug(f"[annexure_finder] artifact analysis lookup failed: {_art_err}")

        if not text.strip():
            return None
        # Match "Annexure-1", "Annexure 1.3", "Schedule II", "Appendix-A",
        # "Proforma 2", "Form-C". Normalize to dedupe references that recur
        # across the report body and the required-documents table.
        pat = re.compile(
            r"\b(annexure|schedule|appendix|proforma|form)\b[\s\-:]*([0-9]+(?:\.[0-9]+)?|[ivxlcdm]+|[a-z])?",
            re.IGNORECASE,
        )
        seen: set[str] = set()
        for kind, num in pat.findall(text):
            num = (num or "").strip().lower()
            if not num:
                continue  # bare word with no identifier — too vague to count
            seen.add(f"{kind.lower()}-{num}")
        return len(seen) or None
    except Exception as e:
        logger.debug(f"[annexure_finder] expected_annexure_count failed (non-fatal): {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _safe_parse(response: Optional[str]) -> list[dict]:
    """Parse JSON array from LLM response; return empty list on any failure."""
    try:
        return _parse_annexures(response or "")
    except Exception as e:
        logger.warning(f"[annexure_finder] parse warning: {e}")
        return []


async def run_annexure_extraction(
    db: Session,
    tender_id: int,
    pdf_storage_keys: Optional[list[str]] = None,
) -> dict:
    """Main entry point for annexure extraction.

    Args:
        db: SQLAlchemy session (will be committed on success).
        tender_id: Tender row id.
        pdf_storage_keys: Optional list of StorageService keys to analyze.
            When omitted, every PDF TenderDocument on the tender is used.

    Returns a summary dict suitable for both the REST endpoint and the chat
    wrapper to serialize to the user:
      {
        "status": "completed" | "failed",
        "tender_id": int,
        "counts": {"found": n, "created": n, "updated": n, "skipped": n},
        "annexures": [<per-annexure summary>, ...],
        # on failure:
        "error": str, "message"?: str, "raw_excerpt"?: str,
      }
    """
    from app.services.storage_service import get_storage_service
    from app.models.workspace import DocumentFormatTemplate

    # Defensive: a long chat turn does a lot of DB work before annexure
    # extraction. If any earlier statement left this shared session in a failed
    # transaction (PendingRollbackError / InFailedSqlTransaction), our very
    # first query below would raise "A temporary database error occurred".
    # Clearing the slate here makes extraction robust to upstream poisoning.
    try:
        db.rollback()
    except Exception:
        pass

    keys = pdf_storage_keys or _load_tender_pdfs(db, tender_id)
    if not keys:
        return {
            "status": "failed",
            "error": "no_pdfs",
            "message": f"Tender {tender_id} has no PDF documents to analyze.",
        }

    storage = get_storage_service()
    with ExitStack() as stack:
        local_paths: list[str] = []
        for key in keys:
            try:
                local_paths.append(
                    stack.enter_context(storage.as_local_file(key, suffix=".pdf"))
                )
            except Exception as e:
                logger.warning(f"[annexure_finder] skipping {key}: {e}")

        if not local_paths:
            return {"status": "failed", "error": "no_readable_pdfs"}

        stats: dict = {}
        annexures = await _extract_from_pdfs(local_paths, db, tender_id, stats)

    if not isinstance(annexures, list):
        logger.error("[annexure_finder] extraction returned non-list")
        return {"status": "failed", "error": "parse_failed"}

    tmpl = (
        db.query(DocumentFormatTemplate)
        .filter(
            DocumentFormatTemplate.document_category == "annexure",
            DocumentFormatTemplate.is_active == True,  # noqa: E712
            DocumentFormatTemplate.is_system == True,  # noqa: E712
        )
        .first()
    )
    tmpl_id = tmpl.id if tmpl else None

    rows = [_upsert_annexure(db, tender_id, a, tmpl_id) for a in annexures]
    db.commit()

    created = sum(1 for r in rows if r["status"] == "created")
    updated = sum(1 for r in rows if r["status"] == "updated")
    skipped = sum(1 for r in rows if r["status"].startswith("skipped"))
    repaired = sum(1 for r in rows if r["status"] == "repaired")
    skipped_incomplete = sum(1 for r in rows if r["status"] == "skipped_incomplete")

    # Reconcile against what the analysis said the tender requires, so a
    # shortfall is surfaced to the user (no silent under-creation).
    expected = expected_annexure_count(db, tender_id)
    dropped_batches = stats.get("dropped_batches", 0)

    logger.info(
        f"[annexure_finder] tender={tender_id} found={len(annexures)} "
        f"created={created} updated={updated} repaired={repaired} skipped={skipped} "
        f"expected≈{expected} dropped_batches={dropped_batches} "
        f"skipped_incomplete={skipped_incomplete} deduped={stats.get('deduped', 0)} "
        f"transcribed={stats.get('transcribed', 0)} "
        f"transcription_failed={stats.get('transcription_failed', 0)} "
        f"escalated={stats.get('escalated', 0)} "
        f"placeholder_defects={stats.get('placeholder_defects', 0)}"
    )

    return {
        "status": "completed",
        "tender_id": tender_id,
        "counts": {
            "found": len(annexures),
            "created": created,
            "updated": updated,
            "repaired": repaired,
            "skipped": skipped,
            "expected": expected,
            "dropped_batches": dropped_batches,
            "skipped_incomplete": skipped_incomplete,
            "skipped_unchanged": sum(1 for r in rows if r["status"] == "skipped_unchanged"),
            "discovery_reused": stats.get("discovery_reused", 0),
            # The transcription outcome, reported rather than left in the log.
            #
            # `placeholder_defects` is the one that matters: it counts forms
            # accepted with descriptive placeholders still in them after BOTH
            # attempts, which is a submission risk the reader needs to know
            # about while they can still check the page. It used to be
            # computed and dropped on the floor -- the nine Liluah reports
            # each found it by reading the output instead.
            "transcribed": stats.get("transcribed", 0),
            "transcription_failed": stats.get("transcription_failed", 0),
            "escalated": stats.get("escalated", 0),
            "placeholder_defects": stats.get("placeholder_defects", 0),
        },
        "annexures": rows,
    }
