"""
DRPL LangChain - Decision Maker (Master Agent)

An autonomous ReAct orchestrator that sits above the 6 specialized agents
(deep_analyzer, checklist_generator, proposal_creator, costing_researcher,
workspace_manager, annexure_finder) plus platform diagnostic / action / LLM
meta-tools.

Responsibilities:
  - Plan multi-step workflows for ambiguous / cross-agent / troubleshooting requests
  - Call any specialized agent as a sub-tool
  - Inject extra tools into sub-agents for a single call (dynamic tool injection)
  - Invoke any LLM provider per subtask (Claude / OpenAI / Gemini)
  - Stream Thought / Action / Observation events live to the frontend timeline
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _try_parse_leaked_plan(text: str) -> Optional[dict]:
    """Best-effort recovery when the LLM writes the propose_plan call as text
    instead of invoking the tool. Looks for ``<propose_plan>...</propose_plan>``
    blocks (and a couple of related JSON shapes) and reconstructs the plan dict.

    Returns None if no plausible plan can be parsed.
    """
    if not text:
        return None
    import re as _re
    import json as _json

    block_match = _re.search(
        r"<propose_plan[^>]*>([\s\S]*?)</propose_plan>",
        text,
        flags=_re.IGNORECASE,
    )
    body = block_match.group(1) if block_match else text

    title = ""
    title_match = _re.search(r"<title[^>]*>([\s\S]*?)</title>", body, flags=_re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()
    if not title:
        # Fallback: look for "title": "..."
        json_title = _re.search(r'"title"\s*:\s*"([^"]+)"', body)
        if json_title:
            title = json_title.group(1).strip()

    reasoning = ""
    reason_match = _re.search(r"<reasoning[^>]*>([\s\S]*?)</reasoning>", body, flags=_re.IGNORECASE)
    if reason_match:
        reasoning = reason_match.group(1).strip()
    if not reasoning:
        json_reason = _re.search(r'"reasoning"\s*:\s*"([^"]+)"', body)
        if json_reason:
            reasoning = json_reason.group(1).strip()

    steps: list[dict] = []
    steps_match = _re.search(r"<steps[^>]*>([\s\S]*?)</steps>", body, flags=_re.IGNORECASE)
    steps_text = steps_match.group(1) if steps_match else body
    # Pull the first JSON array out of the steps section.
    array_match = _re.search(r"\[[\s\S]*\]", steps_text)
    if array_match:
        try:
            parsed = _json.loads(array_match.group(0))
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    desc = str(item.get("description") or "").strip()
                    if not desc:
                        continue
                    steps.append({
                        "description": desc,
                        "agent": item.get("agent") or None,
                        "rationale": item.get("rationale") or None,
                    })
        except Exception:
            pass

    # Fallback: some models emit XML-style steps instead of JSON:
    #   <item>
    #     <description>…</description>
    #     <agent>…</agent>
    #     <rationale>…</rationale>
    #   </item>
    if not steps:
        item_blocks = _re.findall(
            r"<item[^>]*>([\s\S]*?)</item>",
            steps_text,
            flags=_re.IGNORECASE,
        )
        for block in item_blocks:
            def _tag(name: str) -> str:
                m = _re.search(
                    rf"<{name}[^>]*>([\s\S]*?)</{name}>",
                    block,
                    flags=_re.IGNORECASE,
                )
                return (m.group(1).strip() if m else "")
            desc = _tag("description")
            if not desc:
                continue
            agent = _tag("agent") or None
            rationale = _tag("rationale") or None
            steps.append({
                "description": desc,
                "agent": agent,
                "rationale": rationale,
            })

    # Final fallback: some models use <step> instead of <item>.
    if not steps:
        step_blocks = _re.findall(
            r"<step[^>]*>([\s\S]*?)</step>",
            steps_text,
            flags=_re.IGNORECASE,
        )
        for block in step_blocks:
            def _tag(name: str, blk=block) -> str:
                m = _re.search(
                    rf"<{name}[^>]*>([\s\S]*?)</{name}>",
                    blk,
                    flags=_re.IGNORECASE,
                )
                return (m.group(1).strip() if m else "")
            desc = _tag("description") or block.strip()
            if not desc or len(desc) > 2000:
                continue
            agent = _tag("agent") or None
            rationale = _tag("rationale") or None
            steps.append({
                "description": desc,
                "agent": agent,
                "rationale": rationale,
            })

    if not steps:
        return None

    return {
        "title": (title or "Proposed plan")[:200],
        "steps": steps,
        "reasoning": reasoning,
    }


_EXECUTION_AGENT_HANDLERS = {
    "deep_analyzer":       ("chat_document_analysis",  "Deep Analyzer"),
    "checklist_generator": ("chat_checklist_generation", "Checklist Generator"),
    "proposal_creator":    ("chat_proposal_writing",   "Proposal Creator"),
    "costing_researcher":  ("chat_costing_research",   "Costing Researcher"),
    "annexure_finder":     ("chat_annexure_finder",    "Annexure Finder"),
    "workspace_manager":   ("chat_workspace_operations", "Workspace Manager"),
}

# When an agent hits an unrecoverable context-length limit, tell the user
# exactly which dedicated tool to use instead. These tools chunk / stream
# their way through large documents, so they don't blow past Claude's
# context window the way an inline plan step does.
_CONTEXT_LIMIT_FALLBACK = {
    "annexure_finder": (
        "This tender is too large to extract every annexure in a single "
        "pass. Open the tender workspace and click the **Re-extract** "
        "button on any individual annexure document — the workspace tool "
        "processes large PDFs in smaller batches and keeps this plan moving."
    ),
    "deep_analyzer": (
        "This tender is too large for a single forensic analysis pass. "
        "Open the tender detail page and use the sectional analysis tools "
        "there, or split your request into narrower questions."
    ),
    "checklist_generator": (
        "This tender is too large for a single checklist generation pass. "
        "Open the tender workspace and use **Generate Checklist** there — "
        "it handles long PDFs incrementally."
    ),
    "costing_researcher": (
        "This tender is too large to cost in one pass. Ask for costing on "
        "a specific scope of work (e.g. 'cost the AMC services only') or "
        "use the costing tool from the tender workspace."
    ),
    "proposal_creator": (
        "This tender is too large to draft a proposal in one pass. Use the "
        "proposal creator inside the tender workspace — it drafts sections "
        "one at a time."
    ),
    "workspace_manager": (
        "The workspace is too full to process inline. Use the workspace "
        "tools directly from the tender detail page."
    ),
}

# Per-step context budgets — keep these deliberately small so each handler
# stays far under Claude's context window regardless of how long the original
# tender PDF, user message, or previous steps were.
_MAX_ORIGINAL_MSG_SNIPPET = 600
_MAX_PRIOR_SUMMARY_CHARS = 800
_MAX_STEP_OUTPUT_SUMMARY = 220


def _is_context_too_long(err: Exception | str) -> bool:
    """Detect Claude / OpenAI context-length errors so we can retry with a
    smaller prompt instead of failing the whole step."""
    import re as _re
    text = err if isinstance(err, str) else str(err)
    text = text.lower()
    return bool(_re.search(
        r"(context.{0,10}(length|window|too long|overflow)"
        r"|input.{0,10}too long"
        r"|token.{0,10}limit.{0,20}exceed"
        r"|max.?input.?tokens"
        r"|payload.{0,10}too large)",
        text,
    ))


def _summarize_step_output(output: str) -> str:
    """Compress a step's full output into a single-sentence summary so later
    steps can reference what happened without re-sending the whole payload."""
    if not output:
        return "(no output)"
    # Take the first paragraph / sentence; strip markdown noise.
    import re as _re
    first_para = output.strip().split("\n\n", 1)[0]
    # Remove markdown headers, list markers, bold/italic markers
    first_para = _re.sub(r"^[#>\-*•✅❌⏭\s]+", "", first_para)
    first_para = _re.sub(r"[*_`]+", "", first_para)
    first_para = first_para.strip()
    if len(first_para) > _MAX_STEP_OUTPUT_SUMMARY:
        first_para = first_para[:_MAX_STEP_OUTPUT_SUMMARY].rstrip() + "…"
    return first_para or "(no readable summary)"


def _build_plan_aggregate_markdown(plan: dict, step_outputs: list[dict]) -> str:
    """Compose the final user-facing markdown from the list of completed steps."""
    status_icon = {"completed": "✅", "failed": "❌", "skipped": "⏭"}
    lines: list[str] = [f"# {plan.get('title') or 'Plan execution'}", ""]
    if plan.get("reasoning"):
        lines.append(plan["reasoning"])
        lines.append("")
    for s in step_outputs:
        icon = status_icon.get(s["status"], "•")
        lines.append(f"## {icon} Step {s['index']}: {s['agent_display_name']}")
        lines.append("")
        lines.append(f"**Task:** {s['description']}")
        lines.append("")
        if s["status"] == "failed":
            lines.append(f"> ⚠️ {s.get('error') or 'Step failed.'}")
            lines.append("")
        if s["output"]:
            lines.append(s["output"])
            lines.append("")
    return "\n".join(lines).strip()


async def _run_single_plan_step(
    db: Session,
    plan: dict,
    cursor: int,
    completed_steps: list[dict],
    original_message: str,
    session_id: Optional[str],
    tender_id: Optional[int],
    user_id: Optional[int],
    conversation_history: Optional[list[dict]],
    file_metadata: Optional[dict],
    proposal_session_id: Optional[int],
    stream_callback: Optional[Callable[[str, dict], None]],
) -> dict:
    """Run ONE step of an approved plan (the step at ``cursor``) and return
    the step result dict. The caller is responsible for persisting
    ``completed_steps + [result]`` back to session state.

    This lets /decision/respond execute one step per user click ("Run next
    step") instead of running the whole plan back-to-back — giving each step
    natural artifact breakpoints and sparing the API rate limits.
    """
    from app.services.langchain.graphs import chat_agent_wrappers as cw

    steps = [s for s in (plan.get("steps") or []) if s.get("description")]
    total = len(steps)

    if cursor < 0 or cursor >= total:
        return {
            "status": "invalid_cursor",
            "output": "",
            "error": f"Step cursor {cursor} is out of range (0..{total - 1})",
        }

    # Only emit `plan_execution_started` once (before step 0).
    if cursor == 0 and stream_callback:
        try:
            stream_callback("plan_execution_started", {
                "total_steps": total,
                "title": plan.get("title") or "Approved plan",
            })
        except Exception:
            pass

    # Re-stream a progress snapshot for already-completed steps so a refreshed
    # frontend can rebuild its progress card immediately.
    if stream_callback:
        for prev in completed_steps:
            try:
                stream_callback("plan_step_progress", {
                    "index": prev["index"],
                    "total": total,
                    "agent": prev.get("agent_key"),
                    "agent_display_name": prev.get("agent_display_name"),
                    "description": (prev.get("description") or "")[:200],
                    "status": prev.get("status") or "completed",
                    "error": prev.get("error"),
                })
            except Exception:
                pass

    step_outputs: list[dict] = list(completed_steps)  # read-only copy for context
    idx_iter_start = cursor + 1  # 1-based for display

    # --- Build and run the single step at `cursor` -----------------------
    for idx, step in [(idx_iter_start, steps[cursor])]:
        agent_key = (step.get("agent") or "").strip() or None
        description = step.get("description") or ""
        display_name = None
        handler = None

        if agent_key and agent_key in _EXECUTION_AGENT_HANDLERS:
            attr, display_name = _EXECUTION_AGENT_HANDLERS[agent_key]
            handler = getattr(cw, attr, None)

        if stream_callback:
            try:
                stream_callback("plan_step_progress", {
                    "index": idx,
                    "total": total,
                    "agent": agent_key,
                    "agent_display_name": display_name or (agent_key or "Manual"),
                    "description": description[:200],
                    "status": "running",
                })
            except Exception:
                pass

        step_output = ""
        step_status = "completed"
        step_error = None

        # Guard: most handlers require either a tender (with documents) OR
        # chat-attached PDFs to do anything useful. If neither is available,
        # skip the step with a clear reason instead of letting the handler
        # fail opaquely (which used to leak a confusing error up to the UI).
        needs_document_context = agent_key in {
            "deep_analyzer", "annexure_finder", "checklist_generator",
            "proposal_creator", "costing_researcher",
        }
        has_attachments = bool((file_metadata or {}).get("attachment_paths"))
        if handler is not None and needs_document_context and not tender_id and not has_attachments:
            step_status = "skipped"
            step_error = (
                "No tender linked to this session and no PDF attached — "
                f"skipping this step ({display_name or agent_key}). "
                "Attach a tender PDF or open a tender workspace and try again."
            )
            step_output = f"_Skipped — {step_error}_"
            handler = None  # fall through to the append block below

        # Guard: dependent agents can't run blind when Deep Analyzer failed
        # and no prior tender analysis was persisted. Without this, Step 2
        # silently runs without scope context and hallucinates a costing.
        _DEEP_ANALYZER_DEPENDENTS = {
            "costing_researcher", "annexure_finder",
            "checklist_generator", "proposal_creator",
        }
        if (
            handler is not None
            and step_status != "skipped"
            and agent_key in _DEEP_ANALYZER_DEPENDENTS
        ):
            prior_deep_failed = any(
                (s.get("agent_key") == "deep_analyzer" and s.get("status") == "failed")
                for s in step_outputs
            )
            if prior_deep_failed:
                has_persisted_analysis = False
                if tender_id:
                    try:
                        from app.models.document_analysis import (
                            DocumentExtractionResult, TenderAnalysisSummary,
                        )
                        if db.query(TenderAnalysisSummary).filter(
                            TenderAnalysisSummary.tender_id == tender_id
                        ).first():
                            has_persisted_analysis = True
                        else:
                            # Look for per-doc summaries — but skip rows that
                            # are just "unreadable" markers (Fix 4 persists
                            # those even on doc-level failure for UI display).
                            candidate_rows = (
                                db.query(DocumentExtractionResult)
                                .filter(
                                    DocumentExtractionResult.tender_id == tender_id,
                                    DocumentExtractionResult.summary_json.isnot(None),
                                )
                                .limit(20)
                                .all()
                            )
                            for row in candidate_rows:
                                sj = row.summary_json or {}
                                if isinstance(sj, dict) and not sj.get("unreadable"):
                                    has_persisted_analysis = True
                                    break
                    except Exception as _check_err:
                        logger.debug(
                            f"[decision_maker] dependency check failed (non-fatal): {_check_err}"
                        )
                        try:
                            db.rollback()
                        except Exception:
                            pass
                if not has_persisted_analysis:
                    step_status = "skipped"
                    step_error = (
                        "Skipped — Deep Analyzer step failed and no prior "
                        "analysis is available. Re-run Step 1 first, or run "
                        f"{display_name or agent_key} standalone."
                    )
                    step_output = "_⚠ Skipped — Deep Analyzer dependency unmet._"
                    handler = None  # fall through to the append block below

        # ─── Custom agent dispatch (Agent Builder) ────────────────────────
        # If the plan named an agent key that isn't one of the 6 hardcoded
        # wrappers, check if it matches a published CustomAgent row and if so
        # dispatch via agent_execution_service.execute_agent(). Same path the
        # router graph uses for standalone custom-agent calls.
        custom_agent_dispatched = False
        if handler is None and step_status != "skipped" and agent_key:
            try:
                from app.models.agent_builder import CustomAgent
                from app.services.agent_execution_service import execute_agent
                ca = db.query(CustomAgent).filter(
                    CustomAgent.agent_key == agent_key,
                    CustomAgent.is_enabled == True,  # noqa: E712
                    CustomAgent.is_published == True,  # noqa: E712
                ).first()
                if ca:
                    custom_agent_dispatched = True
                    display_name = ca.display_name or agent_key
                    # Include uploaded document paths so a custom agent's
                    # document-reader tool can reach the attached PDF(s) even
                    # when the session is standalone. execute_agent's prompt
                    # builder picks up the `uploaded_documents` key.
                    _uploaded_paths = [
                        ap.get("path") if isinstance(ap, dict) else ap
                        for ap in ((file_metadata or {}).get("attachment_paths") or [])
                    ]
                    _uploaded_paths = [p for p in _uploaded_paths if p]
                    input_data = {
                        "message": description,
                        "tender_id": tender_id,
                        "uploaded_documents": _uploaded_paths,
                    }
                    try:
                        ca_result = await execute_agent(
                            db=db,
                            agent_key_or_id=agent_key,
                            input_data=input_data,
                            user_id=user_id,
                        )
                        step_output = (ca_result.get("output") or "").strip() if isinstance(ca_result, dict) else str(ca_result)
                        if isinstance(ca_result, dict) and ca_result.get("status") == "failed":
                            step_status = "failed"
                            step_error = (
                                (step_output[:280] if step_output else None)
                                or ca_result.get("error")
                                or "custom_agent_failed"
                            )
                    except Exception as ca_err:
                        logger.exception(f"[decision_maker] custom agent {agent_key} failed")
                        step_status = "failed"
                        step_error = str(ca_err)[:500]
                        step_output = f"_Custom agent failed — {step_error}_"
            except Exception as e:
                logger.debug(f"Custom agent lookup failed (non-fatal): {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

        if handler is None and step_status != "skipped" and not custom_agent_dispatched:
            # No matching agent anywhere — render the description as a manual
            # note so the user sees a placeholder, then continue.
            step_output = f"_Manual step — no automated agent available._\n\n{description}"
            step_status = "skipped"
        elif handler is not None:
            # Build a tightly-bounded per-step message. Each step sees:
            #   1. Its own description (full)
            #   2. A short snippet of the user's original ask (truncated)
            #   3. A compact one-line summary of each prior step (no raw output)
            # This prevents context from ballooning across a multi-step plan,
            # which is what was blowing past Claude's token limit on Step 3.
            orig_snippet = (original_message or "").strip()
            if len(orig_snippet) > _MAX_ORIGINAL_MSG_SNIPPET:
                orig_snippet = orig_snippet[:_MAX_ORIGINAL_MSG_SNIPPET].rstrip() + "…"

            prior_bits: list[str] = []
            # Keep the last few step summaries, bounded by total chars.
            budget = _MAX_PRIOR_SUMMARY_CHARS
            for prev in reversed(step_outputs):  # most recent first
                prev_summary = _summarize_step_output(prev.get("output") or "")
                line = f"- Step {prev['index']} ({prev['agent_display_name']}): {prev_summary}"
                if len(line) > budget:
                    break
                prior_bits.append(line)
                budget -= len(line)
            prior_bits.reverse()
            prior_block = ("\n## Previous steps completed:\n" + "\n".join(prior_bits)) if prior_bits else ""

            compact_message = (
                f"{description}\n\n"
                f"(Original request: {orig_snippet})"
                f"{prior_block}"
            )

            # When this step follows a SUCCESSFUL Deep Analyzer in the same
            # plan, hand the next handler an explicit hint via file_metadata
            # so it can prefer DB-persisted analysis (TenderAnalysisSummary,
            # DocumentExtractionResult, CommandCenterArtifact) over the
            # 220-char prior_block summary. The full analysis content lives
            # in those tables — the prior_block is intentionally tiny for
            # context-window safety, so dependents need to reach for the DB.
            _step_file_metadata: dict = dict(file_metadata or {})
            if any(
                (s.get("agent_key") == "deep_analyzer" and s.get("status") == "completed")
                for s in step_outputs
            ):
                _step_file_metadata["plan_prior_deep_analysis"] = True

            async def _invoke_handler(msg: str, pass_history: bool):
                return await handler(
                    db=db,
                    message=msg,
                    tender_id=tender_id,
                    session_id=session_id,
                    conversation_history=(conversation_history or []) if pass_history else [],
                    file_metadata=_step_file_metadata,
                    proposal_session_id=proposal_session_id,
                )

            def _apply_context_fallback():
                """When the handler has exhausted all context-trim options,
                downgrade to a Skipped step with an actionable suggestion so
                the user has a clear next action instead of a silent failure.
                Mutates step_status / step_error / step_output in-place via
                nonlocal closure."""
                nonlocal step_status, step_error, step_output
                fallback_msg = _CONTEXT_LIMIT_FALLBACK.get(
                    agent_key or "",
                    "This step's input is too large for inline processing. "
                    "Try the dedicated tool for this task from the tender workspace.",
                )
                step_status = "skipped"
                step_error = "Input too large — " + fallback_msg
                step_output = f"_⚠ Skipped — {fallback_msg}_"

            try:
                result = await _invoke_handler(compact_message, pass_history=True)
                step_output = (result.get("output") or "").strip() if isinstance(result, dict) else str(result)
                if isinstance(result, dict) and result.get("status") == "failed":
                    step_status = "failed"
                    step_error = (
                        (step_output[:280] if step_output else None)
                        or result.get("error")
                        or "agent_failed"
                    )
                    # Retry once with the bare-minimum prompt. Note this only
                    # helps for handlers that actually USE the `message` arg;
                    # some (like annexure_finder) build their own prompt
                    # internally, so the retry is basically identical to the
                    # first call — we fall through to the fallback below.
                    if _is_context_too_long(step_error or step_output):
                        logger.warning(
                            f"[decision_maker] step {idx} ({agent_key}) hit context "
                            "limit — retrying with minimal prompt"
                        )
                        try:
                            retry_result = await _invoke_handler(description, pass_history=False)
                            retry_output = (retry_result.get("output") or "").strip() if isinstance(retry_result, dict) else str(retry_result)
                            if isinstance(retry_result, dict) and retry_result.get("status") != "failed":
                                step_output = retry_output
                                step_status = "completed"
                                step_error = None
                            elif _is_context_too_long(retry_output) or _is_context_too_long(
                                (retry_result or {}).get("error") if isinstance(retry_result, dict) else ""
                            ):
                                _apply_context_fallback()
                            else:
                                step_output = retry_output or step_output
                                step_error = (
                                    (retry_output[:280] if retry_output else step_error)
                                    or "agent_failed"
                                )
                        except Exception as retry_err:
                            if _is_context_too_long(retry_err):
                                _apply_context_fallback()
                            else:
                                step_error = str(retry_err)[:500]
            except Exception as e:
                logger.exception(f"[decision_maker] step {idx} ({agent_key}) failed")
                if _is_context_too_long(e):
                    logger.warning(
                        f"[decision_maker] step {idx} ({agent_key}) raised "
                        "context-too-long on first call — attempting minimal-prompt retry"
                    )
                    try:
                        retry_result = await _invoke_handler(description, pass_history=False)
                        if isinstance(retry_result, dict) and retry_result.get("status") != "failed":
                            step_output = (retry_result.get("output") or "").strip()
                            step_status = "completed"
                            step_error = None
                        else:
                            _apply_context_fallback()
                    except Exception as retry_err:
                        if _is_context_too_long(retry_err):
                            _apply_context_fallback()
                        else:
                            step_status = "failed"
                            step_error = str(retry_err)[:500]
                            step_output = f"_Step failed — {step_error}_"
                else:
                    step_status = "failed"
                    step_error = str(e)[:500]
                    step_output = f"_Step failed — {step_error}_"

        # ─── Artifact creation (per step) ──────────────────────────────────
        # Mirror the single-agent branch in streaming_handler (lines ~841-867)
        # so each plan step's output shows up in the session's Artifacts panel
        # the same way a standalone agent run would.
        artifact_info: Optional[dict] = None
        if (
            proposal_session_id
            and step_status == "completed"
            and step_output
            and agent_key
        ):
            try:
                from app.services.artifact_service import (
                    build_artifact_payload, create_artifact,
                )
                # Resolve metadata directly from agent_key + display_name so
                # custom Agent Builder agents (tender_doc_analyzer, etc.) and
                # all six built-in wrappers reliably produce visible artifacts
                # — bypasses the brittle output_type string join the standalone
                # path uses.
                #
                # Forward the agent's structured_data (parsed analysis JSON,
                # costing line items, checklist rows) so the artifact panel
                # renders the styled view (collapsible sections + severity
                # badges for analysis, line-item table for costing, etc.)
                # instead of falling back to raw markdown.
                step_structured_data = (
                    result.get("structured_data") if isinstance(result, dict) else None
                )
                artifact_meta = build_artifact_payload(
                    agent_key=agent_key,
                    output=step_output,
                    display_name=display_name,
                    structured_data=step_structured_data,
                )
                if artifact_meta:
                    artifact = create_artifact(
                        db=db,
                        session_id=proposal_session_id,
                        artifact_type=artifact_meta["artifact_type"],
                        title=artifact_meta["title"],
                        content=artifact_meta["content"],
                        structured_data=artifact_meta.get("structured_data"),
                        agent_key=agent_key,
                        created_by=user_id,
                    )
                    artifact_info = {
                        "artifact_id": artifact.id,
                        "artifact_type": artifact.artifact_type,
                        "title": artifact.title,
                        "version": artifact.version,
                    }
                    if stream_callback:
                        try:
                            stream_callback("artifact_created", artifact_info)
                        except Exception:
                            pass

                # Mirror streaming_handler's second-artifact emit for the
                # costing-agent's XLSX side-effect. Without this the
                # cost_breakdown_xlsx artifact only appears after the next
                # artifact_created refresh — confusing when costing is the
                # last step of the plan.
                xlsx_info = (
                    result.get("xlsx_artifact") if isinstance(result, dict) else None
                )
                if (
                    xlsx_info
                    and isinstance(xlsx_info, dict)
                    and xlsx_info.get("artifact_id")
                    and stream_callback
                ):
                    try:
                        stream_callback("artifact_created", {
                            "artifact_id": xlsx_info["artifact_id"],
                            "artifact_type": xlsx_info.get(
                                "artifact_type", "cost_breakdown_xlsx"
                            ),
                            "title": xlsx_info.get("title", "Cost Breakdown"),
                            "version": xlsx_info.get("version", 1),
                        })
                    except Exception:
                        pass
            except Exception as ae:
                logger.warning(f"[decision_maker] artifact creation failed for step {idx}: {ae}")
                try:
                    db.rollback()
                except Exception:
                    pass

        # ─── Auto-init workspace after a successful checklist_generator ────
        # If the step just generated a checklist AND we have a tender, kick
        # off workspace init now so downstream steps/actions don't trip on
        # "Please generate a checklist first, then initialize workspace."
        workspace_init_info: Optional[dict] = None
        if (
            step_status == "completed"
            and agent_key == "checklist_generator"
            and tender_id
            and user_id
        ):
            try:
                from app.services.workspace_service import init_workspace
                ws_result = init_workspace(db, tender_id, user_id)
                workspace_init_info = {
                    "tender_id": tender_id,
                    "workspace": ws_result,
                }
                if stream_callback:
                    try:
                        stream_callback("workspace_initialized", {
                            "tender_id": tender_id,
                        })
                    except Exception:
                        pass
            except Exception as we:
                logger.warning(f"[decision_maker] auto workspace init failed: {we}")
                try:
                    db.rollback()
                except Exception:
                    pass

        step_outputs.append({
            "index": idx,
            "agent_key": agent_key,
            "agent_display_name": display_name or (agent_key or "Manual"),
            "description": description,
            "status": step_status,
            "error": step_error,
            "output": step_output,
            "artifact": artifact_info,
            "workspace_initialized": bool(workspace_init_info),
        })

        if stream_callback:
            try:
                stream_callback("plan_step_progress", {
                    "index": idx,
                    "total": total,
                    "agent": agent_key,
                    "agent_display_name": display_name or (agent_key or "Manual"),
                    "description": description[:200],
                    "status": step_status,
                    "error": step_error,
                    "artifact_id": (artifact_info or {}).get("artifact_id"),
                })
            except Exception:
                pass

    # The single-step for-loop above executed exactly one iteration and
    # populated `step_outputs` with the new entry appended at the end.
    # Return just that entry plus the global total so the caller knows
    # whether to pause or complete.
    new_entry = step_outputs[-1] if len(step_outputs) > len(completed_steps) else None
    return {
        "status": "ok" if new_entry else "noop",
        "step_result": new_entry,
        "total_steps": total,
        "next_cursor": cursor + 1,
        "is_final_step": (cursor + 1) >= total,
    }


def _render_plan_as_markdown(plan: dict) -> str:
    """Render a proposed plan dict as clean markdown.

    Used as the ``output`` field when the decision_maker returns a
    ``pending_plan`` status — serves as a fallback for clients that don't
    support the structured approval card (e.g. conversation history exports).
    """
    title = (plan.get("title") or "Proposed plan").strip()
    reasoning = (plan.get("reasoning") or "").strip()
    steps = plan.get("steps") or []
    lines = [f"## {title}"]
    if reasoning:
        lines.append("")
        lines.append(reasoning)
    if steps:
        lines.append("")
        lines.append("**Steps:**")
        for i, s in enumerate(steps, start=1):
            agent = s.get("agent") or ""
            agent_suffix = f" _(via `{agent}`)_" if agent else ""
            lines.append(f"{i}. {s.get('description', '').strip()}{agent_suffix}")
    lines.append("")
    lines.append("_Waiting for your approval — use the buttons above to approve, request changes, or deny this plan._")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# System prompt — defines the master agent's charter
# ────────────────────────────────────────────────────────────────────────────

DECISION_MAKER_SYSTEM = """You are DRPL's Master Agent — the autonomous decision maker for the tender
intelligence platform. You sit above a team of specialized agents and have full
authority over the platform to satisfy complex user requests in a single turn.

## Your capabilities
You have 4 families of tools:

**A. Agent wrappers** — call any specialized agent as a sub-tool:
  - call_deep_analyzer           (forensic tender analysis, eligibility, risks)
  - call_checklist_generator     (submission document checklist)
  - call_proposal_creator        (proposal / cover letter / declaration drafts)
  - call_costing_researcher      (market-rate cost breakdowns)
  - call_annexure_finder         (extract every annexure as an editable doc)
  - call_workspace_manager       (init/list/generate canvas workspace items)

**B. Diagnostic tools** — read-only platform state inspection:
  - inspect_workspace, inspect_tender, inspect_session
  - list_recent_errors, check_annexure_extraction

**C. Action tools** — narrowly-scoped repair / re-run:
  - init_workspace_force, regenerate_checklist, regenerate_annexures
  - finalize_document, retry_failed_agent_run

**D. LLM / web / doc tools** — pick the best model per subtask:
  - run_with_llm(provider, prompt, model?)  — anthropic / openai / google one-shot
  - web_search(query)
  - document_reader(tender_id)

## Operating rules
1. THINK STEP BY STEP before each action. Verbalize your plan.
2. Prefer calling specialized agents (Family A) over re-implementing their
   logic with raw LLM calls. They already integrate with the platform's
   artifact, workspace, and checklist systems.
3. For TROUBLESHOOTING — inspect FIRST (Family B), then act (Family C).
   Never blindly repair without diagnosing.
4. For MULTI-STEP requests — fan out to the right specialized agents in
   sequence. Pass the same tender_id to each.
5. For STRUCTURED EXTRACTION or niche reasoning — use run_with_llm with the
   best provider for the job (OpenAI for strict JSON, Gemini Flash for
   cheap vision, Claude for long-context reasoning).
6. BUDGET — you have __BUDGET_ITERS__ iterations and ~__BUDGET_SECS__s wall-clock. Stop early when
   the user's request is fully satisfied. Do not re-invoke the same tool
   with the same args (watch for loops).
7. FINAL ANSWER must be a clean, user-facing Markdown summary that:
   - Describes what you did and why (high-level, not tool-by-tool)
   - Includes key findings, artifacts produced, or fixes applied
   - Does NOT leak internal tool names unless the user asked "how did you do it"

## Relaying a worker's output — the answer belongs to the user

When you delegate, what the worker produced IS the deliverable. The user asked
for the analysis, the costing, the checklist — not for your description of the
fact that one exists. Carry its substance into your answer at the depth the
question calls for: the findings, figures, clauses, assumptions and caveats
that bear on what was asked. Never replace numbers with a sentence saying
numbers were produced, and never compress a page of reasoning to a line merely
because it was long.

Proportion, not transcription. A worker that produced a FILE — a costing
workbook, a generated document — has already delivered it, and the user opens
the artifact for the rows. Give them the totals, the basis, the assumptions and
anything that needs a decision, not six hundred line items retyped into chat.
Findings that exist only in the worker's prose are the opposite case: they live
nowhere but your answer, so they belong in it.

Never claim what you have not read. Every worker result carries
`output_complete`; when it is false the text you were handed stops partway and
`output_handle` names the rest, so call `read_worker_output(handle, offset)`
until you have what your answer needs. When a result says its `structured_data`
was omitted, read it back with the capability for that artifact type rather
than estimating from the prose. And if you are running out of room, say what
you have covered and what remains rather than stopping mid-sentence.
"""


# Planning-mode prompt — only the propose_plan tool is available. The agent
# MUST call propose_plan exactly once via the tool API, and stop.
DECISION_MAKER_PLANNING_SYSTEM = """You are DRPL's Master Agent in PLANNING mode.

The user has asked for help and you must propose a step-by-step plan for their
approval BEFORE any real work happens.

## CRITICAL — never ask the user to share files

If the user's message or the attached context mentions a tender/document/PDF,
assume it has ALREADY been uploaded and will be handed to the execution agents
(deep_analyzer, annexure_finder, etc.), which read the file directly. You do
NOT need to see the file content yourself to design a plan. Your job here is
PLANNING, not analysis.

Forbidden responses: "Please share the tender document", "Upload the PDF",
"Paste the text", "I need to see the document first", or any variant. Even
when you cannot see the attachment content, you MUST still propose a plan
based on the user's stated intent. The execution agents will do the reading.

## How to respond — read carefully

You have ONE tool available: `propose_plan`. You must invoke it through the
tool-use API exactly once. The tool takes three arguments:

- title: a short human-readable title (under 80 chars)
- steps: an ordered list of objects, each with `description`, `agent` (one of
  the agent keys below or null), and an optional `rationale`
- reasoning: a 2-3 sentence paragraph explaining the overall approach

## CRITICAL — what NOT to do

Do NOT write the tool invocation as text. Specifically:
- DO NOT output XML like `<propose_plan>...</propose_plan>` or any nested
  tags such as `<title>`, `<steps>`, `<item>`, `<step>`, `<description>`,
  `<agent>`, `<rationale>`, `<reasoning>`. None of these are valid in your
  response text. The `propose_plan` tool has a JSON schema; use the tool API.
- DO NOT output JSON describing the tool call inside your message
- DO NOT output any "I'll call the tool" or "Here is my plan" preamble
- DO NOT use `<think>` tags or any chain-of-thought markup
- DO NOT write any explanatory text before or after the tool invocation

The propose_plan tool is a real function call, NOT a text format. Invoke it
through the structured tool-use mechanism the SDK provides. Your assistant
message should contain the tool call and nothing else; any text you emit will
be shown raw to the user and is considered a bug.

### Wrong (example of a bug — do NOT do this)
```
<propose_plan>
<title>Some plan</title>
<steps>
<item><description>Step 1</description><agent>deep_analyzer</agent></item>
</steps>
</propose_plan>
```

### Right
Invoke `propose_plan` via the tool API with the arguments `{title, steps, reasoning}`.
No accompanying text in your response.

## Available specialized agents (for each step's `agent` field)
- deep_analyzer       — forensic tender analysis (eligibility, risks, requirements)
- checklist_generator — submission document checklist
- proposal_creator    — proposal / cover letter / declaration drafts
- costing_researcher  — market-rate cost breakdowns
- annexure_finder     — extract annexures / schedules / proformas as editable docs
- workspace_manager   — init / list / generate canvas workspace items

For diagnostic checks, repairs, or direct LLM calls, describe the step in
plain English and set `agent` to null.

## Step dependencies — write the description so the next agent USES the prior step

When a plan includes BOTH `deep_analyzer` and `costing_researcher` for the same
tender, sequence Deep Analyzer FIRST, and phrase the costing step's
`description` to reference the prior analysis explicitly. The downstream
agents are conditioned on the description text — without an explicit handoff
phrase, the costing agent under-uses the prior analysis and produces weaker
reasoning.

Good costing-step descriptions:
- "Using the Deep Analyzer's structured output for this tender, produce an
  item-wise costing for each component in the identified scope."
- "Cost the scope items the Deep Analyzer extracted: derive line-item
  quantities from the BoQ / scope-of-work and apply DRPL rate cards."

Avoid vague descriptions like "Cost the tender" or "Provide costing" — they
don't tell the costing agent that prior analysis is available to build on.
The same pattern applies to `annexure_finder` and `proposal_creator` when
they follow `deep_analyzer`: name the prior step in the description.
"""


# Execution-mode prompt — full tool catalog, with the approved plan injected.
DECISION_MAKER_EXECUTION_SYSTEM = """You are DRPL's Master Agent in EXECUTION mode.

The user has approved the following plan. Execute it using your tools. You may
adapt the plan if you discover something unexpected, but stay within its spirit.

## Approved plan
{approved_plan_block}

## Available tools (same 4 families as before)
A. Agent wrappers: call_deep_analyzer, call_checklist_generator, call_proposal_creator,
   call_costing_researcher, call_annexure_finder, call_workspace_manager
B. Diagnostic: inspect_workspace, inspect_tender, inspect_session, list_recent_errors,
   check_annexure_extraction
C. Action: init_workspace_force, regenerate_checklist, regenerate_annexures,
   finalize_document, retry_failed_agent_run
D. LLM / web / doc: run_with_llm, web_search, document_reader

## Operating rules
1. Do NOT call `propose_plan` again — that phase is over.
2. Prefer specialized agents over re-implementing their work with raw LLM calls.
3. Budget: __BUDGET_ITERS__ iterations / ~__BUDGET_SECS__s. Stop early when the request is satisfied.
4. Your FINAL ANSWER must be clean, user-facing Markdown describing what you did,
   the findings / artifacts produced, and any follow-ups. Do NOT emit <think> tags
   or raw tool JSON.

## Relaying a worker's output — the answer belongs to the user

When you delegate, what the worker produced IS the deliverable. The user asked
for the analysis, the costing, the checklist — not for your description of the
fact that one exists. Carry its substance into your answer at the depth the
question calls for: the findings, figures, clauses, assumptions and caveats
that bear on what was asked. Never replace numbers with a sentence saying
numbers were produced, and never compress a page of reasoning to a line merely
because it was long.

Proportion, not transcription. A worker that produced a FILE — a costing
workbook, a generated document — has already delivered it, and the user opens
the artifact for the rows. Give them the totals, the basis, the assumptions and
anything that needs a decision, not six hundred line items retyped into chat.
Findings that exist only in the worker's prose are the opposite case: they live
nowhere but your answer, so they belong in it.

Never claim what you have not read. Every worker result carries
`output_complete`; when it is false the text you were handed stops partway and
`output_handle` names the rest, so call `read_worker_output(handle, offset)`
until you have what your answer needs. When a result says its `structured_data`
was omitted, read it back with the capability for that artifact type rather
than estimating from the prose. And if you are running out of room, say what
you have covered and what remains rather than stopping mid-sentence.
"""


# Autonomous-mode prompt — the agent forms its OWN plan internally and executes
# it end-to-end with the full tool catalog. No approval gate. It pauses ONLY via
# the `ask_user` tool, and only when genuinely blocked.
DECISION_MAKER_AUTONOMOUS_SYSTEM = """You are DRPL's Master Agent, operating AUTONOMOUSLY.

You sit above a team of specialized agents and have full authority over the
platform to satisfy the user's request in a single turn. You think, decide,
and ACT — you do NOT ask the user to approve a plan before doing the work.

## Your tools (4 families + an escape hatch)

**A. Worker agents** — call any of them as a sub-tool. Your exact roster is
listed under "YOUR WORKER AGENTS" below; it comes from the platform's agent
registry, so it changes when an administrator adds, renames, or disables an
agent. Call them by the names given there and do not invent others.

Every worker already reaches the shared tool repository, so you rarely need to
think about tools at all. When a task genuinely needs a capability a worker
would not reach — a narrowed agent, or a one-off need — pass `extra_tools` with
the tool keys to grant it for that single call. The grant applies to that call
only and does not change the agent's saved configuration. Do not pass
`extra_tools` speculatively.

**B. Diagnostic tools** (read-only): inspect_workspace, inspect_tender,
   inspect_session, list_recent_errors, check_annexure_extraction

**C. Action tools** (repair / re-run): init_workspace_force, regenerate_checklist,
   regenerate_annexures, finalize_document, retry_failed_agent_run

**D. LLM / web / doc**: run_with_llm(provider, prompt, model?), web_search(query),
   document_reader(tender_id)

**Escape hatch — `ask_user(question)`**: use ONLY when you are genuinely blocked by
missing information you cannot derive from the tender, the attached files, or your
tools. NOT for approval or permission — you are autonomous; act first.

## How to operate
1. THINK first: form your own step-by-step plan internally, then execute it
   yourself by calling the tools. Do not narrate a plan and wait — do the work.
2. Prefer specialized agents (Family A) over raw LLM calls — they integrate with
   the platform's artifact / workspace / checklist systems. Pass the same
   tender_id to each so their outputs build on one another.
3. For MULTI-STEP requests, fan out to the right agents in sequence. When costing
   or annexures follow analysis, run analysis first and tell the next agent to use
   its output.
4. For TROUBLESHOOTING, inspect FIRST (Family B), then repair (Family C). Never
   blindly repair without diagnosing.
5. NEVER ask the user to upload or re-share a file. If the message or context
   mentions a tender / document / PDF, it is already uploaded and the execution
   agents read it directly.
6. Only call `ask_user` when truly blocked. Otherwise make a sensible default
   choice, note it in your summary, and proceed.
7. BUDGET: ~__BUDGET_ITERS__ iterations / ~__BUDGET_SECS__s. Stop when the request is satisfied. Do not
   re-invoke the same tool with the same args (avoid loops).
8. FINAL ANSWER: clean, user-facing Markdown — what you did and why (high-level),
   key findings, artifacts produced, fixes applied, and any follow-ups. Do NOT
   leak internal tool names unless asked, and do NOT emit <think> tags or tool JSON.

## Relaying a worker's output — the answer belongs to the user

When you delegate, what the worker produced IS the deliverable. The user asked
for the analysis, the costing, the checklist — not for your description of the
fact that one exists. Carry its substance into your answer at the depth the
question calls for: the findings, figures, clauses, assumptions and caveats
that bear on what was asked. Never replace numbers with a sentence saying
numbers were produced, and never compress a page of reasoning to a line merely
because it was long.

Proportion, not transcription. A worker that produced a FILE — a costing
workbook, a generated document — has already delivered it, and the user opens
the artifact for the rows. Give them the totals, the basis, the assumptions and
anything that needs a decision, not six hundred line items retyped into chat.
Findings that exist only in the worker's prose are the opposite case: they live
nowhere but your answer, so they belong in it.

Never claim what you have not read. Every worker result carries
`output_complete`; when it is false the text you were handed stops partway and
`output_handle` names the rest, so call `read_worker_output(handle, offset)`
until you have what your answer needs. When a result says its `structured_data`
was omitted, read it back with the capability for that artifact type rather
than estimating from the prose. And if you are running out of room, say what
you have covered and what remains rather than stopping mid-sentence.
"""


# ────────────────────────────────────────────────────────────────────────────
# Run budget
#
# The Master Agent delegates; its budget therefore has to cover the workers it
# calls, not just its own thinking. `_run_deep_analyzer_for_tender` alone runs
# for up to 1200s. When this was a 120s literal, every request that produced
# real work was cancelled mid-delegation and the user was asked to type
# "Continue" — which restarted the run and hit the same wall. Both numbers now
# come from PlatformSetting-backed config so they can be tuned without a deploy.
# ────────────────────────────────────────────────────────────────────────────


#: How far inside the RQ job timeout the Master's own wall clock must sit.
#:
#: The graceful timeout path below (`asyncio.TimeoutError` ->
#: `render_partial_timeout_message`, status "partial") only reaches the user if
#: it fires BEFORE RQ's death penalty kills the job. With
#: `master_agent_max_execution_time_s` at 1800 and
#: `run_service._RUN_JOB_TIMEOUT_SECONDS` also at 1800, it could not: on
#: 2026-09-11 a costing run over a dozen annexures ended as
#: `run_router_task[727de58b...] failed: Task exceeded maximum timeout value
#: (1800 seconds)` — the run marked *failed* with a raw RQ message, instead of
#: *partial* with the trace of what it had actually produced.
#:
#: The margin is for what happens after the graceful return: rendering the
#: partial message, the terminal status write, `save_partial_turn`, the
#: run_done event and the notification. Those are a handful of statements, so
#: 120 s is generous rather than tight — and a run that needed the last two
#: minutes of thirty was being killed anyway.
_JOB_TIMEOUT_MARGIN_S = 120.0


def resolve_master_budget() -> tuple[int, float]:
    """(max_iterations, max_execution_time) for one Master Agent run.

    The time budget is clamped to sit strictly inside the RQ job timeout, so
    the run always gets to finish on its own terms and hand back the work it
    did. Raising `master_agent_max_execution_time_s` past the job timeout is
    therefore a no-op rather than a silent regression to a hard kill: to give
    a run longer, `_RUN_JOB_TIMEOUT_SECONDS` has to move too (and so does the
    SSE endpoint's own wall clock, which is deliberately longer than both).
    """
    st = get_settings()
    configured = float(st.master_agent_max_execution_time_s)
    try:
        from app.services.run_service import _RUN_JOB_TIMEOUT_SECONDS
        ceiling = float(_RUN_JOB_TIMEOUT_SECONDS) - _JOB_TIMEOUT_MARGIN_S
    except Exception:  # noqa: BLE001 -- a direct caller outside the worker
        ceiling = configured
    # max(): a deployment that deliberately shortens the budget keeps its
    # choice; the clamp only ever pulls an over-long budget back inside.
    budget = min(configured, ceiling) if ceiling > 0 else configured
    return int(st.master_agent_max_iterations), budget


def render_budget(prompt: str) -> str:
    """Substitute the real budget into a prompt's __BUDGET_*__ placeholders.

    The prompts used to hardcode "15 iterations / ~120s", so raising the budget
    would have left the model still believing it had two minutes and rushing to
    a shallow answer.
    """
    iters, secs = resolve_master_budget()
    return prompt.replace("__BUDGET_ITERS__", str(iters)).replace(
        "__BUDGET_SECS__", str(int(secs))
    )


def build_master_messages(
    conversation_history: Optional[list[dict]],
    human_msg: str,
    max_turns: Optional[int] = None,
    max_chars_per_turn: Optional[int] = None,
) -> list:
    """The conversation the Master receives, ending with the current request.

    `run_decision_maker` took `conversation_history`, handed it to the worker
    tools, and then invoked its own graph with a single HumanMessage. The
    Master is the default `chat_engine` — every Command Center and Global
    Assistant message goes through it — so the platform's assistant had no
    memory of the conversation at all. Asked to build on what was established
    earlier, it answered from the current message alone, which reads exactly
    like an agent that cannot access its own chat history.

    The message-shape rules that keep this from failing on a long or an
    interrupted conversation live in `chat_messages`, shared with the
    generalist, which had its own copy of half of them.
    """
    from app.services.langchain.chat_messages import build_conversation_messages

    st = get_settings()
    return build_conversation_messages(
        conversation_history,
        human_msg,
        max_turns=int(
            max_turns if max_turns is not None else st.master_history_max_turns
        ),
        max_chars_per_turn=int(
            max_chars_per_turn if max_chars_per_turn is not None
            else st.master_history_max_chars_per_turn
        ),
        max_total_chars=int(st.master_history_max_total_chars),
    )


#: Appended when the model stopped because it ran out of output budget.
#: An answer that ends mid-sentence with no explanation is read as the whole
#: answer, and acted on as one.
_TRUNCATED_ANSWER_NOTICE = (
    "\n\n---\n\n_This answer stopped at my output limit and is incomplete. "
    "Ask me to continue and I will pick up from where it ends._"
)


def _hit_output_ceiling(message) -> bool:
    """Did the model stop because it ran out of room, rather than finishing?

    `create_react_agent` manages its own message loop, so `safe_ainvoke`'s
    auto-continuation — which handles exactly this for plain generation calls —
    never sees the Master's requests. Without this check a partial answer is
    returned as if it were whole.
    """
    meta = getattr(message, "response_metadata", None) or {}
    reason = str(meta.get("stop_reason") or meta.get("finish_reason") or "")
    # Anthropic says "max_tokens"; OpenAI says "length".
    return reason in ("max_tokens", "length")


#: Tool calls left when the Master is told, in the conversation itself, that
#: its budget is nearly spent. The `decision_budget` event goes to the UI; the
#: model never saw it, and a run that had spent twenty-two of twenty-five
#: calls reading documents kept reading into the wall.
BUDGET_NUDGE_REMAINING = 6


def budget_exhausted_headline(max_iterations: int) -> str:
    return (
        f"_The request used up its {max_iterations}-step budget before it "
        "finished._"
    )


def build_budget_nudge(step: int, max_iterations: int) -> Optional[str]:
    """The notice the Master reads when few tool calls remain, else None.

    `step` is the number of tool calls made so far (the streamer's count).
    """
    remaining = max_iterations - step
    if remaining > BUDGET_NUDGE_REMAINING:
        return None
    if remaining <= 1:
        return (
            f"[Platform budget notice] You have used {step} of {max_iterations} "
            "tool calls; no further tool calls are available. Stop calling tools "
            "and write your final answer now from what you already have. Say "
            "plainly which parts were not done."
        )
    return (
        f"[Platform budget notice] You have used {step} of {max_iterations} tool "
        f"calls; {remaining} remain. If the user asked for something a "
        "specialist produces -- a costing, an analysis, a checklist, a proposal, "
        "annexures -- and you have not yet called that call_<agent> tool, make "
        "that ONE call now; more reading will not get it done. Otherwise stop "
        "calling tools and write your final answer from what you have."
    )


def make_budget_hook(streamer: Any, max_iterations: int) -> Callable[[dict], dict]:
    """A `pre_model_hook` that appends the budget notice to the model's input.

    The notice rides on `llm_input_messages`, so the graph's own message
    history is untouched, and it is a HumanMessage because the Anthropic
    adapter rejects a SystemMessage that is not at the head of the list.
    """
    def hook(state: dict) -> dict:
        messages = list(state.get("messages") or [])
        nudge = build_budget_nudge(getattr(streamer, "_step", 0), max_iterations)
        if nudge:
            messages.append(HumanMessage(content=nudge))
        return {"llm_input_messages": messages}
    return hook


def render_partial_timeout_message(
    trace: list[dict],
    elapsed: float,
    *,
    headline: Optional[str] = None,
    unfinished: str = "still running when time ran out",
) -> str:
    """What to say when a run ends before it finishes.

    The old timeout branch returned a bare apology and dropped `messages`
    entirely, so a run that had already completed a tender analysis handed back
    nothing — and the retry redid that analysis from scratch. Report the steps
    that finished so the work is visible and the follow-up can build on it.

    The default wording is the timeout's. The failure path passes its own
    ``headline`` and ``unfinished`` label, because telling a user their run
    "ran out of time" when a provider returned an error sends them to fix the
    wrong thing.
    """
    done: list[str] = []
    observed = {
        e.get("step") for e in trace
        if e.get("type") == "observation" and not e.get("is_error")
    }
    for entry in trace:
        if entry.get("type") != "action":
            continue
        tool = entry.get("tool") or "unknown_tool"
        mark = "completed" if entry.get("step") in observed else unfinished
        done.append(f"- `{tool}` — {mark}")

    lines = [
        headline or f"_I ran out of time on this request after {int(elapsed)}s._",
        "",
    ]
    if done:
        lines += ["Here is what had finished by then:", "", *done, ""]
        lines.append(
            "Anything marked completed is saved — say **Continue** and I will pick "
            "up from the unfinished step rather than redo it."
        )
    else:
        lines.append(
            "Nothing had completed yet. Try a more focused request, or say "
            "**Continue** to retry."
        )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Timeline streaming callback — converts LangChain callback events into SSE-
# friendly payloads delivered via a caller-supplied stream_callback(event, data)
# ────────────────────────────────────────────────────────────────────────────


class _TimelineStreamer(AsyncCallbackHandler):
    """
    Emits decision_thought / decision_action / decision_observation / decision_budget
    events as the ReAct loop runs. Events flow through the caller's
    stream_callback(event_type: str, payload: dict) function.
    """

    def __init__(
        self,
        stream_callback: Optional[Callable[[str, dict], None]],
        started_at: float,
        max_iterations: Optional[int] = None,
        max_execution_time: Optional[float] = None,
    ):
        super().__init__()
        _iters, _secs = resolve_master_budget()
        self._cb = stream_callback
        self._started_at = started_at
        self._step = 0
        # Every step the run actually took. The streamer used to emit and
        # forget, so a run cancelled by the wall-clock timeout could report
        # nothing at all — not even the delegations that had succeeded.
        self.partial_trace: list[dict] = []
        self._tool_step_by_id: dict[str, int] = {}
        self._seen_actions: dict[tuple[str, str], int] = {}
        self._loop_stop = False
        self._max_iterations = max_iterations if max_iterations is not None else _iters
        self._max_execution_time = (
            max_execution_time if max_execution_time is not None else _secs
        )

    # ---- helpers --------------------------------------------------------

    def _emit(self, event: str, payload: dict) -> None:
        if not self._cb:
            return
        try:
            self._cb(event, payload)
        except Exception as e:  # never let streaming break the agent
            logger.debug(f"[decision_maker] stream_callback raised (non-fatal): {e}")

    def _emit_budget(self) -> None:
        elapsed = time.monotonic() - self._started_at
        self._emit("decision_budget", {
            "iteration": self._step,
            "max_iterations": self._max_iterations,
            "elapsed_s": round(elapsed, 2),
            "max_execution_time": self._max_execution_time,
        })

    @property
    def budget_exceeded(self) -> bool:
        return (time.monotonic() - self._started_at) > self._max_execution_time

    @property
    def loop_detected(self) -> bool:
        return self._loop_stop

    # ---- LLM token streaming -------------------------------------------

    async def on_llm_new_token(self, token: str, **kwargs) -> None:  # type: ignore[override]
        # Stream reasoning tokens as they flow (frontend groups them by step)
        if token:
            self._emit("decision_thought", {"step": self._step, "content": token})

    # ---- Tool call start/end -------------------------------------------

    async def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs,
    ) -> None:  # type: ignore[override]
        self._step += 1
        tool_name = (serialized or {}).get("name", "unknown_tool")
        self._tool_step_by_id[str(run_id)] = self._step

        # Loop guard — same (tool, args) tuple ≥ 3 times = force stop
        key = (tool_name, (input_str or "")[:200])
        self._seen_actions[key] = self._seen_actions.get(key, 0) + 1
        if self._seen_actions[key] >= 3:
            self._loop_stop = True

        action = {
            "type": "action",
            "step": self._step,
            "tool": tool_name,
            "input_preview": (input_str or "")[:500],
        }
        self.partial_trace.append(action)
        self._emit("decision_action", {
            k: v for k, v in action.items() if k != "type"
        })

        # decision_action carries the raw tool name and lands in the collapsed
        # technical detail, so on its own the Master Agent's work was invisible
        # while it ran — the user saw a spinner and nothing else. Emit the same
        # step as a plain-language status too, using the shared vocabulary the
        # specialist agents use, so both surfaces can show what is happening.
        try:
            import json as _json_local

            from app.services.langchain.activity_labels import describe_tool

            args = {}
            if input_str:
                try:
                    parsed = _json_local.loads(input_str)
                    if isinstance(parsed, dict):
                        args = parsed
                except Exception:
                    pass
            self._emit("agent_status", {
                "agent_key": "decision_maker",
                "phase": "tool_running",
                "step": self._step,
                "tool": tool_name,
                "message": describe_tool(tool_name, args),
                # The Command Center pill keys on run_id and clears on the
                # matching tool_done. Without both it would show the step and
                # never dismiss it.
                "run_id": str(run_id),
                "started_at_ms": int(time.time() * 1000),
            })
        except Exception:
            logger.debug("activity label emit failed", exc_info=True)

        self._emit_budget()

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs,
    ) -> None:  # type: ignore[override]
        step = self._tool_step_by_id.get(str(run_id), self._step)
        preview = ""
        try:
            preview = str(output)[:600]
        except Exception:
            preview = "<unserialisable>"
        self._emit("agent_status", {
            "agent_key": "decision_maker", "phase": "tool_done",
            "step": step, "run_id": str(run_id),
        })
        observation = {"type": "observation", "step": step, "result_preview": preview}
        self.partial_trace.append(observation)
        self._emit("decision_observation", {
            "step": step,
            "result_preview": preview,
        })

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs,
    ) -> None:  # type: ignore[override]
        step = self._tool_step_by_id.get(str(run_id), self._step)
        self._emit("agent_status", {
            "agent_key": "decision_maker", "phase": "tool_done",
            "step": step, "run_id": str(run_id),
        })
        self.partial_trace.append({
            "type": "observation",
            "step": step,
            "result_preview": f"ERROR: {str(error)[:500]}",
            "is_error": True,
        })
        self._emit("decision_observation", {
            "step": step,
            "result_preview": f"ERROR: {str(error)[:500]}",
            "is_error": True,
        })


# ────────────────────────────────────────────────────────────────────────────
# Intermediate-step serialization (for persistence in ProposalMessage.metadata)
# ────────────────────────────────────────────────────────────────────────────


def build_master_catalog(
    db: Session,
    *,
    user_id: Optional[int],
    user_role: Optional[str],
    context: dict,
    reachable_only: bool = False,
    session_id: Optional[str] = None,
    proposal_session_id: Optional[int] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    stream_callback: Optional[Callable] = None,
) -> list:
    """The Master Agent's two-tier catalog, from the capability registry.

    Replaces the five inline builders whose divergence from every other
    catalog left the Master with three content tools and no way to browse.

    Tier 1 — bound directly: the deduplicated worker `call_*` roster plus the
    cross-cutting reads every job starts from (`MASTER_ALWAYS_BOUND`). Tier 2 —
    everything else on the master surface, reached through one validating
    `use_capability` dispatcher. Reach is total; bound schema is not, because
    large tool sets measurably degrade tool selection.

    ``reachable_only=True`` returns the flat key list (Tier 1 ∪ Tier 2) for
    the parity test that asserts, by enumeration, that no capability was lost
    in the move. ``propose_plan`` / ``ask_user`` stay with ``run_decision_maker`` and
    ``read_worker_output`` with ``build_agent_wrapper_tools``: each needs a
    container this run owns wired in.
    """
    from app.services.langchain.capability_registry import keys_for_surface
    from app.services.langchain.graphs.capability_dispatcher import (
        MASTER_ALWAYS_BOUND,
        build_capability_dispatcher,
    )
    from app.services.langchain.graphs.orchestrator_tools import (
        WorkerOutputStore,
        build_agent_wrapper_tools,
        build_worker_output_reader,
    )
    from app.services.langchain.db_recycle import recycle_session_between_calls
    from app.services.langchain.tools.tool_loader import load_tools_by_keys

    surface_keys = keys_for_surface("master", user_role)

    if reachable_only:
        return list(surface_keys)

    bound_read_keys = [
        k for k in surface_keys
        if k in MASTER_ALWAYS_BOUND
        and k not in ("propose_plan", "ask_user", "read_worker_output")
    ]

    # One store per run, shared by every worker call and the reader that pages
    # through what did not fit. A reader over a different store would report
    # every handle the Master was just given as unknown.
    output_store = WorkerOutputStore()

    tools: list = []
    tools += build_agent_wrapper_tools(
        session_id=session_id,
        proposal_session_id=proposal_session_id,
        user_id=user_id,
        conversation_history=conversation_history,
        file_metadata=file_metadata,
        stream_callback=stream_callback,
        db=db,
        output_store=output_store,
    )
    tools.append(build_worker_output_reader(output_store))
    tools += load_tools_by_keys(
        db, bound_read_keys,
        agent_key="decision_maker",
        proposal_session_id=proposal_session_id,
        router_session_id=session_id,
    )
    tools.append(build_capability_dispatcher(
        db,
        user_id=user_id,
        user_role=user_role,
        surface="master",
        context={
            **(context or {}),
            "session_id": session_id,
            "proposal_session_id": proposal_session_id,
        },
        stream_callback=stream_callback,
    ))
    # Release the run's read transaction after every call. `load_tools_by_keys`
    # already recycles what it builds; the worker roster and the dispatcher are
    # assembled here and would otherwise hold a connection open across a model
    # call. The production failure was `document_reader` in THIS catalog, so
    # covering only the loader would have left the actual bug in place.
    return recycle_session_between_calls(tools, db)


def build_master_prompt_preamble(
    user_role: Optional[str],
    workers: Optional[list[dict]] = None,
) -> str:
    """The Platform Capability Manual plus the delegation doctrine.

    Prepended to the Master's system prompt in every mode. This is the
    instruction set the user asked for: the Master reads what each platform
    function is for instead of inferring it from a tool name — and the
    doctrine, not a pre-dispatch, is what keeps a costing out of its own
    hands.
    """
    from app.services.langchain.capability_registry import (
        DELEGATION_DOCTRINE,
        render_capability_manual,
    )

    manual = render_capability_manual(
        "master", user_role, include_workers=workers,
    )
    return f"{manual}\n\n{DELEGATION_DOCTRINE}\n\n"


def _message_text(content) -> str:
    """The human-readable text of a message, whatever shape the provider used.

    With extended thinking on, Anthropic returns content as a LIST of typed
    blocks — thinking (with its signature), then text. str() on that list
    serialised the whole structure, so the user's "final answer" opened with a
    base64 thinking signature. Only text blocks are the answer.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "".join(parts)
    return str(content or "")


def _serialize_messages_as_trace(messages: list[Any]) -> list[dict]:
    """Convert a LangGraph message list into a compact step-by-step trace."""
    trace: list[dict] = []
    step = 0
    for msg in messages:
        if isinstance(msg, AIMessage):
            content = _message_text(msg.content)
            tool_calls = getattr(msg, "tool_calls", None) or []
            if content.strip():
                step += 1
                trace.append({
                    "step": step, "type": "thought",
                    "content": content[:2000],
                })
            for tc in tool_calls:
                step += 1
                trace.append({
                    "step": step, "type": "action",
                    "tool": tc.get("name", "unknown"),
                    "args": tc.get("args", {}),
                })
        elif isinstance(msg, ToolMessage):
            step += 1
            out = msg.content or ""
            if not isinstance(out, str):
                out = str(out)
            trace.append({
                "step": step, "type": "observation",
                "tool_call_id": msg.tool_call_id,
                "result_preview": out[:1500],
            })
    return trace


# ────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────


def _worker_roster_section(db: Session) -> str:
    """Render the master agent's callable worker roster for its prompt.

    Reads the same registry the `call_*` tools are built from, so the prompt
    and the actual tool list cannot disagree. They used to: planning mode ran
    its own query with a narrower filter, so a plan could name an agent that
    was not actually callable.
    """
    try:
        from app.services.langchain.graphs.orchestrator_tools import list_worker_agents

        workers = list_worker_agents(db)
    except Exception as e:
        logger.warning("Could not render the worker roster: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return ""
    if not workers:
        return (
            "\n\n## YOUR WORKER AGENTS\n"
            "No worker agents are currently enabled. Tell the user an "
            "administrator needs to enable agents in Admin -> Agent Builder, "
            "and do the work yourself where you can.\n"
        )
    import re as _re

    lines = [
        "",
        "## YOUR WORKER AGENTS",
        "These are the ONLY agents you may call. They come from the platform's",
        "agent registry — an administrator controls this list.",
        "",
    ]
    for w in workers:
        safe = _re.sub(r"[^a-zA-Z0-9_]", "_", w["agent_key"])[:58]
        desc = " ".join((w["description"] or "").split())[:220]
        lines.append(f"- `call_{safe}` — {w['display_name']}: {desc}")
    lines.append("")
    return "\n".join(lines)


async def run_decision_maker(
    db: Session,
    message: str,
    session_id: Optional[str] = None,
    tender_id: Optional[int] = None,
    user_id: Optional[int] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    proposal_session_id: Optional[int] = None,
    stream_callback: Optional[Callable[[str, dict], None]] = None,
    max_iterations: Optional[int] = None,
    max_execution_time: Optional[float] = None,
    mode: str = "planning",
    approved_plan: Optional[dict] = None,
    change_feedback: Optional[str] = None,
    approved_action: Optional[dict] = None,
) -> dict:
    """
    Run the Decision Maker on the user's message and return a dict matching
    the specialized-agent output shape expected by the router graph:

        {
            "output": str,                 # user-facing markdown summary
            "output_type": "decision_maker_trace",
            "agent_key": "decision_maker",
            "tool_calls": list[dict],      # serialized tool call history
            "trace": list[dict],           # step-by-step thought/action/observation
            "metrics": dict,
            "status": "completed" | "failed" | "partial",
        }

    Events fired (via stream_callback, if provided):
        decision_thought     {step, content}
        decision_action      {step, tool, input_preview}
        decision_observation {step, result_preview, is_error?}
        decision_budget      {iteration, max_iterations, elapsed_s, ...}
        decision_final       {content}
    """
    started_at = time.monotonic()

    # Lazy imports so the module loads fast
    from langgraph.prebuilt import create_react_agent
    from app.services.langchain.llm_factory import get_chat_model
    from app.services.langchain.callback_handler import DRPLCallbackHandler
    # Only the two capture-bound builders and the captures themselves: the
    # five family builders this used to import were replaced by
    # `build_master_catalog` and have been dead imports since.
    from app.services.langchain.graphs.orchestrator_tools import (
        build_plan_proposal_tool,
        build_ask_user_tool,
        PlanCapture,
        AskUserCapture,
    )
    from app.services.langchain.tool_policy import (
        ApprovalGrant,
        PendingActionCapture,
        policy_scope,
        wrap_tools_with_policy,
    )
    from app.core.config import get_settings

    # The caller's role bounds what platform tools they get. Resolved from the
    # database, never from anything the client sent — a role passed in from the
    # request body would be a trivial privilege escalation.
    _user_role: Optional[str] = None
    if user_id:
        try:
            from app.models.user import User as _User

            _user_role = (
                db.query(_User.role).filter(_User.id == user_id).scalar()
            )
        except Exception:
            logger.warning("Could not resolve role for user %s; platform tools withheld", user_id)
            _user_role = None

    # A confirmation the user just granted. It authorises exactly one call —
    # same tool, same arguments — and is consumed on use, so the agent cannot
    # turn one "yes" into repeated writes.
    approval_grant = (
        ApprovalGrant(approved_action.get("tool"), approved_action.get("args") or {})
        if approved_action and approved_action.get("tool")
        else None
    )

    is_planning = mode == "planning"
    is_autonomous = mode == "autonomous"
    plan_capture: PlanCapture = PlanCapture()
    ask_capture: AskUserCapture = AskUserCapture()
    # Share the run's ambient capture when the caller opened a policy_scope, so
    # a write gated inside a sub-agent's own tools and one gated in this
    # agent's catalog land in the same place. Falls back to a private capture
    # when run_decision_maker is called outside a scope.
    from app.services.langchain.tool_policy import current_capture as _current_capture
    pending_action_capture: PendingActionCapture = (
        _current_capture() or PendingActionCapture()
    )


    # NOTE: execution mode (approved_plan) is no longer handled here — the
    # streaming handler drives single plan steps via `_run_single_plan_step`
    # directly, persisting state in `pipeline_state["plan_execution"]` between
    # user clicks on "Run next step". See `stream_decision_maker_directly`
    # and the `/decision/respond` endpoint.

    # Assemble tools. Planning mode gets ONLY the propose_plan tool so the LLM
    # cannot take real actions before the user approves. Execution mode gets the
    # full 4-family catalog minus the planner.
    if is_planning:
        tools = [build_plan_proposal_tool(capture=plan_capture, stream_callback=stream_callback)]
    else:
        # The two-tier catalog from the capability registry: the deduplicated
        # worker roster + the cross-cutting reads bound directly, everything
        # else on the master surface through the use_capability dispatcher.
        # This replaces the five inline builders whose divergence from every
        # other catalog left the Master with three content tools and no way
        # to browse — see build_master_catalog for the shape.
        tools = build_master_catalog(
            db,
            user_id=user_id,
            user_role=_user_role,
            context={},
            session_id=session_id,
            proposal_session_id=proposal_session_id,
            conversation_history=conversation_history,
            file_metadata=file_metadata,
            stream_callback=stream_callback,
        )
        # Autonomous mode gets the escape-hatch tool so it can pause and ask the
        # user when (and only when) it is genuinely blocked.
        if is_autonomous:
            tools.append(
                build_ask_user_tool(capture=ask_capture, stream_callback=stream_callback)
            )

        # Write-confirmation gate. Reads pass through untouched; anything that
        # changes the user's data suspends and waits for an explicit yes. An
        # approved action passes through once and re-arms the gate behind it.
        tools = wrap_tools_with_policy(
            tools,
            capture=pending_action_capture,
            stream_callback=stream_callback,
            enabled=get_settings().assistant_confirm_gate_enabled,
            grant=approval_grant,
        )

    # Context block — tender / file context the user implicitly shared
    context_lines: list[str] = []
    if tender_id:
        context_lines.append(f"Active tender_id: {tender_id}")
    if proposal_session_id:
        context_lines.append(f"Active session_id (proposal_session): {proposal_session_id}")
    # Surface attached-file information in whatever shape file_metadata has —
    # resolve_file_attachments returns {attachment_paths, files, has_files};
    # older callers may pass {file_ids}. Without this block the LLM has zero
    # signal that the user attached a PDF and keeps asking them to re-share.
    if file_metadata:
        try:
            files_list = (file_metadata or {}).get("files") or []
            attachment_paths = (file_metadata or {}).get("attachment_paths") or []
            file_ids = list((file_metadata or {}).get("file_ids") or [])
            file_names: list[str] = []
            for f in files_list:
                n = f.get("name") if isinstance(f, dict) else None
                if n:
                    file_names.append(str(n))
            if not file_names and attachment_paths:
                import os as _os
                for ap in attachment_paths:
                    path = ap.get("path") if isinstance(ap, dict) else ap
                    if path:
                        file_names.append(_os.path.basename(str(path)))
            if file_names:
                context_lines.append(
                    f"User attached {len(file_names)} file(s): {', '.join(file_names[:10])}"
                )
                context_lines.append(
                    "IMPORTANT: these files are already uploaded and accessible "
                    "to the execution agents (deep_analyzer, annexure_finder, "
                    "etc.). Do NOT ask the user to share or re-upload the "
                    "document — propose a plan that analyses the attached file(s)."
                )
            if file_ids:
                context_lines.append(f"User-attached file_ids: {file_ids[:10]}")
        except Exception:
            pass
    context_block = ("\n".join(context_lines) + "\n\n") if context_lines else ""

    if is_planning and change_feedback:
        # Re-planning after the user requested a change — surface their feedback
        # ahead of the original request so the LLM incorporates it.
        human_msg = (
            f"{context_block}User request: {message}\n\n"
            f"## User feedback on your previous plan\n{change_feedback}\n\n"
            f"Revise the plan to address this feedback, then call `propose_plan` again."
        )
    else:
        human_msg = f"{context_block}User request: {message}"

    # Select the system prompt based on mode. Execution mode injects the approved
    # plan so the agent can track which steps remain.
    if is_planning:
        # Append any user-defined custom agents so the planning LLM knows
        # it can name them under a step's `agent` field. Without this, plans
        # can only reference the 6 hardcoded built-in agents.
        custom_agent_section = _worker_roster_section(db)
        system_prompt = render_budget(
            build_master_prompt_preamble(_user_role)
            + DECISION_MAKER_PLANNING_SYSTEM + custom_agent_section
        )
    elif is_autonomous:
        system_prompt = render_budget(
            build_master_prompt_preamble(_user_role)
            + DECISION_MAKER_AUTONOMOUS_SYSTEM + _worker_roster_section(db)
        )
    else:
        plan_block = "_No approved plan attached._"
        if approved_plan:
            steps_lines = []
            for i, step in enumerate(approved_plan.get("steps") or [], start=1):
                agent = step.get("agent") or "direct"
                desc = step.get("description") or ""
                steps_lines.append(f"{i}. [{agent}] {desc}")
            plan_block = (
                f"**{approved_plan.get('title') or 'Approved plan'}**\n\n"
                f"{approved_plan.get('reasoning') or ''}\n\n"
                + "\n".join(steps_lines)
            )
        system_prompt = render_budget(
            build_master_prompt_preamble(_user_role)
            + DECISION_MAKER_EXECUTION_SYSTEM.format(approved_plan_block=plan_block)
        )

    # The conversation so far. Not part of `context_block`: prior turns belong
    # in the message list, where the model reads them as things that were said,
    # rather than pasted into one user message as text about the conversation.
    conversation_messages = build_master_messages(conversation_history, human_msg)

    # Both budgets come from config unless a caller pinned them explicitly.
    _default_iters, _default_secs = resolve_master_budget()
    if max_iterations is None:
        max_iterations = _default_iters
    if max_execution_time is None:
        max_execution_time = _default_secs

    streamer = _TimelineStreamer(
        stream_callback=stream_callback,
        started_at=started_at,
        max_iterations=max_iterations,
        max_execution_time=max_execution_time,
    )
    metrics_cb = DRPLCallbackHandler(db, agent_name="decision_maker")

    try:
        llm = get_chat_model(
            db,
            agent_name="decision_maker",
            temperature=0.2,
            max_tokens=get_settings().master_agent_max_output_tokens,
            # The Master Agent plans across every worker and decides what the
            # platform does; it runs on the flagship tier even when the workers
            # are on the cheaper one. An admin model set in Agent Builder still
            # takes precedence over this.
            default_model=get_settings().master_agent_model,
        )
        agent = create_react_agent(
            model=llm,
            tools=tools,
            prompt=SystemMessage(content=system_prompt),
            pre_model_hook=make_budget_hook(streamer, max_iterations),
        )

        # LangGraph create_react_agent respects recursion_limit = 2 * max_iterations
        # (one iteration = AI message + tool message pair).
        config = {
            "callbacks": [streamer, metrics_cb],
            "recursion_limit": max_iterations * 2 + 2,
        }

        # Kick it off with a wall-clock timeout as a hard safety net. The run
        # executes inside its own policy_scope: Tier 2 tools are wrapped
        # lazily inside the use_capability dispatcher and bind to the AMBIENT
        # capture/grant at run time — without this scope, a direct caller of
        # run_decision_maker (tests, scripts) would get a gate that fails
        # closed without recording anything to confirm. The streaming
        # handler's outer scope shares the same capture, so nesting is a
        # no-op there.
        try:
            with policy_scope(
                capture=pending_action_capture,
                grant=approval_grant,
                stream_callback=stream_callback,
                user_id=user_id,
            ):
                result = await asyncio.wait_for(
                    agent.ainvoke(
                        {"messages": conversation_messages},
                        config=config,
                    ),
                    timeout=max_execution_time,
                )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started_at
            logger.warning(f"[decision_maker] timed out after {elapsed:.1f}s")
            partial_msg = render_partial_timeout_message(
                streamer.partial_trace, elapsed
            )
            if stream_callback:
                try:
                    stream_callback("decision_final", {"content": partial_msg})
                except Exception:
                    pass
            return {
                "output": partial_msg,
                "output_type": "decision_maker_trace",
                "agent_key": "decision_maker",
                "tool_calls": [
                    {"tool": e.get("tool"), "input": e.get("input_preview")}
                    for e in streamer.partial_trace
                    if e.get("type") == "action"
                ],
                "trace": list(streamer.partial_trace),
                "metrics": {"elapsed_s": round(elapsed, 2), "timed_out": True},
                "status": "partial",
            }

        messages = result.get("messages", []) if isinstance(result, dict) else []

        # Planning-mode recovery: some models write the propose_plan invocation
        # as XML/JSON text instead of issuing a real tool call. Detect that and
        # convert it into a captured plan so the user still sees the approval
        # card instead of raw `<propose_plan>` markup in chat.
        if is_planning and not plan_capture.get("plan"):
            for m in reversed(messages):
                if not isinstance(m, AIMessage):
                    continue
                c = _message_text(m.content)
                recovered = _try_parse_leaked_plan(c)
                if recovered:
                    logger.info(
                        "[decision_maker] recovered leaked propose_plan from text "
                        "(LLM wrote XML instead of calling the tool)"
                    )
                    plan_capture["plan"] = recovered
                    if stream_callback:
                        try:
                            stream_callback("plan_proposed", recovered)
                        except Exception:
                            pass
                    break

        # Planning-mode short-circuit: if the LLM successfully called propose_plan
        # (or we recovered it from a text leak above), return pending_plan status
        # so the Command Center can render the approval UI.
        if is_planning and plan_capture.get("plan"):
            elapsed = time.monotonic() - started_at
            plan = plan_capture["plan"]
            return {
                "output": _render_plan_as_markdown(plan),
                "output_type": "decision_plan_proposed",
                "agent_key": "decision_maker",
                "tool_calls": [{"tool": "propose_plan", "input": plan}],
                "trace": _serialize_messages_as_trace(messages),
                "metrics": {"elapsed_s": round(elapsed, 2)},
                "status": "pending_plan",
                "pending_plan": plan,
            }

        # Write-confirmation short-circuit: the agent tried to change the user's
        # data. The call was NOT executed — it is parked here so the caller can
        # render a confirmation card and replay it on approval. Checked before
        # ask_user because a gated tool ends the run more decisively than a
        # question does.
        if pending_action_capture.is_pending():
            elapsed = time.monotonic() - started_at
            summary = pending_action_capture["summary"]
            if stream_callback:
                try:
                    stream_callback("decision_final", {"content": summary})
                except Exception:
                    pass
            return {
                "output": summary,
                "output_type": "decision_maker_trace",
                "agent_key": "decision_maker",
                "tool_calls": [
                    {
                        "tool": pending_action_capture["tool"],
                        "input": pending_action_capture["args"],
                    }
                ],
                "trace": _serialize_messages_as_trace(messages),
                "metrics": {"elapsed_s": round(elapsed, 2)},
                "status": "needs_confirmation",
                "pending_action": dict(pending_action_capture),
            }

        # Autonomous-mode short-circuit: the agent hit a genuine blocker and
        # called ask_user. Return needs_input with its question so the Command
        # Center surfaces it as a normal assistant message (no approval card).
        if is_autonomous and ask_capture.get("question"):
            elapsed = time.monotonic() - started_at
            question = ask_capture["question"]
            if stream_callback:
                try:
                    stream_callback("decision_final", {"content": question})
                except Exception:
                    pass
            return {
                "output": question,
                "output_type": "decision_maker_trace",
                "agent_key": "decision_maker",
                "tool_calls": [{"tool": "ask_user", "input": {"question": question}}],
                "trace": _serialize_messages_as_trace(messages),
                "metrics": {"elapsed_s": round(elapsed, 2)},
                "status": "needs_input",
            }

        # Final answer = last AIMessage with non-empty TEXT. Block lists are
        # flattened to their text blocks — see _message_text.
        final_content = ""
        final_message = None
        for m in reversed(messages):
            if isinstance(m, AIMessage):
                c = _message_text(m.content)
                if c.strip():
                    final_content = c
                    final_message = m
                    break

        if _hit_output_ceiling(final_message):
            logger.warning(
                "[decision_maker] final answer hit the output ceiling (%s tokens)",
                get_settings().master_agent_max_output_tokens,
            )
            final_content = final_content.rstrip() + _TRUNCATED_ANSWER_NOTICE

        trace = _serialize_messages_as_trace(messages)

        # Flatten tool_calls for metadata persistence / admin replay
        tool_calls: list[dict] = []
        for m in messages:
            if isinstance(m, AIMessage):
                for tc in (getattr(m, "tool_calls", None) or []):
                    tool_calls.append({
                        "tool": tc.get("name", "unknown"),
                        "input": tc.get("args", {}),
                    })

        if stream_callback:
            try:
                stream_callback("decision_final", {"content": final_content})
            except Exception:
                pass

        elapsed = time.monotonic() - started_at
        status = "completed"
        if streamer.loop_detected:
            status = "partial"
            final_content = (
                final_content
                or "_The decision maker stopped because it detected a tool-call loop._"
            )

        return {
            "output": final_content or "_Decision maker produced no output._",
            "output_type": "decision_maker_trace",
            "agent_key": "decision_maker",
            "tool_calls": tool_calls,
            "trace": trace,
            "metrics": {
                **(metrics_cb.get_summary() if hasattr(metrics_cb, "get_summary") else {}),
                "elapsed_s": round(elapsed, 2),
                "iterations": len([t for t in trace if t.get("type") == "action"]),
                "loop_detected": streamer.loop_detected,
            },
            "status": status,
        }

    except GraphRecursionError:
        # The step budget ran out. This is the timeout's twin -- work was done
        # and is saved -- and it used to fall through to the generic handler
        # below, which does not know the exception and showed "Something went
        # wrong while processing your request" over a list of completed steps.
        elapsed = time.monotonic() - started_at
        partial = list(getattr(streamer, "partial_trace", []) or [])
        n_actions = len([ev for ev in partial if ev.get("type") == "action"])
        logger.warning(
            f"[decision_maker] step budget ({max_iterations}) spent after "
            f"{elapsed:.1f}s and {n_actions} tool call(s) without a final answer"
        )
        partial_msg = render_partial_timeout_message(
            partial,
            elapsed,
            headline=budget_exhausted_headline(max_iterations),
            unfinished="in progress when the budget ran out",
        )
        if stream_callback:
            try:
                stream_callback("decision_final", {"content": partial_msg})
            except Exception:
                pass
        return {
            "output": partial_msg,
            "output_type": "decision_maker_trace",
            "agent_key": "decision_maker",
            "tool_calls": [
                {"tool": ev.get("tool"), "input": ev.get("input_preview")}
                for ev in partial
                if ev.get("type") == "action"
            ],
            "trace": partial,
            "metrics": {
                "elapsed_s": round(elapsed, 2),
                "iterations": n_actions,
                "budget_exhausted": True,
            },
            "status": "partial",
        }

    except Exception as e:
        logger.error(f"[decision_maker] run failed: {e}", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        from app.services.langchain.error_utils import format_user_error
        err_msg = format_user_error(e)

        # Name what completed before the failure, the way the timeout path
        # already does. A run that analysed a tender and then died costing it
        # has produced something the user can use; returning a bare apology and
        # an empty trace throws that away and invites them to run the whole
        # thing again. `partial_trace` is the streamer's own record, so this
        # costs nothing and is always available.
        partial = list(getattr(streamer, "partial_trace", []) or [])
        if partial:
            err_msg = (
                f"{err_msg}\n\n"
                + render_partial_timeout_message(
                    partial,
                    time.monotonic() - started_at,
                    headline="_The request stopped partway through._",
                    unfinished="in progress when it stopped",
                )
            )
            if stream_callback:
                try:
                    stream_callback("decision_final", {"content": err_msg})
                except Exception:
                    pass
        elif stream_callback:
            try:
                stream_callback("decision_final", {"content": err_msg})
            except Exception:
                pass
        return {
            "output": err_msg,
            "output_type": "decision_maker_trace",
            "agent_key": "decision_maker",
            "tool_calls": [
                {"tool": ev.get("tool"), "input": ev.get("input_preview")}
                for ev in partial
                if ev.get("type") == "action"
            ],
            "trace": partial,
            "metrics": {"elapsed_s": round(time.monotonic() - started_at, 2)},
            "status": "failed",
            "error": str(e),
        }
