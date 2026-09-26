"""
DRPL LangChain Agent - Enhanced Costing Research
A LangGraph StateGraph that replaces the single-function costing agent with a
multi-node pipeline supporting:
  - Training data injection from assigned datasets
  - Interactive clarification (chat mode only)
  - Anonymization of company/tender identifiers before every web search
  - Stable delegation interface for the future travel research sub-agent

Entry point: run_enhanced_costing_research()
Called by:
  - costing_agent.run_costing_research() (pipeline adapter shim)
  - chat_agent_wrappers.chat_costing_research() (chat mode)
"""

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Annotated, Optional

from app.core.run_context import current_run_id, run_id_scope

# Strips any pseudo-tool-call XML that the ReAct agent may echo in its final prose.
_TOOL_TAG_RE = re.compile(
    r"<(memory_retrieve|memory_store|costing_training_retrieval|"
    r"anonymizing_web_search|delegate_travel_research|web_search)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_tool_tags(text: str) -> str:
    if not text:
        return text
    return _TOOL_TAG_RE.sub("", text).strip()


def _extract_tender_pdf_text(
    db: "Session",
    tender_id: int,
    max_docs: int = 3,
    max_chars: int = 30000,
) -> str:
    """Per-tender PDF text extraction with OCR/vision fallback for scanned pages.

    Uses ``advanced_document_parser.extract_text_from_pdf_advanced`` which
    cascades:
       1. pdfplumber for text-searchable pages,
       2. Claude vision (cached per-page sha1) for sparse pages,
       3. Tesseract OCR as a final fallback.

    This means scanned-image tender PDFs (no embedded text layer) still
    produce usable scope text instead of an empty string. The costing
    agent's prompt then has reliable text to derive line items from, even
    when its native-PDF document blocks alone aren't enough (very long
    scans, low-DPI scans, faded photocopies).

    Up to ``max_docs`` PDFs are merged and the result is truncated to
    ``max_chars`` (~7,500 tokens) to keep the prompt size predictable.
    Returns "" on any failure — caller falls back to the native PDF
    blocks alone.
    """
    try:
        from app.models.tender import TenderDocument
        from app.services.advanced_document_parser import (
            extract_text_from_pdf_advanced,
        )
        from app.services.storage_service import get_storage_service
    except Exception as e:
        logger.warning(f"[costing] PDF text fallback imports unavailable: {e}")
        return ""

    try:
        docs = (
            db.query(TenderDocument)
            .filter(
                TenderDocument.tender_id == tender_id,
                TenderDocument.mime_type == "application/pdf",
            )
            .order_by(TenderDocument.id.asc())
            .limit(max_docs)
            .all()
        )
    except Exception as e:
        logger.warning(f"[costing] PDF text fallback DB query failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return ""

    if not docs:
        return ""

    storage = get_storage_service()
    chunks: list[str] = []
    total = 0
    method_counts: dict[str, int] = {}
    with contextlib.ExitStack() as stack:
        for doc in docs:
            key = getattr(doc, "file_path", None)
            if not key:
                continue
            try:
                local_path = stack.enter_context(
                    storage.as_local_file(key, suffix=".pdf")
                )
            except FileNotFoundError:
                logger.warning(
                    f"[costing] doc {doc.id} not in storage (key={key})"
                )
                continue
            except Exception as e:
                logger.warning(
                    f"[costing] doc {doc.id} materialise failed: {e}"
                )
                continue
            try:
                result = extract_text_from_pdf_advanced(local_path)
            except Exception as e:
                logger.warning(
                    f"[costing] advanced parser failed for doc {doc.id}: {e}"
                )
                continue
            pages = result.get("pages") or []
            if not pages:
                continue
            label = getattr(doc, "file_name", None) or f"doc#{doc.id}"
            page_texts: list[str] = []
            for p in pages:
                text = (p.get("text") or "").strip()
                if not text:
                    continue
                method = p.get("method") or "text"
                method_counts[method] = method_counts.get(method, 0) + 1
                page_texts.append(f"[page {p.get('page_num')} · {method}]\n{text}")
            if not page_texts:
                continue
            body = "\n\n".join(page_texts)
            block = f"--- {label} ---\n{body}"
            chunks.append(block)
            total += len(block)
            if total >= max_chars:
                break

    if not chunks:
        return ""
    combined = "\n\n".join(chunks)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n…(truncated)"
    logger.info(
        f"[costing] PDF text extracted: {len(combined)} chars, "
        f"methods={method_counts}, docs={len(chunks)}, tender={tender_id}"
    )
    return combined


_PAGE_TAG_RE = re.compile(r"\[page \d+ · ([a-z_]+)\]")
_DOC_LABEL_RE = re.compile(r"^--- .+ ---$", re.MULTILINE)


def _extract_duplicates_blocks(pdf_extracts: str, attached_blocks: int) -> bool:
    """True when the text extract adds nothing to the attached PDF blocks:
    every page tag is `text` (pdfplumber read the PDF's own text layer) and
    the extract covers no more documents than were attached."""
    if not pdf_extracts or attached_blocks <= 0:
        return False
    methods = _PAGE_TAG_RE.findall(pdf_extracts)
    if not methods or any(m != "text" for m in methods):
        return False
    docs_in_extract = len(_DOC_LABEL_RE.findall(pdf_extracts))
    return 0 < docs_in_extract <= attached_blocks


def _build_tender_pdf_content_blocks(
    db: "Session",
    tender_id: int,
    *,
    max_docs: int = 3,
    doc_type_allowlist: Optional[set] = None,
) -> list[dict]:
    """Build native Anthropic document content blocks from the tender's PDFs.

    Lets the costing agent read the FULL PDF (text + tables + visual layout)
    the way claude.ai does, instead of the truncated pdfplumber text dump
    used by `_extract_tender_pdf_text`. Each block is base64 with
    ephemeral cache_control so subsequent ReAct turns hit the prompt cache.

    When `doc_type_allowlist` is provided, only PDFs whose per-doc
    `summary_json.doc_type` falls in the allowlist are attached. Its only
    caller is the context-budget rebuild, which passes the trim policy's own
    `_NIT_CLASS_DOC_TYPES` to shed annexed/T&C/drawing PDFs once the captured
    bidding schedule already covers the schedule content. Allowlist=None —
    the ordinary path — attaches everything up to max_docs, annexures
    included.

    Note what this filter is NOT for. Deciding which documents carry priced
    scope belongs to `boq_parser_service._pick_nit_source_docs`, which feeds
    the structured schedule; excluding an annexure there is how a costing
    silently loses most of its scope (issue #7). This one is a token-budget
    valve that only opens when the run is already over budget AND the
    schedule has been captured.

    Each returned block carries a `_drpl_meta` field with
    {doc_id, doc_type, page_count} so the trim policy can reason about
    sizes. Callers MUST run `_strip_internal_block_metadata(blocks)` before
    sending the list to Anthropic.

    Returns [] when:
      - the `claude_pdf_native_enabled` PlatformSetting is False
      - no PDF TenderDocuments exist for the tender
      - every doc fails the 32 MB / max-pages / read checks
      - the allowlist filtered out every candidate
    Caller falls back to `_extract_tender_pdf_text` in those cases.
    """
    try:
        from app.models.document_analysis import DocumentExtractionResult
        from app.models.tender import TenderDocument
        from app.services.ai_service import _build_pdf_content_blocks, _get_pdf_page_count
        from app.services.settings_service import get_effective_setting
        from app.services.storage_service import get_storage_service
    except Exception as e:
        logger.warning(f"[costing pdf] native PDF imports unavailable: {e}")
        return []

    try:
        native_enabled = get_effective_setting(db, "claude_pdf_native_enabled", True)
        if isinstance(native_enabled, str):
            native_enabled = native_enabled.lower() in ("true", "1", "yes")
        if not native_enabled:
            logger.info(
                f"[costing pdf] tender {tender_id}: skipped — "
                f"claude_pdf_native_enabled is False"
            )
            return []
        max_pages = int(get_effective_setting(db, "claude_pdf_max_pages", 200) or 200)
        cache_enabled = get_effective_setting(db, "claude_pdf_cache_enabled", True)
        if isinstance(cache_enabled, str):
            cache_enabled = cache_enabled.lower() in ("true", "1", "yes")
    except Exception as e:
        logger.warning(f"[costing pdf] tender {tender_id}: settings query failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return []

    try:
        docs = (
            db.query(TenderDocument)
            .filter(
                TenderDocument.tender_id == tender_id,
                TenderDocument.mime_type == "application/pdf",
            )
            .order_by(TenderDocument.id.asc())
            .all()
        )
    except Exception as e:
        logger.warning(f"[costing pdf] tender {tender_id}: DB query failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return []

    if not docs:
        logger.info(
            f"[costing pdf] tender {tender_id}: no PDF TenderDocument rows"
        )
        return []

    # Resolve per-doc doc_type from the Haiku pass's summary_json. Used for
    # the allowlist filter and for attaching metadata to blocks.
    doc_type_by_id: dict[int, str] = {}
    try:
        extractions = (
            db.query(DocumentExtractionResult)
            .filter(DocumentExtractionResult.tender_id == tender_id)
            .all()
        )
        for ext in extractions:
            summary = ext.summary_json or {}
            if isinstance(summary, dict):
                dtype = (summary.get("doc_type") or "").strip()
                if dtype and ext.document_id is not None:
                    doc_type_by_id[ext.document_id] = dtype
    except Exception as e:
        logger.debug(f"[costing pdf] doc_type lookup failed (non-fatal): {e}")
        try:
            db.rollback()
        except Exception:
            pass

    if doc_type_allowlist is not None:
        before_count = len(docs)
        # `.get(d.id, "")` — a document the per-doc pass has not reached yet
        # has no entry, and absence of a classification is not evidence that
        # it holds no schedule. `""` is in the allowlist so it is attached.
        docs = [
            d for d in docs
            if doc_type_by_id.get(d.id, "") in doc_type_allowlist
        ]
        logger.info(
            f"[costing pdf] tender {tender_id}: allowlist filter "
            f"{doc_type_allowlist} → kept {len(docs)} of {before_count} PDFs"
        )
        if not docs:
            # Allowlist filtered everything out — better to attach nothing
            # than to silently fall back to the unfiltered list.
            return []

    # The cap counts ELIGIBLE documents. It used to run in SQL before the
    # allowlist, so a tender whose first three PDFs by id were a drawing set
    # and two T&C booklets attached zero documents while the priced schedule
    # sat at position four (issue #7).
    if len(docs) > max_docs:
        logger.info(
            f"[costing pdf] tender {tender_id}: {len(docs)} eligible PDFs — "
            f"attaching the first {max_docs}"
        )
        docs = docs[:max_docs]

    storage = get_storage_service()
    blocks: list[dict] = []
    skipped_summary: list[str] = []
    with contextlib.ExitStack() as stack:
        for doc in docs:
            key = getattr(doc, "file_path", None)
            if not key:
                skipped_summary.append(f"doc {doc.id}: empty file_path")
                continue
            try:
                local_path = stack.enter_context(
                    storage.as_local_file(key, suffix=".pdf")
                )
            except FileNotFoundError:
                logger.warning(
                    f"[costing pdf] doc {doc.id}: not found in storage "
                    f"(key={key})"
                )
                skipped_summary.append(f"doc {doc.id}: storage miss")
                continue
            except Exception as e:
                logger.warning(
                    f"[costing pdf] doc {doc.id}: materialise failed: {e}"
                )
                skipped_summary.append(f"doc {doc.id}: materialise error")
                continue
            try:
                file_size = os.path.getsize(local_path)
                if file_size > 32 * 1024 * 1024:
                    logger.info(
                        f"[costing pdf] doc {doc.id}: too large "
                        f"({file_size} bytes > 32 MB) — skipping"
                    )
                    skipped_summary.append(f"doc {doc.id}: >32 MB")
                    continue
                page_count = _get_pdf_page_count(local_path)
                if page_count > max_pages:
                    logger.info(
                        f"[costing pdf] doc {doc.id}: {page_count} pages "
                        f"> max_pages={max_pages} — skipping. "
                        f"To allow this PDF, raise the "
                        f"`claude_pdf_max_pages` PlatformSetting."
                    )
                    skipped_summary.append(
                        f"doc {doc.id}: {page_count}p > {max_pages}"
                    )
                    continue
                # Only mark the FIRST block with cache_control. Anthropic
                # caches the whole prompt prefix from any breakpoint, so
                # a single mark covers all subsequent docs and stays well
                # under the 4-breakpoint limit (system prompt may use 1-2).
                this_cache = cache_enabled and not blocks
                new_blocks = _build_pdf_content_blocks(local_path, this_cache)
                # Attach internal metadata for the context-budget trim policy
                # (Phase 2). _strip_internal_block_metadata clears these
                # before the blocks reach Anthropic.
                doc_meta = {
                    "doc_id": doc.id,
                    "doc_type": doc_type_by_id.get(doc.id),
                    "page_count": page_count,
                }
                for blk in new_blocks:
                    if isinstance(blk, dict):
                        blk["_drpl_meta"] = doc_meta
                blocks.extend(new_blocks)
                logger.info(
                    f"[costing pdf] doc {doc.id}: built block "
                    f"({page_count}p, {file_size} bytes, "
                    f"cache_control={this_cache}, "
                    f"doc_type={doc_type_by_id.get(doc.id)!r})"
                )
            except Exception as e:
                logger.warning(
                    f"[costing pdf] doc {doc.id}: build_block failed: {e}"
                )
                skipped_summary.append(f"doc {doc.id}: build error")
                continue

    if blocks:
        logger.info(
            f"[costing pdf] tender {tender_id}: total "
            f"{len(blocks)} block(s) attached"
            + (f"; skipped: {skipped_summary}" if skipped_summary else "")
        )
    elif skipped_summary:
        logger.warning(
            f"[costing pdf] tender {tender_id}: 0 blocks built — "
            f"all docs skipped: {skipped_summary}"
        )
    return blocks


def _strip_internal_block_metadata(blocks: list) -> list:
    """Remove `_drpl_meta` fields that the trim policy uses. Anthropic's
    SDK is tolerant of extra keys today but we shouldn't depend on that —
    serialize a clean list before sending to the API.
    """
    out: list = []
    for blk in blocks or []:
        if isinstance(blk, dict):
            clean = {k: v for k, v in blk.items() if not k.startswith("_drpl_")}
            out.append(clean)
        else:
            out.append(blk)
    return out


from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from sqlalchemy.orm import Session
from typing_extensions import TypedDict

from app.services.langchain.callback_handler import DRPLCallbackHandler
from app.services.langchain.graphs.costing_agent import (
    COSTING_AGENT_SYSTEM_PROMPT,
    _parse_costing_response,
)
from app.services.langchain.llm_factory import get_chat_model
from app.services.langchain.tools.tool_loader import load_tools_by_keys

logger = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────────────────────────

class CostingAgentState(TypedDict):
    # Core inputs
    messages: Annotated[list, add_messages]
    tender_id: Optional[int]
    analysis_result: dict
    mode: str                        # "pipeline" | "chat"
    session_id: Optional[str]
    user_id: Optional[int]
    proposal_session_id: Optional[int]   # Command Center session (for clarify tool persistence)

    # Training data
    training_context: str
    training_chars: int

    # Anonymization — placeholder → original (built from the tender's records)
    anonymization_map: dict

    # Clarification
    clarification_needed: bool
    clarification_question: str
    clarification_answers: dict      # populated when user replies in chat mode

    # Travel delegation
    travel_delegation_request: Optional[dict]
    travel_result: Optional[dict]

    # Output
    costing_result: dict
    errors: Annotated[list, add_messages]
    status: str                      # "running"|"needs_clarification"|"completed"|"failed"


# ── System prompt additions ──────────────────────────────────────────────────

_PROMPT_PRIVACY = """
## Privacy Rules for External Research
ALL web searches MUST use the `anonymizing_web_search` tool.
NEVER use `web_search` directly.
Company names, tender reference numbers, and internal project codes are automatically
stripped from every query by the anonymizing tool before the search is executed."""

_PROMPT_TRAINING_DATA = """
## Costing Training Data
Rate cards and DSR tables have been injected into your context above under
"COSTING TRAINING DATA". Prioritise these rates over web-searched values.
Apply approximately 6% annual escalation for data older than 12 months.
Use the `costing_training_retrieval` tool for on-demand lookups of specific items."""

_PROMPT_CLARIFICATION_CHAT = """
## Clarification (chat mode)
If the user's costing request has a genuine ambiguity that would change the
cost estimate by more than 10%, ask ONE consolidated clarifying question as
a plain response — do NOT call any tool for the clarification itself.
In pipeline mode, make the most conservative reasonable assumption and record
it under `assumptions`."""

_PROMPT_CLARIFICATION_PIPELINE = """
## Clarification (pipeline mode)
You are running in automated pipeline mode. Never pause to ask questions.
Make the most conservative reasonable assumption for any ambiguity and record
it under `assumptions`."""

_PROMPT_TRAVEL = """
## Travel and Crew Mobilisation Costs
For crew travel costs (mobilisation/demobilisation), use the `delegate_travel_research`
tool with the origin city, destination city, crew count, and travel class.
Include the returned estimates as Transport line items in your cost breakdown."""

_PROMPT_COMPONENT_EXPANSION = """
## COMPONENT BUILD-UP MODE (bottom-up, ratecard-driven) — ACTIVE

This tender is costed BOTTOM-UP, not as a 1:1 mirror of the NIT bidding
schedule. The work scope names engine types and maintenance checks (e.g.
"D-check on VTA 28L engine", "C-check on NTA 855R"). Each such scope item
expands into a LIST of constituent spare parts whose rates come from the
structured ratecard — NOT a single priced line.

PROCEDURE (follow exactly):
1. For each scope item that names an engine type + check level, call
   `ratecard_lookup` with mode="expand_check", engine_type=<type>,
   check_level=<B|C|D>. This returns the full kit of spare parts with their
   per-engine qty, rate, and source.
2. Emit ONE line_item PER returned part:
     - description = part description (prefix with the Part No, e.g.
       "[3077198] GASKET,ROCKER LEVER COVER")
     - item_code   = the Part No (verbatim)
     - quantity    = kit_qty × number_of_engines × number_of_checks
     - unit        = the part's UoM (default "Nos")
     - rate        = the ratecard "Our Rate", VERBATIM (do NOT mark up — margin
       is applied later by the user)
     - rate_source = "ratecard"
     - source_ref  = the rate source string (Cummins / Fleetguard / Market / …)
     - annexure    = the annexure group letter (A..L) when known, else group
       related parts under the same letter you assign consistently
     - cost_buildup_note = "<source> ratecard, <check_level>-check <engine_type>"
3. For an individual part NOT part of a kit, use mode="part" with the Part No,
   or mode="fuzzy" with a description.
4. If the ratecard lacks a part's rate, set rate_source="needs_user_input" and
   leave the rate null — NEVER invent a part rate. Only after the ratecard and
   training data both miss should you use `anonymizing_web_search`, tagging that
   line source_ref with the source URL.
5. Group the output by annexure. Number of engines / number of checks comes from
   the NIT scope; if unstated, assume 1 and record that under `assumptions`.

This mode supersedes the 1:1 NIT-mirror rule for this run."""


def _build_system_prompt(
    mode: str,
    training_context: str,
    db: Optional[Session] = None,
    component_mode: bool = False,
) -> str:
    """Assemble the full system prompt for the costing ReAct agent.

    Phase 3d — base prompt now resolves through `canonical_registry`:
      - if the admin has user-customized the prompt in Agent Builder UI AND
        the customization preserves the required placeholders, that text is
        used as the base
      - otherwise (or if validation fails), falls back to
        `COSTING_AGENT_SYSTEM_PROMPT` (the canonical code constant)

    Whichever base resolves, the SAME runtime injections still apply: org
    defaults (placeholder substitution), privacy section, training data
    section, mode-conditional clarification, travel block, and (when
    present) the agent's training-context dump.
    """
    clarification_section = (
        _PROMPT_CLARIFICATION_CHAT if mode == "chat" else _PROMPT_CLARIFICATION_PIPELINE
    )

    # Resolve the BASE prompt (CustomAgent.system_prompt or canonical).
    from app.services.langchain.canonical_registry import resolve_system_prompt
    base_prompt_template, prompt_source = resolve_system_prompt(
        db, "costing_researcher",
        canonical_builder=lambda: COSTING_AGENT_SYSTEM_PROMPT,
    )

    # Resolve org defaults and patch the placeholder tokens in the base prompt.
    from app.services.costing_format_service import get_costing_defaults
    defaults = get_costing_defaults(db)
    base_prompt = (
        base_prompt_template
        .replace("<overhead_percent>", f"{defaults['overhead_percent']:g}")
        .replace("<margin_percent>", f"{defaults['margin_percent']:g}")
        .replace("<gst_percent>", f"{defaults['gst_percent']:g}")
    )
    org_defaults_section = (
        f"\n## Org Defaults (admin-configured, applied unless tender mandates otherwise)\n"
        f"- Overhead: {defaults['overhead_percent']:g}%\n"
        f"- Profit margin: {defaults['margin_percent']:g}%\n"
        f"- GST: {defaults['gst_percent']:g}%\n"
    )

    parts = [
        base_prompt,
        org_defaults_section,
        _PROMPT_PRIVACY,
        _PROMPT_TRAINING_DATA,
        clarification_section,
        _PROMPT_TRAVEL,
    ]
    if component_mode:
        parts.append(_PROMPT_COMPONENT_EXPANSION)
    if training_context:
        # Replace the generic header with a costing-specific one
        costing_training = training_context.replace(
            "# ═══ TRAINING DATA ═══",
            "# ═══ COSTING TRAINING DATA ═══",
        )
        parts.append(costing_training)

    logger.info(
        f"[costing] system prompt resolved: source={prompt_source}, "
        f"base_len={len(base_prompt_template)}, full_len={sum(len(p) for p in parts)}"
    )
    return "\n".join(parts)


# ── Node: load training context + build anonymization map ────────────────────

async def load_training_context_node(state: CostingAgentState, db: Session) -> dict:
    """
    1. Load training datasets assigned to the costing_researcher agent.
    2. Build the anonymization_map from the tender's records (no model call).
    """
    training_context = ""
    training_chars = 0
    anonymization_map = {}

    # --- Training data ---
    try:
        from app.models.agent_builder import CustomAgent
        from app.services.training_dataset_service import get_agent_training_context

        agent_record = (
            db.query(CustomAgent)
            .filter(CustomAgent.agent_key == "costing_researcher")
            .first()
        )
        if agent_record:
            training_context = get_agent_training_context(
                db, agent_record.id, max_chars=200_000
            )
            training_chars = len(training_context)
            if training_chars > 0:
                logger.info(
                    f"Loaded {training_chars:,} chars of costing training data "
                    f"for agent id={agent_record.id}"
                )
    except Exception as e:
        logger.warning(f"Could not load training context: {e}")
        try:
            db.rollback()
        except Exception:
            pass

    # --- Anonymization map ---
    # Built from the tender's own records (reference, buyer, the firm's
    # letterhead name) plus reference-number tokens in the analysis -- no
    # model call. See app/services/costing/anonymization.py.
    from app.services.costing.anonymization import build_anonymization_map

    anonymization_map = build_anonymization_map(
        db, state.get("tender_id"), state.get("analysis_result") or {},
    )
    logger.debug(f"Built anonymization map with {len(anonymization_map)} entities")

    return {
        "training_context": training_context,
        "training_chars": training_chars,
        "anonymization_map": anonymization_map,
    }


# ── Node: check if clarification is needed ──────────────────────────────────

async def check_clarification_needed_node(state: CostingAgentState, db: Session) -> dict:
    """
    Determine whether the agent needs to ask the user for clarification before
    proceeding. Only runs in chat mode and only when no answers have been given yet.
    """
    mode = state.get("mode", "pipeline")
    clarification_answers = state.get("clarification_answers") or {}

    # Skip in pipeline mode or if answers already provided
    if mode != "chat" or clarification_answers:
        return {"clarification_needed": False, "clarification_question": ""}

    # Hard guard — if a tender is in scope OR the analysis_result has any
    # usable content, do NOT ask the user "what should I cost?". The user
    # explicitly attached a tender; the agent must derive scope itself and
    # record any uncertainty under `assumptions`. Prior failure mode: the
    # check LLM would flag missing quantities and return a clarification
    # question, breaking the chat in the screenshot the user reported.
    tender_id_in_scope = state.get("tender_id")
    analysis_result = state.get("analysis_result") or {}
    has_analysis_content = bool(
        (analysis_result.get("summary") or "").strip()
        or analysis_result.get("boq_items")
        or (analysis_result.get("requirements") or {})
    )
    if tender_id_in_scope or has_analysis_content:
        logger.info(
            f"[costing] clarification check suppressed — tender_id="
            f"{tender_id_in_scope}, has_analysis_content={has_analysis_content}; "
            "agent will derive scope from attached tender."
        )
        return {"clarification_needed": False, "clarification_question": ""}

    messages = state.get("messages") or []
    last_user_message = ""
    for msg in reversed(messages):
        if hasattr(msg, "type") and msg.type == "human":
            last_user_message = msg.content
            break
        if isinstance(msg, dict) and msg.get("role") == "human":
            last_user_message = msg.get("content", "")
            break

    analysis_result = state.get("analysis_result") or {}
    analysis_snippet = json.dumps(analysis_result, default=str)[:2000]

    try:
        llm = get_chat_model(
            db,
            agent_name="costing_researcher",
            max_tokens=256,
            temperature=0.1,
        )

        check_prompt = (
            "You are assisting with a costing request. Determine whether there is a CRITICAL "
            "ambiguity in the user's request that:\n"
            "  (a) cannot be resolved from the tender analysis provided, AND\n"
            "  (b) would change the total cost estimate by more than 10%.\n\n"
            "Only flag genuine blockers. Do NOT ask about minor uncertainties.\n\n"
            f"User request: {last_user_message}\n\n"
            f"Available tender analysis: {analysis_snippet}\n\n"
            "Return ONLY a JSON object (no markdown):\n"
            '{"needs_clarification": true/false, "question": "...", '
            '"missing_keys": ["key1", "key2"]}'
        )

        from app.services.langchain.llm_factory import safe_ainvoke
        response = await safe_ainvoke(
            llm,
            [HumanMessage(content=check_prompt)],
            agent_name="costing_researcher.clarification_check",
        )
        raw = (response.content or "").strip()

        if "```" in raw:
            raw = raw.split("```")[1] if "```json" not in raw else raw.split("```json")[1]
            raw = raw.split("```")[0].strip()

        parsed = json.loads(raw)
        needs = bool(parsed.get("needs_clarification", False))
        question = parsed.get("question", "").strip()

        return {
            "clarification_needed": needs,
            "clarification_question": question if needs else "",
        }

    except Exception as e:
        logger.warning(f"Clarification check failed: {e}. Proceeding without clarification.")
        return {"clarification_needed": False, "clarification_question": ""}


# ── Node: emit clarification ─────────────────────────────────────────────────

def emit_clarification_node(state: CostingAgentState) -> dict:
    """Format the clarification question as an AI message and mark status."""
    question = state.get("clarification_question", "")
    return {
        "messages": [AIMessage(content=question)],
        "status": "needs_clarification",
    }


# ── Refusal detection ────────────────────────────────────────────────────────

def _looks_like_costing_refusal(text: str) -> bool:
    """Heuristic: did the costing agent's final message refuse to estimate?

    Triggered when the response either contains no parseable cost JSON OR
    matches the typical "ask for BOQ" refusal pattern. We use this to decide
    whether to re-invoke the agent with a stronger nudge before falling
    through to the canned needs_user_input fallback.

    Conservative on purpose — false positives cost one extra agent round-trip
    (~5–10s); false negatives leave the user with the bad fallback experience.
    """
    if not text:
        return True

    cleaned = (text or "").strip()
    if len(cleaned) < 80:
        # Empty-stub-with-trivial-content. Almost certainly a refusal.
        return True

    # Marker check — every successful costing response carries the
    # COSTING_JSON markers. Their absence means parse will fail.
    if "COSTING_JSON_START" not in cleaned:
        # No JSON markers — try a permissive JSON scan: a response with
        # priced line_items as raw JSON is rare but legal.
        lower = cleaned.lower()
        # Refusal phrases the agent commonly emits when giving up.
        refusal_signals = [
            "cannot price",
            "cannot derive",
            "cannot estimate",
            "could not derive",
            "could not estimate",
            "unable to price",
            "unable to estimate",
            "please share",
            "please provide the boq",
            "please provide the schedule of rates",
            "need the boq",
            "need the schedule of rates",
            "need more information",
            "needs more information",
            "scope is unclear",
            "scope insufficient",
            "without the boq",
            "without the schedule of rates",
        ]
        if any(sig in lower for sig in refusal_signals):
            return True
        # No JSON markers AND no obvious refusal phrase — treat as suspicious.
        # The parser would fail anyway; better to retry once with a nudge.
        return True

    # Markers present — peek at whether there's at least one priceable item.
    # We don't fully parse here (the parser does that downstream); just
    # check for a numeric "rate" field. The canonical schema is single-rate
    # (RULE 1 in COSTING_AGENT_SYSTEM_PROMPT): one `rate` + one `amount` per
    # line. Legacy range fields (rate_low/expected/high) are still accepted
    # here so prompts edited via Agent Builder that emit ranges aren't
    # mis-flagged as refusals.
    import re as _re
    has_priced_item = bool(_re.search(
        r'"(?:rate|rate_low|rate_expected|rate_high)"\s*:\s*-?[0-9]',
        cleaned,
    ))
    if not has_priced_item:
        return True

    return False


# ── Section 7 (Costing Handoff) extractor ────────────────────────────────────

# Matches the analyzer's Section 7 header — both the canonical
# "### SECTION 7: COSTING BASIS HANDOFF" form and a few legacy variants that
# admin-edited prompts may emit. Case-insensitive. Captures everything until
# the next "### SECTION " header or end-of-string.
_SECTION_7_RE = re.compile(
    r"#{1,6}\s*SECTION\s*7\b[^\n]*\n"
    r"(?P<body>.+?)"
    r"(?=\n#{1,6}\s*SECTION\s*\d+\b|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _extract_section_7_from_analysis_markdown(
    db: "Session", tender_id: Optional[int]
) -> str:
    """Pull the verbatim Section 7 "Costing Basis Handoff" block from the
    tender_doc_analyzer's markdown report.

    Why: ``costing_scope_extractor`` already produces a typed structured scope
    from the per-doc summaries, but Section 7 contains analyst-curated
    verbatim quotes for pricing format, payment milestones, LD clauses and
    PBG details — the exact things the costing agent should anchor margin
    sizing and risk premium on. Injecting both into the prompt gives the
    agent typed dispatch (from the scope extractor) AND verbatim source text
    (from Section 7) so it can quote contractually-sensitive clauses
    directly rather than paraphrasing.

    Returns the matched markdown block (with a leading "### SECTION 7" header
    for clarity), capped at 6000 chars to keep prompt size predictable.
    Returns "" when no analysis markdown exists or Section 7 isn't present.
    """
    if not tender_id:
        return ""
    try:
        from app.models.document_analysis import TenderAnalysisSummary
        row = (
            db.query(TenderAnalysisSummary)
            .filter(TenderAnalysisSummary.tender_id == tender_id)
            .first()
        )
        if not row or not row.requirement_summary:
            return ""
        match = _SECTION_7_RE.search(row.requirement_summary)
        if not match:
            return ""
        body = (match.group("body") or "").strip()
        if not body:
            return ""
        # 6000 chars typically covers the full 7.1-7.10 sub-sections for
        # a normal tender. Truncate with a marker so the agent knows it
        # was cut rather than thinking the analyst stopped writing.
        if len(body) > 6000:
            body = body[:6000].rstrip() + "\n\n[…Section 7 truncated for prompt size — refer to the full analysis artifact for the rest…]"
        return f"### SECTION 7 — COSTING BASIS HANDOFF (verbatim from analyzer)\n\n{body}"
    except Exception as e:
        logger.warning(
            f"[costing] Section 7 extraction failed for tender {tender_id}: "
            f"{type(e).__name__}: {e}"
        )
        try:
            db.rollback()
        except Exception:
            pass
        return ""


# ── Retry helper ─────────────────────────────────────────────────────────────

# HTTP statuses that warrant a retry. 529 is Anthropic's "Overloaded" code.
_TRANSIENT_HTTP_STATUSES = {502, 503, 504, 529}


async def _ainvoke_with_retry(agent, payload, config, *, attempts: int = 3):
    """ReAct invocation with retry-with-backoff for transient Anthropic 5xx.

    Sonnet 4.6 occasionally returns 503/529 during peak load; one or two
    quick retries clear the vast majority. Re-raises on non-transient
    errors and after the final attempt. Backoff is 1s, 2s (between 3 attempts).
    """
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return await agent.ainvoke(payload, config=config)
        except Exception as exc:
            # langchain-anthropic surfaces APIStatusError; some wrappers
            # nest the original error in __cause__ or .response.
            status = (
                getattr(exc, "status_code", None)
                or getattr(getattr(exc, "response", None), "status_code", None)
            )
            msg = str(exc).lower()
            transient = (
                status in _TRANSIENT_HTTP_STATUSES
                or "overloaded" in msg
                or "rate_limit" in msg
                or "rate limit" in msg
            )
            last_exc = exc
            if not transient or i + 1 == attempts:
                raise
            wait_s = 2 ** i  # 1s, 2s
            logger.warning(
                f"[costing] transient error from Anthropic "
                f"(status={status}, type={type(exc).__name__}), "
                f"retry {i + 1}/{attempts - 1} in {wait_s}s: {exc!r}"
            )
            await asyncio.sleep(wait_s)
    if last_exc:  # safety net (loop should have re-raised already)
        raise last_exc


# ── Node: run costing ReAct agent ────────────────────────────────────────────

def _render_bidding_schedule_block(
    boq_subset: list[dict],
    withhold_published: bool = False,
    schedule_titles: Optional[dict] = None,
) -> str:
    """Render the `### BIDDING SCHEDULE` markdown table + `<bidding_schedule_json>`
    sidecar for a set of BOQ rows (the RULE 5 1:1-mirror contract).

    Shared by the single-call path (`run_costing_react_node`) and the batched
    path (`run_costing_batched_node`) so both render the schedule identically.
    The sidecar is the machine-readable contract the agent parses for verbatim
    boq_item_id / item_code / schedule_name / bidding_unit / escalation_pct /
    is_tax_line / basic_value values.

    `withhold_published` (batched path) blanks the published rate and basic
    value of every schedule row. An agent that can see the railway's rate
    anchors on it: told not to strip a margin off it, it back-solved a
    build-up to exactly 0.800 of it instead (Mid-Life NIT, 52 rows). The
    batched skeleton already holds `tender_rate`, and merge never takes it
    from the agent, so the margin is still measured against it afterwards.
    Annexure components keep their printed rates (_COMPONENT_RESEARCH_RULE).
    The single-call path cannot withhold: its agent emits `tender_rate`.

    `schedule_titles` ({code: banner}) heads each schedule with what it
    prices: "B -- COST OF LABOUR ..." is labour, "A -- Cost of Material ..."
    is the part itself, and the two list the same "Web to Drg No LE11185".
    """
    import json as _json_mod
    if withhold_published:
        boq_subset = [
            it if it.get("component_of")
            else {**it, "estimated_rate": None, "basic_value": None}
            for it in boq_subset
        ]
    try:
        sidecar = _json_mod.dumps(boq_subset, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        sidecar = "[]"

    groups: dict = {}
    for it in boq_subset:
        if it.get("component_of"):
            sched = f"Annexure-{it.get('annexure_ref') or '?'} components"
        elif it.get("annexure_ref") and not it.get("schedule_name"):
            sched = f"Annexure-{it.get('annexure_ref')}"
        else:
            sched = it.get("schedule_name") or "?"
        groups.setdefault(sched, []).append(it)

    tbl_lines: list[str] = []
    tbl_lines.append(
        f"### BIDDING SCHEDULE — {len(boq_subset)} row(s) ({len(groups)} schedule(s))"
    )
    tbl_lines.append("")
    tbl_lines.append(
        "RULE 5: emit EXACTLY one `line_items` row per row below, in this order, "
        "preserving boq_item_id / item_code / schedule_name / bidding_unit / "
        "escalation_pct / is_tax_line / basic_value verbatim. The "
        "<bidding_schedule_json> sidecar is the machine-readable contract — "
        "parse it for the exact values."
    )
    if any(it.get("component_of") for it in boq_subset):
        tbl_lines.append("")
        tbl_lines.append(
            "ANNEXURE COMPONENTS: rows under an \"Annexure-N components\" heading are "
            "the materials that make up ONE schedule item (named in the row's "
            "\"Component of\" cell). Their Qty is PER SET of that item — e.g. 12 LTR "
            "per paint set — not for the whole contract. Price each component per "
            "its own unit (the market rate for one litre, one kg, one number); the "
            "platform multiplies by the per-set quantity and sums the components "
            "into the parent item's rate. Do NOT scale a component's quantity by "
            "the number of sets, and do NOT price the parent item here. A component "
            "with a printed reference still needs current rate evidence (see the "
            "ANNEXURE COMPONENTS WITH A PRINTED RATE rule); use shared material-family searches."
        )
    tbl_lines.append("")
    for sched in sorted(groups.keys()):
        rows = groups[sched]
        heading = sched if sched.startswith("Annexure") else f"Schedule {sched}"
        _banner = (schedule_titles or {}).get(str(sched).strip().upper()) if not sched.startswith("Annexure") else None
        if _banner:
            heading = f"{heading}: {_banner}"
        tbl_lines.append(f"#### {heading} — {len(rows)} row(s)")
        if rows and rows[0].get("component_of"):
            tbl_lines.append("")
            tbl_lines.append(f"Component of: {rows[0]['component_of']} — quantities are {rows[0].get('quantity_basis') or 'per set'}.")
        tbl_lines.append("")
        tbl_lines.append(
            "| Sr | Item Code | Description | Qty | Unit | "
            "Published Ref Rate (benchmark — DO NOT COPY as your cost) | "
            "Basic Value | Escl.% | Bidding Unit | Tax? | boq_item_id |"
        )
        tbl_lines.append(
            "|----|-----------|-------------|-----|------|-----------|"
            "-------------|--------|--------------|------|-------------|"
        )
        for it in rows:
            sr = it.get("sr_no", "?")
            code = (it.get("item_code") or "").strip() or "—"
            desc = (it.get("description") or "").strip().replace("|", "\\|")[:120]
            qty = it.get("quantity")
            unit = (it.get("unit") or "").strip() or "—"
            rate = it.get("estimated_rate")
            basic = it.get("basic_value")
            escl = it.get("escalation_pct")
            bu = (it.get("bidding_unit") or "").strip() or "—"
            tax = "yes" if it.get("is_tax_line") else "no"
            bid = it.get("boq_item_id") or "—"
            tbl_lines.append(
                f"| {sr} | {code} | {desc} | {qty if qty is not None else '—'} | "
                f"{unit} | {rate if rate is not None else '—'} | "
                f"{basic if basic is not None else '—'} | "
                f"{escl if escl is not None else '—'} | {bu} | {tax} | {bid} |"
            )
        tbl_lines.append("")

    tbl_lines.append("<bidding_schedule_json>")
    tbl_lines.append(sidecar)
    tbl_lines.append("</bidding_schedule_json>")
    return "\n".join(tbl_lines)


def _system_message_for_model(model_name: str, system_prompt: str) -> SystemMessage:
    """The costing system prompt as a prompt-cacheable block.

    The costing prefix (base prompt + training data + budget block) is
    identical on every ReAct step of every batch and every sweep, and it is
    the bulk of each request. On Anthropic it is sent as one text block with
    `cache_control: ephemeral`, so every step after the first reads it at the
    cache rate instead of paying for it in full. Other providers get the
    plain string they always got (OpenAI caches automatically; a
    `cache_control` key would be rejected there). The text is byte-identical
    either way, so the agent sees exactly the prompt it saw before.
    """
    try:
        from app.services.langchain.provider_config import detect_provider
        provider = detect_provider(model_name or "")
    except Exception:
        provider = ""
    if provider != "anthropic":
        return SystemMessage(content=system_prompt)
    return SystemMessage(content=[{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }])


async def _warm_prompt_cache(built: dict, tender_id, label: str, timeout_s: float = 90.0) -> None:
    """Create the prompt-cache entry for the costing prefix before batches fan out.

    Batches start together with the same prefix; without a warm entry every
    one of them pays the cache-write premium and none reads. One tiny call
    (same tools, same system block, a one-word reply) writes the entry, and
    the batches that follow read it. Best effort: any failure is logged and
    the batches run exactly as they would have.
    """
    try:
        try:
            from app.services.langchain.provider_config import detect_provider
            if detect_provider(built.get("model_name") or "") != "anthropic":
                return
        except Exception:
            return
        llm = built["llm"]
        tools = built.get("tools") or []
        model = llm.bind_tools(tools) if tools else llm
        messages = [
            _system_message_for_model(built.get("model_name") or "", built["system_prompt"]),
            HumanMessage(content="Cache warm-up only. Reply with exactly: OK"),
        ]
        started = time.monotonic()
        response = await asyncio.wait_for(
            model.ainvoke(messages, config={"callbacks": [built["callback"]]}),
            timeout=timeout_s,
        )
        usage = {}
        try:
            usage = (getattr(response, "usage_metadata", None) or {})
            details = usage.get("input_token_details") or {}
            usage = {
                "cache_creation": details.get("cache_creation", 0),
                "cache_read": details.get("cache_read", 0),
                "input": usage.get("input_tokens", 0),
            }
        except Exception:
            usage = {}
        logger.info(
            f"[costing batched] tender {tender_id}: prompt cache warmed for {label} "
            f"in {time.monotonic() - started:.1f}s {usage}"
        )
    except Exception as e:
        logger.warning(
            f"[costing batched] tender {tender_id}: prompt cache warm-up for {label} "
            f"skipped ({type(e).__name__}: {e}); batches run uncached"
        )


def _build_costing_agent(
    state: CostingAgentState,
    db: Session,
    training_context: str,
    mode: str,
) -> dict:
    """Build the costing ReAct agent (tools + model + system prompt).

    Factored out of `run_costing_react_node` so the single-call and batched
    costing paths build an identical agent. Returns the compiled `agent` plus
    the pieces the single-call path needs to rebuild it after a context-budget
    trim (`llm`, `tools`, `base_system_prompt`, `budget_block`).
    """
    from langgraph.prebuilt import create_react_agent
    from app.services.langchain.tools.anonymizing_web_search_tool import AnonymizingWebSearchTool
    from app.services.langchain.canonical_registry import resolve_tool_keys
    from app.services.settings_service import get_effective_setting
    from app.services.ai_service import _get_agent_config, _get_effective_model
    from app.services.langchain.model_limits import (
        compute_dynamic_max_tokens, render_token_budget_protocol,
    )

    anonymization_map = state.get("anonymization_map") or {}
    anon_search = AnonymizingWebSearchTool(db=db, anonymization_map=anonymization_map)

    # No local default_keys — the canonical registry's default_tools decides.
    # anonymizing_web_search is in that list but constructed specially above
    # (it needs this run's anonymization_map), so it is filtered from the
    # generic load below and added as the pre-built instance.
    resolved_keys, tool_src = resolve_tool_keys(db, "costing_researcher")
    other_tool_keys = [k for k in resolved_keys if k != "anonymizing_web_search"]
    other_tools = load_tools_by_keys(
        db,
        other_tool_keys,
        agent_key="costing_researcher",
        proposal_session_id=state.get("proposal_session_id"),
        router_session_id=state.get("session_id"),
    )
    tools = [anon_search] + other_tools
    logger.info(f"[costing] tool keys resolved: source={tool_src} keys={resolved_keys}")

    _costing_agent_config = _get_agent_config(db, "costing_researcher")
    _costing_model = _get_effective_model(db, _costing_agent_config) or "claude-sonnet-4-6"
    _costing_max_tokens = compute_dynamic_max_tokens(_costing_model, requested=None)

    _thinking_on = get_effective_setting(db, "costing_thinking_enabled", False)
    if isinstance(_thinking_on, str):
        _thinking_on = _thinking_on.lower() in ("true", "1", "yes")
    _thinking_mode = "auto" if _thinking_on else "disabled"
    llm = get_chat_model(
        db,
        agent_name="costing_researcher",
        max_tokens=_costing_max_tokens,
        thinking_mode_override=_thinking_mode,
    )
    callback = DRPLCallbackHandler(db, agent_name="costing_researcher")

    base_system_prompt = _build_system_prompt(
        mode, training_context, db=db,
        component_mode=bool(state.get("_component_mode")),
    )
    _budget_block = render_token_budget_protocol(
        max_tokens=_costing_max_tokens,
        priority_hierarchy=(
            "1. Priceable line items (description + qty + unit + unit_rate + total)\n"
            "2. Manpower / labor decomposition with rates\n"
            "3. Subtotal, GST, and grand total\n"
            "4. needs_user_input rows (only when scope is genuinely unclear)\n"
            "5. Recommendations (markup strategy, risk reserves)\n"
            "6. Assumptions used for the estimate"
        ),
    )
    system_prompt = base_system_prompt + "\n\n" + _budget_block

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=_system_message_for_model(_costing_model, system_prompt),
    )
    return {
        "agent": agent,
        "llm": llm,
        "tools": tools,
        "callback": callback,
        "max_tokens": _costing_max_tokens,
        "model_name": _costing_model,
        "base_system_prompt": base_system_prompt,
        "budget_block": _budget_block,
        "system_prompt": system_prompt,
    }


async def run_costing_react_node(state: CostingAgentState, db: Session) -> dict:
    """
    The main costing research node. Creates a ReAct agent with the full tool set
    and invokes it with the tender context and any clarification answers.
    """
    mode = state.get("mode", "pipeline")
    tender_id = state.get("tender_id")
    analysis_result = state.get("analysis_result") or {}
    clarification_answers = state.get("clarification_answers") or {}
    training_context = state.get("training_context", "")
    anonymization_map = state.get("anonymization_map") or {}

    # Build the ReAct agent (tools + model + system prompt) via the shared
    # helper so the single-call and batched paths are identical. The single
    # path also needs llm / tools / base_system_prompt / budget_block in scope
    # for the post-trim agent rebuild below.
    _built = _build_costing_agent(state, db, training_context, mode)
    agent = _built["agent"]
    llm = _built["llm"]
    tools = _built["tools"]
    callback = _built["callback"]
    _costing_max_tokens = _built["max_tokens"]
    base_system_prompt = _built["base_system_prompt"]
    _budget_block = _built["budget_block"]
    system_prompt = _built["system_prompt"]

    # Build the user message from analysis context
    requirements = analysis_result.get("requirements", {})
    boq_items = analysis_result.get("boq_items") or []
    summary_text = (analysis_result.get("summary") or "").strip()
    context = json.dumps({
        "tender_summary": analysis_result.get("summary", ""),
        "technical_specifications": requirements.get("technical_specs", []),
        "financial_requirements": requirements.get("financial", []),
        "required_documents": analysis_result.get("required_documents", []),
    }, default=str)[:4000]

    tender_ref = f"tender ID {tender_id}" if tender_id else "this costing request"

    # Workstream 1 — token reduction. Run the costing scope extractor against
    # the per-doc summaries already produced by tender_doc_analyzer to derive
    # a compact (~3-5K char) structured scope. When extraction confidence is
    # high, this lets us send only the scope JSON to the costing prompt and
    # skip the 30K-char PDF text dump — typical input-token reduction is 90%+
    # for AMC-style tenders. The full PDF docs remain attached as native blocks
    # (Anthropic) so the agent can verify specific sections via vision when
    # needed.
    costing_scope: dict = {}
    if tender_id:
        # Diagnostic: how many per-doc summaries does this tender have? When
        # the count is 0, the extractor will return an empty-confidence stub
        # because tender_doc_analyzer hasn't run on this tender yet — useful
        # signal when debugging "no line items" failures.
        try:
            from app.models.document_analysis import DocumentExtractionResult
            _summary_count = db.query(DocumentExtractionResult).filter(
                DocumentExtractionResult.tender_id == tender_id,
                DocumentExtractionResult.summary_json.isnot(None),
            ).count()
            logger.info(
                f"[costing scope] tender {tender_id}: per_doc_summaries={_summary_count}"
            )
        except Exception as _diag_e:
            logger.debug(f"[costing scope] per-doc summary count diag failed: {_diag_e}")
            try:
                db.rollback()
            except Exception:
                pass

        try:
            from app.services.langchain.graphs.costing_scope_extractor import (
                extract_costing_scope,
            )
            costing_scope = await extract_costing_scope(db, tender_id)
            logger.info(
                f"[costing scope] tender {tender_id}: extracted scope "
                f"(confidence={costing_scope.get('extraction_confidence')}, "
                f"tender_type={costing_scope.get('tender_type')}, "
                f"pricing_mechanism={costing_scope.get('pricing_mechanism')}, "
                f"source={costing_scope.get('_extraction_source')})"
            )
        except Exception as e:
            logger.warning(
                f"[costing scope] extraction failed for tender {tender_id}: {e} "
                f"— falling back to full PDF text"
            )
            try:
                db.rollback()
            except Exception:
                pass
            costing_scope = {}

    # Native-PDF document blocks: attach whenever we have a tender on
    # Anthropic. v2 — applied to BOTH BOQ-populated AND BOQ-empty paths so
    # the agent always sees the full PDF (matches claude.ai behaviour).
    pdf_doc_blocks: list[dict] = []
    if tender_id:
        from app.services.ai_service import (
            _get_agent_config as _get_costing_agent_config,
            _get_effective_model as _get_costing_effective_model,
        )
        from app.services.langchain.provider_config import detect_provider as _detect_provider

        _agent_cfg = _get_costing_agent_config(db, "costing_researcher")
        _model_name = _get_costing_effective_model(db, _agent_cfg)
        _provider = _detect_provider(_model_name)
        if _provider == "anthropic":
            pdf_doc_blocks = _build_tender_pdf_content_blocks(
                db, tender_id, max_docs=3,
            )
            logger.info(
                f"[costing pdf] tender {tender_id}: native PDF attach -> "
                f"{len(pdf_doc_blocks)} block(s) "
                f"(provider=anthropic, model={_model_name})"
            )
        else:
            logger.info(
                f"[costing pdf] tender {tender_id}: skipping native PDF "
                f"(provider={_provider}, model={_model_name})"
            )

    # Extract per-page text via the advanced parser (pdfplumber → Claude
    # vision → Tesseract OCR). Always computed when tender_id is set so
    # scanned-image PDFs still give the agent reliable scope text — without
    # this, Claude's native-PDF vision can fail on poor scans and the
    # agent refuses with "no extractable text". For text-searchable PDFs
    # this is essentially free (just pdfplumber). The extracted text is
    # always injected in the user message below.
    pdf_extracts: str = ""
    if tender_id:
        # Inject the PDF text dump so the agent reads the actual tender content
        # (this is what makes Agent Builder Test produce real costings). The cap
        # is setting-driven and sized for FULL NIT schedules: the single-call
        # path only runs when no structured BOQ was captured, and the old 30K
        # (~7.5K tokens) truncated mid-spares so the agent never saw rows
        # A744..A748/B7.. and invented a "summary" line. Default 120K (~30K
        # tokens) fits a full ~341-row schedule; the context-budget trim still
        # protects multi-100-page tenders downstream.
        from app.services.settings_service import get_setting_value
        try:
            _pdf_cap = int(get_setting_value(db, "costing.single_call_pdf_max_chars", 120000) or 120000)
        except Exception:
            _pdf_cap = 120000
        try:
            pdf_extracts = await asyncio.to_thread(
                _extract_tender_pdf_text, db, tender_id, 3, _pdf_cap,
            )
        except Exception as e:
            logger.warning(
                f"[costing] advanced PDF text extraction failed for tender "
                f"{tender_id}: {e}"
            )
            pdf_extracts = ""
        # The text extract exists for scanned pages, where the native PDF
        # block may be unreadable. When every extracted page came straight
        # off the PDF's own text layer, the attached document blocks already
        # carry that exact text and the extract would be the same documents
        # a second time. Keep it whenever any page needed vision or OCR, or
        # when fewer documents were attached than extracted.
        if pdf_doc_blocks and pdf_extracts and _extract_duplicates_blocks(
            pdf_extracts, len(pdf_doc_blocks)
        ):
            logger.info(
                f"[costing] tender {tender_id}: dropping {len(pdf_extracts)}-char "
                f"text extract — every page is text-layer and the same "
                f"{len(pdf_doc_blocks)} document(s) are attached as PDF blocks"
            )
            pdf_extracts = ""

    # Always extract the user's actual chat message so we can prepend it as
    # an explicit USER REQUEST block. Previously this was only used in
    # standalone (no-tender) mode, which meant the agent on the plan path
    # never saw the user's stated intent.
    original_messages = state.get("messages") or []
    user_request_text = ""
    for msg in reversed(original_messages):
        if hasattr(msg, "type") and msg.type == "human":
            user_request_text = (msg.content or "").strip()
            break
        if isinstance(msg, dict) and msg.get("role") == "human":
            user_request_text = (msg.get("content") or "").strip()
            break

    # Standalone mode (no tender_id, no analysis) — keep the legacy raw-message
    # behaviour so general costing chat still works.
    if not tender_id and user_request_text:
        user_message_parts = [user_request_text]
    else:
        user_message_parts = []
        if user_request_text:
            user_message_parts += [
                f"USER REQUEST:\n{user_request_text}",
                "",
            ]

        # Workstream 1 — inject the structured costing scope BEFORE the verbose
        # tender analysis blob. This is the agent's primary source of truth:
        # tender_type, pricing_mechanism, sites, equipment, BoQ items, penalty
        # clauses, payment terms — all pre-extracted by costing_scope_extractor
        # so the costing agent doesn't need to re-derive them from PDF text.
        if costing_scope and costing_scope.get("scope_summary"):
            from app.services.langchain.graphs.costing_scope_extractor import (
                format_scope_for_costing_prompt,
            )
            user_message_parts += [
                format_scope_for_costing_prompt(costing_scope),
                "",
            ]

        # Section 7 (Costing Basis Handoff) from the analyzer's markdown
        # report. This complements the structured scope above with the
        # analyst's verbatim quotes for pricing format, payment milestones,
        # LD clauses and PBG details. The structured scope tells the agent
        # WHAT the mechanism is; Section 7 lets it cite the exact clause
        # text when sizing margin / risk premium.
        section_7_block = _extract_section_7_from_analysis_markdown(db, tender_id)
        if section_7_block:
            user_message_parts += [section_7_block, ""]
            logger.info(
                f"[costing] tender {tender_id}: injected Section 7 verbatim "
                f"({len(section_7_block)} chars)"
            )

        user_message_parts += [
            f"Prepare a detailed cost estimate for {tender_ref}.",
        ]
        # Only emit the Tender Analysis block when there is actual content.
        # An empty JSON header (e.g. {"tender_summary": "", ...}) reads to the
        # agent as "the tender analysis says: nothing", which makes it punt to
        # needs_user_input even when the tender PDFs are perfectly usable.
        _summary_text = (analysis_result or {}).get("summary", "").strip()
        _has_requirements = any((requirements or {}).values()) if requirements else False
        if _summary_text or _has_requirements:
            user_message_parts += ["", f"Tender Analysis:\n{context}"]

        # Surface BOQ items prominently — these are the lines the agent must cost.
        # If the NIT-aware parser captured the structured schedule (any row has
        # item_code / schedule_name / basic_value / bidding_unit / escalation_pct /
        # is_tax_line), emit a "### BIDDING SCHEDULE" block with a JSON sidecar
        # to satisfy the costing prompt's RULE 5 (1:1 NIT mirror). Otherwise
        # fall back to the thin legacy rendering.
        if boq_items:
            has_nit_structure = any(
                bool(it.get("item_code"))
                or bool(it.get("schedule_name"))
                or it.get("basic_value") is not None
                or bool(it.get("bidding_unit"))
                or it.get("escalation_pct") is not None
                or bool(it.get("is_tax_line"))
                for it in boq_items
            )

            if has_nit_structure:
                # Render the FULL bidding schedule (no 200-row cap). Large
                # tenders are routed to the batched costing path; this
                # single-call path only runs for tenders at or under
                # costing.batch_size rows, so emitting every row here is safe.
                user_message_parts += ["", _render_bidding_schedule_block(boq_items)]
            else:
                # Legacy thin BOQ — keep prior behaviour. Agent treats this
                # as the freeform/non-NIT path (RULE 5 fall-through clause).
                boq_lines = [f"BOQ ITEMS — {len(boq_items)} line item(s) to cost:", ""]
                for item in boq_items:
                    sr = item.get("sr_no", "?")
                    desc = (item.get("description") or "").strip()[:200]
                    qty = item.get("quantity")
                    unit = item.get("unit") or ""
                    est = item.get("estimated_rate")
                    est_part = f" | tender estimate: ₹{est}" if est else ""
                    qty_part = f" | qty: {qty} {unit}".rstrip() if qty else ""
                    boq_lines.append(f"  {sr}. {desc}{qty_part}{est_part}")
                user_message_parts += ["", "\n".join(boq_lines)]
            if pdf_doc_blocks:
                user_message_parts += [
                    "",
                    "The full tender PDFs are ATTACHED ABOVE as document blocks. "
                    "Cross-reference each BOQ item with the actual scope, schedules "
                    "and any rate tables in the PDFs. Use the PDFs to derive rates "
                    "when training_data lookups fail — do NOT return "
                    "rate_source='needs_user_input' until you have read the relevant "
                    "scope section in the attached PDFs.",
                ]
            if pdf_extracts:
                user_message_parts += [
                    "",
                    f"TENDER PDF EXTRACTS ({len(pdf_extracts)} chars; "
                    f"per-page method tags shown in [brackets] — pdfplumber "
                    f"text / Claude vision / Tesseract OCR):",
                    pdf_extracts,
                ]
        else:
            # No structured BOQ. The agent has up to two sources of scope
            # data: native PDF document blocks (already attached above when
            # the provider is Anthropic) AND text extracted by the advanced
            # parser (pdfplumber + Claude vision + Tesseract OCR). We
            # include both whenever they're available so scanned-image PDFs
            # still produce a usable estimate even when Claude's native
            # vision misses content.
            if pdf_doc_blocks and pdf_extracts:
                user_message_parts += [
                    "",
                    "BOQ ITEMS: no structured BOQ table was found in the tender PDFs.",
                    "MANDATORY: derive line items DIRECTLY from the FULL TENDER "
                    "PDFs ATTACHED TO THIS MESSAGE as document blocks. The "
                    "PDF TEXT EXTRACTS below (pdfplumber + OCR fallback for "
                    "scanned pages) are provided as a reliable text source — "
                    "use them when the visual layout is hard to read. Produce "
                    "at least 5 line items covering the major scope buckets "
                    "you can identify (for an AMC: monthly preventive visits, "
                    "quarterly major service, annual overhaul, breakdown "
                    "attendance, consumables, transport, supervision, statutory "
                    "compliance). Use the TENDER TYPE PLAYBOOK in your system "
                    "prompt. Look up the rate via `costing_training_retrieval` "
                    "first; only mark rate_source='needs_user_input' if the "
                    "SPECIFIC RATE truly cannot be inferred or researched.",
                    "",
                    f"TENDER PDF EXTRACTS ({len(pdf_extracts)} chars; "
                    f"per-page method tags shown in [brackets]):",
                    pdf_extracts,
                ]
            elif pdf_doc_blocks:
                user_message_parts += [
                    "",
                    "BOQ ITEMS: no structured BOQ table was found in the tender PDFs.",
                    "MANDATORY: derive line items DIRECTLY from the FULL TENDER "
                    "PDFs ATTACHED TO THIS MESSAGE as document blocks. Read the "
                    "scope, schedules, and any rate tables in the PDFs to "
                    "produce at least 5 line items covering the major scope "
                    "buckets you can identify (e.g. for an AMC: monthly "
                    "preventive visits, quarterly major service, annual "
                    "overhaul, breakdown attendance, consumables, transport, "
                    "supervision, statutory compliance). Use the TENDER TYPE "
                    "PLAYBOOK in your system prompt to pick the right cost "
                    "structure for the tender type you identify here. "
                    "For each line, look up the rate via "
                    "`costing_training_retrieval` first; only mark "
                    "rate_source='needs_user_input' if the SPECIFIC RATE truly "
                    "cannot be inferred or researched (do NOT use it as a "
                    "shortcut to skip pricing the scope).",
                ]
            elif pdf_extracts:
                user_message_parts += [
                    "",
                    "BOQ ITEMS: no structured BOQ table was found in the tender PDFs.",
                    "MANDATORY: derive line items DIRECTLY from the PDF EXTRACTS "
                    "below (pdfplumber + OCR fallback for scanned pages). This "
                    "is a real DRPL bid — you MUST produce at least 5 line "
                    "items covering the major scope buckets you can identify "
                    "(for an AMC: monthly preventive visits, quarterly major "
                    "service, annual overhaul, breakdown attendance, "
                    "consumables, transport, supervision, statutory "
                    "compliance). Use the TENDER TYPE PLAYBOOK in your system "
                    "prompt to pick the right cost structure for the tender "
                    "type you identify here. Look up the rate via "
                    "`costing_training_retrieval` first; only mark "
                    "rate_source='needs_user_input' if the SPECIFIC RATE truly "
                    "cannot be inferred or researched (do NOT use it as a "
                    "shortcut to skip pricing the scope).",
                    "",
                    f"TENDER PDF EXTRACTS ({len(pdf_extracts)} chars; "
                    f"per-page method tags shown in [brackets]):",
                    pdf_extracts,
                ]
            else:
                user_message_parts += [
                    "",
                    "BOQ ITEMS: none could be auto-extracted from the tender PDFs.",
                    "MANDATORY: produce a useful response — do NOT return empty "
                    "arrays. Use the USER REQUEST and Tender Analysis above to "
                    "build whatever line items you can. If the scope is "
                    "genuinely too thin, return at least ONE needs_user_input "
                    "line item explaining EXACTLY what the user must share "
                    "(BOQ table, scope of work with quantities, drawing list, "
                    "AMC visit frequency, etc.) and mirror that question in "
                    "the `recommendations` array. Empty `line_items` is NOT "
                    "an acceptable response.",
                ]

            # Anti-summarisation guard (safety net for when BOQ extraction
            # genuinely returned 0 rows). NITs often contain dense,
            # individually-coded spares schedules (A7 → A71..A748, B7 →
            # B71..B7138). In freeform mode the agent tends to collapse these
            # into one "A7-Summary" row — forbid that: every coded row is a
            # priceable line item.
            if pdf_doc_blocks or pdf_extracts:
                user_message_parts += [
                    "",
                    "ITEMISE EVERY CODED ROW (CRITICAL): if the tender shows a "
                    "schedule of items with individual item codes (e.g. A1..A6, "
                    "A71..A748, B71..B7138, C71.., D71..), you MUST emit EXACTLY "
                    "ONE line item per coded row — carry the item code and its "
                    "schedule onto each line. NEVER collapse, aggregate, or "
                    "summarise a coded spares schedule (A7/B7/C7/D7 etc.) into a "
                    "single '…-Summary' / 'aggregate' row, and never write "
                    "'N-line aggregate'. A 341-row NIT must yield 341 line items. "
                    "The 'at least 5 line items' floor above applies ONLY to "
                    "genuinely unstructured scopes that have no coded schedule.",
                ]

    if clarification_answers:
        answers_text = "\n".join(f"- {k}: {v}" for k, v in clarification_answers.items())
        user_message_parts += [
            "",
            f"User clarifications provided:\n{answers_text}",
        ]

    user_message_parts += [
        "",
        "Research order (training_data → memory → tender_estimate → web_search → derived_estimate). Do NOT narrate tool calls.",
        "Every priced line item MUST carry: a SINGLE `rate` (best-evidence unit cost), a SINGLE `amount` (= quantity × rate, transcribed from cost_calculator output per RULE 4), `profit_pct: null`, `rate_source`, `source_ref`, `confidence`. NO ranges, NO margin fields. The user adds margin/overhead/GST in the editor.",
        "BEFORE writing your final JSON, do the manpower + resource decomposition (emit it as `manpower_resource_analysis`) — every line_item must trace back to one row of that decomposition.",
        "Then call `cost_calculator` EXACTLY ONCE with the full line_items array and {margin_percent: 0, overhead_percent: 0, gst_percent: 0}. Copy each returned `line_amounts[i].amount_expected` verbatim into the corresponding line item's `amount` field. Leave the top-level `totals` object null — the backend computes totals after the user applies margin/overhead/GST.",
        "Use needs_user_input ONLY as a last resort, after exhausting training_data → web_search → derived_estimate. A defensible single rate with documented assumptions beats a refusal.",
        "Group recommendations into '**Items needing your input**' (only for genuine needs_user_input) and '**Web-researched items to verify**'.",
        "",
        "ZERO-LINE-ITEMS IS A FAILURE MODE — never return an empty `line_items` array.",
        "RANGE-RATE LINES ARE A FAILURE MODE — every priced line gets ONE `rate` and ONE `amount`. NO `rate_low`/`rate_expected`/`rate_high`. NO `profit_amount_*`. `profit_pct` is always `null`.",
        "TRAINING-DATA-AS-PROSE IS A FAILURE MODE — if training data has scope-equivalent rates, convert each row into a priced `line_item` with rate_source='training_data'. Do NOT summarise training rates in `recommendations`.",
        "ONE-NEEDS-INPUT-LINE-WHEN-DATA-EXISTS IS A FAILURE MODE — if you found benchmark rates (training_data or web_search) for THIS scope, you must emit a full breakdown using those rates. A single needs_user_input row in that case = giving up despite having the data.",
        "RECOMMENDATIONS-AS-STATUS-REPORT IS A FAILURE MODE — `recommendations` is a SHORT bullet list (≤25 words each, no `##` headings, no multi-paragraph prose, no tables). Long analyses go into `line_items` (rates) or `assumptions` (caveats).",
        "'BOQ NOT INCLUDED — DOWNLOAD FROM IREPS' is tender mechanics, NOT a refusal trigger. Build the estimate from training data + PDF scope, and add 'recommend cross-check against IREPS BOQ' to `assumptions`.",
        "",
        "If you cannot price ANYTHING after exhausting research, return at least one line with",
        "rate_source='needs_user_input' explaining the SPECIFIC scope info you need.",
        "The template below is a SCHEMA reference — never copy it verbatim.",
        "",
        "FINAL MESSAGE CONTRACT (strict):",
        "Your FINAL assistant message MUST contain ONLY a JSON object wrapped between",
        "the exact markers COSTING_JSON_START and COSTING_JSON_END. No prose, no",
        "markdown fences, no <tool_name>...</tool_name> syntax.",
        "",
        "MARGIN-ANALYSIS MODE: when the BOQ block above shows `tender estimate: ₹X` per",
        "line, this tender has stated BOQ rates — emit `tender_rate` + `tender_amount` per",
        "line (the backend computes margin once the user applies their markup) and a",
        "top-level `strategic_summary` object (tender snapshot + schedule_breakdown +",
        "key_observations + recommended bid). Group by `schedule_section` so all lines in",
        "one schedule are contiguous.",
        "",
        "EMIT FIELDS IN THIS EXACT ORDER (essentials FIRST so truncation can't lose them;",
        "audit trail LAST as optional):",
        "",
        "COSTING_JSON_START",
        "{",
        '  "line_items": [',
        '    {',
        '      "sr_no": 1,',
        '      "description": "...",',
        '      "category": "material|labour|equipment|transport|overhead|sub_contract|consumable",',
        '      "schedule_section": "Schedule A — Part 1 Service & Repair",  /* group rows by this for multi-schedule tenders */',
        '      "quantity": 0,',
        '      "unit": "...",',
        '      "tender_rate": null,     /* OMIT or null unless margin-analysis mode (tender stated a rate) */',
        '      "tender_amount": null,   /* = tender_rate × quantity when applicable */',
        '      "rate": 0,               /* single best-evidence unit cost (NO ranges) */',
        '      "amount": 0,             /* = quantity × rate, transcribed from cost_calculator.line_amounts[i].amount_expected */',
        '      "profit_pct": null,      /* ALWAYS null — user sets margin in the editor */',
        '      "rate_source": "training_data|tender_estimate|web_search|memory|derived_estimate|needs_user_input",',
        '      "source_ref": "citation / URL / build-up formula / question",',
        '      "cost_buildup_note": "Item A.1 + A.2 + A.5 + 2 hr labour @ D.2",  /* one short sentence */',
        '      "confidence": "high|medium|low"',
        '    }',
        '  ],',
        '  "totals": null,            /* leave null — backend computes after user applies margin/overhead/GST */',
        '  "strategic_summary": {     /* REQUIRED in margin-analysis mode; lighter for chat-only ad-hoc */',
        '    "tender_snapshot": {"tender_no": "...", "tender_value_inr": 0, "scope_one_liner": "...", "period": "...", "depots_or_locations": [], "emd_inr": 0, "performance_guarantee": "...", "bid_validity_days": 0, "penalty_cap_pct_of_contract": 0, "min_eligibility": []},',
        '    "schedule_breakdown": [{"schedule": "Schedule A — ...", "tender_value_inr": 0, "estimated_cost_inr": 0}],',
        '    "key_observations": ["3-6 short ACTIONABLE bullets — which line is gold, which schedule risky, where to push margin"],',
        '    "recommended_bid_strategy": "one sentence with a concrete bid range or instruction"',
        '  },',
        '  "rate_sources": [],',
        '  "assumptions": [],',
        '  "recommendations": [],',
        '  "cost_assumptions": [   /* rate-card library matching reference Sheet 2; group by section */',
        '    {"section": "A. Materials — Kits & Consumables", "item": "...", "rate_inr": 0, "uom": "drum|kit|set|ea", "source_ref": "vendor + date OR DRPL training data ref"}',
        '  ],',
        '  "manpower_resource_analysis": [   /* OPTIONAL — safe to omit if approaching token budget */',
        '    {',
        '      "scope_bucket": "...",',
        '      "manpower": [{"role": "Fitter", "headcount": 0, "deployment": "X days/month × Y months", "rate_inr": 0, "rate_unit": "per day basic + 24% statutory"}],',
        '      "resources": [{"type": "material|equipment|consumable|transport|overhead", "item": "...", "quantity_per_cycle": "...", "frequency": "...", "rate_inr": 0, "rate_unit": "..."}],',
        '      "volume_drivers": ["..."]',
        '    }',
        '  ]',
        "}",
        "COSTING_JSON_END",
    ]

    user_message = "\n".join(user_message_parts)

    # ─── Phase 2: pre-flight context-budget trim ────────────────────────
    # Estimate total input tokens BEFORE invoking Anthropic. If we're over
    # budget, run the deterministic trim sequence (drop annexed PDFs when a
    # bidding schedule exists → cap training data → drop PDF text extracts
    # → cap BOQ sidecar). Each trim is recorded in `trim_notes` for the
    # streaming layer to render as a one-line warning bubble. See plan:
    # now-i-need-to-synchronous-taco.md (Phase 2).
    trim_notes: list[str] = []
    try:
        from app.services.langchain.context_budget import (
            _NIT_CLASS_DOC_TYPES,
            BudgetConfig,
            TrimContext,
            apply_trim_policy,
            estimate_pdf_block_tokens,
            estimate_text_tokens,
            log_budget_event,
        )

        _budget_cfg = BudgetConfig.load(db)
        has_bidding_schedule = "### BIDDING SCHEDULE" in user_message

        _trim_ctx = TrimContext(
            system_prompt_base_chars=len(base_system_prompt),
            user_message_other_chars=max(0, len(user_message) - len(pdf_extracts or "")),
            training_data_text=training_context or "",
            pdf_blocks=list(pdf_doc_blocks),
            pdf_extracts_text=pdf_extracts or "",
            bidding_schedule_block_text="",  # already inside user_message
            bidding_schedule_sidecar_text="",  # already inside user_message
            tender_analysis_text="",  # already inside user_message
            section_7_text="",  # already inside user_message
            has_bidding_schedule=has_bidding_schedule,
        )

        _pre_estimate = (
            len(base_system_prompt) // 4
            + len(user_message) // 4
            + estimate_pdf_block_tokens(pdf_doc_blocks)
            + estimate_text_tokens(training_context)
        )
        logger.info(
            f"[context_budget] tender {tender_id}: pre-trim estimate "
            f"~{_pre_estimate} tokens (target={_budget_cfg.target_tokens}, "
            f"hard_floor={_budget_cfg.hard_floor_tokens}, "
            f"has_bidding_schedule={has_bidding_schedule})"
        )

        if _pre_estimate > _budget_cfg.target_tokens:
            _trim_ctx, trim_notes, before_t, after_t = apply_trim_policy(
                _trim_ctx, _budget_cfg
            )
            log_budget_event(
                tender_id=tender_id,
                agent_key="costing_researcher",
                before_tokens=before_t,
                after_tokens=after_t,
                trims_applied=trim_notes,
            )

            # Apply the trim outcomes back onto the live inputs.
            #
            # 1. PDF blocks — if the policy dropped annexed PDFs, also
            #    rebuild from scratch using doc_type_allowlist so the
            #    metadata is fresh AND the function honours the same
            #    NIT-class filter used by the policy.
            if len(_trim_ctx.pdf_blocks) < len(pdf_doc_blocks):
                pdf_doc_blocks = _build_tender_pdf_content_blocks(
                    db,
                    tender_id,
                    max_docs=3,
                    # The policy's OWN set, imported rather than
                    # restated: this rebuild runs because the policy just
                    # shed annexed PDFs, so a wider allowlist here would
                    # re-attach them and make the trim a no-op.
                    doc_type_allowlist=_NIT_CLASS_DOC_TYPES,
                )
                logger.info(
                    f"[context_budget] rebuilt pdf_doc_blocks with NIT-class "
                    f"allowlist → {len(pdf_doc_blocks)} block(s)"
                )

            # 2. Training context — if the policy truncated it, re-run
            #    _build_system_prompt with the smaller value so the system
            #    prompt that goes to the agent matches the budgeted size.
            if _trim_ctx.training_data_text != (training_context or ""):
                training_context = _trim_ctx.training_data_text
                base_system_prompt = _build_system_prompt(
                    mode, training_context, db=db
                )
                system_prompt = base_system_prompt + "\n\n" + _budget_block
                # Re-create the agent with the updated system prompt.
                agent = create_react_agent(
                    model=llm,
                    tools=tools,
                    prompt=_system_message_for_model(_built["model_name"], system_prompt),
                )

            # 3. PDF extracts — if the policy cleared them, strip the
            #    relevant block from user_message. The TENDER PDF EXTRACTS
            #    section uses a deterministic header so a substring drop
            #    is safe.
            if not _trim_ctx.pdf_extracts_text and pdf_extracts:
                _marker = "TENDER PDF EXTRACTS"
                _idx = user_message.find(_marker)
                if _idx != -1:
                    user_message = user_message[:_idx].rstrip() + (
                        "\n\n(PDF text extracts were dropped to fit the "
                        "context budget — the structured bidding schedule "
                        "above is the source of truth.)"
                    )
                pdf_extracts = ""

            logger.info(
                f"[context_budget] tender {tender_id}: post-trim "
                f"pdf_blocks={len(pdf_doc_blocks)} "
                f"training_chars={len(training_context or '')} "
                f"user_message_chars={len(user_message)}"
            )
    except Exception as _budget_err:
        # The pre-flight trim must NEVER block a costing run. If it errors,
        # log and proceed with the un-trimmed inputs — Anthropic will reject
        # if too large, and Phase 3's typed error event still surfaces it.
        logger.warning(
            f"[context_budget] pre-flight trim failed (non-fatal): "
            f"{type(_budget_err).__name__}: {_budget_err}"
        )
        trim_notes = []

    # When we have native PDF blocks, send a multimodal HumanMessage so the
    # model reads the full PDFs the way claude.ai does. langchain-anthropic
    # forwards a content list of {"type":"document",...} + {"type":"text",...}
    # parts straight through to Anthropic's content-blocks API.
    if pdf_doc_blocks:
        # Strip internal metadata before sending to Anthropic.
        _clean_blocks = _strip_internal_block_metadata(pdf_doc_blocks)
        human_content = _clean_blocks + [{"type": "text", "text": user_message}]
    else:
        human_content = user_message

    logger.info(
        f"[costing] tender {tender_id}: invoking ReAct agent — "
        f"multimodal={bool(pdf_doc_blocks)}, pdf_blocks={len(pdf_doc_blocks)}, "
        f"boq_items={len(boq_items)}, user_message_chars={len(user_message)}, "
        f"trim_notes={len(trim_notes)}"
    )

    try:
        result = await _ainvoke_with_retry(
            agent,
            {"messages": [HumanMessage(content=human_content)]},
            # Bumped recursion_limit from LangGraph default 25 → 40 so the
            # agent has room for per-line web-research round-trips
            # (anonymizing_web_search calls + corroboration). Each priced
            # line typically needs 2-3 tool calls when training data
            # doesn't cover it; with 15-25 priced lines this would otherwise
            # hit the cap mid-research. Capped at 40 to bound cost.
            config={"callbacks": [callback], "recursion_limit": 40},
        )

        agent_messages = result.get("messages", [])
        final_content = agent_messages[-1].content if agent_messages else ""
        final_content = _strip_tool_tags(final_content)

        # ── Refusal-detection retry ────────────────────────────────────────
        # When the agent has SOME usable scope context but its final response
        # is empty / unparseable / prose-style ("I cannot price this without
        # the BOQ, please share..."), claude.ai handles the same input by
        # making best-effort estimates. The system prompt already instructs
        # the agent NOT to refuse, but in practice it sometimes gives up
        # anyway. Detect that failure here and re-invoke the agent with a
        # stronger nudge — this closes the gap with claude.ai's behavior.
        #
        # Retry whenever ANY input signal is present. Native PDF blocks are
        # the strongest, but extracted text, BOQ items, or a populated
        # analysis_result are all signals that the agent has data to work
        # with — refusing in any of those cases is the bug we want to fix.
        _input_signal_strength = {
            "pdf_doc_blocks": len(pdf_doc_blocks or []),
            "pdf_extracts_chars": len(pdf_extracts or ""),
            "boq_items": len(boq_items or []),
            "analysis_summary_chars": len(
                (analysis_result or {}).get("summary") or ""
            ),
            "tender_id": tender_id,
        }
        has_input_signal = (
            (_input_signal_strength["pdf_doc_blocks"] or 0) > 0
            or (_input_signal_strength["pdf_extracts_chars"] or 0) > 500
            or (_input_signal_strength["boq_items"] or 0) > 0
            or (_input_signal_strength["analysis_summary_chars"] or 0) > 200
            or bool(tender_id)
        )
        agent_refused = _looks_like_costing_refusal(final_content)

        # Always log the decision so we can verify in production logs that
        # the new code path is live and behaving as expected.
        logger.info(
            f"[costing] tender {tender_id}: refusal_check — "
            f"has_input_signal={has_input_signal}, agent_refused={agent_refused}, "
            f"signals={_input_signal_strength}, "
            f"final_content_len={len(final_content)}"
        )

        if has_input_signal and agent_refused:
            logger.warning(
                f"[costing] tender {tender_id}: agent gave up despite input "
                f"signal — retrying with 'do not refuse' nudge "
                f"(signals={_input_signal_strength})"
            )
            # Surface the recovery to the UI via the reliability event pipe
            # (Layer 1c). Users see a "Recovering empty costing..." indicator
            # rather than a frozen screen.
            from app.services.ai_service import _emit_reliability_event
            _emit_reliability_event(None, "assistant_retried_empty", {
                "agent": "costing_researcher",
                "stop_reason": "agent_refused_to_estimate",
            })

            retry_nudge = (
                "Your previous response did not contain priceable line items. "
                "The tender provides enough information to make a defensible "
                "estimate — re-read the TENDER PDF EXTRACTS / scope, identify "
                "the dominant scope buckets (manpower, materials, transport, "
                "consumables, supervision), and produce line items with a "
                "single `rate` and `amount = qty × rate` per line, using "
                "`training_data`, `web_search`, or `derived_estimate` as the "
                "rate source. Set `profit_pct: null` on every line — the user "
                "adds margin in the editor. Document inferences in "
                "`assumptions`. Do NOT emit a single needs_user_input row — "
                "asking the user for the BOQ is exactly what the system prompt "
                "tells you not to do. Produce the JSON output now, with at "
                "least the manpower + materials + overheads buckets priced."
            )
            retry_messages = list(agent_messages) + [
                HumanMessage(content=retry_nudge)
            ]
            try:
                retry_result = await _ainvoke_with_retry(
                    agent,
                    {"messages": retry_messages},
                    config={"callbacks": [callback], "recursion_limit": 40},
                )
                retry_messages_out = retry_result.get("messages", [])
                retry_content = (
                    retry_messages_out[-1].content if retry_messages_out else ""
                )
                retry_content = _strip_tool_tags(retry_content)
                if retry_content and not _looks_like_costing_refusal(retry_content):
                    logger.info(
                        f"[costing] tender {tender_id}: retry produced usable "
                        f"output (raw_len={len(retry_content)})"
                    )
                    final_content = retry_content
                else:
                    logger.warning(
                        f"[costing] tender {tender_id}: retry also returned "
                        f"empty/refusal — falling through to fallback"
                    )
            except Exception as retry_err:
                logger.warning(
                    f"[costing] tender {tender_id}: retry invocation failed: "
                    f"{type(retry_err).__name__}: {retry_err}"
                )
                # Fall through with the original final_content; the parse layer
                # will synthesize the canned needs_user_input fallback.

        return {
            "messages": [AIMessage(content=final_content)],
            "costing_result": {
                "_raw_response": final_content,
                "_pdf_block_count": len(pdf_doc_blocks),
                # Phase 2 — surface trim notes through the state machine so
                # the streaming layer can emit a typed agent_warning.
                "trim_notes": trim_notes,
            },
            "_callback": callback,
        }

    except Exception as e:
        status = (
            getattr(e, "status_code", None)
            or getattr(getattr(e, "response", None), "status_code", None)
        )
        logger.error(
            f"Costing ReAct agent failed (status={status}, "
            f"type={type(e).__name__}, pdf_blocks={len(pdf_doc_blocks)}): {e!r}",
            exc_info=True,
        )
        # Synthesize a structured fallback so the renderer shows a clear
        # "agent failed, here's what to do" Cost Estimate table — never the
        # bare "produced no line items" diagnostic. The graph routes status=
        # "failed" past parse_and_return_node, so the synthesis must happen
        # here. See plan: drpl-platform "no line items" regression fix.
        err_label = type(e).__name__
        err_msg = str(e)[:300] or "(no error message)"
        explanation = (
            f"The costing agent encountered an error before it could produce "
            f"a cost breakdown: `{err_label}: {err_msg}`. Try again — large "
            f"PDFs or transient AI provider errors are the usual causes. If "
            f"the issue persists, share the BOQ / scope-of-work directly in "
            f"chat (paste the table or describe the items) so the agent can "
            f"work from the chat context."
        )
        fallback_costing = {
            "line_items": [
                {
                    "sr_no": 1,
                    "description": "Costing agent failed — retry or share scope manually",
                    "category": "overhead",
                    "quantity": None,
                    "unit": None,
                    "rate": None,
                    "amount": None,
                    "rate_source": "needs_user_input",
                    "source_ref": explanation,
                    "confidence": "low",
                }
            ],
            "subtotal": 0,
            "overheads": 0,
            "profit_margin": 0,
            "gst": 0,
            "grand_total": 0,
            "travel_costs": None,
            "rate_sources": [],
            "assumptions": [
                f"Auto-generated fallback — costing agent raised "
                f"{err_label} (status={status}, pdf_blocks={len(pdf_doc_blocks)})."
            ],
            "recommendations": [explanation],
            "_pdf_block_count": len(pdf_doc_blocks),
            "_agent_error": f"{err_label}: {err_msg}",
        }
        # If the failure was a context overflow, surface that distinctly so
        # the streaming layer emits a `context_overflow` agent_error (not
        # the generic "agent failed" one). Phase 3 reads this code.
        _overflow_codes = ("context_length", "input is too long", "too many tokens")
        _is_overflow = any(c in str(e).lower() for c in _overflow_codes)
        fallback_costing["trim_notes"] = trim_notes
        if _is_overflow:
            fallback_costing["_overflow_after_trim"] = True
        return {
            "errors": [AIMessage(content=str(e))],
            "costing_result": fallback_costing,
            "status": "failed",
            "_callback": callback,
        }


# ── Node: component build-up costing (ratecard-driven) ────────────────────────

async def run_costing_component_node(state: CostingAgentState, db: Session) -> dict:
    """Bottom-up component build-up costing.

    Reuses the single-call ReAct node verbatim — same agent, tools, model,
    training context — but flips `_component_mode` on so the system prompt gains
    the COMPONENT BUILD-UP section instructing the agent to expand each
    engine/check scope item into its constituent ratecard parts via the
    `ratecard_lookup` tool. The output flows through the same parse_and_return
    path; persistence (with NIT validation skipped) happens at the caller.
    """
    new_state = dict(state)
    new_state["_component_mode"] = True
    logger.info(
        f"[costing] tender {state.get('tender_id')}: running COMPONENT build-up "
        f"costing (ratecard-driven)"
    )
    return await run_costing_react_node(new_state, db)


# ── Node: batched costing (large tenders) ─────────────────────────────────────

async def run_costing_batched_node(state: CostingAgentState, db: Session) -> dict:
    """Batched costing for large tenders (more BOQ rows than costing.batch_size).

    1. Build a deterministic CostBreakdown SKELETON from the captured BOQItem
       rows (every line present, 1:1 NIT mirror, `tender_rate` from the NIT's
       published rate → margin-analysis mode). This guarantees full coverage
       regardless of any LLM output-token limit — no row can be silently
       dropped or lost to a truncated JSON blob.
    2. Cost the non-tax rows in batches of costing.batch_size, merging each
       batch's researched rates into the existing skeleton rows. Commit per
       batch → resumable / idempotent.
    3. Recompute totals and return a costing_result flagged `_already_persisted`
       so the chat wrapper does NOT create a duplicate breakdown version.
    """
    import math
    from app.services import cost_breakdown_service as cbs
    from app.services.settings_service import get_setting_value
    from app.services.ai_service import _emit_reliability_event

    tender_id = state.get("tender_id")
    mode = state.get("mode", "pipeline")
    analysis_result = state.get("analysis_result") or {}
    training_context = state.get("training_context", "")
    proposal_session_id = state.get("proposal_session_id")

    batch_size = int(get_setting_value(db, "costing.batch_size", 60) or 60)
    # In-code fallback matches the seeded default (2000) so the guardrail is
    # correct even on a DB where platform settings haven't been seeded yet
    # (seed_defaults runs on admin-settings load, not at app boot).
    max_line_items = int(get_setting_value(db, "costing.max_line_items", 2000) or 2000)

    # Build the BOQ row dicts from BOQItem directly (same source + order as the
    # skeleton) so this node works in both chat and pipeline modes regardless of
    # whether analysis_result was hydrated with boq_items.
    from app.models.costing_template import BOQItem as _BOQItem
    _boq_rows = (
        db.query(_BOQItem)
        .filter(_BOQItem.tender_id == tender_id)
        .order_by(_BOQItem.schedule_name.asc().nullsfirst(), _BOQItem.sr_no.asc())
        .all()
    )
    boq_items = [
        {
            "boq_item_id": it.id,
            "sr_no": it.sr_no,
            "item_code": it.item_code,
            "description": it.description,
            "quantity": it.quantity,
            "unit": it.unit,
            "estimated_rate": it.estimated_rate,
            "basic_value": it.basic_value,
            "escalation_pct": it.escalation_pct,
            "bidding_unit": it.bidding_unit,
            "schedule_name": it.schedule_name,
            "is_tax_line": bool(it.is_tax_line),
            "annexure_ref": getattr(it, "annexure_ref", None),
            "parent_item_id": getattr(it, "parent_item_id", None),
        }
        for it in _boq_rows
    ]
    _annotate_component_rows(boq_items)

    # 1) Deterministic skeleton — every NIT line, 1:1 mirror.
    breakdown = cbs.build_skeleton_from_boq(
        db, tender_id, session_id=proposal_session_id,
    )
    if breakdown is None:
        # Unreachable on the batched route (router requires a captured schedule),
        # but be safe: fall back to the single-call path and parse its output.
        logger.warning(
            f"[costing batched] tender {tender_id}: no BOQ skeleton — falling "
            f"back to single-call costing"
        )
        single = await run_costing_react_node(state, db)
        merged_state = {**state, "costing_result": single.get("costing_result", {})}
        return parse_and_return_node(merged_state)

    # Non-tax rows are the ones that need a researched rate. A captured
    # schedule with ZERO of them means every row was classified a tax line —
    # nothing gets costed and the run "succeeds" having priced nothing, so say
    # so loudly rather than returning a bare "[NEEDS RATE]" skeleton.
    cost_rows, no_costable_note = split_costable_rows(boq_items)
    if no_costable_note:
        logger.error(f"[costing batched] tender {tender_id}: {no_costable_note}")
        _emit_reliability_event(None, "costing_no_costable_rows", {
            "agent": "costing_researcher",
            "tender_id": tender_id,
            "captured_rows": len(boq_items),
        })
    # The cap is a guardrail against pathological extractions (thousands of
    # spurious rows), NOT a normal operating limit — the default is set well
    # above any real tender. When it trips it is surfaced loudly (reliability
    # event + an assumptions note) so capping is never silent.
    cap_note: Optional[str] = None
    _costable_total = len(cost_rows)
    if max_line_items and _costable_total > max_line_items:
        cap_note = (
            f"⚠️ This tender has {_costable_total} costable line items, which "
            f"exceeds the safety ceiling costing.max_line_items={max_line_items}. "
            f"Only the first {max_line_items} were auto-costed; the remaining "
            f"{_costable_total - max_line_items} are left as 'needs input'. Raise "
            f"costing.max_line_items to auto-cost them all."
        )
        logger.warning(f"[costing batched] tender {tender_id}: {cap_note}")
        _emit_reliability_event(None, "costing_cap_hit", {
            "agent": "costing_researcher",
            "tender_id": tender_id,
            "costable_total": _costable_total,
            "max_line_items": max_line_items,
            "capped_off": _costable_total - max_line_items,
        })
        cost_rows = cost_rows[:max_line_items]

    # 1b) Rows that print a published rate and have no firm rate data behind
    # them are built up by the platform itself (costing/cost_buildup.py), not
    # sent to the agent's batches, whose figure for them was a guess. They
    # keep the market research. See app/services/costing/model_routing.py.
    from app.models.cost_breakdown import CostBreakdownLine as _CBL
    from app.services.costing import cost_buildup as _cb
    from app.services.costing.model_routing import route_rows_for_model
    _skeleton = (
        db.query(_CBL.boq_item_id, _CBL.tender_rate, _CBL.quantity)
        .filter(_CBL.cost_breakdown_id == breakdown.id)
        .all()
    )
    buildup_cfg = _cb.read_settings(db)
    buildup_key = _buildup_api_key(db) if buildup_cfg.enabled else None
    routing = route_rows_for_model(
        db, cost_rows,
        published_rates={b: tr for b, tr, _q in _skeleton if b is not None},
        quantities={b: q for b, _tr, q in _skeleton if b is not None},
        training_chars=len(training_context or ""),
        buildup_available=bool(buildup_key),
    )
    model_rows = routing.model_rows
    if routing.skipped:
        logger.info(
            f"[costing batched] tender {tender_id}: {routing.skipped} row(s) print a "
            f"published rate and {routing.reason}; the platform builds up their cost "
            f"itself instead of sending them to the costing agent -- market research "
            f"still runs for them, and a verified market price still wins"
        )
    elif routing.reason:
        logger.info(
            f"[costing batched] tender {tender_id}: every row goes to the model "
            f"({routing.reason})"
        )

    # 1c) What each schedule prices (its printed banner), for the agent and
    # the platform's build-up alike, and the build-up's own inputs.
    from app.services.costing.schedule_context import schedule_contexts
    schedules = schedule_contexts(db, tender_id)
    schedule_titles = {code: ctx.title for code, ctx in schedules.items()}
    published_by_id = {b: tr for b, tr, _q in _skeleton if b is not None}
    from app.services.costing_format_service import get_costing_defaults as _gcd
    _cost_defaults = _gcd(db)
    buildup_context = (
        _buildup_tender_context(db, tender_id, analysis_result, schedules,
                                state.get("anonymization_map") or {})
        if buildup_key else ""
    )

    # 2) A compact, batch-invariant context preamble. The agent itself is
    # built per batch, on that batch's own DB session (see _cost_batch).
    requirements = analysis_result.get("requirements", {})
    scope_context = json.dumps({
        "tender_summary": (analysis_result.get("summary") or "")[:3000],
        "technical_specifications": requirements.get("technical_specs", []),
        "financial_requirements": requirements.get("financial", []),
    }, default=str)[:4000]

    max_sweeps = int(get_setting_value(db, "costing.costing_max_sweeps", 2) or 2)
    concurrency = _batch_concurrency(db)

    # Wall-clock budget. Same inputs as the outer `asyncio.wait_for` in
    # `_run_enhanced_costing_research_inner` (every captured row, the batch
    # size, the sweep count, the concurrency), so the two agree; this node
    # stops issuing work `_FINALIZE_RESERVE_S` before that timer fires and
    # returns what the batches saved. Before: four batches of sixty ran one
    # after another under a 2,880 s budget that the RQ job (1,800 s) never
    # honoured -- batch 3/4 finished at ~29 min and the job died with nothing.
    budget_s = _costing_timeout_seconds(
        len(boq_items), batch_size, max_sweeps,
        concurrency=concurrency, cap=_costing_budget_cap_s(),
    )
    started_at = time.monotonic()
    deadline = started_at + max(_MIN_BATCH_SECONDS, budget_s - _FINALIZE_RESERVE_S)

    def _remaining() -> float:
        return deadline - time.monotonic()

    total_batches = max(1, math.ceil(len(model_rows) / batch_size)) if model_rows else 0
    logger.info(
        f"[costing batched] tender {tender_id}: skeleton v{breakdown.version} "
        f"({len(boq_items)} rows); costing {len(model_rows)} of {len(cost_rows)} "
        f"non-tax row(s) with the model in {total_batches} batch(es) of {batch_size}, "
        f"{concurrency} at a time; budget {budget_s}s, batches stop at "
        f"{int(deadline - started_at)}s"
    )
    if total_batches:
        _emit_reliability_event(None, "costing_batch_progress", {
            "agent": "costing_researcher",
            "tender_id": tender_id,
            "batch": 0,
            "total_batches": total_batches,
            "lines_done": 0,
            "lines_total": len(model_rows),
        })

    accumulated_assumptions: list[str] = []
    accumulated_cost_assumptions: list[dict] = []
    accumulated_manpower: list[dict] = []
    if no_costable_note:
        accumulated_assumptions.append(no_costable_note)
    if cap_note:
        accumulated_assumptions.append(cap_note)
    total_matched = 0
    total_unmatched = 0
    batches_done = 0
    lines_done = 0
    out_of_time = False

    # Shared 1:1-contract tail appended to every batch instruction (main loop
    # AND the needs_input sweep) so both use the identical output shape.
    _instruction_tail = (
        "Cost ONLY the line item(s) in the BIDDING SCHEDULE below — do NOT add, "
        "drop, merge, or emit any rows outside this batch. Emit a JSON object "
        "(between the COSTING_JSON markers) with ONLY the `line_items` array "
        "(exactly one row per schedule row, carrying boq_item_id AND item_code "
        "verbatim), plus optional `cost_assumptions` and `assumptions`. Use a "
        "single `rate` and `amount = qty × rate` per line, `profit_pct: null`. "
        "Call `cost_calculator` exactly once for this batch before emitting the JSON. "
        "CRITICAL (RULE 2b): schedule rows show NO published rate — it is withheld "
        "so that your cost is independent; the platform compares your cost with the "
        "tender's rate after you answer. Find the firm's INDEPENDENT Est. Unit Cost "
        "(training_data → web_search → derived_estimate). Never force a margin, and "
        "never guess the tender's rate to work backwards from it. Any rate computed "
        "from a published rate (no 'published ÷ 1.25', no 'stripping N% margin/overhead "
        "off published', no '% of published') is rejected and re-costed. A derived_estimate is a first-principles "
        "build-up (material weight × market rate + manhours × loaded rate + bought-out "
        "parts) and its rate is that build-up's sum. "
        "MARKET PRICES: `anonymizing_web_search` returns real, cited prices from Indian "
        "sellers. Use it for items sold in the market -- cables and wires, light "
        "fittings, switchgear, transformers, heaters, valves, sanitaryware, fasteners, "
        "sections and sheet per kg or metre. Group like items into one search, at most "
        "15 searches in this batch. When a result prices the SAME item (spec, size, "
        "unit), set rate_source='web_search', put that page's URL in `source_url` and "
        "the quoted price in `source_ref`. Never price an RDSO-specification or "
        "drawing-specific item from a generic product. Drawing-specific parts and "
        "labour lots have no market price: build them up. "
        "`rate_source` for these non-tax lines MUST be one of training_data / "
        "web_search / memory / derived_estimate — NEVER 'tender_estimate'. "
        + _COMPONENT_RESEARCH_RULE
    )

    _slots = asyncio.Semaphore(concurrency)
    # Read once, before the batches run, so no coroutine touches the node's
    # session while other sessions are writing.
    breakdown_id = breakdown.id

    async def _cost_batch(batch: list, instruction: str, label: str) -> None:
        """Render → invoke → parse → merge one batch into the skeleton.

        Shared by the main batch loop and the needs_input sweep so both honour
        the identical 1:1 NIT-mirror contract, parsing, and merge. Accumulates
        narrative fields + match counters. Failures are caught + logged; the
        batch's rows stay needs_input (the sweep re-attempts them).

        Runs `concurrency` at a time. Each batch gets its OWN SQLAlchemy
        session for its agent (the web-search tool and the callback read
        settings from executor threads) and for its merge, so no two batches
        -- and neither of them and this node -- share a session across
        threads. The node's `db` is idle while batches are in flight and is
        expired before it is read again.
        """
        nonlocal total_matched, total_unmatched, batches_done, lines_done, out_of_time
        if not batch:
            return
        async with _slots:
            remaining = _remaining()
            if remaining < _MIN_BATCH_SECONDS:
                out_of_time = True
                logger.warning(
                    f"[costing batched] tender {tender_id}: {label} skipped -- "
                    f"{int(max(0, remaining))}s of budget left; its "
                    f"{len(batch)} row(s) stay needs_input"
                )
                return
            schedule_block = _render_bidding_schedule_block(
                batch, withhold_published=True, schedule_titles=schedule_titles,
            )
            user_message = "\n".join([
                f"USER REQUEST: produce the firm's cost estimate for tender ID {tender_id}.",
                "",
                instruction,
                "",
                f"Tender context:\n{scope_context}",
                "",
                schedule_block,
            ])
            # Each researched item may need 2-3 round-trips; a component with
            # a printed rate needs none. Give the ReAct loop room in
            # proportion (each round ≈ 2 graph super-steps).
            researched = sum(
                1 for it in batch
                if not (it.get("component_of") and _has_printed_rate(it))
            )
            _recursion = max(40, researched * 5 + (len(batch) - researched) * 2)
            from app.core.database import SessionLocal
            session = SessionLocal()
            try:
                built = _build_costing_agent(state, session, training_context, mode)
                result = await asyncio.wait_for(
                    _ainvoke_with_retry(
                        built["agent"],
                        {"messages": [HumanMessage(content=user_message)]},
                        config={"callbacks": [built["callback"]], "recursion_limit": _recursion},
                    ),
                    timeout=remaining,
                )
                agent_messages = result.get("messages", [])
                final_content = agent_messages[-1].content if agent_messages else ""
                final_content = _strip_tool_tags(final_content)
                parsed = _parse_costing_response(final_content)
                batch_items = parsed.get("line_items") or []
                merge = cbs.merge_batch_rates(session, breakdown_id, batch_items)
                total_matched += merge.get("matched", 0)
                total_unmatched += merge.get("unmatched", 0)
                for a in (parsed.get("assumptions") or []):
                    if a not in accumulated_assumptions:
                        accumulated_assumptions.append(a)
                accumulated_cost_assumptions.extend(parsed.get("cost_assumptions") or [])
                accumulated_manpower.extend(parsed.get("manpower_resource_analysis") or [])
                logger.info(
                    f"[costing batched] tender {tender_id}: {label} → "
                    f"{len(batch_items)} item(s) parsed, {merge.get('matched', 0)} "
                    f"merged, {merge.get('unmatched', 0)} unmatched"
                    + (f", {merge['rejected_anchored']} rejected as computed from the published rate"
                       if merge.get("rejected_anchored") else "")
                    + f" ({int(time.monotonic() - started_at)}s elapsed)"
                )
            except asyncio.TimeoutError:
                out_of_time = True
                logger.error(
                    f"[costing batched] tender {tender_id}: {label} ran out of the "
                    f"costing budget after {int(remaining)}s; its rows stay needs_input"
                )
                try:
                    session.rollback()
                except Exception:
                    pass
            except Exception as e:
                logger.error(
                    f"[costing batched] tender {tender_id}: {label} failed "
                    f"({type(e).__name__}: {e}); these rows stay needs_input",
                    exc_info=True,
                )
                try:
                    session.rollback()
                except Exception:
                    pass
            finally:
                try:
                    session.close()
                except Exception:
                    pass
            batches_done += 1
            lines_done = min(lines_done + len(batch), len(model_rows))
            _emit_reliability_event(None, "costing_batch_progress", {
                "agent": "costing_researcher",
                "tender_id": tender_id,
                "batch": batches_done,
                "total_batches": total_batches,
                "lines_done": lines_done,
                "lines_total": len(model_rows),
            })

    main_batches = []
    for bi in range(total_batches):
        batch = model_rows[bi * batch_size:(bi + 1) * batch_size]
        if not batch:
            continue
        instruction = (
            f"This is batch {bi + 1} of {total_batches}. {_instruction_tail}"
        )
        main_batches.append(_cost_batch(batch, instruction, f"batch {bi + 1}/{total_batches}"))
    # Market-price research runs beside the batches: one focused web search
    # per row that can have a market price (see market_price_research). The
    # agent searches at its own discretion -- eleven searches for 286 rows on
    # the Mid-Life NIT -- so the platform does it for every such row.
    research_task = _start_market_research(
        db, cost_rows, _remaining(), tender_id,
        anonymization_map=state.get("anonymization_map") or {},
    )
    # The platform's own build-up for the rows the agent does not price. It
    # needs its rate basis (wages, material prices) first; the rows the
    # agent does price may need one later, so the basis is researched once
    # and shared.
    basis_task = None
    buildup_task = None
    if buildup_key and published_by_id and any(v for v in published_by_id.values()):
        basis_task = _start_rate_basis(buildup_key, buildup_cfg, buildup_context,
                                       [r for r in cost_rows if not r.get("component_of")],
                                       deadline, tender_id)
    if buildup_key and routing.reference_rows:
        buildup_task = asyncio.create_task(_buildup_rows(
            routing.reference_rows, basis_task,
            breakdown_id=breakdown_id, tender_id=tender_id, context=buildup_context,
            schedules=schedules, published=published_by_id, api_key=buildup_key,
            cfg=buildup_cfg, defaults=_cost_defaults, deadline=deadline,
            anonymization_map=state.get("anonymization_map") or {},
        ))
    _release_transaction(db)
    if main_batches:
        # Write the prompt-cache entry once so the wave of batches reads it
        # instead of each writing its own (see _warm_prompt_cache).
        if len(main_batches) > 1 and _remaining() > _MIN_BATCH_SECONDS:
            await _warm_prompt_cache(
                _build_costing_agent(state, db, training_context, mode),
                tender_id, "main batches",
            )
        _release_transaction(db)
        await asyncio.gather(*main_batches)

    # 2b) Bounded sweep — re-cost any rows still needs_input after the main
    # batch loop (failed/truncated batches, merge mismatches). Each sweep
    # re-detects leftover rows and is idempotent (merge only writes cost
    # fields), so a crash mid-sweep resumes cleanly. Bounded by
    # costing.costing_max_sweeps so a genuinely un-pricable row can't loop,
    # and by the deadline so a slow first pass does not spend the budget the
    # finalisation needs.
    # Sweep only over the IN-SCOPE rows the main loop attempted (cost_rows is
    # already capped), so rows intentionally left out by costing.max_line_items
    # stay needs_input and the cap guardrail holds.
    # Rows routed away from the model are not swept either: finalisation
    # prices them, and re-sending them would recreate the discarded work.
    boq_by_id = {
        it["boq_item_id"]: it
        for it in model_rows
        if it.get("boq_item_id") is not None
    }
    for sweep in range(max(0, max_sweeps)):
        if out_of_time or _remaining() < _MIN_BATCH_SECONDS:
            out_of_time = True
            break
        db.expire_all()
        db.refresh(breakdown)
        leftover_ids = [
            ln.boq_item_id
            for ln in breakdown.lines
            if ln.needs_input and not ln.is_tax_line and ln.boq_item_id is not None
        ]
        leftover = [boq_by_id[i] for i in leftover_ids if i in boq_by_id]
        if not leftover:
            break
        sweep_batches = max(1, math.ceil(len(leftover) / batch_size))
        logger.info(
            f"[costing batched] tender {tender_id}: sweep {sweep + 1}/{max_sweeps} "
            f"re-costing {len(leftover)} still-needs_input row(s) in "
            f"{sweep_batches} batch(es); {int(_remaining())}s left"
        )
        _release_transaction(db)
        sweep_coros = []
        for sb in range(sweep_batches):
            batch = leftover[sb * batch_size:(sb + 1) * batch_size]
            instruction = (
                f"This is RE-COST sweep {sweep + 1}, batch {sb + 1} of "
                f"{sweep_batches}. These {len(batch)} line item(s) were left "
                f"UNPRICED on the first pass, or their rate was rejected because it "
                f"was computed from the Published Ref Rate — research and price them "
                f"now from evidence or a first-principles build-up. "
                f"{_instruction_tail}"
            )
            sweep_coros.append(_cost_batch(
                batch, instruction, f"sweep {sweep + 1} batch {sb + 1}/{sweep_batches}"
            ))
        if len(sweep_coros) > 1 and _remaining() > _MIN_BATCH_SECONDS:
            # The 5-minute cache entry may have lapsed since the main wave.
            await _warm_prompt_cache(
                _build_costing_agent(state, db, training_context, mode),
                tender_id, f"sweep {sweep + 1}",
            )
            _release_transaction(db)
        await asyncio.gather(*sweep_coros)

    # Rows the agent priced without evidence -- a blind build-up, or nothing --
    # are built up by the platform too, when the firm's data did not price
    # them and there is time.
    buildup_runs: list[dict] = []
    if buildup_key and model_rows and _remaining() > 150:
        db.expire_all()
        db.refresh(breakdown)
        _leftover = _rows_without_evidence(breakdown, model_rows)
        if _leftover:
            logger.info(
                f"[costing batched] tender {tender_id}: {len(_leftover)} row(s) the agent "
                f"priced without the firm's data or a verified price go to the platform's "
                f"cost build-up"
            )
            _release_transaction(db)
            buildup_runs.append(await _buildup_rows(
                _leftover, basis_task,
                breakdown_id=breakdown_id, tender_id=tender_id, context=buildup_context,
                schedules=schedules, published=published_by_id, api_key=buildup_key,
                cfg=buildup_cfg, defaults=_cost_defaults, deadline=deadline,
                anonymization_map=state.get("anonymization_map") or {},
            ))
    if buildup_task is not None:
        try:
            buildup_runs.append(await asyncio.wait_for(buildup_task, timeout=max(1.0, _remaining())))
        except Exception as e:
            buildup_task.cancel()
            logger.warning(f"[costing batched] tender {tender_id}: cost build-up cut short: "
                           f"{type(e).__name__}: {e}")
    if basis_task is not None and not basis_task.done():
        basis_task.cancel()

    # A verified market price replaces the row's figure -- unless the firm's
    # own rate data priced it, or it is more than 2x from the railway's figure
    # (then it is a different item, and the build-up stands).
    await _merge_market_research(
        research_task, breakdown_id, _remaining(), tender_id,
        schedules=schedules, defaults=_cost_defaults,
    )

    _bnote = _buildup_note(buildup_runs)
    if _bnote:
        accumulated_assumptions.append(_bnote)

    if out_of_time:
        db.expire_all()
        db.refresh(breakdown)
        _unpriced = sum(
            1 for ln in breakdown.lines
            if ln.needs_input and not ln.is_tax_line and ln.boq_item_id in boq_by_id
        )
        _timeout_note = (
            f"⚠️ The costing ran out of its {budget_s}s time budget after "
            f"{batches_done} of {total_batches} batch(es): {_unpriced} row(s) "
            f"were not researched. Their rates are derived from the published "
            f"rate where one is printed, otherwise left as 'needs input'; run "
            f"the costing again to research them."
        )
        logger.error(f"[costing batched] tender {tender_id}: {_timeout_note}")
        accumulated_assumptions.insert(0, _timeout_note)
        _emit_reliability_event(None, "costing_out_of_time", {
            "agent": "costing_researcher",
            "tender_id": tender_id,
            "batches_done": batches_done,
            "total_batches": total_batches,
            "unpriced": _unpriced,
            "budget_s": budget_s,
        })

    return _finalize_batched_breakdown(
        db, breakdown,
        tender_id=tender_id,
        analysis_result=analysis_result,
        assumptions=accumulated_assumptions,
        cost_assumptions=accumulated_cost_assumptions,
        manpower=accumulated_manpower,
        total_batches=total_batches,
        max_sweeps=max_sweeps,
        total_matched=total_matched,
        total_unmatched=total_unmatched,
    )


def _recover_partial_batched_result(
    db: Session,
    tender_id: int,
    *,
    since,
    analysis_result: dict,
    budget_s: int,
    max_sweeps: int,
) -> Optional[dict]:
    """After the outer timer cancelled the graph: find the skeleton breakdown
    this run built (newest costing_researcher breakdown for the tender created
    since the run started) and finalise it from the batches already merged.
    None when there is no such breakdown (the run never reached the batched
    node), in which case the caller re-raises the timeout."""
    from app.models.cost_breakdown import CostBreakdown
    try:
        db.rollback()
    except Exception:
        pass
    try:
        bd = (
            db.query(CostBreakdown)
            .filter(
                CostBreakdown.tender_id == tender_id,
                CostBreakdown.created_by_agent == "costing_researcher",
                CostBreakdown.created_at >= since,
            )
            .order_by(CostBreakdown.version.desc(), CostBreakdown.id.desc())
            .first()
        )
    except Exception as e:
        logger.error(f"[costing] tender {tender_id}: partial-result lookup failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return None
    if bd is None:
        return None
    db.expire_all()
    db.refresh(bd)
    priced = sum(1 for ln in bd.lines if not ln.is_tax_line and not ln.needs_input)
    unpriced = sum(1 for ln in bd.lines if not ln.is_tax_line and ln.needs_input)
    note = (
        f"⚠️ The costing was stopped at its {budget_s}s time limit with "
        f"{priced} row(s) priced and {unpriced} not yet researched. The "
        f"unresearched rows take a rate derived from the published rate where "
        f"one is printed, otherwise stay 'needs input'; run the costing again "
        f"to research them."
    )
    logger.error(f"[costing] tender {tender_id}: {note}")
    try:
        from app.services.ai_service import _emit_reliability_event
        _emit_reliability_event(None, "costing_out_of_time", {
            "agent": "costing_researcher",
            "tender_id": tender_id,
            "priced": priced,
            "unpriced": unpriced,
            "budget_s": budget_s,
            "recovered": True,
        })
    except Exception:
        pass
    try:
        return _finalize_batched_breakdown(
            db, bd,
            tender_id=tender_id,
            analysis_result=analysis_result,
            assumptions=[note],
            cost_assumptions=[],
            manpower=[],
            total_batches=0,
            max_sweeps=max_sweeps,
        )
    except Exception as e:
        logger.error(
            f"[costing] tender {tender_id}: finalising the partial breakdown "
            f"failed: {e}", exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _release_transaction(db: Session) -> None:
    """End the node's read transaction before the batches' own sessions
    write. Nothing is pending on `db` at these points; on SQLite an open
    reader would block every batch's commit ("database is locked"), and on
    Postgres it only pins a snapshot the node no longer wants."""
    try:
        db.rollback()
    except Exception:
        pass


def _finalize_batched_breakdown(
    db: Session,
    breakdown,
    *,
    tender_id: Optional[int],
    analysis_result: dict,
    assumptions: list[str],
    cost_assumptions: list[dict],
    manpower: list[dict],
    total_batches: int,
    max_sweeps: int,
    total_matched: int = 0,
    total_unmatched: int = 0,
) -> dict:
    """Roll up, normalise, summarise and total a batched breakdown, then build
    the `costing_result` from the persisted rows.

    Called by `run_costing_batched_node` when its batches are done (or its
    deadline passed) and by `_run_enhanced_costing_research_inner` when the
    outer timer fires before the node returned: every batch that finished is
    already committed, so the breakdown on disk is the result, not a loss.
    Deterministic and idempotent -- no model call.
    """
    from app.services import cost_breakdown_service as cbs
    from app.services.settings_service import get_setting_value

    accumulated_assumptions = list(assumptions or [])
    accumulated_cost_assumptions = list(cost_assumptions or [])
    accumulated_manpower = list(manpower or [])
    db.expire_all()
    db.refresh(breakdown)

    # 2b') Annexure components roll up into the schedule item that cites
    # them: the sum of a parent's priced components (per set) is its estimated
    # rate. This runs BEFORE the copied-rate guard below, which would
    # otherwise see the parent still unpriced (it was kept out of the batches
    # on purpose) and derive it from the published rate instead.
    try:
        _rollup = cbs.rollup_component_lines(db, breakdown.id)
        for _n in _rollup.get("notes") or []:
            if _n not in accumulated_assumptions:
                accumulated_assumptions.append(_n)
    except Exception as _e:
        logger.warning(
            f"[costing batched] tender {tender_id}: component roll-up failed "
            f"(non-fatal): {_e}"
        )
        try:
            db.rollback()
        except Exception:
            pass

    # 2c) Deterministic safety net (RULE 2b): re-derive any non-tax line that
    # copied the published rate (zero margin) so the breakdown never shows a
    # 1:1 copy, regardless of whether the agent obeyed the prompt. Genuinely
    # researched rates (below the published reference) are left untouched.
    try:
        from app.services.costing_format_service import get_costing_defaults
        if get_setting_value(db, "costing.derive_cost_from_reference", True):
            _defaults = get_costing_defaults(db)
            cbs.normalize_copied_rates(
                db, breakdown.id,
                overhead_pct=_defaults["overhead_percent"],
                margin_pct=_defaults["margin_percent"],
            )
    except Exception as _e:
        logger.warning(
            f"[costing batched] tender {tender_id}: normalize_copied_rates "
            f"failed (non-fatal): {_e}"
        )
        try:
            db.rollback()
        except Exception:
            pass

    # 2d) Priced blind to the published rate, each row now takes the most
    # accurate figure its evidence supports: a cited market price or a
    # build-up when it is plausible for the item, else the railway's own
    # estimate less overhead and margin -- and says which, in plain words.
    # Same switch as the guard above: both lean on the published reference.
    try:
        from app.services.costing_format_service import get_costing_defaults
        if get_setting_value(db, "costing.derive_cost_from_reference", True):
            _defaults = get_costing_defaults(db)
            from app.services.costing.schedule_context import schedule_contexts
            _sched = schedule_contexts(db, tender_id, read_documents=False) if tender_id else {}
            cbs.settle_rates_on_evidence(
                db, breakdown.id,
                overhead_pct=_defaults["overhead_percent"],
                margin_pct=_defaults["margin_percent"],
                gst_pct=_defaults["gst_percent"],
                taxes_inclusive={c: x.taxes_inclusive for c, x in _sched.items()},
            )
    except Exception as _e:
        logger.warning(
            f"[costing batched] tender {tender_id}: settle_rates_on_evidence "
            f"failed (non-fatal): {_e}"
        )
        try:
            db.rollback()
        except Exception:
            pass

    # 3) Persist accumulated narrative fields + recompute totals.
    db.refresh(breakdown)
    if accumulated_assumptions:
        try:
            existing = json.loads(breakdown.assumptions_json) if breakdown.assumptions_json else []
        except Exception:
            existing = []
        breakdown.assumptions_json = json.dumps(existing + accumulated_assumptions, default=str)
    if accumulated_cost_assumptions:
        breakdown.cost_assumptions_json = json.dumps(accumulated_cost_assumptions, default=str)
    if accumulated_manpower:
        breakdown.manpower_resource_analysis_json = json.dumps(accumulated_manpower, default=str)
    # Populate the strategic summary deterministically (the batched path never
    # set it → blank Summary sheet). Schedule-wise profitability from the costed
    # lines + tender snapshot + observations.
    # The copied-rate fallback changes component costs. Roll them up again
    # before taking the summary snapshot, otherwise Summary and detail differ.
    db.commit()
    cbs.recompute_breakdown_totals(db, breakdown)
    db.refresh(breakdown)
    try:
        _summary = cbs.build_strategic_summary(
            db, breakdown, analysis_result=analysis_result, tender_id=tender_id,
        )
        breakdown.strategic_summary_json = json.dumps(_summary, default=str)
    except Exception as _e:
        logger.warning(
            f"[costing batched] tender {tender_id}: build_strategic_summary "
            f"failed (non-fatal): {_e}"
        )
    db.commit()
    cbs.recompute_breakdown_totals(db, breakdown)
    db.refresh(breakdown)

    # Task 7 (fabrication lockdown) — defense-in-depth: the batched path can't
    # structurally invent rows (the skeleton IS the captured schedule and
    # merge_batch_rates only ever updates existing rows, never inserts), but
    # run the same authoritative RULE 5 validator here too so a future change
    # to the merge logic can't silently regress into adding/dropping rows
    # without being caught. Any finding is a bug in this node, not something
    # the agent can cause — log loudly and surface it, never drop rows here
    # (there is nothing invented to drop; the schedule is already 1:1).
    try:
        _final_line_dicts = [
            {
                "sr_no": ln.sr_no, "item_code": ln.item_code,
                "schedule_name": ln.schedule_name, "boq_item_id": ln.boq_item_id,
                "is_tax_line": ln.is_tax_line, "rate": ln.rate, "amount": ln.amount,
            }
            for ln in breakdown.lines
        ]
        _batched_validation_errors = cbs.validate_nit_mirror(db, tender_id, _final_line_dicts)
        if _batched_validation_errors:
            logger.error(
                f"[costing batched] tender {tender_id}: RULE 5 validator found "
                f"{len(_batched_validation_errors)} issue(s) on the FINAL batched "
                f"breakdown (unexpected — the skeleton should already be 1:1). "
                f"Investigate merge_batch_rates / build_skeleton_from_boq."
            )
            for _err in _batched_validation_errors:
                logger.error(f"[costing batched]   ↳ {_err}")
    except Exception as _e:
        logger.warning(
            f"[costing batched] tender {tender_id}: post-merge RULE 5 validation "
            f"check failed (non-fatal): {_e}"
        )

    # Build the costing_result from the persisted breakdown (ORM rows carry
    # every NIT field so the wrapper's XLSX + markdown render correctly).
    line_items = [
        {
            "sr_no": ln.sr_no,
            "description": ln.description,
            "category": ln.category,
            "quantity": ln.quantity,
            "unit": ln.unit,
            "rate": ln.rate,
            "amount": ln.amount,
            "tender_rate": ln.tender_rate,
            "tender_amount": ln.tender_amount,
            "margin_amount": ln.margin_amount,
            "margin_pct": ln.margin_pct,
            "boq_item_id": ln.boq_item_id,
            "item_code": ln.item_code,
            "schedule_name": ln.schedule_name,
            "bidding_unit": ln.bidding_unit,
            "basic_value": ln.basic_value,
            "escalation_pct": ln.escalation_pct,
            "is_tax_line": bool(ln.is_tax_line),
            "rate_source": ln.rate_source,
            "source_ref": ln.source_ref,
            "confidence": ln.confidence,
            "needs_input": ln.needs_input,
            "cost_buildup_note": ln.cost_buildup_note,
            "schedule_section": ln.schedule_section,
            "parent_boq_item_id": ln.parent_boq_item_id,
            "annexure_ref": ln.annexure_ref,
            "is_component": ln.parent_boq_item_id is not None,
            "oem_manufacturer": ln.oem_manufacturer,
            "source_url": ln.source_url,
        }
        for ln in cbs.order_lines_for_display(breakdown.lines)
    ]
    still_needs = sum(1 for ln in breakdown.lines if ln.needs_input and not ln.is_tax_line)
    logger.info(
        f"[costing batched] tender {tender_id}: complete — {len(line_items)} "
        f"line(s); {total_matched} merged, {total_unmatched} unmatched, "
        f"{still_needs} still need input"
    )
    # Surface a non-zero residual so it's visible in the breakdown, not buried
    # in logs. After the bounded sweeps this should normally be 0.
    if still_needs > 0:
        _residual_note = (
            f"⚠️ {still_needs} line item(s) could not be auto-costed after "
            f"{max_sweeps} re-cost sweep(s) — left as 'needs input' for manual "
            f"pricing."
        )
        if _residual_note not in accumulated_assumptions:
            accumulated_assumptions.insert(0, _residual_note)
        try:
            existing_a = json.loads(breakdown.assumptions_json) if breakdown.assumptions_json else []
        except Exception:
            existing_a = []
        if _residual_note not in existing_a:
            breakdown.assumptions_json = json.dumps([_residual_note] + existing_a, default=str)
            db.commit()

    try:
        _strategic = json.loads(breakdown.strategic_summary_json) if breakdown.strategic_summary_json else {}
    except Exception:
        _strategic = {}
    costing_result = {
        "line_items": line_items,
        "totals": None,
        "assumptions": accumulated_assumptions,
        "cost_assumptions": accumulated_cost_assumptions,
        "manpower_resource_analysis": accumulated_manpower,
        "strategic_summary": _strategic,
        "_already_persisted": True,
        "cost_breakdown_id": breakdown.id,
        "_batched": True,
        "_batches": total_batches,
        "_needs_input_remaining": still_needs,
    }
    return {"costing_result": costing_result, "status": "completed"}


def _tender_wants_component_expansion(state: CostingAgentState, db: Session) -> bool:
    """True when this tender should be costed bottom-up from the ratecard.

    Two conditions, both required:
      1. The `costing.component_expansion_enabled` setting is on (default True).
      2. The tender's scope is component-buildup-shaped — its captured BOQItem
         descriptions mention a B/C/D check AND an engine type that we actually
         have a ratecard check-schedule for. Without a matching ratecard there
         is nothing to expand, so we fall back to the normal NIT-mirror path.
    """
    tender_id = state.get("tender_id")
    if not tender_id:
        return False
    try:
        from app.services.settings_service import get_effective_setting
        enabled = get_effective_setting(db, "costing.component_expansion_enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.lower() in ("true", "1", "yes")
        if not enabled:
            return False
    except Exception:
        pass  # default-on

    try:
        from app.models.costing_template import BOQItem
        from app.models.ratecard import RatecardCheckSchedule

        # Engine types we can actually expand.
        engines = {
            (e or "").strip()
            for (e,) in db.query(RatecardCheckSchedule.engine_type).distinct().all()
            if e
        }
        if not engines:
            return False

        rows = db.query(BOQItem.description).filter(BOQItem.tender_id == tender_id).all()
        check_re = re.compile(r"['\"]?\b[BCD]\b['\"]?\s*[-/ ]?\s*check\b", re.I)
        for (desc,) in rows:
            text = (desc or "")
            if not check_re.search(text):
                continue
            for eng in engines:
                # match on a loose token (e.g. "VTA 28L" -> "VTA" + "28")
                toks = [t for t in re.split(r"\s+", eng) if t]
                if all(re.search(re.escape(t), text, re.I) for t in toks):
                    logger.info(
                        f"[costing] tender {tender_id}: scope matches ratecard "
                        f"engine '{eng}' + a check → component_expansion"
                    )
                    return True
        return False
    except Exception as e:
        logger.warning(
            f"[costing] tender {tender_id}: component-expansion detection failed "
            f"({e}) — using default routing"
        )
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _pick_costing_strategy(state: CostingAgentState, db: Session) -> str:
    """Single-call vs batched vs component-expansion costing routing.

    Route to the deterministic skeleton/batched path whenever a captured
    BOQItem schedule EXISTS (count > 0) — regardless of size. The skeleton
    mirrors the NIT 1:1 (every row, verbatim) and rates are filled in batches
    of costing.batch_size (a single batch when small), so the agent can never
    summarise or drop line items for a tender that has a schedule. Tenders with
    no captured schedule (non-NIT / freeform chat) use the single-call path.
    Falls back to single on any error so a costing run is never blocked.

    Task 7 (fabrication lockdown) confirmation: this is already the sole route
    guard needed. The freeform "produce at least 5 line items" / AMC-playbook
    prompt branch (`_build_user_message` ~1423-1509) is ONLY reachable when the
    `boq_items` list passed into that prompt builder is empty — and
    `chat_agent_wrappers.py` always hydrates `analysis_result["boq_items"]`
    from the BOQItem table whenever any captured rows exist, for every route
    (single, batched-fallback, component_expansion). So a tender with
    `count > 0` here can never reach that freeform branch: "batched" renders
    the full bidding-schedule block instead, and "component_expansion" reuses
    the single-call node but with `boq_items` already populated, so it also
    renders the bidding-schedule block (see ~1374-1421) rather than the
    freeform instructions. The one true escape hatch — `run_costing_batched_node`
    falling back to `run_costing_react_node` when `build_skeleton_from_boq`
    unexpectedly returns None despite `count > 0` — is a same-session race
    that the code already flags as "Unreachable on the batched route" and logs
    loudly if it ever fires.
    """
    tender_id = state.get("tender_id")
    if not tender_id:
        return "single"

    # Component build-up (ratecard-driven) takes precedence over the 1:1 NIT
    # mirror when the scope is component-shaped AND we have a matching ratecard.
    if _tender_wants_component_expansion(state, db):
        return "component_expansion"

    from app.models.costing_template import BOQItem

    def _count_boq() -> int:
        return db.query(BOQItem).filter(BOQItem.tender_id == tender_id).count()

    # Falling back to single-call for a tender that HAS a schedule means the
    # agent may summarise/drop rows — the exact failure we're guarding against.
    # So a transient count-query error rolls back and retries once before we
    # ever take that dangerous fallback, and the fallback is logged loudly.
    count = None
    for _attempt in range(2):
        try:
            count = _count_boq()
            break
        except Exception as e:
            logger.warning(
                f"[costing] tender {tender_id}: BOQ count query failed on "
                f"attempt {_attempt + 1} ({e})"
            )
            try:
                db.rollback()
            except Exception:
                pass
    if count is None:
        logger.error(
            f"[costing] tender {tender_id}: BOQ count query failed twice — "
            f"falling back to single-call costing. If this tender has a captured "
            f"schedule, its rows may be SUMMARISED rather than mirrored 1:1."
        )
        return "single"
    if count > 0:
        logger.info(
            f"[costing] tender {tender_id}: {count} captured BOQ row(s) "
            f"→ batched/skeleton costing (1:1 NIT mirror)"
        )
        return "batched"
    return "single"


def split_costable_rows(
    boq_items: list[dict],
) -> tuple[list[dict], Optional[str]]:
    """Split captured BOQ rows into the non-tax rows that need a researched rate.

    Returns ``(cost_rows, note)``. `note` is non-None only when the tender HAS
    captured rows but none of them are costable — every row was flagged
    `is_tax_line`. That combination prices nothing while still reporting
    success, which is how Command Center session 285 shipped a single
    "[NEEDS RATE]" line with no error in the logs or the UI.

    An empty schedule returns no note: that's the single-call path's business,
    not a mislabelling.
    """
    cost_rows = [it for it in boq_items if not it.get("is_tax_line")]
    # A component the material list totals at Rs 0.00 is costed at nil by the
    # skeleton; a researched rate on it would be spent and then discarded.
    cost_rows = [
        it for it in cost_rows
        if not (it.get("parent_item_id") and it.get("quantity") is not None
                and float(it.get("quantity") or 0) == 0.0)
    ]
    if boq_items and not cost_rows:
        note = (
            f"⚠️ All {len(boq_items)} captured line item(s) were classified as "
            f"tax rows, so none could be costed. This is almost always a "
            f"misclassification — a work item whose description mentions GST "
            f"(e.g. \"rates are inclusive of GST @ 18%\") is NOT a tax row. "
            f"Nothing was priced; re-parse the schedule or supply rates manually."
        )
        return [], note
    # A schedule item whose annexure components were captured is not
    # researched on its own: its rate is the sum of its components, rolled up
    # deterministically after the batches (`rollup_component_lines`). Sending
    # it to the agent as well would spend the calls and then be overwritten.
    parents_with_components = {
        it.get("parent_item_id") for it in boq_items if it.get("parent_item_id")
    }
    if parents_with_components:
        before = len(cost_rows)
        cost_rows = [it for it in cost_rows if it.get("boq_item_id") not in parents_with_components]
        logger.info(
            f"[costing batched] {before - len(cost_rows)} schedule item(s) are built "
            f"up from annexure components and are not researched separately"
        )
    return cost_rows, None


def _annotate_component_rows(boq_items: list[dict]) -> None:
    """In place: give each annexure component row the context the agent
    needs to price it correctly -- which schedule item it belongs to and
    that its quantity is per set, not per contract."""
    by_id = {it.get("boq_item_id"): it for it in boq_items if it.get("boq_item_id")}
    for it in boq_items:
        pid = it.get("parent_item_id")
        parent = by_id.get(pid) if pid else None
        if parent is None:
            continue
        it["component_of"] = (
            f"Schedule {parent.get('schedule_name') or '?'} item "
            f"{parent.get('item_code') or parent.get('sr_no')}: "
            f"{(parent.get('description') or '')[:90]}"
        )
        it["quantity_basis"] = f"per {parent.get('unit') or 'set'}"


# Budget for a run with no captured schedule — the genuinely quick single-call
# path. Anything with a captured schedule routes batched and is budgeted as such.
_SINGLE_CALL_TIMEOUT_S = 300
# Per-batch headroom: one LLM pass with per-line web research, ~4 min.
_BATCH_HEADROOM_S = 240
# Floor for any batched run, however few rows.
_BATCHED_MIN_TIMEOUT_S = 900
# How many batches are in flight at once (costing.batch_concurrency). Four
# batches of sixty priced one after another took ~29 min on the Liluah
# tender; the RQ job is killed at 30. Four at once is one wave.
_DEFAULT_BATCH_CONCURRENCY = 4
# The costing budget must sit inside the two limits that enclose it -- the RQ
# job (`run_service._RUN_JOB_TIMEOUT_SECONDS`) and the Master Agent's own
# `asyncio.wait_for` (`master_agent_max_execution_time_s`) -- with room for
# what runs around the graph (schedule self-heal, XLSX render, the reply).
_OUTER_HEADROOM_S = 300
# Inside the budget, the batched node stops issuing work this long before
# the outer timer so roll-up, totals and the summary always get to run.
_FINALIZE_RESERVE_S = 180
# A batch is not started with less than this left.
_MIN_BATCH_SECONDS = 90


def _start_market_research(
    db, cost_rows: list, budget_left_s: float, tender_id, anonymization_map: Optional[dict] = None,
) -> Optional["asyncio.Task"]:
    """Start the per-row market-price research as a task, or None when it is
    switched off (`costing.market_research`) or there is no Anthropic key."""
    try:
        from app.services.settings_service import get_setting_value
        if not get_setting_value(db, "costing.market_research", True):
            return None
        from app.core.config import get_settings
        from app.services.costing.market_price_research import research_market_prices
        from app.services.langchain.provider_config import get_api_key
        key = get_api_key(db, "anthropic")
        if not key:
            return None
        cfg = get_settings()
        concurrency = int(get_setting_value(db, "costing.market_research_concurrency", 8) or 8)
        cache_days = float(get_setting_value(db, "costing.market_research_cache_days", 14) or 0)
        return asyncio.create_task(research_market_prices(
            [dict(r) for r in cost_rows],
            api_key=key,
            model=cfg.anthropic_search_model or "claude-haiku-4-5",
            max_uses=max(1, int(cfg.anthropic_search_max_uses or 3)),
            concurrency=concurrency,
            deadline_s=max(0.0, budget_left_s - 30),
            anonymization_map=anonymization_map,
            cache_ttl_days=cache_days,
        ))
    except Exception as e:
        logger.warning(f"[costing batched] tender {tender_id}: market research not started: {e}")
        return None


async def _merge_market_research(
    task, breakdown_id: int, wait_s: float, tender_id,
    *, schedules: Optional[dict] = None, defaults: Optional[dict] = None,
) -> None:
    """Merge what the research found, on a session of its own. A research
    run that fails or overruns loses its prices, never the costing.

    A verified price replaces the row's figure unless the firm's own rate
    data priced the row (the firm's purchase price beats a listing), or it
    is more than 2x from the railway's figure for the row -- then the listing
    is a different item (a generic lamp for an RDSO fitting) and whatever
    the row already has, the platform's build-up included, stands.
    """
    if task is None:
        return
    try:
        found = await asyncio.wait_for(task, timeout=max(1.0, wait_s))
    except Exception as e:
        task.cancel()
        logger.warning(f"[costing batched] tender {tender_id}: market research dropped: {type(e).__name__}: {e}")
        return
    if not found:
        return
    from app.core.database import SessionLocal
    from app.models.cost_breakdown import CostBreakdownLine
    from app.services import cost_breakdown_service as cbs
    from app.services.costing.cost_buildup import BAND, benchmark_cost
    from app.services.costing.market_price_research import as_batch_lines
    session = SessionLocal()
    try:
        if defaults is None:
            from app.services.costing_format_service import get_costing_defaults
            defaults = get_costing_defaults(session)
        lines = {
            ln.boq_item_id: ln
            for ln in session.query(CostBreakdownLine)
            .filter(CostBreakdownLine.cost_breakdown_id == breakdown_id,
                    CostBreakdownLine.boq_item_id.in_(list(found.keys())))
            .all()
        }
        keep: dict = {}
        firm = far = 0
        for bid, ev in found.items():
            ln = lines.get(bid)
            if ln is None:
                continue
            if cbs.line_has_firm_rate(ln):
                firm += 1
                continue
            ctx = (schedules or {}).get((ln.schedule_name or "").strip().upper())
            ref = benchmark_cost(
                ln.tender_rate, overhead_pct=defaults["overhead_percent"],
                margin_pct=defaults["margin_percent"], gst_pct=defaults["gst_percent"],
                taxes_inclusive=getattr(ctx, "taxes_inclusive", None),
            )
            if ref and not (BAND[0] <= float(ev["rate"]) / ref <= BAND[1]):
                far += 1
                continue
            keep[bid] = ev
        merge = cbs.merge_batch_rates(
            session, breakdown_id, as_batch_lines(keep), platform_verified=True,
        ) if keep else {"matched": 0}
        logger.info(
            f"[costing batched] tender {tender_id}: market research -> "
            f"{merge.get('matched', 0)} row(s) priced from cited web listings"
            + (f"; {firm} kept the firm's own rate" if firm else "")
            + (f"; {far} listing(s) more than 2x from the railway's figure not used "
               f"(a different item or specification)" if far else "")
        )
    except Exception as e:
        session.rollback()
        logger.warning(f"[costing batched] tender {tender_id}: market research merge failed: {e}")
    finally:
        session.close()


# ── The platform's own cost build-up (costing/cost_buildup.py) ───────────────

def _buildup_api_key(db) -> Optional[str]:
    """The Anthropic key the build-up runs on, or None (then it is skipped)."""
    try:
        from app.services.langchain.provider_config import get_api_key
        return get_api_key(db, "anthropic") or None
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _buildup_tender_context(
    db, tender_id, analysis_result: dict, schedules: dict, anonymization_map: dict,
) -> str:
    """What the build-up is told about the tender: the work, its place, its
    scope and every schedule's banner -- with the tender's identity (its
    reference, buyer, the firm) swapped out, since the model writes its web
    searches from this text."""
    from app.models.tender import Tender
    from app.services.costing.market_price_research import anonymize_description

    parts: list[str] = []
    t = None
    try:
        t = db.query(Tender).filter(Tender.id == tender_id).first()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    title = (getattr(t, "title", "") or "").strip()
    if title and title.lower() not in ("new session", "untitled", "new chat"):
        parts.append(f"Name of work: {title[:300]}")
    where = [w for w in (getattr(t, "location", None), getattr(t, "delivery_location", None)) if w]
    if where:
        parts.append("Work location: " + "; ".join(dict.fromkeys(where))[:200])
    analysis_result = analysis_result or {}
    summary = (analysis_result.get("summary") or "").strip()
    if summary:
        parts.append("Scope, from the tender analysis: " + summary[:2500])
    tech = (analysis_result.get("requirements") or {}).get("technical_specs") or []
    if tech:
        parts.append("Technical requirements: " + "; ".join(str(x) for x in tech)[:1200])
    if schedules:
        parts.append("Schedules of this tender, each banner as printed:")
        for code in sorted(schedules):
            parts.append(f"  {code}: {schedules[code].title[:600]}")
    text = "\n".join(parts) or "An Indian Railways tender."
    return anonymize_description(text, anonymization_map)


def _start_rate_basis(api_key: str, cfg, context: str, rows: list, deadline: float, tender_id):
    """The build-up's rate basis (wages, material prices) as a task, or None."""
    try:
        import anthropic

        from app.services.costing import cost_buildup as cb
        client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
        return asyncio.create_task(cb.research_rate_basis(
            client, cfg.model, context=context, families=cb.material_families_for(rows),
            loading_pct=cfg.loading_pct, deadline=min(deadline, time.monotonic() + 240),
        ))
    except Exception as e:
        logger.warning(f"[costing batched] tender {tender_id}: rate basis not started: {e}")
        return None


async def _buildup_rows(
    rows: list, basis_task, *, breakdown_id: int, tender_id, context: str, schedules: dict,
    published: dict, api_key: str, cfg, defaults: dict, deadline: float,
    anonymization_map: Optional[dict] = None,
) -> dict:
    """Build up `rows` on the shared rate basis (waiting for it if it is
    still being researched) and merge them. Never raises."""
    from app.services.costing import cost_buildup as cb

    basis = None
    if basis_task is not None:
        try:
            basis = await asyncio.wait_for(
                asyncio.shield(basis_task), timeout=max(1.0, deadline - time.monotonic() - 60),
            )
        except Exception as e:
            logger.warning(f"[costing batched] tender {tender_id}: rate basis unavailable "
                           f"({type(e).__name__}); the last central wage notification is used")
    if basis is None:
        basis = cb.fallback_basis(cfg.loading_pct)
    try:
        return await cb.run_platform_buildup(
            rows, breakdown_id=breakdown_id, tender_id=tender_id, tender_context=context,
            schedules=schedules, published=published, api_key=api_key, settings=cfg,
            overhead_pct=defaults["overhead_percent"], margin_pct=defaults["margin_percent"],
            gst_pct=defaults["gst_percent"], deadline=deadline,
            anonymization_map=anonymization_map, basis=basis,
        )
    except Exception as e:
        logger.error(f"[costing batched] tender {tender_id}: cost build-up failed: "
                     f"{type(e).__name__}: {e}", exc_info=True)
        return {"rows": len(rows), "priced": 0, "failed": len(rows)}


def _rows_without_evidence(breakdown, model_rows: list) -> list:
    """The agent's rows that print a published rate and came back without the
    firm's own rate data or a verified market price behind them."""
    from app.services import cost_breakdown_service as cbs

    wanted = {
        r["boq_item_id"]: r for r in model_rows
        if r.get("boq_item_id") is not None and not r.get("component_of")
    }
    out = []
    for ln in breakdown.lines:
        r = wanted.get(ln.boq_item_id)
        if r is None or ln.is_tax_line or ln.parent_boq_item_id is not None or ln.quantity is None:
            continue
        try:
            tr = float(ln.tender_rate or 0)
        except (TypeError, ValueError):
            tr = 0.0
        if tr <= 0 or ln.rate_source in (cbs.PRINTED_NIL_SOURCE, cbs.COMPONENT_BUILDUP_SOURCE):
            continue
        if cbs.line_has_own_evidence(ln):
            continue
        out.append(r)
    return out


def _buildup_note(runs: list) -> Optional[str]:
    """One plain sentence on what the platform built up, for the breakdown's
    assumptions."""
    priced = sum(int(r.get("priced", 0) or 0) for r in runs)
    failed = sum(int(r.get("failed", 0) or 0) for r in runs)
    looks = sum(int(r.get("second_looks", 0) or 0) for r in runs)
    if not (priced or failed):
        return None
    note = (
        f"The platform built up the cost of {priced} row(s) itself: what one unit is, its "
        f"materials at current prices, labour hours at the central minimum wages plus "
        f"statutory costs, consumables and transport. The railway's rate is the benchmark "
        f"for margin, never the figure."
    )
    if looks:
        note += (f" {looks} build-up(s) came out more than 2x from the railway's figure and "
                 f"were re-derived from scratch; the nearer of the two stands.")
    if failed:
        note += (f" {failed} row(s) could not be built up in the time available and show the "
                 f"railway's estimate less overhead and margin; running the costing again "
                 f"builds them up.")
    return note

# Annexure component rows print their own rate (the material list gives each
# item's rate and weight). Researching every one of them on the web is what
# made a sixty-row annexure batch take ten minutes; the printed rate is the
# anchor and the firm's cost is derived from it.
_COMPONENT_RESEARCH_RULE = (
    "ANNEXURE COMPONENTS WITH A PRINTED RATE: a row under an \"Annexure-N "
    "components\" heading whose Published Ref Rate is printed is a material the "
    "tender has benchmarked, not a verified current purchase price. Do NOT web-search "
    "every part separately. Retrieve applicable dated training rates first, then "
    "use shared searches for material families (steel per kg, fasteners, paint), "
    "matching grade, unit, location and tax basis. At most 8 searches per batch in total. "
    "Use actual tool evidence and include its URL or dataset reference and date; "
    "never invent a supplier, quote or freshness date. A historical printed rate "
    "is a last-resort derived_estimate, explicitly provisional, not current market evidence."
)


def _has_printed_rate(item: dict) -> bool:
    """True when the captured row carries a published/printed unit rate."""
    try:
        return item.get("estimated_rate") is not None and float(item["estimated_rate"]) > 0
    except (TypeError, ValueError):
        return False


def _batch_concurrency(db: Session) -> int:
    """How many costing batches run at once (costing.batch_concurrency)."""
    try:
        from app.services.settings_service import get_setting_value
        v = int(get_setting_value(db, "costing.batch_concurrency", _DEFAULT_BATCH_CONCURRENCY)
                or _DEFAULT_BATCH_CONCURRENCY)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        v = _DEFAULT_BATCH_CONCURRENCY
    # Each batch holds one pooled connection for its whole run (the worker's
    # pool is DB_POOL_SIZE=5 + DB_MAX_OVERFLOW=10), alongside the node's own
    # session and the short usage-log sessions the callbacks open; six in
    # flight leaves room, eight did not.
    return max(1, min(v, 6))


def _costing_budget_cap_s() -> int:
    """The most wall-clock a costing graph may take: the tighter of the RQ job
    timeout and the Master Agent budget, less `_OUTER_HEADROOM_S`.

    Before this cap the batched budget for ~190 rows was 2,880 s, and the job
    that carried it was killed at 1,800 s -- the costing could not finish
    inside the process that ran it, whatever the batches did.
    """
    # Both enclosing limits are taken at face value here, and the ordering
    # still holds with the Master's clamp (`_JOB_TIMEOUT_MARGIN_S`, 120 s):
    # the job timeout is inside this min(), so this cap is at least
    # `_OUTER_HEADROOM_S` below it and therefore below the Master's clamped
    # budget too. Reading the clamped budget instead would only take another
    # 120 s off the pricing time for no additional safety.
    limits: list[float] = []
    try:
        from app.services.run_service import _RUN_JOB_TIMEOUT_SECONDS
        limits.append(float(_RUN_JOB_TIMEOUT_SECONDS))
    except Exception:
        pass
    try:
        from app.core.config import get_settings
        limits.append(float(get_settings().master_agent_max_execution_time_s))
    except Exception:
        pass
    if not limits:
        return 1500
    return int(max(_BATCHED_MIN_TIMEOUT_S, min(limits) - _OUTER_HEADROOM_S))


def _costing_timeout_seconds(
    count: int,
    batch_size: int,
    max_sweeps: int,
    *,
    concurrency: int = 1,
    cap: Optional[int] = None,
) -> int:
    """Wall-clock budget for the costing graph, keyed on the ROUTE.

    Must stay in sync with `_pick_costing_strategy`, which routes to the
    batched path whenever a captured schedule exists (``count > 0``),
    regardless of size.

    This previously widened the budget only when ``count > batch_size``, still
    assuming the pre-skeleton rule that small tenders took the quick
    single-call path. They don't — they take the batched path with per-line
    research and up to `max_sweeps` re-cost passes, on the 300s single-call
    budget. That made `batch_size` a cliff (44 rows → 300s, 61 rows → 900s) and
    killed Command Center session 286 (tender 3810, 44 rows) at exactly 300s.

    ``concurrency`` batches run at once, so the budget is per WAVE of batches,
    not per batch; ``cap`` (see `_costing_budget_cap_s`) keeps it inside the
    job and Master Agent limits that enclose the run.
    """
    if count <= 0:
        return _SINGLE_CALL_TIMEOUT_S
    batches = math.ceil(count / max(1, batch_size))
    in_flight = max(1, min(concurrency, batches))
    waves = math.ceil(batches / in_flight)
    # Batches in flight together share the model's throughput, so a wave of
    # four takes longer than one batch alone: headroom grows with the wave
    # (x1 for one or two, x2 for four), never past what the cap allows.
    wave_headroom = _BATCH_HEADROOM_S * max(1.0, in_flight / 2.0)
    sweep_budget = 1 + max(0, max_sweeps)
    budget = int(max(_BATCHED_MIN_TIMEOUT_S, waves * wave_headroom * sweep_budget))
    if cap is not None:
        budget = min(budget, int(cap))
    return budget


# ── Node: parse and return ────────────────────────────────────────────────────

def parse_and_return_node(state: CostingAgentState) -> dict:
    """Parse the ReAct agent's final message into structured costing data.

    Defensive fallback: when the agent returns an empty / stub JSON (zero
    line items, zero recommendations), we synthesize a single
    `needs_user_input` line so the user sees a useful next-step question
    rather than the bare "produced no line items" diagnostic. This is
    especially important for AMC / rate-card tenders where the agent often
    fills the empty template instead of deriving line items from PDF text.
    """
    costing_raw = state.get("costing_result", {})
    raw_response = costing_raw.get("_raw_response", "")
    pdf_block_count = costing_raw.get("_pdf_block_count", 0)
    tender_id = state.get("tender_id")

    costing_data = _parse_costing_response(raw_response)

    # Diagnostic — log the raw agent output so we can debug empty/malformed responses
    try:
        n_items = len(costing_data.get("line_items") or [])
        n_recs = len(costing_data.get("recommendations") or [])
        logger.info(
            f"[costing] parsed agent output: {n_items} line items, "
            f"{n_recs} recommendations, raw_len={len(raw_response)}, "
            f"parse_error={costing_data.get('parse_error')}, "
            f"pdf_blocks_attached={pdf_block_count}"
        )
        if n_items == 0:
            # Promote to WARNING — this is the failure mode the user sees as
            # the "Scope clarification required" stub. Include pdf block
            # count so we can tell whether the agent had the PDFs and still
            # gave up, vs the helper returned 0 blocks.
            logger.warning(
                f"[costing] tender {tender_id}: agent produced 0 line items "
                f"(pdf_blocks={pdf_block_count}, raw_len={len(raw_response)}, "
                f"parse_error={costing_data.get('parse_error')}). "
                f"First 1500 chars of raw: {raw_response[:1500]!r}"
            )
    except Exception:
        pass

    # No canned-fallback substitution. The user's explicit ask: "I want just
    # direct usage from the agent to get the output." If the agent emitted
    # parseable JSON, the structured Cost Breakdown UI renders. If it emitted
    # prose instead, the chat handler renders that prose verbatim via the
    # raw_response field (no canned "Scope clarification required" message
    # is substituted). This matches claude.ai's behavior and exposes the
    # agent's actual output so we can debug + tune prompts based on what
    # it really says rather than what a synthetic fallback says.
    #
    # The legacy fallback that previously lived here was substituting a
    # generic "I could not derive priceable line items..." message whenever
    # the agent's JSON was empty or its output was prose — masking the
    # actual agent behavior and confusing users with an artificial reply.
    # See the prior session's enhanced_costing_agent.py:1514+ history.

    # Surface the raw_response on the costing_data dict so downstream chat
    # rendering can fall through to plain markdown when no structured items
    # were produced. The frontend renders this as the assistant message.
    if not (costing_data.get("line_items") or []) and raw_response.strip():
        costing_data["raw_response"] = raw_response

    # Phase 2 — propagate trim notes from the raw costing_result so the
    # caller (chat_agent_wrappers / streaming_handler) can emit a typed
    # agent_warning describing what was dropped.
    _trim_notes = costing_raw.get("trim_notes") if isinstance(costing_raw, dict) else None
    if _trim_notes:
        costing_data["trim_notes"] = _trim_notes
    if isinstance(costing_raw, dict) and costing_raw.get("_overflow_after_trim"):
        costing_data["_overflow_after_trim"] = True

    # Propagate the component build-up marker so the persist caller skips
    # NIT-mirror validation and defaults to the client-annexure layout.
    if state.get("_component_mode"):
        costing_data["_component_mode"] = True

    return {
        "costing_result": costing_data,
        "status": "completed",
    }


# ── Routing ───────────────────────────────────────────────────────────────────

def _should_clarify(state: CostingAgentState) -> str:
    if state.get("clarification_needed"):
        return "clarify"
    return "run"


def _is_failed(state: CostingAgentState) -> str:
    return "parse"


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_enhanced_costing_graph(db: Session):
    """Compile and return the enhanced costing StateGraph."""

    async def _load_training(state):
        return await load_training_context_node(state, db)

    async def _check_clarification(state):
        return await check_clarification_needed_node(state, db)

    async def _run_react(state):
        return await run_costing_react_node(state, db)

    async def _run_batched(state):
        return await run_costing_batched_node(state, db)

    async def _run_component(state):
        return await run_costing_component_node(state, db)

    def _route_costing(state):
        # Clarification always wins; otherwise pick single-call vs batched by
        # the captured BOQ size (closure over db for the count query).
        if state.get("clarification_needed"):
            return "clarify"
        return _pick_costing_strategy(state, db)

    graph = StateGraph(CostingAgentState)

    graph.add_node("load_training_context", _load_training)
    graph.add_node("check_clarification_needed", _check_clarification)
    graph.add_node("emit_clarification", emit_clarification_node)
    graph.add_node("run_costing_react", _run_react)
    graph.add_node("run_costing_batched", _run_batched)
    graph.add_node("run_costing_component", _run_component)
    graph.add_node("parse_and_return", parse_and_return_node)

    graph.set_entry_point("load_training_context")
    graph.add_edge("load_training_context", "check_clarification_needed")

    graph.add_conditional_edges(
        "check_clarification_needed",
        _route_costing,
        {
            "clarify": "emit_clarification",
            "single": "run_costing_react",
            "batched": "run_costing_batched",
            "component_expansion": "run_costing_component",
        },
    )

    graph.add_edge("emit_clarification", END)

    graph.add_conditional_edges(
        "run_costing_react",
        _is_failed,
        {"end": END, "parse": "parse_and_return"},
    )

    # Component build-up reuses the react node, so it routes identically.
    graph.add_conditional_edges(
        "run_costing_component",
        _is_failed,
        {"end": END, "parse": "parse_and_return"},
    )

    # The batched node persists + sets costing_result directly (no _raw_response
    # to parse), so it routes straight to END — sending it through
    # parse_and_return would overwrite its result with an empty parse.
    graph.add_edge("run_costing_batched", END)

    graph.add_edge("parse_and_return", END)

    return graph.compile()


# ── Public entry point ────────────────────────────────────────────────────────

async def run_enhanced_costing_research(
    db: Session,
    tender_id: Optional[int],
    analysis_result: dict,
    user_id: Optional[int] = None,
    mode: str = "pipeline",
    session_id: Optional[str] = None,
    clarification_answers: Optional[dict] = None,
    initial_message: Optional[str] = None,
    proposal_session_id: Optional[int] = None,
) -> dict:
    """
    Run the enhanced costing research agent.

    Args:
        db: Database session
        tender_id: Tender to cost (None for standalone general queries)
        analysis_result: Document analysis output from Step 1 (empty dict if standalone)
        user_id: User triggering the research
        mode: "pipeline" (automated, no clarification) or "chat" (interactive)
        session_id: Chat session ID for conversation tracking
        clarification_answers: Answers from user to a previous clarification question
        initial_message: Raw user message (used in standalone chat mode without tender_id)

    Returns:
        Dict with keys: tender_id, costing, metrics, status
        Additional keys in chat mode: clarification_question (when status="needs_clarification")
    """
    # Inherit any caller-supplied run_id (e.g. RQ worker → chat wrapper); only
    # synthesise a local one when this entrypoint is called stand-alone.
    rid = current_run_id() or (
        f"costing-s{session_id}" if session_id
        else f"costing-t{tender_id}" if tender_id
        else "costing-adhoc"
    )
    with run_id_scope(rid):
        return await _run_enhanced_costing_research_inner(
            db=db,
            tender_id=tender_id,
            analysis_result=analysis_result,
            user_id=user_id,
            mode=mode,
            session_id=session_id,
            clarification_answers=clarification_answers,
            initial_message=initial_message,
            proposal_session_id=proposal_session_id,
        )


async def _run_enhanced_costing_research_inner(
    db: Session,
    tender_id: Optional[int],
    analysis_result: dict,
    user_id: Optional[int] = None,
    mode: str = "pipeline",
    session_id: Optional[str] = None,
    clarification_answers: Optional[dict] = None,
    initial_message: Optional[str] = None,
    proposal_session_id: Optional[int] = None,
) -> dict:
    initial_state: CostingAgentState = {
        "messages": [HumanMessage(content=initial_message or "")] if initial_message else [],
        "tender_id": tender_id,
        "analysis_result": analysis_result or {},
        "mode": mode,
        "session_id": session_id,
        "user_id": user_id,
        "proposal_session_id": proposal_session_id,
        "training_context": "",
        "training_chars": 0,
        "anonymization_map": {},
        "clarification_needed": False,
        "clarification_question": "",
        "clarification_answers": clarification_answers or {},
        "travel_delegation_request": None,
        "travel_result": None,
        "costing_result": {},
        "errors": [],
        "status": "running",
    }

    try:
        import asyncio as _asyncio
        import math as _math

        # Self-heal: ensure the NIT bidding schedule is captured before the graph
        # routes. Idempotent no-op when a complete schedule already exists (chat
        # path pre-parses; pipeline runs after the analyzer). This guarantees the
        # deterministic batched/skeleton path engages for any entry point that
        # reaches the engine with an unparsed schedule — so large NITs are never
        # summarised by the single-call fallback.
        if tender_id:
            try:
                from app.services.boq_parser_service import ensure_boq_parsed
                await ensure_boq_parsed(db, tender_id)
            except Exception as _e:
                logger.warning(f"[costing] ensure_boq_parsed pre-graph failed (non-fatal): {_e}")
                try:
                    db.rollback()
                except Exception:
                    pass

        compiled_graph = build_enhanced_costing_graph(db)

        # Dynamic timeout, keyed on the route the run will actually take, and
        # capped inside the RQ job / Master Agent limits that enclose this call.
        _timeout = _SINGLE_CALL_TIMEOUT_S
        _cnt = 0
        _max_sweeps = 2
        try:
            if tender_id:
                from app.services.settings_service import get_setting_value
                from app.models.costing_template import BOQItem
                _bs = int(get_setting_value(db, "costing.batch_size", 60) or 60)
                _max_sweeps = int(get_setting_value(db, "costing.costing_max_sweeps", 2) or 2)
                _conc = _batch_concurrency(db)
                _cnt = db.query(BOQItem).filter(BOQItem.tender_id == tender_id).count()
                _timeout = _costing_timeout_seconds(
                    _cnt, _bs, _max_sweeps, concurrency=_conc, cap=_costing_budget_cap_s(),
                )
                if _cnt > 0:
                    logger.info(
                        f"[costing] tender {tender_id}: batched timeout "
                        f"{_timeout}s for {_cnt} rows / "
                        f"{_math.ceil(_cnt / max(1, _bs))} batch(es), "
                        f"{_conc} at a time (+{_max_sweeps} sweep budget)"
                    )
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        # While this costing runs, the analyzer must not re-capture the
        # schedule out from under it (every BOQItem id would change).
        _costing_started = datetime.now(timezone.utc)
        _flagged = False
        if tender_id:
            try:
                from app.services.boq_parser_service import mark_costing_running
                _flagged = mark_costing_running(tender_id, ttl_seconds=int(_timeout) + _OUTER_HEADROOM_S)
            except Exception:
                _flagged = False
        try:
            try:
                final_state = await _asyncio.wait_for(
                    compiled_graph.ainvoke(initial_state),
                    timeout=_timeout,
                )
            except _asyncio.TimeoutError:
                # The batched node commits every batch as it finishes, so a
                # timeout mid-run is a partial result on disk, not a loss.
                # Return it (rolled up, totalled) instead of dying with nothing.
                partial = None
                if tender_id and _cnt > 0:
                    partial = _recover_partial_batched_result(
                        db, tender_id, since=_costing_started,
                        analysis_result=analysis_result or {},
                        budget_s=int(_timeout), max_sweeps=_max_sweeps,
                    )
                if partial is None:
                    raise
                final_state = {**initial_state, **partial}
        finally:
            if _flagged:
                try:
                    from app.services.boq_parser_service import clear_costing_running
                    clear_costing_running(tender_id)
                except Exception:
                    pass

        status = final_state.get("status", "completed")
        costing = final_state.get("costing_result", {})

        result = {
            "tender_id": tender_id,
            "costing": costing,
            "status": status,
            "metrics": {},
        }

        if status == "needs_clarification":
            result["clarification_question"] = final_state.get("clarification_question", "")

        return result

    except Exception as e:
        logger.error(f"Enhanced costing research failed: {e}", exc_info=True)
        return {
            "tender_id": tender_id,
            "costing": {
                "line_items": [{
                    "sr_no": 1,
                    "description": "Costing pipeline error — retry or share scope manually",
                    "category": "overhead",
                    "quantity": None, "unit": None, "rate": None, "amount": None,
                    "rate_source": "needs_user_input",
                    "source_ref": f"Pipeline error: {type(e).__name__}: {str(e)[:300]}",
                    "confidence": "low",
                }],
                "assumptions": [f"Auto-generated fallback — pipeline raised {type(e).__name__}."],
                "recommendations": [f"The costing pipeline encountered an error: {str(e)[:300]}. Try again."],
            },
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }
