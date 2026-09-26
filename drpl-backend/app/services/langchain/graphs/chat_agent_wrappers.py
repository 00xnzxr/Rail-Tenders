"""
DRPL LangChain - Chat Agent Wrappers
Thin async wrappers that adapt existing pipeline agents for chat-based invocation
by the agent router. Each wrapper calls the existing pipeline function and formats
the result for the unified chat interface.
"""

import asyncio
import json
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.services.langchain.tools.xlsx_generator_tool import build_cost_xlsx

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# execute_agent bridge — shared helpers
# ────────────────────────────────────────────────────────────────────────────
#
# The bridge shims (chat_*_via_execute_agent) all need the same prep step:
# embed any available tender context (PDF text, prior analysis, BOQ items)
# into the user message as plain text, then call execute_agent which goes
# through the standard LangChain ReAct path that creates AgentExecution
# rows for monitoring.
#
# We send context as TEXT rather than native multimodal vision blocks so the
# generic execute_langchain_agent path handles it — that path doesn't know
# about PDF blocks. For text-searchable PDFs this is essentially lossless;
# for scans, _extract_tender_pdf_text uses Claude vision + OCR fallbacks
# transparently.


def _resilient_update_execution(
    db: Session,
    execution,
    *,
    status: str,
    error_message: Optional[str] = None,
    output_summary: Optional[str] = None,
    latency_ms: Optional[int] = None,
) -> None:
    """Mark an AgentExecution row terminal via a fresh DB session.

    Thin shim over `agent_execution_finalizer.finalize_execution_row` —
    kept for callers that already have an `execution` ORM object. The
    fresh-session helper isolates the update from whatever state the
    main `db` session is in (the auto-chain pipeline does its own
    commits/rollbacks, which often leaves the caller's session unable
    to commit by the time we want to record the run's outcome).
    """
    if not execution:
        return
    from app.services.agent_execution_finalizer import finalize_execution_row
    finalize_execution_row(
        getattr(execution, "id", None),
        status=status,
        error_message=error_message,
        output_summary=output_summary,
        latency_ms=latency_ms,
    )


async def _ensure_tender_analysis(
    db: Session,
    tender_id: int,
    *,
    # Bumped from 480s to 1200s (20 min). The analyzer now retries Anthropic
    # overload at 30/60/90s outer backoff per call, so a single sustained
    # overload window can add ~3 minutes per LLM call. With 3-5 PDFs running
    # sequentially (tender_analyzer_max_parallel=1) plus synthesis, the
    # worst-case path is now ~15 min. 1200s gives headroom without making
    # the user wait forever on a hung run.
    timeout_s: float = 1200.0,
    parent_execution_id: Optional[int] = None,
) -> Optional[object]:
    """Ensure a TenderAnalysisSummary exists for ``tender_id``, running the
    deep_analyzer pipeline first if it doesn't.

    This is the auto-chain primitive: when the costing agent (or any
    downstream agent that consumes structured analysis) is invoked
    without a prior analysis, we run it inline before continuing. The
    summary becomes the canonical compact (~5K chars) tender context
    that downstream agents read instead of raw 200-page PDF dumps —
    sidestepping the input-too-long failure mode and producing better
    estimates because the analyzer has properly extracted scope.

    Returns the (possibly-just-created) ``TenderAnalysisSummary`` row,
    or ``None`` if the analysis is unavailable AND running it failed
    or timed out. Callers should fall through to a degraded path
    (e.g., raw PDF extraction) when None is returned.
    """
    import time as _time
    from app.models.document_analysis import TenderAnalysisSummary
    from app.models.agent_builder import AgentExecution, CustomAgent
    from app.models.tender import TenderDocument

    # Cached? Only honour the cache when the document set hasn't grown since
    # the last analysis — otherwise the user uploaded more PDFs and expects
    # the analyzer to read them. Stale-cache reuse on grown document sets
    # was producing analyses that silently ignored the latest BoQ / addendum
    # PDFs and propagating into every downstream agent.
    try:
        existing = (
            db.query(TenderAnalysisSummary)
            .filter(TenderAnalysisSummary.tender_id == tender_id)
            .order_by(TenderAnalysisSummary.created_at.desc())
            .first()
        )
        if existing and existing.requirement_summary and existing.requirement_summary.strip():
            # Freshness check: compare the analyzed-doc count against the
            # current PDF count. PDFs are what the v2 analyzer reads — if
            # the count matches what the cached summary was built from, the
            # cache is fresh; otherwise re-run.
            try:
                current_pdf_count = (
                    db.query(TenderDocument)
                    .filter(
                        TenderDocument.tender_id == tender_id,
                        TenderDocument.mime_type == "application/pdf",
                    )
                    .count()
                )
            except Exception as _count_err:
                logger.warning(
                    f"[bridge] tender {tender_id}: doc-count freshness check "
                    f"failed: {_count_err} — using cache anyway"
                )
                try:
                    db.rollback()
                except Exception:
                    pass
                current_pdf_count = None

            # `documents_analyzed` counts the READABLE documents; the PDFs the
            # analyzer could not read (page cap, bad file) are counted apart.
            # Comparing readable-only against every PDF made a tender with one
            # unreadable PDF "stale" on every call and re-analysed it each time.
            analyzed_count = (getattr(existing, "documents_analyzed", 0) or 0) + (
                getattr(existing, "per_doc_unreadable_count", 0) or 0
            )
            cache_is_fresh = (
                current_pdf_count is None  # couldn't check — trust the cache
                or current_pdf_count <= analyzed_count
            )
            # Exact check when the per-doc rows carry content hashes: fresh
            # only if every PDF on the tender today was read as-is.
            try:
                from app.services.analysis_reuse import analysis_is_current, reuse_enabled
                if reuse_enabled(db):
                    _current, _why = analysis_is_current(db, tender_id)
                    if _current:
                        cache_is_fresh = True
                    elif _why not in ("no_hashed_per_doc_rows", "no_pdfs") and not _why.startswith("check_failed"):
                        cache_is_fresh = False
                        logger.info(f"[bridge] tender {tender_id}: analysis not current ({_why})")
            except Exception as _fresh_err:
                logger.debug(f"[bridge] hash freshness check skipped: {_fresh_err}")
            if cache_is_fresh:
                logger.info(
                    f"[bridge] tender {tender_id}: reusing cached analysis "
                    f"summary ({len(existing.requirement_summary)} chars, "
                    f"docs_analyzed={analyzed_count}, "
                    f"current_pdfs={current_pdf_count})"
                )
                return existing
            logger.info(
                f"[bridge] tender {tender_id}: cached analysis is STALE — "
                f"current_pdfs={current_pdf_count} > docs_analyzed={analyzed_count}. "
                f"Re-running deep_analyzer."
            )
    except Exception as e:
        logger.warning(
            f"[bridge] analysis lookup failed for tender {tender_id}: {e}"
        )
        try:
            db.rollback()
        except Exception:
            pass
        # Fall through and try to run the analyzer anyway

    logger.info(
        f"[bridge] tender {tender_id}: no cached analysis — running "
        f"deep_analyzer (timeout {timeout_s}s)"
    )

    # Create an AgentExecution row for the analyzer call so it shows up in
    # monitoring just like a user-triggered Test run. We COMMIT the row
    # immediately rather than just flushing — the analyzer pipeline below
    # performs its own commits/rollbacks, and a rollback there would
    # otherwise discard our flushed-but-uncommitted row, leaving no
    # monitoring evidence that the analyzer was even invoked.
    analyzer_record: Optional[CustomAgent] = None
    execution: Optional[AgentExecution] = None
    try:
        analyzer_record = (
            db.query(CustomAgent)
            .filter(CustomAgent.agent_key == "tender_doc_analyzer")
            .first()
        )
        if analyzer_record:
            execution = AgentExecution(
                agent_id=analyzer_record.id,
                agent_version=analyzer_record.current_version or 1,
                trigger="chat",
                input_summary=(
                    f"Chat-triggered analysis of tender {tender_id} "
                    f"to produce TenderAnalysisSummary"
                )[:2000],
                status="running",
                parent_execution_id=parent_execution_id,
            )
            db.add(execution)
            db.commit()
            db.refresh(execution)
            logger.info(
                f"[bridge] tender {tender_id}: created AgentExecution "
                f"id={execution.id} for chat analyzer (committed)"
            )
    except Exception as e:
        logger.warning(f"[bridge] could not create AgentExecution row: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        execution = None

    start = _time.time()
    from app.services.langchain.graphs.document_analysis_agent import (
        run_document_analysis,
    )
    from app.services.langchain.error_utils import is_recoverable_error

    # Auto-retry recoverable failures (transient provider overload / rate-limit,
    # or a recoverable DB transaction state) before giving up — a re-analysis
    # shouldn't dump "please try again" on the user when a rollback + retry fixes
    # it. Timeouts and non-recoverable errors fail fast. The non-destructive
    # persist (see document_analysis_agent) guarantees a failed attempt never
    # wipes the prior good summary.
    _MAX_ATTEMPTS = 3
    _ran = False
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            await asyncio.wait_for(
                run_document_analysis(db=db, tender_id=tender_id),
                timeout=timeout_s,
            )
            _ran = True
            break
        except asyncio.TimeoutError:
            elapsed_ms = int((_time.time() - start) * 1000)
            msg = f"timed out after {timeout_s}s"
            logger.warning(f"[bridge] analyzer {msg} for tender {tender_id}")
            # Loud stdout print — surfaces in the uvicorn terminal even when
            # SQL logs are flooding it.
            print(
                f"[DRPL ANALYZER ERROR] tender {tender_id}: analyzer {msg}",
                flush=True,
            )
            _resilient_update_execution(
                db, execution,
                status="failed",
                error_message=f"timeout after {timeout_s}s",
                latency_ms=elapsed_ms,
            )
            return None
        except Exception as e:
            elapsed_ms = int((_time.time() - start) * 1000)
            logger.exception(
                f"[bridge] analyzer attempt {attempt}/{_MAX_ATTEMPTS} raised for "
                f"tender {tender_id}: {type(e).__name__}: {e}"
            )
            print(
                f"[DRPL ANALYZER ERROR] tender {tender_id} attempt {attempt}: "
                f"{type(e).__name__}: {str(e)[:500]}",
                flush=True,
            )
            try:
                db.rollback()
            except Exception:
                pass
            if is_recoverable_error(e) and attempt < _MAX_ATTEMPTS:
                backoff = 2.0 * attempt
                logger.warning(
                    f"[bridge] analyzer recoverable error — auto-retrying in "
                    f"{backoff:.1f}s (attempt {attempt + 1}/{_MAX_ATTEMPTS})"
                )
                await asyncio.sleep(backoff)
                continue
            _resilient_update_execution(
                db, execution,
                status="failed",
                error_message=str(e)[:1000],
                latency_ms=elapsed_ms,
            )
            return None

    if not _ran:
        return None

    elapsed_ms = int((_time.time() - start) * 1000)

    # Re-query for the freshly-persisted summary
    summary: Optional[TenderAnalysisSummary] = None
    try:
        summary = (
            db.query(TenderAnalysisSummary)
            .filter(TenderAnalysisSummary.tender_id == tender_id)
            .order_by(TenderAnalysisSummary.created_at.desc())
            .first()
        )
    except Exception as e:
        logger.warning(f"[bridge] re-query for analysis summary failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass

    summary_chars = (
        len(summary.requirement_summary or "") if summary and summary.requirement_summary else 0
    )

    _resilient_update_execution(
        db, execution,
        status="completed" if summary_chars > 0 else "failed",
        latency_ms=elapsed_ms,
        output_summary=(
            f"Auto-chain analysis completed in {elapsed_ms}ms — "
            f"summary {summary_chars} chars"
        ),
        error_message=(
            None if summary_chars > 0
            else "analyzer ran but produced no requirement_summary"
        ),
    )

    if summary_chars == 0:
        logger.warning(
            f"[bridge] tender {tender_id}: analyzer ran but produced "
            f"no usable requirement_summary"
        )
        print(
            f"[DRPL ANALYZER ERROR] tender {tender_id}: analyzer "
            f"completed but produced no requirement_summary "
            f"(elapsed {elapsed_ms}ms). Check tender_analysis_summaries "
            f"row + DocumentExtractionResult rows for this tender — "
            f"likely all per-doc extractions returned unreadable.",
            flush=True,
        )
        return None

    logger.info(
        f"[bridge] tender {tender_id}: analyzer completed "
        f"in {elapsed_ms}ms — summary {summary_chars} chars"
    )
    return summary


def _build_tender_context_block(
    db: Session,
    tender_id: Optional[int],
    file_metadata: Optional[dict] = None,
    *,
    max_pdf_chars: int = 20_000,
    max_total_chars: int = 25_000,
) -> str:
    """Build a plain-text tender context block to embed in a user message.

    Concatenates whatever's available — PDF extracts, prior analysis summary,
    BOQ items — in a clearly delimited format the agent's system prompt
    knows how to read ("TENDER PDF EXTRACTS", "PRIOR ANALYSIS", etc.).
    Returns an empty string when there is no usable tender context, so the
    caller can decide whether to skip it entirely.

    Size discipline: caps each individual source at ``max_pdf_chars`` AND
    the merged total at ``max_total_chars``. Prevents the "input too long"
    error when the costing agent's training-data injection (up to 200K
    chars in the system prompt) plus our context block plus the user
    message together exceed the model's context window.

    Deduplication: when ``tender_id`` is set, the tender's PDFs are the
    canonical source — we DO NOT also embed file_metadata.extracted_text
    in that case, since chat-attached PDFs are auto-registered as
    TenderDocument rows and would just duplicate the same content. We
    only fall back to file_metadata when there's no tender at all.
    """
    parts: list[str] = []
    used_chars = 0

    def _try_add(label: str, body: str, cap: int) -> None:
        nonlocal used_chars
        if not body or not body.strip():
            return
        remaining = max_total_chars - used_chars
        if remaining <= 200:  # not enough headroom for a meaningful section
            return
        clip = min(cap, remaining)
        snippet = body[:clip]
        parts.append(f"{label}:\n{snippet}")
        used_chars += len(snippet)

    if tender_id:
        # Look up the prior analysis summary FIRST so we can decide whether
        # the PDF extracts (which can be 20K+ chars) are still needed. When
        # the deep_analyzer has already distilled the same content into the
        # structured summary, re-shoveling the raw PDF text is the dominant
        # source of "input too long" overflow on the costing chat path —
        # the costing system prompt already carries up to 200K chars of
        # training-data injection, and adding 20K of redundant PDF on top
        # pushes total context past Anthropic's 200K ceiling.
        prior_summary_text = ""
        try:
            from app.models.document_analysis import TenderAnalysisSummary
            summary_row = (
                db.query(TenderAnalysisSummary)
                .filter(TenderAnalysisSummary.tender_id == tender_id)
                .order_by(TenderAnalysisSummary.created_at.desc())
                .first()
            )
            if summary_row and summary_row.requirement_summary:
                prior_summary_text = summary_row.requirement_summary.strip()
        except Exception as e:
            logger.debug(f"[bridge] analysis lookup failed for tender {tender_id}: {e}")
            try:
                db.rollback()
            except Exception:
                pass

        _has_useful_analysis = len(prior_summary_text) >= 1_000

        # PDF text extracts — included only when the analyzer hasn't produced
        # a usable summary yet. With a populated summary, the extracts are
        # redundant and just inflate the input toward context overflow.
        if not _has_useful_analysis:
            try:
                from app.services.langchain.graphs.enhanced_costing_agent import (
                    _extract_tender_pdf_text,
                )
                pdf_text = _extract_tender_pdf_text(
                    db, tender_id, max_docs=3, max_chars=max_pdf_chars,
                )
                _try_add(f"TENDER PDF EXTRACTS (tender #{tender_id})", pdf_text, max_pdf_chars)
            except Exception as e:
                logger.debug(f"[bridge] PDF extract failed for tender {tender_id}: {e}")
        else:
            logger.info(
                f"[bridge] tender {tender_id}: prior analysis summary present "
                f"({len(prior_summary_text)} chars) — skipping raw PDF extracts "
                f"to stay under context budget"
            )

        if prior_summary_text:
            # Give the analysis summary more room (8K) when it's the
            # primary source of tender context; it's the LLM-distilled
            # version of everything we'd otherwise shovel as raw PDF text.
            _summary_cap = 8_000 if _has_useful_analysis else 4_000
            _try_add("PRIOR ANALYSIS SUMMARY", prior_summary_text, _summary_cap)

    else:
        # No tender_id — fall back to file_metadata.extracted_text if the
        # user attached files but the auto-tender registration didn't fire.
        # We DON'T do this when tender_id is set: that path's PDF extraction
        # already covers the same content (chat attachments are registered
        # as TenderDocument rows for the auto-created tender).
        fm = file_metadata or {}
        extracted = fm.get("extracted_text") or fm.get("file_content")
        if extracted and isinstance(extracted, str):
            _try_add("ATTACHED FILE CONTENT", extracted, max_pdf_chars)

    block = "\n\n".join(parts).strip()
    if block:
        logger.info(
            f"[bridge] tender_context_block built — {used_chars} chars, "
            f"{len(parts)} section(s), tender_id={tender_id}"
        )
    return block


def _build_user_message_with_context(
    db: Session,
    user_message: str,
    *,
    tender_id: Optional[int] = None,
    file_metadata: Optional[dict] = None,
    framing: Optional[str] = None,
) -> str:
    """Assemble the final user_message that goes into execute_agent.

    Layout (all sections optional, omitted when empty):

        <framing — agent-specific intro, e.g. "Produce a costing for...">

        USER REQUEST: <user_message>

        <tender context block>

    The agent's system prompt already knows how to read the TENDER PDF
    EXTRACTS / PRIOR ANALYSIS sections; we just have to feed them in.
    """
    sections: list[str] = []
    if framing:
        sections.append(framing.strip())
    sections.append(f"USER REQUEST: {user_message.strip()}")
    ctx = _build_tender_context_block(db, tender_id, file_metadata)
    if ctx:
        sections.append(ctx)
    return "\n\n".join(sections)


def _format_bridge_result(
    result: dict,
    *,
    agent_key: str,
    output_type: str,
    structured_data: Optional[dict] = None,
) -> dict:
    """Adapt the dict returned by execute_agent() to the chat-handler shape
    that streaming_handler.py expects.

    Critical: output is the agent's RAW text — no canned-fallback substitution.
    The user said "I want just direct usage from the agent to get the output."

    Persistence signals (added in the costing-reliability work — see plan
    now-i-need-to-synchronous-taco.md) are forwarded so the streaming layer
    can decide whether to emit `artifact_created` (real persistence) or
    `agent_error` (persistence failed / no line items). Without these
    fields the streaming layer would fall back to the heuristic artifact
    extractor, which is the "lying artifact card" bug.
    """
    return {
        "output": result.get("output") or "",
        "output_type": output_type,
        "agent_key": agent_key,
        "structured_data": structured_data,
        "tool_calls": result.get("tool_calls") or [],
        "metrics": {
            "tokens_input": result.get("tokens_input"),
            "tokens_output": result.get("tokens_output"),
            "cost_estimate": result.get("cost_estimate"),
            "latency_ms": result.get("latency_ms"),
            "execution_id": result.get("execution_id"),
        },
        "execution_id": result.get("execution_id"),
        "status": result.get("status", "completed"),
        "error": result.get("error"),
        # Persistence signals — forwarded for the cost-breakdown path so the
        # streaming layer can gate `artifact_created` events on real DB writes.
        "persistence_failure": result.get("persistence_failure"),
        "cost_breakdown_id": result.get("cost_breakdown_id"),
        "xlsx_artifact": result.get("xlsx_artifact"),
        # Context-budget signals (Phase 2 — see plan). The trim policy attaches
        # `trim_notes` listing what was dropped so the streaming layer can
        # emit a typed `agent_warning` with code=context_trimmed.
        "trim_notes": result.get("trim_notes"),
    }


def _kick_post_analysis_pipeline(tender_id: int) -> None:
    """Fire-and-forget background run of the post-analysis fan-out.

    Used by the chat analysis path so annexures/checklist/workspace fill in
    after the chat response has already been streamed back to the user.
    Uses a fresh DB session inside the task (SA sessions are not safe to
    share across coroutines / request boundaries). Best-effort — never
    raises into the caller.
    """
    try:
        from app.services.post_analysis_pipeline import (
            run_pipeline_in_background,
        )
        # Schedule on the running loop; if no loop (shouldn't happen inside an
        # async handler), fall back to a thread so the chat response isn't
        # blocked.
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                None, run_pipeline_in_background, tender_id, None
            )
        except RuntimeError:
            import threading
            threading.Thread(
                target=run_pipeline_in_background,
                args=(tender_id, None),
                daemon=True,
            ).start()
        logger.info(f"[chat] post-analysis pipeline scheduled for tender {tender_id}")
    except Exception as e:
        logger.warning(f"[chat] failed to schedule post-analysis pipeline: {e}")


# --- Session Context Helpers ---


def _build_history_context(
    conversation_history: list[dict],
    max_turns: int = 10,
    max_chars_per_turn: int = 2000,
) -> str:
    """Format recent conversation history as a context block for agent prompts."""
    if not conversation_history:
        return ""

    recent = conversation_history[-max_turns:]
    lines = ["## Conversation History"]

    for turn in recent:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        output_type = turn.get("output_type", "")
        routed_from = turn.get("routed_from", "")

        # Truncate content
        if len(content) > max_chars_per_turn:
            content = content[:max_chars_per_turn] + "..."

        # Build label
        if role == "assistant" and routed_from:
            label = f"[assistant → {routed_from}"
            if output_type:
                label += f" ({output_type})"
            label += "]"
        else:
            label = f"[{role}]"

        lines.append(f"{label}: {content}")

    return "\n".join(lines)


def _get_session_context(
    db: Session,
    conversation_history: list[dict],
    proposal_session_id: Optional[int] = None,
) -> dict:
    """
    Extract session context from conversation history and artifacts.
    Returns a dict with prior analysis, file content, and artifact info.
    """
    context = {
        "has_prior_analysis": False,
        "analysis_content": "",
        "has_prior_checklist": False,
        "has_prior_costing": False,
        "prior_file_content": "",
        "artifacts": [],
    }

    if not conversation_history:
        conversation_history = []

    # Scan conversation history for prior outputs
    for turn in conversation_history:
        output_type = turn.get("output_type", "")
        content = turn.get("content", "")

        if output_type == "document_analysis" and content:
            context["has_prior_analysis"] = True
            # Keep the most recent analysis (last one wins)
            context["analysis_content"] = content[:8000]
        elif output_type == "checklist":
            context["has_prior_checklist"] = True
        elif output_type == "cost_breakdown":
            context["has_prior_costing"] = True

        # Extract file content from earlier messages
        if "[FILE CONTENT:" in content and not context["prior_file_content"]:
            # Grab the file content block from the message
            fc_start = content.find("[FILE CONTENT:")
            context["prior_file_content"] = content[fc_start:fc_start + 8000]

    # Load artifacts from the session if available
    if proposal_session_id:
        try:
            from app.services.artifact_service import list_artifacts
            artifacts = list_artifacts(db, proposal_session_id)
            context["artifacts"] = artifacts

            # Also check artifacts for analysis/checklist/costing
            for a in artifacts:
                atype = a.get("artifact_type", "")
                if atype == "analysis" and not context["has_prior_analysis"]:
                    context["has_prior_analysis"] = True
                    context["analysis_content"] = (a.get("content", "") or "")[:8000]
                elif atype == "checklist":
                    context["has_prior_checklist"] = True
                elif atype == "cost_breakdown":
                    context["has_prior_costing"] = True
        except Exception as e:
            logger.warning(f"Failed to load session artifacts: {e}")
            try:
                db.rollback()
            except Exception:
                pass

    return context


def _build_session_context_block(session_ctx: dict) -> str:
    """Build a session context block to inject into agent user messages."""
    parts = []
    completed = []
    if session_ctx.get("has_prior_analysis"):
        completed.append("Document analysis (deep_analyzer)")
    if session_ctx.get("has_prior_checklist"):
        completed.append("Checklist generation (checklist_generator)")
    if session_ctx.get("has_prior_costing"):
        completed.append("Costing research (costing_researcher)")

    if not completed and not session_ctx.get("prior_file_content"):
        return ""

    parts.append("## Session Context")
    parts.append("Previously in this session:")

    if completed:
        for item in completed:
            parts.append(f"- {item} was completed")

    if session_ctx.get("prior_file_content"):
        parts.append("- Documents were uploaded and are available in the conversation history")

    if session_ctx.get("analysis_content"):
        # Include a brief summary from the prior analysis
        analysis_preview = session_ctx["analysis_content"][:6000]
        parts.append(f"\n### Prior Analysis Summary\n{analysis_preview}")

    parts.append(
        "\nUse this context to fulfill the user's request. "
        "Do NOT ask for documents or IDs that are already available in the session."
    )
    return "\n".join(parts)


def _persist_chat_analysis_to_db(db: Session, tender_id: int, result: dict) -> None:
    """Persist analysis output from the chat-upload path to DocumentExtractionResult
    so that workspace agents have access to the analysis context.

    If the result contains structured_data (from run_document_analysis pipeline),
    delegate to _persist_analysis_results. Otherwise, save the raw output text
    as a single 'full_analysis' extraction record.
    """
    try:
        structured = result.get("structured_data")
        if structured and isinstance(structured, dict):
            from app.services.langchain.graphs.document_analysis_agent import _persist_analysis_results
            _persist_analysis_results(db, tender_id, structured, result.get("metrics", {}))
            return

        # Fallback: save the full markdown output as a raw extraction result
        output_text = result.get("output", "")
        if not output_text or len(output_text) < 100:
            return

        from app.models.document_analysis import DocumentExtractionResult
        extraction = DocumentExtractionResult(
            tender_id=tender_id,
            document_name="chat_attachment_analysis",
            extraction_type="full_analysis",
            items=[{"text": output_text[:10000], "category": "full_analysis"}],
            raw_text=output_text[:50000],
            extraction_model="chat_upload_analysis",
            completeness_score=0.7,
        )
        db.add(extraction)
        db.commit()
        logger.info(f"Persisted chat analysis results for tender {tender_id}")
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to persist chat analysis results: {e}")


def _stored_analysis_result(db: Session, tender_id: int) -> Optional[dict]:
    """The chat result for a tender whose analysis is already current, in the
    same shape `chat_document_analysis` returns after a fresh run."""
    from app.models.document_analysis import TenderAnalysisSummary
    try:
        existing = (
            db.query(TenderAnalysisSummary)
            .filter(TenderAnalysisSummary.tender_id == tender_id)
            .first()
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None
    if not existing or not (existing.requirement_summary or "").strip():
        return None
    analysis = {"report_markdown": existing.requirement_summary}
    when = existing.last_analyzed_at.isoformat() if existing.last_analyzed_at else "earlier"
    output = _format_analysis_output(analysis, tender_id) + (
        f"\n_Stored analysis from {when}: the tender's documents have not changed "
        f"since it was produced, so they were not re-read. Ask to re-analyze "
        f"to run it again._\n"
    )
    metrics = {
        "method": "stored_analysis",
        "reused": True,
        "documents_count": (existing.documents_analyzed or 0)
        + (existing.per_doc_unreadable_count or 0),
        "documents_unreadable": existing.per_doc_unreadable_count or 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cost_usd": 0.0,
        "tool_calls": [],
    }
    return {
        "output": output,
        "output_type": "document_analysis",
        "agent_key": "deep_analyzer",
        "tool_calls": [],
        "metrics": metrics,
        "structured_data": analysis,
        "status": "completed",
    }


async def chat_document_analysis(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
) -> dict:
    """
    Chat wrapper for document analysis.
    Wraps run_document_analysis() for tender-linked requests,
    or delegates to tender_doc_analyzer for uploaded file analysis.
    """
    if not tender_id:
        tender_id = _extract_tender_id(message)

    # File content in the message OR PDF attachment paths take priority — even when tender_id is set.
    # This handles: (a) text extraction succeeded, (b) text extraction failed but PDF paths exist.
    has_file_content = "[FILE CONTENT:" in message
    has_pdf_attachments = bool(_get_pdf_paths(file_metadata))

    if has_file_content or has_pdf_attachments:
        logger.info(
            f"Document analysis: using uploaded files (text_in_msg={has_file_content}, "
            f"pdf_paths={has_pdf_attachments})"
        )
        if has_pdf_attachments:
            # The files know their tender (the streaming handler registered
            # them on the session's tender); a delegated tender_id is the
            # model's guess and can be missing or wrong.
            from app.services.langchain.graphs.chat_upload_analysis import tender_of_attachments
            _owner = tender_of_attachments(db, _get_pdf_paths(file_metadata))
            if _owner and _owner != tender_id:
                if tender_id:
                    logger.warning(
                        f"[chat upload] delegated tender_id={tender_id} but the attached "
                        f"files belong to tender {_owner}; analysing them there"
                    )
                tender_id = _owner
        # Enrich message with linked document context
        linked_docs_note = _get_linked_documents_context(db, file_metadata, tender_id)
        enriched_message = message
        if linked_docs_note:
            enriched_message = message + "\n\n" + linked_docs_note
        # Attached PDFs are read one document at a time (cached by their
        # bytes, a few at once) and reported in the same 7-section format;
        # the one-call path below is the fallback. See chat_upload_analysis.
        result = None
        if has_pdf_attachments and tender_id:
            from app.services.analysis_reuse import wants_fresh_analysis
            from app.services.langchain.graphs.chat_upload_analysis import (
                analyze_chat_uploads,
                per_doc_uploads_enabled,
            )
            if per_doc_uploads_enabled(db):
                try:
                    result = await analyze_chat_uploads(
                        db, enriched_message, tender_id, _get_pdf_paths(file_metadata),
                        sizes=_attachment_sizes(file_metadata),
                        force_refresh=wants_fresh_analysis(message),
                    )
                except Exception as e:
                    logger.warning(
                        f"[chat upload] per-document analysis failed "
                        f"({type(e).__name__}: {e}); using the one-call path",
                        exc_info=True,
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    result = None
        if result is None:
            result = await _analyze_uploaded_document(db, enriched_message, session_id, file_metadata=file_metadata, tender_id=tender_id)

        # Persist analysis results so workspace agents can access them
        if tender_id and result.get("status") == "completed":
            _persist_chat_analysis_to_db(db, tender_id, result)
            # Fan out: annexure extraction + checklist gen + workspace init.
            _kick_post_analysis_pipeline(tender_id)

        return result

    # Check session context for prior file content if nothing in current message
    if not tender_id:
        session_ctx = _get_session_context(db, conversation_history or [], proposal_session_id)
        if session_ctx.get("prior_file_content"):
            enriched = message + "\n\n" + session_ctx["prior_file_content"]
            return await _analyze_uploaded_document(db, enriched, session_id, file_metadata=file_metadata, tender_id=tender_id)

    if not tender_id:
        return {
            "output": (
                "I need a tender ID or an uploaded document to analyze. Please either "
                "specify a tender (e.g., 'Analyze tender 42'), select a tender, "
                "or upload a document file."
            ),
            "output_type": "general",
            "agent_key": "deep_analyzer",
            "tool_calls": [],
            "metrics": {},
            "status": "needs_input",
        }

    try:
        from app.services.langchain.graphs.document_analysis_agent import run_document_analysis
        from app.services.analysis_reuse import (
            analysis_is_current,
            post_analysis_outputs_exist,
            reuse_enabled,
            wants_fresh_analysis,
        )

        # A stored analysis built from exactly these documents is the answer;
        # re-reading every PDF again would produce the same report at full
        # cost. The user re-runs it by asking ("re-analyze", "again", ...).
        _force = wants_fresh_analysis(message)
        if not _force and reuse_enabled(db):
            _current, _why = analysis_is_current(db, tender_id)
            if _current:
                _stored = _stored_analysis_result(db, tender_id)
                if _stored is not None:
                    logger.info(
                        f"[chat] tender {tender_id}: returning the stored analysis "
                        f"({_why}); no documents re-read"
                    )
                    if not post_analysis_outputs_exist(db, tender_id):
                        _kick_post_analysis_pipeline(tender_id)
                    return _stored
            else:
                logger.info(f"[chat] tender {tender_id}: analysis will run ({_why})")

        result = await run_document_analysis(
            db=db, tender_id=tender_id, force_refresh=_force
        )

        analysis = result.get("analysis", {})
        metrics = result.get("metrics", {})

        # Check if pipeline found no documents
        if analysis.get("error") == "no_documents":
            # If we have PDF attachments from the current message, try those instead
            if has_pdf_attachments:
                logger.info("Tender has no documents, falling back to uploaded file analysis")
                return await _analyze_uploaded_document(db, message, session_id, file_metadata=file_metadata, tender_id=tender_id)

            # Also check session context for prior uploads
            session_ctx = _get_session_context(db, conversation_history or [], proposal_session_id)
            if session_ctx.get("prior_file_content"):
                enriched = message + "\n\n" + session_ctx["prior_file_content"]
                return await _analyze_uploaded_document(db, enriched, session_id, file_metadata=file_metadata, tender_id=tender_id)

            return {
                "output": (
                    f"Tender #{tender_id} does not have any uploaded documents yet. "
                    "Please upload the tender document files first (via the Tender page), "
                    "or attach them directly to this message for analysis."
                ),
                "output_type": "general",
                "agent_key": "deep_analyzer",
                "tool_calls": [],
                "metrics": {},
                "status": "needs_input",
            }

        # Format as readable markdown with structured data
        output = _format_analysis_output(analysis, tender_id)

        # Fan out: annexure extraction + checklist gen + workspace init.
        if tender_id:
            _kick_post_analysis_pipeline(tender_id)

        return {
            "output": output,
            "output_type": "document_analysis",
            "agent_key": "deep_analyzer",
            "tool_calls": metrics.get("tool_calls", []),
            "metrics": metrics,
            "structured_data": analysis,
            "status": "completed",
        }

    except Exception as e:
        logger.error(f"Chat document analysis failed: {e}", exc_info=True)
        print(f"[DRPL ERROR] Chat document analysis failed: {type(e).__name__}: {e}", flush=True)
        try:
            db.rollback()
        except Exception:
            pass
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "deep_analyzer",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }


async def _analyze_uploaded_document(
    db: Session,
    message: str,
    session_id: Optional[str] = None,
    file_metadata: Optional[dict] = None,
    tender_id: Optional[int] = None,
) -> dict:
    """
    Analyze uploaded document content without requiring a tender_id.
    Priority order:
    1. Native PDF document blocks (when PDF attachments present — handles scanned PDFs
       via Claude Vision on the raw bytes materialized from R2 storage keys). The ReAct
       `tender_doc_analyzer` can't see ChatAttachment storage keys — its tool surface
       only accepts tender_id / document_id / local file_path — so preferring it here
       would strand scanned uploads as "unreadable".
    2. tender_doc_analyzer custom agent (for text-only / no-attachment / tender_id flows)
    3. Direct LLM call (last resort)
    """
    # PRIORITY 1: Native PDF for chat-attached PDFs — materializes R2 keys to tempfiles
    # and hands bytes directly to Claude Vision. This is the only path that actually works
    # for scanned (image-only) PDFs uploaded through chat.
    pdf_paths = _get_pdf_paths(file_metadata)
    if pdf_paths:
        try:
            logger.info(f"Document analysis: native PDF first ({len(pdf_paths)} PDFs attached)")
            result = await _analyze_with_native_pdf(db, message, pdf_paths)
            if result:
                return result
            logger.warning("Native PDF returned None — falling through to custom agent")
        except Exception as e:
            logger.warning(f"Native PDF analysis failed ({e}), falling through to custom agent")
            try:
                db.rollback()
            except Exception:
                pass

    # PRIORITY 2: tender_doc_analyzer custom agent — used when there are no PDF
    # attachments (text-only or tender_id-based requests) OR when native PDF failed.
    try:
        from app.models.agent_builder import CustomAgent
        analyzer = db.query(CustomAgent).filter(
            CustomAgent.agent_key == "tender_doc_analyzer",
            CustomAgent.is_enabled == True,
        ).first()

        if analyzer:
            logger.info(f"Document analysis: using tender_doc_analyzer (type={analyzer.agent_type})")
            result = await chat_custom_agent(
                db=db, message=message, tender_id=tender_id,
                session_id=session_id, agent_key="tender_doc_analyzer",
            )
            # Only return if the agent succeeded — fall through to fallbacks on failure
            if result.get("status") != "failed":
                result["output_type"] = "document_analysis"
                result["agent_key"] = "deep_analyzer"
                return result
            logger.warning(
                f"tender_doc_analyzer returned failed, falling back to next priority: "
                f"{result.get('error', result.get('output', 'unknown')[:200])}"
            )
            print(f"[DRPL] tender_doc_analyzer failed, trying fallbacks", flush=True)
            # Clean session before fallback attempts
            try:
                db.rollback()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"tender_doc_analyzer failed: {e}", exc_info=True)
        print(f"[DRPL ERROR] tender_doc_analyzer failed: {type(e).__name__}: {e}", flush=True)
        try:
            db.rollback()
        except Exception:
            pass

    # Fallback: direct LLM analysis of the uploaded content
    try:
        from app.services.langchain.llm_factory import get_chat_model, safe_ainvoke
        from app.services.langchain.model_limits import (
            compute_dynamic_max_tokens, render_token_budget_protocol,
        )
        from app.services.ai_service import _get_agent_config, _get_effective_model
        from langchain_core.messages import SystemMessage, HumanMessage

        logger.info("Document analysis: using direct LLM fallback (priority 3)")

        # Bump from the prior hardcoded 16K to the model ceiling (Sonnet 4.6 = 64K).
        # Auto-continuation in safe_ainvoke handles overrun gracefully, so picking
        # a generous upfront budget just means fewer continuation round-trips.
        _agent_cfg = _get_agent_config(db, "deep_analyzer")
        _model = _get_effective_model(db, _agent_cfg) or "claude-sonnet-4-6"
        _max_tokens = compute_dynamic_max_tokens(_model, requested=None)
        llm = get_chat_model(db, agent_name="deep_analyzer", max_tokens=_max_tokens)

        base_system_prompt = (
            "You are a critical tender document analyst. Every missed clause is a potential bid rejection. "
            "Analyze the uploaded document and provide:\n"
            "1. **Tender Summary**: Title, reference, issuing authority, scope (quantities/items), estimated value, "
            "EMD, ALL key dates, delivery location & timeline, evaluation method\n"
            "2. **Document Requirements**: ALL required submission documents — name, mandatory/optional, "
            "format, validity, which envelope. Search across ALL sections.\n"
            "3. **Eligibility Criteria**: Turnover, experience, certifications — quote exact requirements. "
            "Provide GO/NO-GO/CONDITIONAL assessment.\n"
            "4. **Negative Keywords & Rejection Risks**: Scan every page for disqualification triggers. "
            "Table format: Quoted Clause | Keyword | Risk Level | Required Action\n"
            "5. **What's Missing**: Payment terms, scope boundaries, force majeure, referenced but missing annexures\n"
            "6. **Key Risks & Observations**: Critical clauses, contradictions, unusual terms\n"
            "7. **Next Steps**: Prioritized action plan — immediate tasks, pre-bid questions, documents to prepare\n\n"
            "Quote exact text. Cross-reference across sections. Use Indian procurement terminology. "
            "ALWAYS end with actionable next steps."
        )
        system_prompt = base_system_prompt + "\n\n" + render_token_budget_protocol(
            max_tokens=_max_tokens,
            priority_hierarchy=(
                "1. Section 3 — Eligibility GO/NO-GO\n"
                "2. Section 1 — Summary, key dates, EMD\n"
                "3. Section 4 — Negative keywords / rejection risks\n"
                "4. Section 7 — Next steps\n"
                "5. Section 5 — Missing items\n"
                "6. Section 2 — Required documents\n"
                "7. Section 6 — Risks/observations"
            ),
        )

        result = await safe_ainvoke(
            llm,
            [SystemMessage(content=system_prompt), HumanMessage(content=message)],
            agent_name="deep_analyzer.fallback",
        )

        output = result.content if isinstance(result.content, str) else str(result.content)

        return {
            "output": output,
            "output_type": "document_analysis",
            "agent_key": "deep_analyzer",
            "tool_calls": [],
            "metrics": {},
            "status": "completed",
        }

    except Exception as e:
        logger.error(f"Fallback document analysis failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "deep_analyzer",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }


async def _analyze_with_native_pdf(
    db: Session,
    message: str,
    pdf_paths: list[str],
) -> Optional[dict]:
    """Analyze documents using native PDF document blocks for full-fidelity processing.

    `pdf_paths` holds StorageService keys (not filesystem paths). We materialize
    each key to a local tempfile under an ExitStack for the duration of the
    single batched call_ai_with_documents call.
    """
    import os
    from contextlib import ExitStack
    from app.services.ai_service import call_ai_with_documents
    from app.services.storage_service import get_storage_service

    if not pdf_paths:
        return None

    storage = get_storage_service()

    with ExitStack() as stack:
        valid_paths: list[str] = []
        for key in pdf_paths:
            try:
                local_path = stack.enter_context(
                    storage.as_local_file(key, suffix=".pdf")
                )
                valid_paths.append(local_path)
            except FileNotFoundError:
                logger.warning(f"Attachment missing in storage: {key}")
                continue
            except Exception as e:
                logger.warning(f"Could not materialize attachment {key}: {e}")
                continue

        if not valid_paths:
            return None

        total_size = sum(os.path.getsize(p) for p in valid_paths)
        if total_size > 100 * 1024 * 1024:  # 100MB cumulative limit for native PDF
            logger.warning(
                f"PDF files too large for native processing: {total_size / (1024*1024):.1f}MB "
                f"across {len(valid_paths)} files (limit: 100MB)"
            )
            return None

        return await _run_native_pdf_analysis_inner(
            db, message, valid_paths
        )


async def _run_native_pdf_analysis_inner(
    db: Session,
    message: str,
    valid_paths: list[str],
) -> Optional[dict]:
    """The original body of _analyze_with_native_pdf, split out so the outer
    function can own the ExitStack lifetime. valid_paths are LOCAL filesystem
    paths (tempfiles on R2, existing files on local backend).
    """
    from app.services.ai_service import call_ai_with_documents

    from app.services.langchain.graphs.chat_upload_analysis import (
        CHAT_REPORT_PDF_PROMPT as system_prompt,
    )

    # Extract user's specific request from message (strip file content blocks)
    user_request = re.sub(r'\[ATTACHED FILES\].*', '', message, flags=re.DOTALL).strip()
    user_prompt = user_request if user_request else "Analyze the attached tender document(s) comprehensively."

    response_text = await call_ai_with_documents(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        document_paths=valid_paths,
        db=db,
        agent_name="deep_analyzer",
    )

    if not response_text:
        return None

    return {
        "output": response_text,
        "output_type": "document_analysis",
        "agent_key": "deep_analyzer",
        "tool_calls": [],
        "metrics": {"method": "native_pdf", "documents_count": len(valid_paths)},
        "status": "completed",
    }


async def chat_checklist_generation(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
) -> dict:
    """
    Chat wrapper for checklist generation.
    Wraps generate_checklist() from checklist_service.py,
    or generates from uploaded file content when no tender_id.
    Falls back to session context (prior analysis/file content) when available.
    """
    if not tender_id:
        tender_id = _extract_tender_id(message)

    # File content in the message ALWAYS takes priority — even when tender_id is set.
    # This handles uploads to tender-linked sessions where the Tender DB has no documents.
    has_file_content = "[FILE CONTENT:" in message
    if has_file_content:
        result = await _generate_checklist_from_content(db, message, file_metadata=file_metadata)
        _salvage_and_persist_checklist(db, tender_id, proposal_session_id, result)
        return result

    # Check session context for prior analysis or file content
    session_ctx = _get_session_context(db, conversation_history or [], proposal_session_id)

    # Use prior analysis as source for checklist
    if session_ctx.get("has_prior_analysis") and session_ctx.get("analysis_content"):
        context_block = _build_session_context_block(session_ctx)
        enriched = (
            f"{context_block}\n\n"
            f"User request: {message}\n\n"
            f"Generate a submission checklist based on the above analysis."
        )
        result = await _generate_checklist_from_content(db, enriched, file_metadata=file_metadata)
        _salvage_and_persist_checklist(db, tender_id, proposal_session_id, result)
        return result

    # Use prior file content as source
    if session_ctx.get("prior_file_content"):
        enriched = message + "\n\n" + session_ctx["prior_file_content"]
        result = await _generate_checklist_from_content(db, enriched, file_metadata=file_metadata)
        _salvage_and_persist_checklist(db, tender_id, proposal_session_id, result)
        return result

    # Try DB-based checklist if tender_id is set and no file/session content was found
    if tender_id:
        try:
            from app.services.checklist_service import generate_checklist

            items = await generate_checklist(db, tender_id)

            # Format as structured checklist
            checklist_data = []
            for item in items:
                checklist_data.append({
                    "name": item.item_name,
                    "description": item.item_description or "",
                    "is_required": item.is_required,
                    "status": "pending",
                })

            # Only use DB result if it actually has items
            if checklist_data:
                output = _format_checklist_output(checklist_data, tender_id)
                return {
                    "output": output,
                    "output_type": "checklist",
                    "agent_key": "checklist_generator",
                    "tool_calls": [],
                    "metrics": {"items_generated": len(checklist_data)},
                    "structured_data": checklist_data,
                    "status": "completed",
                }
        except Exception as e:
            logger.warning(f"DB checklist generation failed for tender {tender_id}: {e}")

    return {
        "output": (
            "I need a tender ID with documents or an uploaded document to generate a checklist. "
            "Please upload a document file or link a tender that has documents."
        ),
        "output_type": "general",
        "agent_key": "checklist_generator",
        "tool_calls": [],
        "metrics": {},
        "status": "needs_input",
    }


async def chat_proposal_writing(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
) -> dict:
    """
    Chat wrapper for proposal writing.
    Uses the proposal_creator ReAct agent for single-document generation.
    """
    if not tender_id:
        tender_id = _extract_tender_id(message)

    try:
        from app.services.langchain.llm_factory import get_chat_model
        from app.services.langchain.callback_handler import DRPLCallbackHandler
        from app.services.langchain.tools.tool_loader import load_tools_by_keys
        from app.services.langchain.graphs.proposal_agent import PROPOSAL_AGENT_SYSTEM_PROMPT
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
        from langgraph.prebuilt import create_react_agent

        tool_keys = ["document_generator", "web_search", "memory_retrieve", "memory_store", "clarify"]
        tools = load_tools_by_keys(
            db, tool_keys, agent_key="proposal_creator",
            proposal_session_id=proposal_session_id,
            router_session_id=session_id,
        )

        llm = get_chat_model(db, agent_name="proposal_creator")
        callback = DRPLCallbackHandler(db, agent_name="proposal_creator")

        agent = create_react_agent(
            model=llm,
            tools=tools,
            prompt=SystemMessage(content=PROPOSAL_AGENT_SYSTEM_PROMPT),
        )

        # Build session context
        session_ctx_block = ""
        if conversation_history:
            session_ctx = _get_session_context(db, conversation_history, proposal_session_id)
            session_ctx_block = _build_session_context_block(session_ctx)

        # Add tender context to message
        user_msg = message
        if tender_id:
            user_msg = (
                f"Tender ID: {tender_id}\n\n"
                f"User request: {message}\n\n"
                f"Instructions: Generate professional, submission-ready content."
            )
        if session_ctx_block:
            user_msg = f"{session_ctx_block}\n\n{user_msg}"

        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_msg)]},
            config={"callbacks": [callback]},
        )

        messages = result.get("messages", [])
        final_content = messages[-1].content if messages else ""
        metrics = callback.get_summary()

        # Extract tool calls
        tool_calls = []
        for msg in messages:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append({
                        "tool": tc.get("name", "unknown"),
                        "input": tc.get("args", {}),
                    })

        return {
            "output": final_content,
            "output_type": "proposal_document",
            "agent_key": "proposal_creator",
            "tool_calls": tool_calls,
            "metrics": metrics,
            "status": "completed",
        }

    except Exception as e:
        logger.error(f"Chat proposal writing failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "proposal_creator",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }


async def chat_costing_research_via_execute_agent(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    clarification_answers: Optional[dict] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Bridge wrapper: route the costing request through ``execute_agent``.

    Replaces the legacy LangGraph state machine with the standard agent
    execution path used by the Agent Builder "Test Agent" UI. This means:

      * An ``AgentExecution`` row IS created (visible in admin monitoring)
      * The agent's RAW output flows through to the chat — no canned
        "Scope clarification required" substitution. If the agent emitted
        prose explaining what's missing, that prose is what the user sees.
      * Edits to the costing_researcher prompt in Admin → Agent Builder
        take effect immediately (resolved via ``resolve_system_prompt``).

    Tender context (PDF text + prior analysis + BOQ) is embedded in the
    user message as TEXT so the generic ``execute_langchain_agent`` path
    handles it without needing native multimodal vision blocks.
    """
    from app.services.agent_execution_service import execute_agent

    # Auto-chain disabled platform-wide — the costing agent runs directly on
    # whatever context is available (cached TenderAnalysisSummary if a prior
    # direct analysis populated it, otherwise raw PDF extraction). Users now
    # explicitly invoke Deep Analyzer first when they want a fresh analysis.

    framing = (
        "You are running the costing_researcher agent for a tender pricing "
        "request. Use the PRIOR ANALYSIS SUMMARY (preferred) or TENDER PDF "
        "EXTRACTS (fallback) below to derive priceable line items with "
        "low/expected/high rate ranges. Follow the COSTING_JSON_START / "
        "COSTING_JSON_END marker contract specified in your system prompt "
        "so the structured Cost Breakdown UI can render. If the scope is "
        "genuinely insufficient to estimate ANY items, say so directly in "
        "prose — but make best-effort estimates for whatever IS pricable "
        "from what's available."
    )
    user_message = _build_user_message_with_context(
        db, message,
        tender_id=tender_id,
        file_metadata=file_metadata,
        framing=framing,
    )

    try:
        result = await execute_agent(
            db,
            agent_key_or_id="costing_researcher",
            input_data={
                "message": user_message,
                "tender_id": tender_id,
                # _skip_save_turn=True: the streaming_handler already wrote
                # the canonical user turn via save_conversation_turn before
                # invoking this shim — letting execute_langchain_agent save
                # again would create a duplicate row in
                # AgentConversationHistory.
                "_skip_save_turn": True,
            },
            user_id=user_id,
            session_id=session_id,  # propagates to execute_langchain_agent so
                                    # multi-turn refinement loads prior turns
            # Conservative context budget for the chat costing path. The
            # default 500K-char budget reserves 300K for training data + 100K
            # for memories, which combined with the costing_researcher's
            # large base prompt + tender-context block + LangChain ReAct
            # scratchpad pushes total input past Claude's 200K-token ceiling
            # ("The input is too long..."). 80K keeps training at ~48K and
            # memories at ~16K — still room for the most relevant examples
            # while staying well under the ceiling. The structured cost
            # breakdown doesn't need the entire training corpus.
            context_budget=80_000,
        )
    except Exception as e:
        logger.exception(
            f"[bridge] costing_researcher execute_agent failed: {type(e).__name__}: {e}"
        )
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "costing_researcher",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }

    # Best-effort structured parse for the Cost Breakdown panel. ONLY surface
    # structured_data when the agent actually emitted priced line items —
    # otherwise the frontend creates an empty artifact card that opens to
    # "No cost breakdown yet" (confusing). When the agent gave us prose
    # analysis instead of priced JSON, we let the prose render as markdown
    # in chat with no companion artifact.
    structured = None
    raw_output = result.get("output") or ""
    if raw_output:
        try:
            from app.services.langchain.graphs.enhanced_costing_agent import (
                _parse_costing_response,
            )
            parsed = _parse_costing_response(raw_output)
            line_items = (parsed or {}).get("line_items") or []

            # Check ANY field that could indicate a real numeric rate. The
            # canonical schema uses `tender_rate` (rate quoted in the tender
            # doc) + `rate_low/expected/high` (DRPL's estimated range), but
            # agents sometimes emit only one or the other depending on
            # whether the tender provides rates. Past versions of this
            # check missed `tender_rate`-only rows and dropped real
            # structured output; accept any positive-number rate field
            # to fix that.
            _RATE_FIELDS = (
                "rate_expected", "rate_low", "rate_high", "rate",
                "tender_rate", "tender_amount",
                "amount", "amount_low", "amount_expected", "amount_high",
                "margin_amount", "margin_amount_low", "margin_amount_high",
            )

            def _row_has_priced(item: dict) -> bool:
                if not isinstance(item, dict):
                    return False
                if item.get("rate_source") == "needs_user_input":
                    return False
                for k in _RATE_FIELDS:
                    v = item.get(k)
                    if isinstance(v, (int, float)) and v > 0:
                        return True
                    # Some emitters serialize rates as strings like "26500"
                    # or "₹26,500". Accept those too — strip currency
                    # symbols + commas + whitespace, then try float().
                    if isinstance(v, str):
                        cleaned = v.replace("₹", "").replace(",", "").strip()
                        try:
                            if float(cleaned) > 0:
                                return True
                        except ValueError:
                            pass
                return False

            has_priced = any(_row_has_priced(it) for it in line_items)
            n_items = len(line_items)
            n_priced = sum(1 for it in line_items if _row_has_priced(it))
            # Diagnostic: capture WHY the parse may have produced 0 line items.
            _raw_len = len(raw_output)
            _parsed_keys = list((parsed or {}).keys())[:20]
            _parse_error = (parsed or {}).get("parse_error")
            _has_start_marker = "COSTING_JSON_START" in raw_output
            _has_end_marker = "COSTING_JSON_END" in raw_output
            _tail = raw_output[-300:].replace("\n", " ")
            _head = raw_output[:200].replace("\n", " ")
            logger.info(
                f"[bridge] costing parse — line_items={n_items}, "
                f"priced={n_priced}, has_priced={has_priced}, "
                f"raw_len={_raw_len}, parsed_keys={_parsed_keys}, "
                f"parse_error={_parse_error!r}, "
                f"start_marker={_has_start_marker}, end_marker={_has_end_marker}"
            )
            if not has_priced:
                logger.warning(
                    f"[bridge] costing parse FAILED — head: {_head!r}"
                )
                logger.warning(
                    f"[bridge] costing parse FAILED — tail: {_tail!r}"
                )
            if has_priced:
                structured = parsed
                # Persist into the CostBreakdown ORM table so the artifact
                # panel's CostBreakdownEditor (which fetches from
                # /api/tenders/{tenderId}/cost-breakdown) finds real data
                # instead of returning 404. Without this, structured_data
                # ends up only on Artifact.structured_data — and that column
                # is unread by the editor component, so the panel shows
                # "No cost breakdown yet" despite valid JSON in chat.
                #
                # Reuses cost_breakdown_service.persist_from_agent_output
                # which already handles versioning (always creates a new
                # version, preserving history), line-item normalisation,
                # and totals recomputation against org defaults.
                if tender_id:
                    try:
                        from app.services import cost_breakdown_service
                        _is_component = bool(parsed.get("_component_mode"))
                        persisted = cost_breakdown_service.persist_from_agent_output(
                            db,
                            tender_id=tender_id,
                            costing=parsed,
                            created_by_agent="costing_researcher",
                            title=f"Cost Breakdown — Tender #{tender_id}",
                            skip_nit_validation=_is_component,
                            cost_sheet_template="client_annexure" if _is_component else None,
                        )
                        if persisted:
                            # persist_from_agent_output already commits;
                            # no extra commit needed here.
                            logger.info(
                                f"[bridge] CostBreakdown v{persisted.version} "
                                f"saved for tender {tender_id} "
                                f"({n_items} lines, {n_priced} priced)"
                            )
                            print(
                                f"[DRPL COSTING] tender {tender_id}: "
                                f"CostBreakdown v{persisted.version} persisted "
                                f"({n_items} lines) — artifact editor will "
                                f"now render real data",
                                flush=True,
                            )
                            # Phase 1c — attach persistence proof onto the
                            # result dict so streaming_handler can emit a
                            # `artifact_created` event that actually
                            # corresponds to a real CostBreakdown row.
                            result["cost_breakdown_id"] = persisted.id
                        else:
                            # persist_from_agent_output returned None despite
                            # has_priced=True — this shouldn't happen but be
                            # explicit if it does.
                            logger.error(
                                f"[bridge] persist_from_agent_output returned "
                                f"None for tender {tender_id} despite has_priced "
                                f"({n_items} items, {n_priced} priced)"
                            )
                            result["persistence_failure"] = {
                                "reason": "persist_returned_none",
                                "message": (
                                    "Cost breakdown could not be saved despite "
                                    "the agent producing priced line items. "
                                    "Please retry; if this persists, contact "
                                    "support with the tender ID."
                                ),
                                "tender_id": tender_id,
                                "n_items": n_items,
                            }
                    except Exception as e:
                        # No more silent swallow — log at ERROR with the full
                        # exception class so the failure surfaces in
                        # costing_run.log. The user sees a typed error event
                        # in chat (Phase 3) instead of a fake "ready" badge.
                        logger.error(
                            f"[bridge] CostBreakdown persist failed for "
                            f"tender {tender_id}: {type(e).__name__}: {e}",
                            exc_info=True,
                        )
                        try:
                            db.rollback()
                        except Exception:
                            pass
                        result["persistence_failure"] = {
                            "reason": "persist_exception",
                            "exception_type": type(e).__name__,
                            "message": (
                                "Cost breakdown could not be saved due to a "
                                f"backend error ({type(e).__name__}). Please "
                                "retry; if this persists, contact support with "
                                "the tender ID."
                            ),
                            "tender_id": tender_id,
                        }
            elif tender_id:
                # has_priced is False — the agent emitted prose but no
                # parseable line items. Surface this as a typed persistence
                # failure so the streaming layer can emit a clean error
                # bubble instead of letting the heuristic extractor spawn a
                # lying artifact card.
                result["persistence_failure"] = {
                    "reason": (
                        "no_priced_line_items"
                        if n_items > 0
                        else "no_line_items"
                    ),
                    "message": (
                        "The agent's response did not contain a valid JSON "
                        "schedule with priced line items. Please retry; "
                        "if this persists, simplify the request or attach "
                        "the NIT only."
                    ),
                    "tender_id": tender_id,
                    "n_items": n_items,
                    "n_priced": n_priced,
                    "parse_error": _parse_error,
                    "has_start_marker": _has_start_marker,
                    "has_end_marker": _has_end_marker,
                }
        except Exception as e:
            logger.error(
                f"[bridge] costing structured parse failed: {type(e).__name__}: {e}",
                exc_info=True,
            )
            # The parse itself blew up — also a persistence failure from
            # the user's perspective, surface it.
            result["persistence_failure"] = {
                "reason": "parse_exception",
                "exception_type": type(e).__name__,
                "message": (
                    "Could not interpret the costing agent's response. "
                    "Please retry."
                ),
                "tender_id": tender_id,
            }

    return _format_bridge_result(
        result,
        agent_key="costing_researcher",
        output_type="cost_breakdown" if structured else "general",
        structured_data=structured,
    )


# ────────────────────────────────────────────────────────────────────────────
# Bridge shims for the remaining 5 specialized agents
# ────────────────────────────────────────────────────────────────────────────
#
# Each follows the costing shim's template:
#   1. Build user_message with framing + tender context (capped + dedup'd)
#   2. Call execute_agent — creates AgentExecution row for monitoring
#   3. Best-effort parse for structured_data; fall through to plain markdown
#      if no structured artifact can be extracted
#   4. Return via _format_bridge_result
#
# No canned-fallback substitution. The agent's actual output flows through.


async def chat_document_analysis_via_execute_agent(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Bridge wrapper: deep_analyzer chat-path entry point.

    Two-priority strategy — vision-first, ReAct fallback:

      1. **v2 native PDF vision pipeline** (preferred): when ``tender_id`` is
         present, run ``_ensure_tender_analysis`` which sends each PDF to
         Claude Haiku 4.5 as native ``{"type": "document"}`` blocks (real
         multimodal vision — sees tables, diagrams, scanned content) and
         synthesises a markdown report. Result is cached in
         ``TenderAnalysisSummary.requirement_summary`` so downstream agents
         (checklist, annexure, costing) can consume it as their
         ``<tender_context>`` block.

      2. **LangChain ReAct fallback** (degraded): when there's no tender,
         when the v2 analyzer fails (timeout, all-PDFs-unreadable, etc.),
         or when the cache is empty AND the inline run failed. Sends
         text-extracted PDFs to Sonnet 4.6 via ``execute_agent`` — lower
         quality (loses tables / scans) but keeps the chat responsive.

    Why two paths: the v2 vision path is what the user remembers as the
    "good" Deep Analyzer — but it lives inside ``_ensure_tender_analysis``
    and only fires as a prerequisite for downstream agents. Without this
    rerouting, the chat-path Deep Analyzer would silently degrade to the
    text-only ReAct shim (different model AND lower modality).

    The output_type is ``document_analysis`` whenever we have a substantive
    markdown report (≥200 chars), regardless of whether a structured JSON
    tail was emitted. This keeps the artifact pipeline firing for the
    markdown-narrative reports the canonical prompt now produces.
    """
    # ── PRIORITY 1: v2 native PDF vision path ────────────────────────────
    if tender_id:
        try:
            summary_row = await _ensure_tender_analysis(db, tender_id)
            if (
                summary_row
                and getattr(summary_row, "requirement_summary", None)
                and summary_row.requirement_summary.strip()
            ):
                logger.info(
                    f"[bridge] tender {tender_id}: returning v2 vision "
                    f"analysis ({len(summary_row.requirement_summary)} chars) "
                    f"to chat"
                )
                return {
                    "output": summary_row.requirement_summary,
                    "output_type": "document_analysis",
                    "agent_key": "deep_analyzer",
                    "structured_data": None,
                    "tool_calls": [],
                    "metrics": {
                        "source": "v2_native_pdf_vision",
                        "summary_chars": len(summary_row.requirement_summary),
                    },
                    "status": "completed",
                }
            logger.warning(
                f"[bridge] v2 analyzer returned empty requirement_summary for "
                f"tender {tender_id} — falling through to LangChain ReAct"
            )
        except Exception as e:
            logger.warning(
                f"[bridge] v2 analyzer raised for tender {tender_id} "
                f"({type(e).__name__}: {e}) — falling through to LangChain ReAct"
            )
            try:
                db.rollback()
            except Exception:
                pass

    # ── PRIORITY 2: LangChain ReAct fallback (text-extracted PDFs) ───────
    from app.services.agent_execution_service import execute_agent

    framing = (
        "You are running the tender_doc_analyzer agent. Produce a "
        "comprehensive forensic markdown report following the eight-section "
        "structure specified in your system prompt (Tender Summary & Key "
        "Intelligence, Requirements Extraction, Eligibility GO/NO-GO, "
        "Negative Keywords & Rejection Risks, Required Documents, What's "
        "Missing, Risks & Observations, Next Steps). Quote exact tender "
        "text. Use markdown tables where the system prompt specifies them. "
        "Be exhaustive — missing a rejection clause could cost the bid."
    )
    user_message = _build_user_message_with_context(
        db, message,
        tender_id=tender_id,
        file_metadata=file_metadata,
        framing=framing,
    )

    try:
        result = await execute_agent(
            db,
            agent_key_or_id="tender_doc_analyzer",
            input_data={
                "message": user_message,
                "tender_id": tender_id,
                "_skip_save_turn": True,
            },
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as e:
        logger.exception(
            f"[bridge] tender_doc_analyzer execute_agent failed: "
            f"{type(e).__name__}: {e}"
        )
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "deep_analyzer",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }

    # Best-effort parse for any trailing JSON block (legacy contract — the
    # current canonical prompt is markdown-only, but we keep the parser for
    # backward compat with admin-edited prompts that still emit JSON).
    structured: Optional[dict] = None
    raw_output = result.get("output") or ""
    if raw_output:
        try:
            import re as _re
            m = _re.search(r"```json\s*(.+?)\s*```", raw_output, _re.DOTALL)
            if m:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, dict) and (
                    parsed.get("requirements") or parsed.get("required_documents")
                    or parsed.get("summary")
                ):
                    structured = parsed
        except Exception as e:
            logger.debug(f"[bridge] analyzer structured parse failed (non-fatal): {e}")

    # output_type=document_analysis whenever the agent produced a
    # substantive markdown report — gates the artifact pipeline. The
    # 200-char floor filters out empty / "I cannot analyze" stubs but
    # admits any real analysis (typical reports are 5–15K chars).
    has_real_output = len(raw_output.strip()) >= 200

    # Cache the ReAct output in TenderAnalysisSummary so that the next
    # direct analysis call on this tender finds a valid summary and skips
    # re-running the analyzer. Also primes the cache for any future
    # re-introduction of auto-chain in downstream agents.
    if tender_id and has_real_output:
        try:
            from app.models.document_analysis import TenderAnalysisSummary as _TAS
            from datetime import datetime as _dt, timezone as _tz
            _row = db.query(_TAS).filter(_TAS.tender_id == tender_id).first()
            if not _row:
                _row = _TAS(tender_id=tender_id)
                db.add(_row)
            if not (_row.requirement_summary and _row.requirement_summary.strip()):
                _row.requirement_summary = raw_output
                _row.analysis_status = "completed"
                _row.analysis_version = "v1_react"
                _row.last_analyzed_at = _dt.now(_tz.utc)
                db.commit()
                logger.info(
                    f"[bridge] cached ReAct fallback analysis for tender {tender_id} "
                    f"({len(raw_output)} chars) into TenderAnalysisSummary"
                )
        except Exception as _cache_err:
            logger.warning(
                f"[bridge] failed to cache ReAct analysis for tender {tender_id}: {_cache_err}"
            )
            try:
                db.rollback()
            except Exception:
                pass

    return _format_bridge_result(
        result,
        agent_key="deep_analyzer",
        output_type="document_analysis" if has_real_output else "general",
        structured_data=None,
    )


async def chat_checklist_generation_via_execute_agent(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Bridge wrapper: route checklist_generator through ``execute_agent``.

    Runs directly on whatever tender context is available — auto-chain to the
    analyzer is disabled platform-wide; users invoke Deep Analyzer explicitly
    when they want a fresh analysis to ground checklist output.
    """
    from app.services.agent_execution_service import execute_agent

    framing = (
        "You are running the checklist_generator agent. Produce the "
        "complete submission checklist for this tender as a structured "
        "JSON list, where each entry has: name (exact document name as "
        "stated in tender), mandatory (bool), format (original/copy/"
        "notarized/self-attested), envelope (technical/financial/PQ), "
        "and any validity or certification requirements. Search across "
        "ALL sections of the tender — requirements are often scattered. "
        "Wrap the JSON in ```json ... ``` markers."
    )
    user_message = _build_user_message_with_context(
        db, message,
        tender_id=tender_id,
        file_metadata=file_metadata,
        framing=framing,
    )

    try:
        result = await execute_agent(
            db,
            agent_key_or_id="checklist_generator",
            input_data={
                "message": user_message,
                "tender_id": tender_id,
                "_skip_save_turn": True,
            },
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as e:
        logger.exception(
            f"[bridge] checklist_generator execute_agent failed: "
            f"{type(e).__name__}: {e}"
        )
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "checklist_generator",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }

    # Parse the JSON list for the Checklist artifact panel
    structured: Optional[dict] = None
    raw_output = result.get("output") or ""
    if raw_output:
        try:
            import re as _re
            m = _re.search(r"```json\s*(.+?)\s*```", raw_output, _re.DOTALL)
            if m:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, list) and parsed:
                    structured = {"items": parsed}
                elif isinstance(parsed, dict) and parsed.get("items"):
                    structured = parsed
        except Exception as e:
            logger.debug(f"[bridge] checklist structured parse failed (non-fatal): {e}")

    return _format_bridge_result(
        result,
        agent_key="checklist_generator",
        output_type="checklist" if structured else "general",
        structured_data=structured,
    )


async def chat_proposal_writing_via_execute_agent(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Bridge wrapper: route proposal_creator through ``execute_agent``.

    Proposals are markdown documents — no structured parse needed. The
    agent's output flows through to chat as rendered markdown, and the
    artifact panel will pick it up via the document artifact path.
    """
    from app.services.agent_execution_service import execute_agent

    # Auto-chain disabled platform-wide. The proposal agent runs directly on
    # whatever tender context is available — users invoke Deep Analyzer
    # explicitly first when a fresh analysis is needed.

    framing = (
        "You are running the proposal_creator agent. Produce the "
        "professional proposal document the user requested in markdown. "
        "Use the PRIOR ANALYSIS SUMMARY (preferred) or TENDER PDF EXTRACTS "
        "below to ground every claim in the tender's actual requirements. "
        "Be specific about DRPL's compliance with eligibility criteria, "
        "technical specifications, and submission timelines."
    )
    user_message = _build_user_message_with_context(
        db, message,
        tender_id=tender_id,
        file_metadata=file_metadata,
        framing=framing,
    )

    try:
        result = await execute_agent(
            db,
            agent_key_or_id="proposal_creator",
            input_data={
                "message": user_message,
                "tender_id": tender_id,
                "_skip_save_turn": True,
            },
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as e:
        logger.exception(
            f"[bridge] proposal_creator execute_agent failed: "
            f"{type(e).__name__}: {e}"
        )
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "proposal_creator",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }

    # Proposal is markdown — render directly. structured_data left None
    # (no JSON to parse, the document IS the deliverable).
    return _format_bridge_result(
        result,
        agent_key="proposal_creator",
        output_type="proposal_document",
        structured_data=None,
    )


async def chat_annexure_finder_via_execute_agent(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Bridge wrapper: route annexure_finder through ``execute_agent``."""
    from app.services.agent_execution_service import execute_agent

    framing = (
        "You are running the annexure_finder agent. Find and list every "
        "annexure / schedule / appendix / proforma / form / declaration "
        "in the attached tender PDF(s). Emit a JSON list, each entry "
        "with: title (exact name as in the tender), annexure_id (e.g. "
        "'Annexure-A', 'Schedule-3'), page_range (e.g. '12-15'), and a "
        "brief description of what the bidder must fill in. Wrap the "
        "JSON in ```json ... ``` markers."
    )
    user_message = _build_user_message_with_context(
        db, message,
        tender_id=tender_id,
        file_metadata=file_metadata,
        framing=framing,
    )

    try:
        result = await execute_agent(
            db,
            agent_key_or_id="annexure_finder",
            input_data={
                "message": user_message,
                "tender_id": tender_id,
                "_skip_save_turn": True,
            },
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as e:
        logger.exception(
            f"[bridge] annexure_finder execute_agent failed: "
            f"{type(e).__name__}: {e}"
        )
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "annexure_finder",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }

    structured: Optional[dict] = None
    raw_output = result.get("output") or ""
    if raw_output:
        try:
            import re as _re
            m = _re.search(r"```json\s*(.+?)\s*```", raw_output, _re.DOTALL)
            if m:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, list) and parsed:
                    structured = {"annexures": parsed}
                elif isinstance(parsed, dict) and parsed.get("annexures"):
                    structured = parsed
        except Exception as e:
            logger.debug(f"[bridge] annexure structured parse failed (non-fatal): {e}")

    return _format_bridge_result(
        result,
        agent_key="annexure_finder",
        output_type="annexures_extracted" if structured else "general",
        structured_data=structured,
    )


async def chat_workspace_operations_via_execute_agent(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Bridge wrapper: route workspace_manager through ``execute_agent``.

    Workspace operations are conversational — the agent reads the user's
    request, looks at workspace state, and produces an action plan or
    direct instruction. Output flows through as markdown, no structured
    parsing required.
    """
    from app.services.agent_execution_service import execute_agent

    framing = (
        "You are running the workspace_manager agent. Help the user with "
        "their workspace request — initialise the workspace, generate a "
        "specific named document, show progress, or update document "
        "statuses as appropriate. Use the tender context below to ground "
        "your response."
    )
    user_message = _build_user_message_with_context(
        db, message,
        tender_id=tender_id,
        file_metadata=file_metadata,
        framing=framing,
    )

    try:
        result = await execute_agent(
            db,
            agent_key_or_id="workspace_manager",
            input_data={
                "message": user_message,
                "tender_id": tender_id,
                "_skip_save_turn": True,
            },
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as e:
        logger.exception(
            f"[bridge] workspace_manager execute_agent failed: "
            f"{type(e).__name__}: {e}"
        )
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "workspace_manager",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }

    return _format_bridge_result(
        result,
        agent_key="workspace_manager",
        output_type="workspace_operations",
        structured_data=None,
    )


async def chat_costing_research(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    clarification_answers: Optional[dict] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
) -> dict:
    """
    Chat wrapper for costing research.
    Delegates to run_enhanced_costing_research() in chat mode, which supports:
      - Standalone general queries (no tender_id required)
      - Tender-bound costing with analysis context
      - Interactive clarification pause-and-ask
      - Training data injection and web search anonymization
    """
    from app.services.langchain.graphs.enhanced_costing_agent import (
        run_enhanced_costing_research,
    )

    if not tender_id:
        tender_id = _extract_tender_id(message)

    # Plan-mode hint: when the orchestrator runs Costing right after a
    # successful Deep Analyzer, prefer the DB-persisted analysis (full
    # content) over the 220-char prior_block summary that lives in `message`.
    plan_prior_deep_analysis = bool(
        (file_metadata or {}).get("plan_prior_deep_analysis")
    )

    # Load tender analysis if a tender ID is known
    analysis_result = {}
    per_doc_summary_count = 0
    capture_report: dict = {}
    if tender_id:
        # Diagnostic: count per-doc summaries up front so we can tell at a
        # glance whether the Deep Analyzer ever wrote anything for this
        # tender. extract_costing_scope() further downstream depends on
        # these rows.
        try:
            from app.models.document_analysis import (
                DocumentExtractionResult, TenderAnalysisSummary,
            )
            per_doc_summary_count = (
                db.query(DocumentExtractionResult)
                .filter(
                    DocumentExtractionResult.tender_id == tender_id,
                    DocumentExtractionResult.summary_json.isnot(None),
                )
                .count()
            )
            logger.info(
                f"[costing] tender {tender_id}: per_doc_summaries={per_doc_summary_count}"
                f"{' (plan-mode after deep_analyzer)' if plan_prior_deep_analysis else ''}"
            )
        except Exception as e:
            logger.debug(f"[costing] per-doc summary count failed (non-fatal): {e}")
            try:
                db.rollback()
            except Exception:
                pass

        try:
            summary = (
                db.query(TenderAnalysisSummary)
                .filter(TenderAnalysisSummary.tender_id == tender_id)
                .order_by(TenderAnalysisSummary.created_at.desc())
                .first()
            )
            if summary:
                analysis_result = {
                    "summary": summary.requirement_summary or "",
                    "requirements": summary.category_counts or {},
                }
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        # Plan-path fallback: if no TenderAnalysisSummary exists yet, the
        # Analysis plan step (Decision Maker) writes its result as a
        # CommandCenterArtifact rather than a TenderAnalysisSummary. Pull the
        # most recent `analysis` artifact for this Command Center session so
        # the costing agent has scope context to work with.
        if not analysis_result and proposal_session_id:
            try:
                from app.models.artifact import CommandCenterArtifact
                analysis_artifact = (
                    db.query(CommandCenterArtifact)
                    .filter(
                        CommandCenterArtifact.session_id == proposal_session_id,
                        CommandCenterArtifact.artifact_type == "analysis",
                    )
                    .order_by(CommandCenterArtifact.created_at.desc())
                    .first()
                )
                if analysis_artifact and analysis_artifact.content:
                    analysis_result = {
                        "summary": analysis_artifact.content[:8000],
                        "requirements": {},
                    }
                    logger.info(
                        f"[costing] hydrated analysis_result from "
                        f"CommandCenterArtifact #{analysis_artifact.id}"
                    )
            except Exception as e:
                logger.debug(f"[costing] analysis artifact lookup failed (non-fatal): {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

        # Auto-extract the NIT bidding schedule when it hasn't been parsed yet
        # (e.g. costing requested directly on an uploaded NIT, skipping tender
        # analysis). The shared `ensure_boq_parsed` self-heal runs the LIGHTWEIGHT
        # parse_boq_from_tender (pdfplumber + Haiku vision) — NOT the deep
        # analyzer — so costing gets a 1:1 NIT mirror + the reliable
        # batched/skeleton path instead of a freeform single-call that
        # summarises large NITs. Idempotent + non-fatal.
        from app.models.costing_template import BOQItem
        from app.services.boq_parser_service import ensure_boq_parsed, schedule_capture_report
        await ensure_boq_parsed(db, tender_id)
        # What the schedule is made of -- per document, and which annexures
        # the schedule cites versus which arrived -- goes into the reply, so
        # a missing annexure is said to the user rather than left to the
        # worker log.
        try:
            capture_report = schedule_capture_report(db, tender_id)
        except Exception as e:
            logger.debug(f"[costing] capture report failed (non-fatal): {e}")
            capture_report = {}
            try:
                db.rollback()
            except Exception:
                pass

        # Hydrate BOQ items from the DB (now populated either by a prior
        # analyzer run or the auto-parse above). When the NIT-aware parser
        # populated the structured columns (item_code, schedule_name,
        # bidding_unit, basic_value, escalation_pct, is_tax_line), include them
        # — the agent's RULE 5 contract requires them for 1:1 schedule mirroring.
        try:
            boq_rows = (
                db.query(BOQItem)
                .filter(BOQItem.tender_id == tender_id)
                .order_by(BOQItem.schedule_name.asc().nullsfirst(), BOQItem.sr_no.asc())
                .all()
            )
            if boq_rows:
                analysis_result["boq_items"] = [
                    {
                        # boq_item_id is the join key the agent must carry through
                        # to its cost-breakdown rows so the backend can validate
                        # the schedule binding.
                        "boq_item_id": item.id,
                        "sr_no": item.sr_no,
                        "item_code": item.item_code,
                        "description": item.description,
                        "quantity": item.quantity,
                        "unit": item.unit,
                        "estimated_rate": item.estimated_rate,
                        "basic_value": item.basic_value,
                        "escalation_pct": item.escalation_pct,
                        "bidding_unit": item.bidding_unit,
                        "schedule_name": item.schedule_name,
                        "is_tax_line": bool(item.is_tax_line),
                        "annexure_ref": getattr(item, "annexure_ref", None),
                        "parent_item_id": getattr(item, "parent_item_id", None),
                    }
                    for item in boq_rows
                ]
                logger.info(
                    f"[costing] loaded {len(boq_rows)} BOQ items for tender {tender_id} "
                    f"(schedules: {sorted({(r.schedule_name or '?') for r in boq_rows})})"
                )
        except Exception as e:
            logger.warning(f"[costing] BOQ lookup failed (non-fatal): {e}")
            try:
                db.rollback()
            except Exception:
                pass

    # Warm-path reuse: pull prior artifacts (Deep Analyzer / Checklist) produced
    # earlier in this Command Center session so we don't start cold.
    prior_artifacts_ctx = ""
    if proposal_session_id:
        try:
            from app.models.artifact import CommandCenterArtifact
            priors = (
                db.query(CommandCenterArtifact)
                .filter(
                    CommandCenterArtifact.session_id == proposal_session_id,
                    CommandCenterArtifact.artifact_type.in_(["analysis", "checklist"]),
                )
                .order_by(CommandCenterArtifact.created_at.desc())
                .limit(4)
                .all()
            )
            if priors:
                blocks = []
                for a in priors:
                    blocks.append(f"### PRIOR {a.artifact_type.upper()} — {a.title}\n{a.content[:4000]}")
                prior_artifacts_ctx = "\n\n".join(blocks)
                if not analysis_result.get("summary"):
                    analysis_result["summary"] = prior_artifacts_ctx[:6000]
                else:
                    analysis_result["summary"] = (
                        analysis_result["summary"] + "\n\n" + prior_artifacts_ctx
                    )[:10000]
                analysis_result.setdefault("requirements", {})
        except Exception as e:
            logger.debug(f"Prior artifact lookup failed (non-fatal): {e}")
            try:
                db.rollback()
            except Exception:
                pass

    # Check session context for prior analysis (always, not just when DB has nothing)
    if conversation_history:
        session_ctx = _get_session_context(db, conversation_history, proposal_session_id)
        if session_ctx.get("has_prior_analysis") and session_ctx.get("analysis_content"):
            # Session analysis supplements or replaces empty DB analysis
            if not analysis_result:
                analysis_result = {
                    "summary": session_ctx["analysis_content"][:5000],
                    "requirements": {},
                }
            # Enrich the message with session context
            ctx_block = _build_session_context_block(session_ctx)
            if ctx_block:
                message = f"{ctx_block}\n\n{message}"

    # If file content is present in the message, ensure it reaches the costing agent
    has_file_content = "[FILE CONTENT:" in message
    if has_file_content and not analysis_result:
        analysis_result = {"summary": "See attached document content.", "requirements": {}}

    try:
        result = await run_enhanced_costing_research(
            db=db,
            tender_id=tender_id,
            analysis_result=analysis_result,
            mode="chat",
            session_id=session_id,
            clarification_answers=clarification_answers,
            # Always forward the user's actual chat message — even when a
            # tender_id is set — so run_costing_react_node can prepend a
            # "USER REQUEST:" block. Previously this was dropped on the
            # plan path, leaving the agent with no statement of intent.
            initial_message=message,
            proposal_session_id=proposal_session_id,
        )

        status = result.get("status", "completed")
        costing = result.get("costing", {})
        metrics = result.get("metrics", {})

        # Agent is pausing to ask for clarification
        if status == "needs_clarification":
            clarification_question = result.get("clarification_question", "")
            return {
                "output": clarification_question,
                "output_type": "clarification_needed",
                "agent_key": "costing_researcher",
                "clarification_question": clarification_question,
                "tool_calls": [],
                "metrics": metrics,
                "status": "needs_clarification",
            }

        output = _format_costing_output(
            costing, tender_id, capture_report=capture_report,
        ) if tender_id else (
            costing.get("raw_response", str(costing))
        )

        # Diagnostic logging — surface what the agent actually returned so we
        # can debug "ran but produced nothing" cases without grepping later.
        try:
            line_items_count = len(costing.get("line_items") or [])
            logger.info(
                f"[costing] agent finished — status={status} line_items={line_items_count} "
                f"parse_error={costing.get('parse_error')} "
                f"recommendations={len(costing.get('recommendations') or [])} "
                f"raw_len={len(costing.get('raw_response') or '')}"
            )
            if line_items_count == 0:
                print(
                    f"[DRPL COSTING] empty result for tender {tender_id}: "
                    f"keys={list(costing.keys())} "
                    f"raw_excerpt={(costing.get('raw_response') or '')[:300]!r}",
                    flush=True,
                )
        except Exception:
            pass

        # Persist costing as an editable CostBreakdown row so the user can
        # adjust line items in the artifact panel and re-export XLSX. The
        # markdown artifact + downloadable XLSX still get emitted below.
        #
        # Persistence outcome is captured in `persistence_failure` /
        # `cost_breakdown_id` and attached to the response dict below so the
        # streaming layer can emit accurate artifact_created / agent_error
        # events. Phase 1c of the costing-reliability plan
        # (now-i-need-to-synchronous-taco.md).
        persistence_failure: Optional[dict] = None
        cost_breakdown_id: Optional[int] = None
        if costing.get("_already_persisted"):
            # Batched costing already built + saved the breakdown (deterministic
            # NIT skeleton + per-batch rate merges). Reuse its id; do NOT persist
            # again here or we'd create a duplicate CostBreakdown version on
            # every run. See run_costing_batched_node.
            cost_breakdown_id = costing.get("cost_breakdown_id")
            logger.info(
                f"[costing] tender {tender_id}: reusing pre-persisted batched "
                f"breakdown id={cost_breakdown_id} "
                f"(batches={costing.get('_batches')}, "
                f"needs_input_remaining={costing.get('_needs_input_remaining')})"
            )
        elif tender_id and costing.get("line_items"):
            try:
                from app.services.cost_breakdown_service import persist_from_agent_output
                _is_component = bool(costing.get("_component_mode"))
                persisted = persist_from_agent_output(
                    db=db,
                    tender_id=tender_id,
                    costing=costing,
                    session_id=proposal_session_id,
                    created_by_agent="costing_researcher",
                    skip_nit_validation=_is_component,
                    cost_sheet_template="client_annexure" if _is_component else None,
                )
                if persisted:
                    cost_breakdown_id = persisted.id
                else:
                    persistence_failure = {
                        "reason": "persist_returned_none",
                        "message": (
                            "Cost breakdown could not be saved despite the "
                            "agent producing line items. Please retry."
                        ),
                        "tender_id": tender_id,
                    }
            except Exception as e:
                logger.error(
                    f"CostBreakdown persistence failed for tender {tender_id}: "
                    f"{type(e).__name__}: {e}",
                    exc_info=True,
                )
                try:
                    db.rollback()
                except Exception:
                    pass
                persistence_failure = {
                    "reason": "persist_exception",
                    "exception_type": type(e).__name__,
                    "message": (
                        "Cost breakdown could not be saved due to a backend "
                        f"error ({type(e).__name__}). Please retry."
                    ),
                    "tender_id": tender_id,
                }
        elif tender_id:
            # Agent ran but emitted no line items. The user expected a cost
            # breakdown — surface this honestly instead of letting the
            # heuristic artifact extractor lie.
            persistence_failure = {
                "reason": "no_line_items",
                "message": (
                    "The agent's response did not contain a valid JSON "
                    "schedule with priced line items. Please retry; "
                    "if this persists, simplify the request or attach "
                    "the NIT only."
                ),
                "tender_id": tender_id,
            }

        # Emit XLSX artifact when the costing returned structured line items.
        # This runs only when the Command Center is driving the call (we have
        # proposal_session_id). The artifact appears in the artifacts pane with
        # a download button.
        xlsx_artifact_info = None
        try:
            line_items = costing.get("line_items") or []
            if line_items and proposal_session_id:
                xlsx_artifact_info = _emit_costing_xlsx_artifact(
                    db=db,
                    proposal_session_id=proposal_session_id,
                    tender_id=tender_id,
                    costing=costing,
                )
        except Exception as e:
            # Loud: the user expects a downloadable Excel. Even if emission
            # fails here, the download endpoint regenerates from the persisted
            # CostBreakdown on demand, so this is non-fatal — but we want it
            # visible in logs with a traceback.
            logger.error(
                f"Costing xlsx artifact emission failed: {type(e).__name__}: {e}",
                exc_info=True,
            )

        # Recompute the reply with this run's reconciliation report, now that
        # persistence (which runs the reconciliation gate) has completed.
        if tender_id:
            _recon = _latest_reconciliation_for_tender(db, tender_id)
            if _recon is not None:
                output = _format_costing_output(
                    costing, tender_id, reconciliation=_recon,
                    capture_report=capture_report,
                )

        resp = {
            "output": output,
            "output_type": "cost_breakdown",
            "agent_key": "costing_researcher",
            "tool_calls": metrics.get("tool_calls", []),
            "metrics": metrics,
            "structured_data": costing,
            "status": status,
        }
        if xlsx_artifact_info:
            resp["xlsx_artifact"] = xlsx_artifact_info
        # Phase 1c — surface persistence outcome so the streaming layer can
        # gate `artifact_created` events on real CostBreakdown writes and
        # emit a typed `agent_error` when persistence failed.
        if cost_breakdown_id is not None:
            resp["cost_breakdown_id"] = cost_breakdown_id
        if persistence_failure is not None:
            resp["persistence_failure"] = persistence_failure
        # Phase 2 — forward trim notes if the context-budget policy trimmed
        # anything pre-flight, so the streaming layer can emit a typed
        # `agent_warning` with code=context_trimmed.
        trim_notes = costing.get("trim_notes") if isinstance(costing, dict) else None
        if trim_notes:
            resp["trim_notes"] = trim_notes
        return resp

    except Exception as e:
        logger.error(f"Chat costing research failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "costing_researcher",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }


async def chat_workspace_operations(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
) -> dict:
    """
    Chat wrapper for canvas workspace operations.
    Uses a ReAct agent with workspace tools to manage document workspaces:
    initialize, check status, list items, generate specific documents.
    """
    if not tender_id:
        tender_id = _extract_tender_id(message)

    if not tender_id:
        return {
            "output": (
                "I need a tender ID to manage the workspace. Please link a tender to this session, "
                "or specify the tender (e.g., 'Initialize workspace for tender 42')."
            ),
            "output_type": "general",
            "agent_key": "workspace_manager",
            "tool_calls": [],
            "metrics": {},
            "status": "needs_input",
        }

    try:
        from app.services.langchain.llm_factory import get_chat_model
        from app.services.langchain.tools.tool_loader import load_tools_by_keys
        from langgraph.prebuilt import create_react_agent
        from langchain_core.messages import SystemMessage, HumanMessage

        # Load workspace tools + tender_lookup for context
        tool_keys = [
            "workspace_init", "workspace_status",
            "workspace_list_items", "workspace_generate_document",
            "tender_lookup", "clarify",
        ]
        tools = load_tools_by_keys(
            db, tool_keys, agent_key="workspace_manager",
            proposal_session_id=proposal_session_id,
            router_session_id=session_id,
        )

        llm = get_chat_model(db, agent_name="workspace_manager", max_tokens=4096)

        system_prompt = (
            "You are a workspace manager for DRPL's tender document preparation system.\n\n"
            "You help users manage their canvas workspace — a structured environment where each "
            "tender document (annexures, declarations, BOQs, certificates, etc.) is individually "
            "prepared, reviewed, and finalized.\n\n"
            "## Your Capabilities:\n"
            "1. **Initialize workspace**: Set up the workspace from the tender's checklist\n"
            "2. **Check status**: Show workspace progress and document statuses\n"
            "3. **List items**: Show all documents with their review status\n"
            "4. **Generate documents**: Create content for specific documents using AI agents\n\n"
            "## Workflow:\n"
            "- Always check workspace status first before taking actions\n"
            "- If workspace doesn't exist, offer to initialize it\n"
            "- When generating documents, confirm which document the user means\n"
            "- Report results clearly with document names and statuses\n"
            "- Suggest next steps (e.g., 'You can open the workspace to edit these documents individually')\n\n"
            f"Current tender ID: {tender_id}\n"
        )

        # Build conversation context
        history_context = ""
        if conversation_history:
            recent = conversation_history[-5:]
            for turn in recent:
                role = turn.get("role", "user")
                content = turn.get("content", "")[:500]
                history_context += f"{role}: {content}\n"

        user_msg = message
        if history_context:
            user_msg = f"Recent conversation:\n{history_context}\n\nCurrent request: {message}"

        agent = create_react_agent(
            model=llm,
            tools=tools,
            prompt=SystemMessage(content=system_prompt),
        )

        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_msg)]},
        )

        messages = result.get("messages", [])
        final_message = messages[-1].content if messages else "Workspace operation completed."

        output = final_message if isinstance(final_message, str) else str(final_message)

        return {
            "output": output,
            "output_type": "workspace_operations",
            "agent_key": "workspace_manager",
            "tool_calls": [],
            "metrics": {"tender_id": tender_id},
            "status": "completed",
        }

    except Exception as e:
        logger.error(f"Chat workspace operations failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "workspace_manager",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }


# --- Helper Functions ---

def _attachment_sizes(file_metadata: Optional[dict]) -> dict:
    """{storage key: size in bytes} for attachments whose size is known."""
    out: dict = {}
    for ap in (file_metadata or {}).get("attachment_paths", []) or []:
        if isinstance(ap, dict) and ap.get("path") and ap.get("size"):
            try:
                out[ap["path"]] = int(ap["size"])
            except (TypeError, ValueError):
                continue
    return out


def _get_pdf_paths(file_metadata: Optional[dict]) -> list[str]:
    """Extract PDF file paths from file_metadata."""
    if not file_metadata:
        return []
    attachment_paths = file_metadata.get("attachment_paths", [])
    return [
        ap["path"] for ap in attachment_paths
        if isinstance(ap, dict) and ap.get("is_pdf") and ap.get("path")
    ]


def _get_linked_documents_context(
    db: Session,
    file_metadata: Optional[dict],
    tender_id: Optional[int],
) -> str:
    """
    Check if uploaded documents have linked/child documents downloaded from URLs.
    Waits for link extraction to complete (up to 60s), then extracts text content
    from downloaded linked documents and returns it for analysis.
    """
    import time

    notes = []
    linked_content_parts = []

    try:
        # --- Wait for link extraction to finish ---
        _wait_for_link_extraction(db, file_metadata, tender_id, timeout=60)

        # --- Collect linked documents from TenderDocument table ---
        if tender_id:
            from app.models.tender import TenderDocument
            parent_docs = db.query(TenderDocument).filter(
                TenderDocument.tender_id == tender_id,
                TenderDocument.parent_document_id.is_(None),
            ).all()
            for parent in parent_docs:
                children = db.query(TenderDocument).filter(
                    TenderDocument.parent_document_id == parent.id,
                    TenderDocument.extraction_status == "completed",
                ).all()
                if children:
                    child_names = [c.file_name for c in children]
                    notes.append(
                        f"**Master document '{parent.file_name}'** has {len(children)} linked document(s): "
                        f"{', '.join(child_names)}."
                    )
                    # Extract text from each linked document
                    for child in children:
                        text = _extract_linked_doc_text(child.file_path, child.file_name)
                        if text:
                            linked_content_parts.append(text)

        # --- Also check ChatAttachment children ---
        if file_metadata:
            from app.models.chat_attachment import ChatAttachment
            for f_info in file_metadata.get("files", []):
                att_id = f_info.get("id")
                if att_id:
                    children = db.query(ChatAttachment).filter(
                        ChatAttachment.parent_attachment_id == att_id,
                    ).all()
                    for child in children:
                        if child.file_path and child.extraction_status == "completed":
                            text = _extract_linked_doc_text(child.file_path, child.file_name)
                            if text:
                                linked_content_parts.append(text)

    except Exception as e:
        logger.warning(f"Failed to get linked documents context: {e}")
        try:
            db.rollback()
        except Exception:
            pass

    parts = []
    if notes:
        parts.append("## Linked Documents Available\n" + "\n".join(notes))
    if linked_content_parts:
        parts.append("\n## LINKED DOCUMENT CONTENTS\n" +
                      "The following are the full text contents of linked/annexed documents. "
                      "Analyze each one as part of the complete tender package.\n\n" +
                      "\n\n".join(linked_content_parts))

    return "\n".join(parts)


#: A link extraction runs in the background within minutes of its upload. A
#: document still "pending" long after that was never queued for one (the
#: Command Center used to register chat PDFs that way) and must not make
#: every later analysis of its tender wait out the full timeout.
_LINK_EXTRACTION_STALE_S = 15 * 60


def _recently_uploaded(uploaded_at) -> bool:
    from datetime import datetime, timezone

    if uploaded_at is None:
        return False
    if uploaded_at.tzinfo is None:
        uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - uploaded_at).total_seconds() < _LINK_EXTRACTION_STALE_S


def _wait_for_link_extraction(
    db: Session,
    file_metadata: Optional[dict],
    tender_id: Optional[int],
    timeout: int = 60,
) -> None:
    """Poll DB until link extraction is complete or timeout reached."""
    import time

    if not file_metadata and not tender_id:
        return

    start = time.time()
    while (time.time() - start) < timeout:
        try:
            still_processing = False

            # Check ChatAttachment extraction status
            if file_metadata:
                from app.models.chat_attachment import ChatAttachment
                for f_info in file_metadata.get("files", []):
                    att_id = f_info.get("id")
                    if att_id:
                        att = db.query(ChatAttachment).filter(ChatAttachment.id == att_id).first()
                        if att and att.extraction_status in ("pending", "processing"):
                            still_processing = True
                            break

            # Check TenderDocument extraction status
            if not still_processing and tender_id:
                from app.models.tender import TenderDocument
                pending = db.query(TenderDocument.uploaded_at).filter(
                    TenderDocument.tender_id == tender_id,
                    TenderDocument.parent_document_id.is_(None),
                    TenderDocument.extraction_status.in_(["pending", "processing"]),
                ).all()
                if any(_recently_uploaded(u) for (u,) in pending):
                    still_processing = True

            if not still_processing:
                return
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            return  # Stop polling on DB error — don't corrupt session further

        logger.info(f"[DRPL] Waiting for link extraction to complete... ({int(time.time() - start)}s)")
        time.sleep(3)
        db.expire_all()  # Refresh ORM cache to see background task updates

    logger.warning(f"[DRPL] Link extraction wait timed out after {timeout}s")


def _extract_linked_doc_text(file_path: str, file_name: str, max_chars: int = 30000) -> str:
    """Extract text from a downloaded linked document and format it for inclusion in analysis."""
    import os

    if not file_path or not os.path.exists(file_path):
        return ""

    try:
        from app.services.test_document_service import extract_text
        result = extract_text(file_path)
        text = result.get("text", "").strip()
        if not text:
            return ""

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... Document truncated at {max_chars:,} characters ...]"

        return f"### --- {file_name} ---\n{text}"

    except Exception as e:
        logger.warning(f"Failed to extract text from linked doc {file_name}: {e}")
        return ""


def _extract_tender_id(message: str) -> Optional[int]:
    """Try to extract a tender ID from the user message."""
    import re
    patterns = [
        r"tender\s+(?:id\s+)?#?(\d+)",
        r"tender\s+(\d+)",
        r"#(\d+)",
        r"id\s+(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message.lower())
        if match:
            return int(match.group(1))
    return None


def _format_analysis_output(analysis: dict, tender_id: int) -> str:
    """Format analysis results as readable markdown."""
    parts = [f"## Tender Analysis — ID #{tender_id}\n"]

    # Summary
    summary = analysis.get("summary", "")
    if summary:
        parts.append(f"### Summary\n{summary}\n")

    # Requirements by category
    requirements = analysis.get("requirements", {})
    if requirements:
        parts.append("### Requirements Extraction\n")
        category_labels = {
            "requirements": "General Requirements",
            "eligibility": "Eligibility Criteria",
            "terms_conditions": "Terms & Conditions",
            "technical_specs": "Technical Specifications",
            "financial": "Financial Requirements",
            "experience": "Experience Requirements",
            "compliance": "Compliance Requirements",
        }
        for cat, label in category_labels.items():
            items = requirements.get(cat, [])
            if items:
                parts.append(f"**{label}** ({len(items)} items)")
                for item in items:
                    if isinstance(item, dict):
                        parts.append(f"- {item.get('description', item.get('text', str(item)))}")
                    else:
                        parts.append(f"- {item}")
                parts.append("")

    # Negative Keywords
    neg_keywords = analysis.get("negative_keywords", [])
    if neg_keywords:
        parts.append("### Rejection Risks & Negative Keywords\n")
        for item in neg_keywords:
            if isinstance(item, dict):
                severity = item.get("severity", "medium")
                badge = {"critical": "🔴", "high": "🟠", "medium": "🟡"}.get(severity, "⚪")
                sentence = item.get("sentence", item.get("text", str(item)))
                action = item.get("action", "")
                parts.append(f"{badge} **{severity.upper()}**: {sentence}")
                if action:
                    parts.append(f"  → Action: {action}")
            else:
                parts.append(f"- {item}")
        parts.append("")

    # Required Documents
    req_docs = analysis.get("required_documents", [])
    if req_docs:
        parts.append("### Required Documents\n")
        parts.append("| Document | Mandatory | Format |")
        parts.append("|----------|-----------|--------|")
        for doc in req_docs:
            if isinstance(doc, dict):
                name = doc.get("name", doc.get("document", str(doc)))
                mandatory = "Yes" if doc.get("mandatory", True) else "Optional"
                fmt = doc.get("format", "-")
                parts.append(f"| {name} | {mandatory} | {fmt} |")
        parts.append("")

    # Key Risks
    risks = analysis.get("key_risks", [])
    if risks:
        parts.append("### Key Risks\n")
        for risk in risks:
            parts.append(f"- ⚠️ {risk}")
        parts.append("")

    return "\n".join(parts)


def _format_checklist_output(checklist_data: list[dict], tender_id: int) -> str:
    """Format checklist as readable markdown table."""
    parts = [f"## Submission Checklist — Tender #{tender_id}\n"]
    parts.append(f"Generated **{len(checklist_data)}** checklist items.\n")
    parts.append("| # | Document | Required | Description |")
    parts.append("|---|----------|----------|-------------|")
    for i, item in enumerate(checklist_data, 1):
        name = item.get("name", "Unknown")
        required = "✅ Yes" if item.get("is_required", True) else "Optional"
        desc = item.get("description", "")[:200]
        parts.append(f"| {i} | {name} | {required} | {desc} |")
    parts.append("")
    parts.append("💡 *You can ask me to generate any of these documents individually.*")
    return "\n".join(parts)


def _latest_reconciliation_for_tender(db, tender_id: int) -> Optional[dict]:
    """Return the parsed reconciliation report from the tender's most recent
    CostBreakdown, or None. Mirrors cost_breakdown_service.render_breakdown_xlsx_bytes.
    """
    if not tender_id:
        return None
    from app.models.cost_breakdown import CostBreakdown
    try:
        bd = (
            db.query(CostBreakdown)
            .filter(CostBreakdown.tender_id == tender_id)
            .order_by(CostBreakdown.version.desc())
            .first()
        )
        if bd and bd.reconciliation_json:
            return json.loads(bd.reconciliation_json)
    except Exception:
        pass
    return None


def _emit_costing_xlsx_artifact(
    db: Session,
    proposal_session_id: int,
    tender_id: Optional[int],
    costing: dict,
) -> Optional[dict]:
    """Build an Excel workbook from a parsed costing result and persist it as a
    `cost_breakdown_xlsx` artifact attached to the Command Center session.
    Returns {'artifact_id', 'file_name', 'file_path'} on success.
    """
    from app.services.artifact_service import create_artifact
    from app.core.config import get_settings
    import os, uuid, json as _json
    from datetime import datetime, timezone

    # Resolve org-wide default GST % (admin-configurable in Platform Settings)
    from app.services.costing_format_service import get_costing_defaults
    org_gst_default = get_costing_defaults(db)["gst_percent"]

    rows: list[dict] = []
    from app.services.cost_breakdown_service import order_lines_for_display
    for item in order_lines_for_display(costing.get("line_items") or []):
        qty = item.get("quantity") or item.get("qty")
        # Expected band — accept both new (rate_expected) and legacy (rate)
        rate = item.get("rate_expected")
        if rate is None:
            rate = item.get("rate")
        amount = item.get("amount_expected")
        if amount is None:
            amount = item.get("amount")

        # Range fields — surface low/high explicitly in the workbook
        rate_low = item.get("rate_low")
        rate_high = item.get("rate_high")
        amount_low = item.get("amount_low")
        amount_high = item.get("amount_high")

        # Per-line profit
        profit_pct = item.get("profit_pct")
        profit_amount = item.get("profit_amount_expected")
        if profit_amount is None:
            profit_amount = item.get("profit_amount")
        profit_amount_low = item.get("profit_amount_low")
        profit_amount_high = item.get("profit_amount_high")

        gst_pct = item.get("gst_pct") or item.get("gst_rate")
        if gst_pct is None:
            gst_pct = org_gst_default
        total = item.get("total")

        # Phase 3b — margin-analysis fields (tender_rate / tender_amount /
        # margin_amount / margin_pct / schedule_section / cost_buildup_note)
        tender_rate = item.get("tender_rate")
        tender_amount = item.get("tender_amount")
        if tender_amount is None and tender_rate is not None and qty:
            try:
                tender_amount = round(float(tender_rate) * float(qty), 2)
            except (TypeError, ValueError):
                tender_amount = None
        margin_amount = item.get("margin_amount") or item.get("margin_amount_expected")
        margin_amount_low = item.get("margin_amount_low")
        margin_amount_high = item.get("margin_amount_high")
        margin_pct = item.get("margin_pct")
        item_code = (item.get("item_code") or "").strip()
        schedule_name = (item.get("schedule_name") or "").strip()
        # Group by schedule in the export. Derive the section label from the
        # NIT schedule code when the agent didn't supply schedule_section, so
        # the margin-analysis workbook renders one sheet per schedule (A, A7,
        # B, B7, …) like the NIT, instead of one flat list.
        schedule_section = (item.get("schedule_section") or "").strip()
        if not schedule_section and schedule_name:
            schedule_section = f"Schedule {schedule_name}"
        cost_buildup_note = (item.get("cost_buildup_note") or "").strip()

        rate_source = (item.get("rate_source") or "").strip()
        source_ref = (item.get("source_ref") or "").strip()
        oem_manufacturer = (item.get("oem_manufacturer") or "").strip()
        source_url = (item.get("source_url") or "").strip()
        # Surface "needs user input" lines clearly — clear all numeric fields.
        if rate_source == "needs_user_input" or (rate is None and rate_low is None and rate_high is None):
            rate = rate_low = rate_high = None
            amount = amount_low = amount_high = None
            profit_pct = profit_amount = profit_amount_low = profit_amount_high = None
            margin_amount = margin_amount_low = margin_amount_high = margin_pct = None
            if not source_ref:
                source_ref = "NEEDS USER INPUT"

        rows.append({
            "description": item.get("description", ""),
            "category": item.get("category", ""),
            "qty": qty,
            "unit": item.get("unit", ""),
            "rate": rate,
            "rate_low": rate_low,
            "rate_high": rate_high,
            "amount": amount,
            "amount_low": amount_low,
            "amount_high": amount_high,
            "profit_pct": profit_pct,
            "profit_amount": profit_amount,
            "profit_amount_low": profit_amount_low,
            "profit_amount_high": profit_amount_high,
            # Phase 3b margin-analysis + grouping fields
            "tender_rate": tender_rate,
            "tender_amount": tender_amount,
            "margin_amount": margin_amount,
            "margin_amount_low": margin_amount_low,
            "margin_amount_high": margin_amount_high,
            "margin_pct": margin_pct,
            "item_code": item_code,
            "schedule_section": schedule_section,
            "sr_no": item.get("sr_no"),
            "boq_item_id": item.get("boq_item_id"),
            "parent_boq_item_id": item.get("parent_boq_item_id"),
            "annexure_ref": item.get("annexure_ref"),
            "is_component": item.get("parent_boq_item_id") is not None,
            "cost_buildup_note": cost_buildup_note,
            "gst_pct": gst_pct,
            "total": total,
            "rate_source": rate_source,
            "source_ref": source_ref,
            "oem_manufacturer": oem_manufacturer,
            "source_url": source_url,
        })
    if not rows:
        return None

    title = f"Cost Breakdown — Tender #{tender_id}" if tender_id else "Cost Breakdown"
    # Phase 3b — pass cost_assumptions + strategic_summary so build_cost_xlsx
    # produces the multi-sheet reference-style workbook when margin-analysis
    # mode is in use. Pull org-level overhead/margin/GST defaults so the
    # formula sheet's editable parameter cells start at the right values.
    try:
        from app.services.costing_format_service import get_costing_defaults
        _defaults = get_costing_defaults(db)
    except Exception:
        _defaults = {"overhead_percent": 10.0, "margin_percent": 15.0, "gst_percent": 18.0}

    import tempfile as _tempfile
    from app.services.storage_service import get_storage_service as _get_storage

    # Consolidate all schedules into one tab by default (the NIT-replica view);
    # admins can revert via costing.nit_single_sheet=False. Keeps the auto-gen
    # artifact consistent with the regenerate-xlsx route + Command Center download.
    try:
        from app.services.settings_service import get_setting_value as _get_setting
        _single_sheet = bool(_get_setting(db, "costing.nit_single_sheet", True))
    except Exception:
        _single_sheet = True

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    tender_tag = f"tender_{tender_id}_" if tender_id else ""
    fname = f"cost_{tender_tag}{ts}_{uuid.uuid4().hex[:6]}.xlsx"

    # build_cost_xlsx needs a path → write to a temp file, read bytes, then
    # persist through storage_service (local OR R2) and store the KEY in
    # artifact.file_path so the download endpoint can serve it anywhere.
    _fd, _tmp = _tempfile.mkstemp(suffix=".xlsx")
    os.close(_fd)
    try:
        summary = build_cost_xlsx(
            _tmp,
            title,
            rows,
            cost_assumptions=costing.get("cost_assumptions") or [],
            strategic_summary=costing.get("strategic_summary") or {},
            breakdown_meta={
                "tender_id": tender_id,
                "reconciliation": _latest_reconciliation_for_tender(db, tender_id),
                **_defaults,
            },
            single_sheet=_single_sheet,
        )
        with open(_tmp, "rb") as _f:
            _data = _f.read()
    finally:
        try:
            os.remove(_tmp)
        except OSError:
            pass

    from app.services.cost_breakdown_service import persist_xlsx_artifact

    return persist_xlsx_artifact(
        db,
        session_id=proposal_session_id,
        title=title,
        fname=fname,
        data=_data,
        rows=rows,
        summary=summary,
        metadata_extra={"tender_id": tender_id},
    )


_SOURCE_LABEL = {
    "training_data": "📚 training",
    "tender_estimate": "📄 tender",
    "web_search": "🌐 web",
    "memory": "🧠 memory",
    "derived_estimate": "🧮 derived",
    "needs_user_input": "⚠️ NEEDS INPUT",
}


def _fmt_money(val) -> str:
    """Format a numeric value as ₹1,23,456.00 — empty for None/missing."""
    if val is None or val == "":
        return ""
    try:
        return f"₹{float(val):,.2f}"
    except (TypeError, ValueError):
        return str(val)


def _fmt_money_range(low, expected, high) -> str:
    """Format a low/expected/high triplet as '₹X – ₹Y (exp ₹Z)'.

    Falls back gracefully:
      - all three present and distinct → range with expected
      - low == high → single value
      - only expected present → single value
      - nothing usable → "—"
    """
    def _to_float(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    lo = _to_float(low)
    exp = _to_float(expected)
    hi = _to_float(high)

    if lo is not None and hi is not None and lo != hi:
        exp_part = f" (exp {_fmt_money(exp)})" if exp is not None and exp not in (lo, hi) else ""
        return f"{_fmt_money(lo)} – {_fmt_money(hi)}{exp_part}"
    # Collapsed range — pick the first non-None value
    for v in (exp, lo, hi):
        if v is not None:
            return _fmt_money(v)
    return "—"


def _format_strategic_summary(summary: dict) -> list[str]:
    """Render the Phase 3b strategic_summary as the topmost markdown section.

    Mirrors the reference workbook's Sheet 1: tender snapshot + schedule-wise
    profitability + key observations + recommended bid strategy. Returns the
    list of lines to splice into the output.
    """
    if not summary or not isinstance(summary, dict):
        return []
    lines: list[str] = []

    snap = summary.get("tender_snapshot") or {}
    if snap:
        lines.append("### Tender Snapshot\n")
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        if snap.get("tender_no"):
            lines.append(f"| Tender No. | {snap.get('tender_no')} |")
        if snap.get("scope_one_liner"):
            scope = str(snap.get("scope_one_liner")).replace("|", "\\|")
            lines.append(f"| Scope | {scope} |")
        if snap.get("tender_value_inr") not in (None, 0, ""):
            lines.append(f"| Tender Value | {_fmt_money(snap.get('tender_value_inr'))} |")
        if snap.get("period"):
            lines.append(f"| Period | {snap.get('period')} |")
        depots = snap.get("depots_or_locations") or []
        if depots:
            lines.append(f"| Depots / Locations | {', '.join(str(d) for d in depots)} |")
        if snap.get("emd_inr") not in (None, 0, ""):
            lines.append(f"| EMD | {_fmt_money(snap.get('emd_inr'))} |")
        if snap.get("performance_guarantee"):
            pg = str(snap.get("performance_guarantee")).replace("|", "\\|")
            lines.append(f"| Performance Guarantee | {pg} |")
        if snap.get("bid_validity_days") not in (None, 0, ""):
            lines.append(f"| Bid Validity | {snap.get('bid_validity_days')} days |")
        if snap.get("penalty_cap_pct_of_contract") not in (None, 0, ""):
            lines.append(f"| Penalty Cap | {snap.get('penalty_cap_pct_of_contract')}% of contract value |")
        elig = snap.get("min_eligibility") or []
        if elig:
            elig_str = "; ".join(str(e).replace("|", "\\|") for e in elig)
            lines.append(f"| Min. Eligibility | {elig_str} |")
        lines.append("")

    schedules = summary.get("schedule_breakdown") or []
    if schedules:
        lines.append("### Schedule-wise Profitability\n")
        lines.append("| Schedule | Tender Value | Estimated Cost | Gross Margin | GM % |")
        lines.append("|---|---|---|---|---|")
        sum_tv = sum_ec = sum_gm = 0.0
        for s in schedules:
            label = str(s.get("schedule") or "").replace("|", "\\|")
            tv = s.get("tender_value_inr") or 0
            ec = s.get("estimated_cost_inr") or 0
            gm = s.get("gross_margin_inr")
            if gm is None:
                gm = (tv or 0) - (ec or 0)
            gp = s.get("gross_margin_pct")
            if gp is None and tv:
                gp = round(gm / tv * 100.0, 1)
            sum_tv += tv or 0
            sum_ec += ec or 0
            sum_gm += gm or 0
            gp_str = f"{gp:.1f}%" if isinstance(gp, (int, float)) else (gp or "—")
            lines.append(
                f"| {label} | {_fmt_money(tv)} | {_fmt_money(ec)} | "
                f"{_fmt_money(gm)} | {gp_str} |"
            )
        if len(schedules) > 1:
            sum_pct = round(sum_gm / sum_tv * 100.0, 1) if sum_tv else 0.0
            lines.append(
                f"| **TOTAL CONTRACT** | **{_fmt_money(sum_tv)}** | "
                f"**{_fmt_money(sum_ec)}** | **{_fmt_money(sum_gm)}** | "
                f"**{sum_pct:.1f}%** |"
            )
        lines.append("")

    obs = summary.get("key_observations") or []
    if obs:
        lines.append("### Key Observations & Strategy\n")
        for o in obs:
            lines.append(f"- {o}")
        lines.append("")

    bid = summary.get("recommended_bid_strategy")
    if bid:
        lines.append(f"> **Recommended bid:** {bid}")
        lines.append("")
    return lines


def _reconciliation_callout(reconciliation: Optional[dict]) -> Optional[str]:
    """Failure-only markdown callout for the costing chat reply. Returns None
    when there is no report or the report reconciles; otherwise names the
    schedules whose extracted line-sum doesn't match the NIT's printed total.
    """
    if not reconciliation or reconciliation.get("ok"):
        return None
    failed = [s for s in (reconciliation.get("schedules") or []) if not s.get("ok")]
    if not failed:
        return None
    codes = ", ".join(str(s.get("code")) for s in failed)
    return (
        f"> ⚠️ **{len(failed)} schedule(s) don't reconcile with the NIT** "
        f"({codes}) — the extracted line totals differ from the tender's printed "
        f"sub-totals. Review these schedules before submitting."
    )


def _format_capture_report(report: Optional[dict]) -> list[str]:
    """What the costing was built from, said before the numbers.

    Per document its row count; for the annexures, which the schedule cites,
    which arrived and were bound to their schedule item, which are missing.
    The missing ones are the line that matters: a costing that quietly omits
    an annexure's thirty rows looks exactly like one that read them.
    """
    if not report or not isinstance(report, dict) or not report.get("row_count"):
        return []
    lines: list[str] = ["### What this costing was built from\n"]
    docs = report.get("documents") or []
    for d in docs:
        parts = [f"{d.get('rows', 0)} row(s)"]
        if d.get("schedules"):
            parts.append("Schedule " + ", ".join(str(s) for s in d["schedules"]))
        anx = d.get("annexures") or {}
        if anx:
            parts.append(", ".join(f"Annexure-{k} ({v})" for k, v in anx.items()))
        name = str(d.get("name") or "document").replace("|", "\\|")
        lines.append(f"- **{name}** — " + " · ".join(parts))
    annex = report.get("annexures") or {}
    linked = annex.get("linked") or []
    for l in linked:
        coverage = ""
        if l.get("coverage_pct") is not None:
            coverage = (
                f" The annexure's printed values sum to ₹{l.get('captured_value', 0):,.0f} "
                f"against the item's published ₹{l.get('published_rate', 0):,.0f} per "
                f"{l.get('parent_unit') or 'set'} ({l['coverage_pct']:.0f}% captured)."
            )
        lines.append(
            f"- Annexure-{l.get('annexure')}: {l.get('rows', 0)} component row(s), "
            f"rolled into Schedule {l.get('parent_schedule') or '?'} item "
            f"{l.get('parent_code')} ({(l.get('parent_description') or '')[:70]}…) "
            f"— quantities are per {l.get('parent_unit') or 'set'}; the item's "
            f"{l.get('parent_quantity') if l.get('parent_quantity') is not None else '?'} "
            f"{l.get('parent_unit') or 'sets'} multiply it." + coverage
        )
    unlinked = annex.get("unlinked") or []
    if unlinked:
        lines.append(
            f"- Annexure {', '.join(unlinked)} captured but no schedule item cites "
            f"{'it' if len(unlinked) == 1 else 'them'} — costed as standalone scope."
        )
    missing = annex.get("missing") or []
    if missing:
        lines.append("")
        lines.append(
            f"> ⚠️ **The schedule cites Annexure {', '.join(missing)} and no uploaded "
            f"document carries {'it' if len(missing) == 1 else 'them'}.** The items "
            f"citing {'it' if len(missing) == 1 else 'them'} were costed without their "
            f"material breakdown. Upload the material list that holds "
            f"{'this annexure' if len(missing) == 1 else 'these annexures'} and re-run "
            f"the costing to include it."
        )
    from app.core.config import platform_build
    lines.append(f"- _Platform build {platform_build()}._")
    lines.append("")
    return lines


def _format_costing_output(
    costing: dict,
    tender_id: int,
    reconciliation: Optional[dict] = None,
    capture_report: Optional[dict] = None,
) -> str:
    """Format costing data as readable markdown with rate-source attribution.

    Phase 3b: when `strategic_summary` is present, renders it as the topmost
    section (tender snapshot + schedule-wise profitability + observations).
    When line items have `tender_rate`, switches the table to margin-analysis
    columns and groups rows by `schedule_section`.

    Renders the range schema (rate_low/expected/high, per-line profit, L/E/H
    totals, manpower_resource_analysis decomposition) when present. Falls back
    to single-rate rendering for legacy / persisted data.
    """
    if costing.get("parse_error"):
        raw = costing.get("raw_response") or ""
        if raw.strip():
            return raw
        return (
            f"## Cost Estimate — Tender #{tender_id}\n\n"
            f"⚠️ The costing agent returned an empty response. The most likely causes:\n"
            f"- The tender's BOQ couldn't be auto-extracted from the attached PDFs\n"
            f"- The agent hit an LLM error mid-run (check backend logs)\n"
            f"- The agent ran out of tokens before producing a final answer\n\n"
            f"Try again, or attach a PDF that contains a Bill of Quantities table."
        )

    parts = [f"## Cost Estimate — Tender #{tender_id}\n"]

    # What the schedule was built from -- documents, annexures, and the
    # annexures the schedule cites that never arrived.
    parts.extend(_format_capture_report(capture_report))

    # Phase 3b — strategic summary at the top (renders as Tender Snapshot +
    # Schedule-wise Profitability + Key Observations + Recommended Bid).
    strategic_summary = costing.get("strategic_summary") or {}
    parts.extend(_format_strategic_summary(strategic_summary))

    line_items = costing.get("line_items", []) or []
    assumptions = costing.get("assumptions", []) or []
    recommendations = costing.get("recommendations", []) or []
    decomposition = costing.get("manpower_resource_analysis", []) or []
    cost_assumptions = costing.get("cost_assumptions", []) or []
    totals = costing.get("totals") or {}
    has_range_totals = isinstance(totals, dict) and any(
        isinstance(totals.get(b), dict) for b in ("low", "expected", "high")
    )
    has_legacy_totals = any(
        costing.get(k) not in (None, "", 0)
        for k in ("subtotal", "overheads", "profit_margin", "gst", "grand_total")
    )
    has_totals = has_range_totals or has_legacy_totals
    # Phase 3b — margin-analysis mode triggers when any line has tender_rate.
    show_margin_columns = any(
        i.get("tender_rate") not in (None, "") or i.get("tender_amount") not in (None, "")
        for i in line_items
    )

    # Fully-empty result — surface a clear diagnostic instead of a bare title
    if not line_items and not has_totals and not assumptions and not recommendations:
        return (
            f"## Cost Estimate — Tender #{tender_id}\n\n"
            f"⚠️ The costing agent finished but produced no line items.\n\n"
            f"This usually means the tender's BOQ (Bill of Quantities) couldn't "
            f"be located in the attached documents. Possible next steps:\n"
            f"- Confirm the tender PDF actually contains a BOQ / Schedule of Rates / Price Bid table\n"
            f"- Re-attach the PDF and try again — large PDFs sometimes time out\n"
            f"- Share the BOQ explicitly in chat (paste the table or describe the items)\n"
            f"- Check backend logs for `[costing]` or `Costing ReAct agent failed` errors"
        )

    # Top-level callouts
    def _is_needs_input(it: dict) -> bool:
        if it.get("rate_source") == "needs_user_input":
            return True
        # No priced rate at all
        return all(
            it.get(k) in (None, "")
            for k in ("rate", "rate_expected", "rate_low", "rate_high")
        )

    needs_input = [i for i in line_items if _is_needs_input(i)]
    if needs_input:
        parts.append(
            f"> ⚠️ **{len(needs_input)} item(s) need your input** — see lines marked "
            f"**[NEEDS RATE]** below. Reply with the missing details and I'll re-cost."
        )
        parts.append("")
    # How each line was costed, from the basis settlement wrote at the head of
    # its note (batched path); the single-call path has no basis line, so its
    # rows are counted by source. Stated as fact -- the people reading this
    # bid on it without a review step, so "please verify" is not an answer.
    from app.services import cost_breakdown_service as _cbs
    _costed = [i for i in line_items if not _is_needs_input(i)
               and not i.get("is_tax_line") and not i.get("is_component")]

    def _basis(it: dict) -> str:
        note = it.get("cost_buildup_note") or ""
        for b in (_cbs.BASIS_MARKET, _cbs.BASIS_BUILDUP, _cbs.BASIS_REFERENCE):
            if note.startswith(b):
                return b
        if it.get("rate_source") in ("web_search", "training_data", "memory", "ratecard"):
            return _cbs.BASIS_MARKET
        if it.get("rate_source") in ("derived_estimate", "component_buildup"):
            return _cbs.BASIS_BUILDUP
        return ""

    _by_basis = {b: 0 for b in (_cbs.BASIS_MARKET, _cbs.BASIS_BUILDUP, _cbs.BASIS_REFERENCE)}
    for _it in _costed:
        _b = _basis(_it)
        if _b:
            _by_basis[_b] += 1
    if _by_basis[_cbs.BASIS_BUILDUP]:
        parts.append(
            f"> 🧮 **{_by_basis[_cbs.BASIS_BUILDUP]} item(s) costed by a build-up** — what one "
            f"unit is, its materials at current prices and its labour hours at the minimum "
            f"wages plus statutory costs; each line shows its build-up."
        )
        parts.append("")
    if _by_basis[_cbs.BASIS_MARKET]:
        parts.append(
            f"> 🌐 **{_by_basis[_cbs.BASIS_MARKET]} item(s) at a verified market price or the "
            f"firm's own rate** — the source is on each line."
        )
        parts.append("")
    if _by_basis[_cbs.BASIS_REFERENCE]:
        parts.append(
            f"> 📋 **{_by_basis[_cbs.BASIS_REFERENCE]} item(s) at the railway's estimate less "
            f"overhead and margin** — no build-up could be finished for them; running the "
            f"costing again builds them up."
        )
        parts.append("")

    _recon_callout = _reconciliation_callout(reconciliation)
    if _recon_callout:
        parts.append(_recon_callout)
        parts.append("")

    # Manpower & Resource decomposition (when supplied)
    if decomposition:
        parts.append("### Manpower & Resource Decomposition\n")
        for bucket in decomposition:
            scope = bucket.get("scope_bucket") or "Scope bucket"
            parts.append(f"**{scope}**")
            manpower = bucket.get("manpower") or []
            if manpower:
                parts.append("")
                parts.append("_Manpower:_")
                parts.append("| Role | Headcount | Deployment | Rate (₹) | Unit |")
                parts.append("|------|-----------|------------|----------|------|")
                for m in manpower:
                    role = (m.get("role") or "").replace("|", "\\|")
                    hc = m.get("headcount", "")
                    depl = (m.get("deployment") or "").replace("|", "\\|")
                    rl = m.get("rate_low_inr")
                    rh = m.get("rate_high_inr")
                    rate_str = _fmt_money_range(rl, None, rh)
                    unit = (m.get("rate_unit") or "").replace("|", "\\|")
                    parts.append(f"| {role} | {hc} | {depl} | {rate_str} | {unit} |")
            resources = bucket.get("resources") or []
            if resources:
                parts.append("")
                parts.append("_Resources:_")
                parts.append("| Type | Item | Qty / Cycle | Frequency | Rate (₹) | Unit |")
                parts.append("|------|------|-------------|-----------|----------|------|")
                for r in resources:
                    rtype = (r.get("type") or "").replace("|", "\\|")
                    item = (r.get("item") or "").replace("|", "\\|")
                    qty = (r.get("quantity_per_cycle") or "").replace("|", "\\|") if isinstance(r.get("quantity_per_cycle"), str) else (r.get("quantity_per_cycle") or "")
                    freq = (r.get("frequency") or "").replace("|", "\\|") if isinstance(r.get("frequency"), str) else (r.get("frequency") or "")
                    rl = r.get("rate_low_inr")
                    rh = r.get("rate_high_inr")
                    rate_str = _fmt_money_range(rl, None, rh)
                    unit = (r.get("rate_unit") or "").replace("|", "\\|")
                    parts.append(f"| {rtype} | {item} | {qty} | {freq} | {rate_str} | {unit} |")
            drivers = bucket.get("volume_drivers") or []
            if drivers:
                parts.append("")
                parts.append("_Volume drivers:_ " + "; ".join(str(d) for d in drivers))
            parts.append("")
        parts.append("")

    # Line items — Phase 3b: when margin-analysis mode is active, group rows
    # by schedule_section and use the Tender Rate / Margin column layout.
    if line_items:
        # Group rows by schedule_section for stable per-schedule sub-tables.
        # Lines without a schedule fall into a default "—" group at the end.
        groups: dict[str, list[tuple[int, dict]]] = {}
        order: list[str] = []
        for idx, item in enumerate(line_items, 1):
            sec = (item.get("schedule_section") or "").strip() or "—"
            if sec not in groups:
                groups[sec] = []
                order.append(sec)
            groups[sec].append((idx, item))

        def _render_table_header() -> list[str]:
            if show_margin_columns:
                return [
                    "| # | Description | UoM | Qty | Tender Rate (₹) | Tender Amt (₹) | "
                    "Est. Cost (₹) | Margin (₹) | Margin % | Profit % | Source |",
                    "|---|---|---|---|---|---|---|---|---|---|---|",
                ]
            return [
                "| # | Description | Category | Qty | Unit | Rate (₹) | Amount (₹) | "
                "Profit % | Profit (₹) | Source |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]

        def _render_row(i: int, item: dict) -> str:
            desc = (item.get("description") or "").replace("|", "\\|")
            cat = item.get("category", "") or ""
            qty = item.get("quantity")
            qty_str = f"{qty:g}" if isinstance(qty, (int, float)) else (qty or "")
            unit = item.get("unit") or ""
            src = item.get("rate_source") or ""
            src_label = _SOURCE_LABEL.get(src, src)

            if _is_needs_input(item):
                if show_margin_columns:
                    return (
                        f"| {i} | {desc} | {unit} | {qty_str} | — | — | "
                        f"**[NEEDS RATE]** | — | — | — | {src_label} |"
                    )
                return (
                    f"| {i} | {desc} | {cat} | {qty_str} | {unit} | "
                    f"**[NEEDS RATE]** | — | — | — | {src_label} |"
                )

            amt_str = _fmt_money_range(
                item.get("amount_low"),
                item.get("amount_expected") if item.get("amount_expected") is not None else item.get("amount"),
                item.get("amount_high"),
            )
            pp = item.get("profit_pct")
            profit_pct_str = f"{pp:g}%" if isinstance(pp, (int, float)) else (str(pp) if pp else "—")

            if show_margin_columns:
                t_rate = item.get("tender_rate")
                t_amount = item.get("tender_amount")
                m_amt = item.get("margin_amount") or item.get("margin_amount_expected")
                m_pct = item.get("margin_pct")
                t_rate_str = _fmt_money(t_rate) if t_rate not in (None, "") else "—"
                t_amount_str = _fmt_money(t_amount) if t_amount not in (None, "") else "—"
                m_amt_str = _fmt_money(m_amt) if m_amt not in (None, "") else "—"
                m_pct_str = (
                    f"{m_pct:.1f}%" if isinstance(m_pct, (int, float)) else (str(m_pct) if m_pct else "—")
                )
                # Highlight negative margins in bold so reviewers spot them.
                if isinstance(m_amt, (int, float)) and m_amt < 0:
                    m_amt_str = f"**{m_amt_str}**"
                    m_pct_str = f"**{m_pct_str}**"
                return (
                    f"| {i} | {desc} | {unit} | {qty_str} | {t_rate_str} | {t_amount_str} | "
                    f"{amt_str} | {m_amt_str} | {m_pct_str} | {profit_pct_str} | {src_label} |"
                )

            # Range-only / legacy table
            rate_str = _fmt_money_range(
                item.get("rate_low"),
                item.get("rate_expected") if item.get("rate_expected") is not None else item.get("rate"),
                item.get("rate_high"),
            )
            profit_amt_str = _fmt_money_range(
                item.get("profit_amount_low"),
                item.get("profit_amount_expected"),
                item.get("profit_amount_high"),
            )
            return (
                f"| {i} | {desc} | {cat} | {qty_str} | {unit} | {rate_str} | "
                f"{amt_str} | {profit_pct_str} | {profit_amt_str} | {src_label} |"
            )

        parts.append("### Itemized Cost Breakdown\n")
        # Render each schedule group as its own sub-table when margin mode is on
        # and there's more than one group; otherwise just one table.
        if show_margin_columns and len([g for g in order if g != "—"]) > 1:
            for sec in order:
                if sec == "—":
                    continue
                parts.append(f"#### {sec}\n")
                parts.extend(_render_table_header())
                for i, item in groups[sec]:
                    parts.append(_render_row(i, item))
                parts.append("")
            # Append any unscheduled lines at the end
            if "—" in groups:
                parts.append("#### Unscheduled lines\n")
                parts.extend(_render_table_header())
                for i, item in groups["—"]:
                    parts.append(_render_row(i, item))
                parts.append("")
        else:
            parts.extend(_render_table_header())
            for i, item in enumerate(line_items, 1):
                parts.append(_render_row(i, item))
            parts.append("")

        # Source-detail footnotes — surface anything non-trivial so reviewers
        # can see the build-up / citations / questions inline.
        ref_lines = []
        for i, item in enumerate(line_items, 1):
            src = item.get("rate_source")
            ref = (item.get("source_ref") or "").strip()
            buildup = (item.get("cost_buildup_note") or "").strip()
            if buildup:
                ref_lines.append(f"- **#{i}** build-up — {buildup}")
            if not ref:
                continue
            if src in ("web_search", "needs_user_input", "derived_estimate"):
                ref_lines.append(f"- **#{i}** ({src}) — {ref}")
        if ref_lines:
            parts.append("**Source references & build-ups:**\n")
            parts.extend(ref_lines)
            parts.append("")

    # Totals — prefer range schema, fall back to legacy single-value totals
    if has_range_totals:
        bands = [("low", "Low"), ("expected", "Expected"), ("high", "High")]
        parts.append("### Estimation Range\n")
        parts.append("| Component | Low | Expected | High |")
        parts.append("|-----------|-----|----------|------|")
        rows = [
            ("Subtotal", "subtotal"),
            ("Overheads", "overhead_amount"),
            ("Profit Margin", "margin_amount"),
            ("Pre-tax Total", "pre_tax_total"),
            ("GST", "gst_amount"),
        ]
        for label, key in rows:
            row_vals = []
            for band_key, _ in bands:
                v = (totals.get(band_key) or {}).get(key)
                row_vals.append(_fmt_money(v) if v not in (None, "") else "—")
            parts.append(f"| {label} | {row_vals[0]} | {row_vals[1]} | {row_vals[2]} |")
        # Grand total in bold
        gt_vals = []
        for band_key, _ in bands:
            v = (totals.get(band_key) or {}).get("grand_total")
            gt_vals.append(_fmt_money(v) if v not in (None, "") else "—")
        parts.append(
            f"| **Grand Total** | **{gt_vals[0]}** | **{gt_vals[1]}** | **{gt_vals[2]}** |"
        )
        if needs_input:
            parts.append(
                f"| _Note_ | _Range excludes {len(needs_input)} unpriced item(s)_ | | |"
            )
        parts.append("")
    elif has_legacy_totals:
        # Legacy / persisted single-value totals
        subtotal = costing.get("subtotal")
        overheads = costing.get("overheads")
        profit = costing.get("profit_margin")
        gst = costing.get("gst")
        grand_total = costing.get("grand_total")
        parts.append("### Summary\n")
        parts.append("| Component | Amount |")
        parts.append("|-----------|--------|")
        if subtotal not in (None, ""):
            parts.append(f"| Subtotal | {_fmt_money(subtotal)} |")
        if overheads not in (None, ""):
            parts.append(f"| Overheads | {_fmt_money(overheads)} |")
        if profit not in (None, ""):
            parts.append(f"| Profit Margin | {_fmt_money(profit)} |")
        if gst not in (None, ""):
            parts.append(f"| GST | {_fmt_money(gst)} |")
        if grand_total not in (None, ""):
            parts.append(f"| **Grand Total** | **{_fmt_money(grand_total)}** |")
        if needs_input:
            parts.append(
                f"| _Note_ | _Total excludes {len(needs_input)} unpriced item(s)_ |"
            )
        parts.append("")

    # Assumptions
    if assumptions:
        parts.append("### Assumptions\n")
        for a in assumptions:
            parts.append(f"- {a}")
        parts.append("")

    # Phase 3b — Cost Assumptions library (matches reference Sheet 2).
    # Group entries by `section`, render as a compact rate-card per section.
    if cost_assumptions:
        sections: dict[str, list[dict]] = {}
        section_order: list[str] = []
        for entry in cost_assumptions:
            sec = (entry.get("section") or "Other").strip() or "Other"
            if sec not in sections:
                sections[sec] = []
                section_order.append(sec)
            sections[sec].append(entry)
        parts.append("### Cost Assumptions (Rate-Card Library)\n")
        for sec in section_order:
            parts.append(f"**{sec}**")
            parts.append("")
            parts.append("| Item | Rate (₹) | UoM | Source |")
            parts.append("|---|---|---|---|")
            for entry in sections[sec]:
                item_name = (entry.get("item") or "").replace("|", "\\|")
                rate = entry.get("rate_inr")
                uom = (entry.get("uom") or "").replace("|", "\\|")
                ref = (entry.get("source_ref") or "").replace("|", "\\|")
                rate_str = _fmt_money(rate) if rate not in (None, "") else "—"
                parts.append(f"| {item_name} | {rate_str} | {uom} | {ref} |")
            parts.append("")

    # Recommendations (also surfaces needs-input questions)
    if recommendations:
        parts.append("### What I need from you\n" if needs_input else "### Recommendations\n")
        for r in recommendations:
            parts.append(f"- {r}")

    return "\n".join(parts)


def _parse_checklist_structured_data(output: str) -> list[dict]:
    """Extract structured checklist JSON from LLM output.
    Looks for a JSON array between CHECKLIST_JSON_START/END markers,
    then falls back to ```json code fences, then tries parsing trailing JSON.
    """
    import json as _json

    # Try CHECKLIST_JSON markers first
    marker_pattern = r'CHECKLIST_JSON_START\s*\n?(.*?)\n?\s*CHECKLIST_JSON_END'
    match = re.search(marker_pattern, output, re.DOTALL)
    if match:
        try:
            data = _json.loads(match.group(1).strip())
            if isinstance(data, list) and len(data) > 0:
                return data
        except _json.JSONDecodeError:
            pass

    # Try ```json code fences
    fence_pattern = r'```json\s*\n(.*?)\n\s*```'
    match = re.search(fence_pattern, output, re.DOTALL)
    if match:
        try:
            data = _json.loads(match.group(1).strip())
            if isinstance(data, list) and len(data) > 0:
                return data
        except _json.JSONDecodeError:
            pass

    # Try finding any JSON array in the output
    array_pattern = r'\[\s*\{.*?\}\s*\]'
    match = re.search(array_pattern, output, re.DOTALL)
    if match:
        try:
            data = _json.loads(match.group(0))
            if isinstance(data, list) and len(data) > 0:
                return data
        except _json.JSONDecodeError:
            pass

    # Last-resort: scavenge checkbox lines from the rendered markdown so a
    # partially-compliant LLM (checklist emitted, JSON block dropped) still
    # yields persistable items. See addendum in
    # plans/now-this-is-the-lovely-journal.md (2026-04-21 workspace-init bug).
    fallback = _parse_checklist_markdown_fallback(output)
    if fallback:
        return fallback

    return []


def _parse_checklist_markdown_fallback(output: str) -> list[dict]:
    """Recover checklist items from markdown when no JSON block is present.

    Recognizes lines shaped like:
        - [ ] **Document Name** — description
        - [x] PAN Card (mandatory) — self-attested copy
        * [ ] GST Certificate — original
        1. [ ] Bank Solvency Certificate: recent, on bank letterhead

    Returns a list of ``{name, description, is_required, category}`` dicts.
    Only returns the list when at least 3 items are recovered, so stray
    ``- [ ]`` tokens in unrelated prose don't produce garbage entries.
    """
    if not output:
        return []

    # Match bullet (*/-) or numbered (1.) list items that start a checkbox.
    # Capture the checkbox state and the rest of the line.
    checkbox_re = re.compile(
        r'^\s*(?:[-*]|\d+[.)])\s*\[\s*([ xX])\s*\]\s*(.+?)\s*$',
        re.MULTILINE,
    )

    items: list[dict] = []
    seen_names: set[str] = set()

    for m in checkbox_re.finditer(output):
        raw = m.group(2).strip()
        if not raw:
            continue

        # Strip markdown emphasis & trailing punctuation noise from the raw line.
        lower = raw.lower()
        is_optional = (
            "(optional)" in lower
            or "[optional]" in lower
            or re.search(r'\boptional\b', lower) is not None
        )

        # Name = first bolded span if present, else text up to the first
        # separator (— – - : |). Description = remainder.
        name = ""
        description = ""

        bold_match = re.match(r'\*\*(.+?)\*\*\s*(.*)', raw)
        if bold_match:
            name = bold_match.group(1).strip()
            rest = bold_match.group(2).strip()
            # Drop a single leading separator if present.
            rest = re.sub(r'^[\s\-–—:|•]+', '', rest).strip()
            description = rest
        else:
            # Split on the first em/en dash, colon, or pipe.
            split_match = re.split(r'\s*[—–\-:|]\s+', raw, maxsplit=1)
            name = split_match[0].strip()
            description = split_match[1].strip() if len(split_match) > 1 else ""

        # Clean residual markdown emphasis on name/description.
        name = re.sub(r'\*+', '', name).strip().strip('"').strip("'")
        description = re.sub(r'\*+', '', description).strip()

        # Drop `(optional)` / `(mandatory)` tokens from the display name.
        name = re.sub(
            r'\s*\((?:optional|mandatory|required)\)\s*',
            ' ',
            name,
            flags=re.IGNORECASE,
        ).strip()

        if not name or len(name) < 3:
            continue

        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        items.append({
            "name": name,
            "description": description,
            "is_required": not is_optional,
            "category": "standard",
        })

    # Guard against false positives from stray "- [ ]" in unrelated content.
    if len(items) < 3:
        return []

    return items


def _strip_checklist_markers(output: str) -> str:
    """Remove CHECKLIST_JSON_START/END blocks and any preceding header from LLM output.

    Handles three cases:
      1. Complete block: CHECKLIST_JSON_START ... CHECKLIST_JSON_END
      2. Truncated block: CHECKLIST_JSON_START ... (no END — output was cut off)
      3. Orphaned header: "MACHINE-READABLE CHECKLIST DATA" without a JSON block
    """
    if not output:
        return output

    # 1. Complete blocks (START + END)
    complete_pattern = (
        r'\n*\s*'
        r'(?:[-#*\s]*MACHINE[-_ ]?READABLE[^\n]*\n)?'
        r'\s*CHECKLIST_JSON_START\s*\n?'
        r'.*?'
        r'\n?\s*CHECKLIST_JSON_END\s*\n*'
    )
    cleaned = re.sub(complete_pattern, '', output, flags=re.DOTALL | re.IGNORECASE)

    # 2. Truncated blocks (START without matching END — strips to end of string)
    truncated_pattern = (
        r'\n*\s*'
        r'(?:[-#*\s]*MACHINE[-_ ]?READABLE[^\n]*\n)?'
        r'\s*CHECKLIST_JSON_START\s*\n?'
        r'.*'
    )
    cleaned = re.sub(truncated_pattern, '', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # 3. Orphaned MACHINE-READABLE headers (no JSON block followed)
    header_pattern = r'\n*\s*[-#*\s]*MACHINE[-_ ]?READABLE\s+CHECKLIST\s+DATA[^\n]*'
    cleaned = re.sub(header_pattern, '', cleaned, flags=re.IGNORECASE)

    return cleaned.rstrip()


def _strip_file_content_markers(output: str) -> str:
    """Remove [ATTACHED FILES] and [FILE CONTENT: ...] blocks from LLM output.

    These markers are injected into user prompts for agent processing but should
    never appear in responses streamed to the frontend.
    """
    if not output:
        return output

    # 1. Remove [FILE CONTENT: filename] blocks (content runs until next marker or end)
    cleaned = re.sub(
        r'\[FILE CONTENT:\s*[^\]]*\].*?(?=\[FILE CONTENT:|\[ATTACHED FILES\]|$)',
        '', output, flags=re.DOTALL,
    )

    # 2. Remove [ATTACHED FILES] header + file listing lines (- filename (type, size bytes))
    cleaned = re.sub(
        r'\[ATTACHED FILES\]\s*\n(?:\s*-\s+[^\n]+\n?)*',
        '', cleaned, flags=re.DOTALL,
    )
    # Standalone header without listing
    cleaned = re.sub(r'\[ATTACHED FILES\]\s*', '', cleaned)

    # 3. Remove truncation notices
    cleaned = re.sub(r'\[\.\.\.\s*Document truncated[^\]]*\]', '', cleaned)
    cleaned = re.sub(r'\[Content truncated\s*[\u2014—-]+\s*file limit reached\]', '', cleaned)

    return cleaned.strip()


def _salvage_and_persist_checklist(
    db: Session,
    tender_id: Optional[int],
    proposal_session_id: Optional[int],
    result: dict,
) -> Optional[int]:
    """Belt-and-suspenders wrapper around ``_persist_checklist_items``.

    If the upstream parser returned no ``structured_data`` (LLM dropped the
    JSON block), scavenge the rendered markdown output as a last chance.
    Writes the salvaged list back onto ``result`` so the artifact persisted
    by the streaming handler downstream also carries the items — that is
    what lets the init-workspace recovery path work.
    """
    structured = result.get("structured_data") or []
    if not structured:
        salvaged = _parse_checklist_markdown_fallback(result.get("output", ""))
        if salvaged:
            structured = salvaged
            result["structured_data"] = salvaged
            logger.info(
                f"Checklist markdown fallback recovered {len(salvaged)} items "
                f"(tender_id={tender_id}, session={proposal_session_id})"
            )
    return _persist_checklist_items(db, tender_id, proposal_session_id, structured)


def _persist_checklist_items(
    db: Session,
    tender_id: Optional[int],
    proposal_session_id: Optional[int],
    checklist_items: list[dict],
) -> Optional[int]:
    """Persist structured checklist items as ChecklistItem DB records.
    Resolves tender_id from proposal_session_id if needed, auto-creates
    a Tender for standalone sessions. Returns the tender_id used, or None on failure.
    """
    if not checklist_items:
        return None

    from app.models.checklist import ChecklistItem
    from app.models.proposal import ProposalSession
    from app.models.tender import Tender
    import uuid

    # Resolve tender_id
    if not tender_id and proposal_session_id:
        session = db.query(ProposalSession).filter(
            ProposalSession.id == proposal_session_id
        ).first()
        if session:
            tender_id = session.tender_id

    # Auto-create tender for standalone sessions
    if not tender_id and proposal_session_id:
        session = db.query(ProposalSession).filter(
            ProposalSession.id == proposal_session_id
        ).first()
        if session:
            new_tender = Tender(
                portal="command_center",
                tender_id=f"cc-{str(uuid.uuid4())[:8]}",
                title=session.title or f"Command Center Workspace #{session.id}",
                status="open",
                workflow_status="proposal_draft",
                workspace_enabled=True,
            )
            db.add(new_tender)
            db.flush()

            tender_id = new_tender.id
            session.tender_id = tender_id
            session.mode = "tender_linked"
            session.agent_type = "tender_proposal"
            db.flush()

    if not tender_id:
        logger.warning("Cannot persist checklist items: no tender_id resolved")
        return None

    try:
        # Clear existing auto-generated items (keep manual/uploaded ones)
        db.query(ChecklistItem).filter(
            ChecklistItem.tender_id == tender_id,
            ChecklistItem.is_uploaded == False,
        ).delete()
        db.flush()

        # Create new ChecklistItem records
        for i, item_data in enumerate(checklist_items):
            item = ChecklistItem(
                tender_id=tender_id,
                item_name=item_data.get("name", "Unknown Document"),
                item_description=item_data.get("description", ""),
                is_required=item_data.get("is_required", True),
                display_order=i,
                item_category=item_data.get("category", "standard"),
            )
            db.add(item)

        db.commit()
        logger.info(f"Persisted {len(checklist_items)} checklist items for tender {tender_id}")
        return tender_id
    except Exception as e:
        logger.error(f"Failed to persist checklist items: {e}")
        db.rollback()
        return None


async def _generate_checklist_from_content(
    db: Session,
    message: str,
    file_metadata: Optional[dict] = None,
) -> dict:
    """Generate a submission checklist from uploaded file content using a direct LLM call."""
    system_prompt = (
        "You are a tender submission checklist generator. Analyze the provided document "
        "and extract ALL required submission documents.\n\n"
        "For each document, provide:\n"
        "- **Document Name**: Official name as mentioned in the tender\n"
        "- **Mandatory/Optional**: Whether it is required or optional\n"
        "- **Format**: Original/copy/notarized/self-attested as specified\n"
        "- **Description**: Brief note on what is required\n\n"
        "Present the checklist in a structured markdown format with checkboxes.\n"
        "Group documents by category (Technical, Financial, Legal, Administrative).\n\n"
        "**No preambles. No narration.** Do NOT describe what you are about to do, do NOT "
        "announce which tools you are calling, and do NOT restate the user's request. Start "
        "your response directly with the checklist heading (e.g. '# Submission Checklist'). "
        "Phrases like 'I'll analyze the document', 'Let me now compile', 'Now I will extract' "
        "are forbidden in your final output.\n\n"
        "IMPORTANT: After the markdown checklist, you MUST include a structured JSON block "
        "for machine processing. Use this exact format:\n\n"
        "CHECKLIST_JSON_START\n"
        '[{"name": "Document Name", "description": "Brief description", "is_required": true, "category": "standard"}, ...]\n'
        "CHECKLIST_JSON_END\n\n"
        "The category field should be one of: 'standard' (pre-existing company documents like "
        "PAN, GST, certificates), 'generated' (documents to be drafted like cover letters, "
        "declarations, undertakings), or 'analysis' (computational documents like BOQ, cost estimates).\n"
        "Include EVERY document from the checklist in the JSON array."
    )

    # Try native PDF processing first.
    # pdf_paths holds StorageService keys, not filesystem paths — materialize
    # each via as_local_file so the tempfiles stay alive across the single
    # call_ai_with_documents call.
    pdf_paths = _get_pdf_paths(file_metadata)
    if pdf_paths:
        try:
            from contextlib import ExitStack
            from app.services.ai_service import call_ai_with_documents
            from app.services.storage_service import get_storage_service

            storage = get_storage_service()
            with ExitStack() as stack:
                valid_paths: list[str] = []
                for key in pdf_paths:
                    try:
                        local_path = stack.enter_context(
                            storage.as_local_file(key, suffix=".pdf")
                        )
                        valid_paths.append(local_path)
                    except FileNotFoundError:
                        logger.warning(f"Checklist attachment missing in storage: {key}")
                        continue
                    except Exception as e:
                        logger.warning(f"Could not materialize checklist attachment {key}: {e}")
                        continue

                if valid_paths:
                    user_prompt = re.sub(r'\[ATTACHED FILES\].*', '', message, flags=re.DOTALL).strip()
                    user_prompt = user_prompt or "Generate a complete submission checklist from the attached tender document(s)."

                    response_text = await call_ai_with_documents(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        document_paths=valid_paths,
                        db=db,
                        agent_name="checklist_generator",
                    )
                    if response_text:
                        structured = _parse_checklist_structured_data(response_text)
                        return {
                            "output": _strip_checklist_markers(response_text),
                            "output_type": "checklist",
                            "agent_key": "checklist_generator",
                            "tool_calls": [],
                            "metrics": {"method": "native_pdf"},
                            "structured_data": structured,
                            "status": "completed",
                        }
        except Exception as e:
            logger.warning(f"Native PDF checklist generation failed: {e}")

    # Fallback: direct LLM call with text content
    try:
        from app.services.langchain.llm_factory import get_chat_model
        from langchain_core.messages import SystemMessage, HumanMessage

        llm = get_chat_model(db, agent_name="checklist_generator", max_tokens=4096)

        result = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=message),
        ])

        output = result.content if isinstance(result.content, str) else str(result.content)
        structured = _parse_checklist_structured_data(output)

        return {
            "output": _strip_checklist_markers(output),
            "output_type": "checklist",
            "agent_key": "checklist_generator",
            "tool_calls": [],
            "metrics": {},
            "structured_data": structured,
            "status": "completed",
        }

    except Exception as e:
        logger.error(f"Checklist generation from content failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": "checklist_generator",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }


def _infer_output_type(agent) -> str:
    """Derive output_type from the agent's category field."""
    category_map = {
        "document": "document_analysis",
        "tender_analysis": "document_analysis",
        "proposal": "proposal_document",
        "costing": "cost_breakdown",
    }
    return category_map.get(getattr(agent, "category", None) or "", "general")


async def chat_custom_agent(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    agent_key: str = "",
) -> dict:
    """
    Chat wrapper for any custom agent from the Agent Builder.
    Loads the agent by key and delegates to the appropriate execution service.
    """
    try:
        from app.services.agent_builder_service import get_agent_by_key
        from app.services.langchain.langchain_execution_service import execute_langchain_agent
        from app.services.agent_execution_service import execute_agent

        agent = get_agent_by_key(db, agent_key)
        if not agent:
            return {
                "output": f"Agent '{agent_key}' not found.",
                "output_type": "general",
                "agent_key": agent_key,
                "tool_calls": [],
                "metrics": {},
                "status": "failed",
            }

        input_data = {"message": message}
        if tender_id:
            input_data["tender_id"] = tender_id

        # Use LangChain execution for react/tool_use agents, simple for chain_of_thought
        if agent.agent_type in ("react", "tool_use"):
            # save_turn=False: the router (streaming_handler) owns the canonical
            # conversation-history save with correct routed_from metadata.
            # Without this, both layers save → history-reload renders twice.
            result = await execute_langchain_agent(
                db=db,
                agent=agent,
                input_data=input_data,
                session_id=session_id,
                save_turn=False,
            )
        else:
            result = await execute_agent(
                db=db,
                agent_key_or_id=agent_key,
                input_data=input_data,
            )

        if result.get("status") == "failed":
            raw_err = result.get("error", result.get("output", "unknown"))
            logger.error(f"Custom agent '{agent_key}' returned failed: {raw_err}")
            print(f"[DRPL ERROR] Custom agent '{agent_key}' returned failed status: {raw_err}", flush=True)

        resp = {
            "output": result.get("output") or "",
            "output_type": _infer_output_type(agent),
            "agent_key": agent_key,
            "tool_calls": result.get("tool_calls", []),
            "metrics": {
                "tokens_input": result.get("tokens_input", 0),
                "tokens_output": result.get("tokens_output", 0),
                "cost_estimate": result.get("cost_estimate", 0),
            },
            "status": result.get("status", "completed"),
        }
        # Propagate error field so streaming handler can detect failures
        if result.get("error"):
            resp["error"] = result["error"]
        return resp

    except Exception as e:
        logger.error(f"Custom agent '{agent_key}' failed: {e}", exc_info=True)
        print(f"[DRPL ERROR] Custom agent '{agent_key}' failed: {type(e).__name__}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        from app.services.langchain.error_utils import format_user_error
        return {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": agent_key,
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(e),
        }


async def chat_annexure_finder(
    db: Session,
    message: str,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
) -> dict:
    """Chat wrapper: extract every annexure / schedule / proforma from the
    tender's PDFs and materialize each as an editable workspace document.

    Prefers chat-attached PDFs when provided; otherwise uses the
    TenderDocuments already linked to the tender.
    """
    if not tender_id:
        tender_id = _extract_tender_id(message)

    pdf_paths = _get_pdf_paths(file_metadata) or None

    # Standalone-session path: user uploaded a PDF and asked for annexures
    # without a pre-existing tender. Auto-create one and link the session so
    # annexure_finder can write ChecklistItem/DocumentWorkspace rows against
    # a real tender_id (required by its DB writes). Mirrors the auto-create
    # behavior in _persist_checklist_items so standalone chat flows work
    # end-to-end. Only fires when we actually have PDFs + a session handle.
    if not tender_id and pdf_paths and proposal_session_id:
        try:
            from app.models.proposal import ProposalSession
            from app.models.tender import Tender, TenderDocument
            import os as _os
            import uuid as _uuid

            session_row = db.query(ProposalSession).filter(
                ProposalSession.id == proposal_session_id
            ).first()
            if session_row and session_row.tender_id:
                tender_id = session_row.tender_id
            elif session_row:
                new_tender = Tender(
                    portal="command_center",
                    tender_id=f"cc-{str(_uuid.uuid4())[:8]}",
                    title=session_row.title or f"Command Center Workspace #{session_row.id}",
                    status="open",
                    workflow_status="proposal_draft",
                    workspace_enabled=True,
                )
                db.add(new_tender)
                db.flush()

                # Register uploaded PDFs as TenderDocument rows so downstream
                # re-runs (and the workspace UI) can find them without the
                # chat file_metadata being in scope.
                for key in pdf_paths:
                    file_name = _os.path.basename(key) or "uploaded.pdf"
                    db.add(TenderDocument(
                        tender_id=new_tender.id,
                        file_name=file_name,
                        file_path=key,
                        mime_type="application/pdf",
                    ))

                session_row.tender_id = new_tender.id
                session_row.mode = "tender_linked"
                session_row.agent_type = "tender_proposal"
                db.flush()
                db.commit()
                tender_id = new_tender.id
                logger.info(
                    f"chat_annexure_finder: auto-created tender {tender_id} "
                    f"for standalone session {proposal_session_id} "
                    f"({len(pdf_paths)} PDF(s) linked)"
                )
        except Exception as e:
            logger.exception(
                f"chat_annexure_finder: failed to auto-create tender for session "
                f"{proposal_session_id}: {e}"
            )
            try:
                db.rollback()
            except Exception:
                pass

    if not tender_id:
        return {
            "output": (
                "I need a tender ID or an attached PDF to extract annexures. "
                "Please upload the tender PDF, or select a tender (e.g., 'extract "
                "annexures from tender 63') or open the tender's workspace first."
            ),
            "output_type": "general",
            "agent_key": "annexure_finder",
            "tool_calls": [],
            "metrics": {},
            "status": "needs_input",
        }

    from app.services.langchain.graphs.annexure_finder_agent import (
        run_annexure_extraction,
    )
    from app.services.langchain.error_utils import (
        format_user_error, is_recoverable_error,
    )

    # Auto-retry: if the run fails with an error a retry can actually fix
    # (transient provider overload / rate-limit, or a recoverable DB
    # transaction state), roll back the session and re-run automatically
    # rather than dumping "please try again" on the user. Non-recoverable
    # errors (bad input, parse failure, etc.) fail fast — retrying won't help.
    _MAX_ATTEMPTS = 3
    result = None
    last_err: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            result = await run_annexure_extraction(db, tender_id, pdf_paths)
            last_err = None
            break
        except Exception as e:
            last_err = e
            logger.error(
                f"chat_annexure_finder attempt {attempt}/{_MAX_ATTEMPTS} failed: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            print(
                f"[DRPL ERROR] chat_annexure_finder attempt {attempt} failed: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )
            # Clear the (possibly poisoned) session before any retry.
            try:
                db.rollback()
            except Exception:
                pass
            if is_recoverable_error(e) and attempt < _MAX_ATTEMPTS:
                backoff = 1.5 * attempt
                logger.warning(
                    f"[annexure] recoverable error — auto-retrying in "
                    f"{backoff:.1f}s (attempt {attempt + 1}/{_MAX_ATTEMPTS})"
                )
                await asyncio.sleep(backoff)
                continue
            break

    if last_err is not None or result is None:
        err = last_err or RuntimeError("annexure extraction returned no result")
        return {
            "output": format_user_error(err),
            "output_type": "general",
            "agent_key": "annexure_finder",
            "tool_calls": [],
            "metrics": {},
            "status": "failed",
            "error": str(err),
        }

    if result.get("status") != "completed":
        error_code = result.get("error") or "extraction_incomplete"
        _error_messages = {
            "no_pdfs": "No PDF documents were found for this tender. Please upload the tender PDF first.",
            "no_readable_pdfs": (
                "The uploaded PDF could not be processed. "
                "Please ensure it is a valid, non-password-protected PDF and try again."
            ),
            "parse_failed": (
                "The AI was unable to identify standard annexure forms in this PDF. "
                "The document may not contain fillable annexures, or the format may be unsupported. "
                f"Raw excerpt: {result.get('raw_excerpt', '')[:200]}"
            ),
        }
        friendly_output = _error_messages.get(error_code) or result.get("message") or (
            f"Annexure extraction did not complete: {error_code}"
        )
        return {
            "output": friendly_output,
            "output_type": "general",
            "agent_key": "annexure_finder",
            "tool_calls": [],
            "metrics": result.get("counts", {}),
            "status": "failed",
            "error": None,
        }

    counts = result["counts"]
    lines = [
        f"# Annexure extraction complete — tender #{tender_id}",
        "",
        f"- **Found:** {counts['found']}",
        f"- **Created:** {counts['created']}",
        f"- **Updated:** {counts['updated']}",
        f"- **Repaired:** {counts.get('repaired', 0)}",
        f"- **Skipped (locked or incomplete):** {counts['skipped']}",
        "",
    ]

    # Reconciliation: surface a shortfall vs. what the analysis identified, plus
    # any page-batches that failed or forms that came back incomplete, so the
    # user knows to re-run instead of assuming all annexures were captured.
    expected = counts.get("expected")
    dropped_batches = counts.get("dropped_batches", 0)
    skipped_incomplete = counts.get("skipped_incomplete", 0)
    captured = counts["created"] + counts["updated"]
    shortfall = isinstance(expected, int) and expected > 0 and captured < expected
    if shortfall or dropped_batches or skipped_incomplete:
        reasons = []
        if dropped_batches:
            reasons.append(
                f"{dropped_batches} page-batch(es) failed to extract"
            )
        if skipped_incomplete:
            reasons.append(
                f"{skipped_incomplete} form(s) came back incomplete"
            )
        reason_txt = (" — " + "; ".join(reasons)) if reasons else ""
        if shortfall:
            head = (
                f"> ⚠️ **Possible shortfall:** captured **{captured}** annexure(s), "
                f"but the analysis identified about **{expected}**{reason_txt}."
            )
        else:
            head = f"> ⚠️ **Heads up:**{reason_txt or ' some annexures may be missing.'}"
        lines += [
            head,
            ">",
            "> Re-run annexure extraction to capture the rest — it merges into the "
            "existing set and only fills the gaps.",
            "",
        ]

    lines.append("## Annexures")
    status_label = {
        "created": "new",
        "updated": "merged",
        "repaired": "repaired",
        "skipped_incomplete": "skipped (incomplete)",
    }
    for row in result["annexures"]:
        if row["status"] == "skipped_locked":
            tag = f"skipped ({row.get('lock_reason', 'locked')})"
        else:
            tag = status_label.get(row["status"], row["status"])
        lines.append(f"- **{row['identifier']}** — {tag}")
    lines += [
        "",
        "Open the tender workspace to fill in each annexure and download it as a Word document.",
    ]

    return {
        "output": "\n".join(lines),
        "output_type": "annexures_extracted",
        "agent_key": "annexure_finder",
        "tool_calls": [],
        "metrics": counts,
        "structured_data": result["annexures"],
        "status": "completed",
    }
