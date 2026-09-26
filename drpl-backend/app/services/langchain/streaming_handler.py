"""
DRPL Backend - SSE Streaming Handler
Provides Server-Sent Events (SSE) streaming for the agent router chat.
Wraps the router execution and emits events for real-time frontend updates.
"""

import asyncio
import json
import re
import logging
import time
import uuid
from typing import Optional, AsyncGenerator

from sqlalchemy.orm import Session

from app.services.langchain.error_utils import is_transient_error, format_user_error
from app.services.settings_service import get_effective_setting

logger = logging.getLogger(__name__)


def _fresh_db() -> Session:
    """Create a fresh DB session from the pool (pool_pre_ping validates it)."""
    from app.core.database import SessionLocal
    return SessionLocal()


async def _drive_plan_step(
    db: Session,
    session_id: str,
    proposal_session_id: int,
    user_id: Optional[int],
    tender_id: Optional[int],
    file_metadata: Optional[dict],
    approved_plan: Optional[dict],
    start_time: float,
) -> AsyncGenerator[str, None]:
    """Execute ALL remaining plan steps in a single streaming response.

    State is persisted in ``proposal_sessions.pipeline_state["plan_execution"]``.
    On first call (approve) a fresh state block is initialised from the user's
    pending_plan + stored file_metadata. All steps run to completion without
    pausing — no user intervention needed between steps.
    """
    import json as _json
    from app.models.proposal import ProposalSession, ProposalMessage
    from app.services.langchain.graphs.decision_maker_agent import (
        _run_single_plan_step, _build_plan_aggregate_markdown,
    )
    from app.services.langchain.error_utils import format_user_error

    # ── Load or initialise plan_execution state ──────────────────────────
    ps = db.query(ProposalSession).filter(ProposalSession.id == proposal_session_id).first()
    if not ps:
        yield _sse_event("error", {"message": "Session not found"})
        return
    state = dict(ps.pipeline_state or {})
    plan_exec = state.get("plan_execution")

    if not plan_exec:
        # First step after approval — need an approved_plan
        if not approved_plan:
            yield _sse_event("error", {"message": "No approved plan available for this step"})
            return
        plan_exec = {
            "plan": approved_plan,
            "file_metadata": file_metadata or {},
            "tender_id": tender_id,
            "original_message": "",
            "cursor": 0,
            "completed_steps": [],
            "status": "running",
        }

    # Prefer the file_metadata stored with the plan (original attachments)
    # over whatever this particular request brought along — only the first
    # approval call carries real file_metadata; subsequent "next" clicks
    # pass `{}`.
    effective_file_metadata = plan_exec.get("file_metadata") or file_metadata or {}
    effective_tender_id = plan_exec.get("tender_id") or tender_id
    cursor = plan_exec.get("cursor", 0)
    completed_steps = list(plan_exec.get("completed_steps") or [])
    plan = plan_exec.get("plan") or {}

    # ── Register uploaded PDFs as TenderDocuments (once, at cursor=0) ──────
    # Plan steps that need tender_id (especially CUSTOM agents from Agent
    # Builder, which use their own tools to look up the tender) will report
    # "NO TENDER DOCUMENT DETECTED" unless the uploaded PDFs are stored as
    # TenderDocument rows the tools can read.
    # We do this on EVERY cursor==0 call regardless of whether the session
    # already has a tender — if the session has an existing tender, we append
    # any not-yet-registered uploaded PDFs to it instead of creating a new one.
    if cursor == 0:
        attachment_paths = [
            ap.get("path") if isinstance(ap, dict) else ap
            for ap in (effective_file_metadata.get("attachment_paths") or [])
        ]
        attachment_paths = [p for p in attachment_paths if p]
        if attachment_paths:
            try:
                from app.models.tender import Tender, TenderDocument
                import os as _os
                import uuid as _uuid

                if effective_tender_id:
                    # Session already linked to a tender — register any uploaded
                    # PDFs that aren't yet in TenderDocument for this tender.
                    existing_paths = {
                        row.file_path
                        for row in db.query(TenderDocument.file_path).filter(
                            TenderDocument.tender_id == effective_tender_id
                        ).all()
                    }
                    new_docs = [k for k in attachment_paths if k not in existing_paths]
                    if new_docs:
                        for key in new_docs:
                            file_name = _os.path.basename(key) or "uploaded.pdf"
                            db.add(TenderDocument(
                                tender_id=effective_tender_id,
                                file_name=file_name,
                                file_path=key,
                                mime_type="application/pdf",
                                # Tracked on the ChatAttachment; "pending" here
                                # stalled analysis for a minute (see below).
                                extraction_status="completed",
                            ))
                        db.flush()
                        db.commit()
                        logger.info(
                            f"[_drive_plan_step] registered {len(new_docs)} PDF(s) as "
                            f"TenderDocument for existing tender {effective_tender_id}"
                        )
                else:
                    # No tender linked — create a new one and link it to the session.
                    new_tender = Tender(
                        portal="command_center",
                        tender_id=f"cc-{str(_uuid.uuid4())[:8]}",
                        title=ps.title or f"Command Center Workspace #{ps.id}",
                        status="open",
                        workflow_status="proposal_draft",
                        workspace_enabled=True,
                    )
                    db.add(new_tender)
                    db.flush()
                    for key in attachment_paths:
                        file_name = _os.path.basename(key) or "uploaded.pdf"
                        db.add(TenderDocument(
                            tender_id=new_tender.id,
                            file_name=file_name,
                            file_path=key,
                            mime_type="application/pdf",
                            extraction_status="completed",
                        ))
                    ps.tender_id = new_tender.id
                    ps.mode = "tender_linked"
                    ps.agent_type = "tender_proposal"
                    db.flush()
                    db.commit()
                    effective_tender_id = new_tender.id
                    logger.info(
                        f"[_drive_plan_step] auto-created tender {effective_tender_id} "
                        f"for standalone session {proposal_session_id} "
                        f"({len(attachment_paths)} PDF(s) linked)"
                    )
                plan_exec["tender_id"] = effective_tender_id
            except Exception as e:
                logger.exception(f"[_drive_plan_step] auto-create/register tender failed: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

    # Queue + callback for converting step events → SSE
    event_queue: asyncio.Queue = asyncio.Queue()
    total_steps = len(plan.get("steps") or [])

    def _make_cb() -> callable:
        q: asyncio.Queue = asyncio.Queue()

        def _cb(event_type: str, payload: dict) -> None:
            try:
                q.put_nowait((event_type, payload))
            except Exception:
                pass

        return _cb, q

    async def _drain_until(task: asyncio.Task, q: asyncio.Queue):
        while True:
            if task.done() and q.empty():
                break
            try:
                ev_type, ev_payload = await asyncio.wait_for(q.get(), timeout=0.25)
                yield _sse_event(ev_type, ev_payload)
            except asyncio.TimeoutError:
                continue

    # ── Loop through ALL remaining steps automatically ────────────────────
    while cursor < total_steps:
        _cb, _q = _make_cb()
        step_task = asyncio.create_task(_run_single_plan_step(
            db=db,
            plan=plan,
            cursor=cursor,
            completed_steps=completed_steps,
            original_message=plan_exec.get("original_message") or "",
            session_id=session_id,
            tender_id=effective_tender_id,
            user_id=user_id,
            conversation_history=[],
            file_metadata=effective_file_metadata,
            proposal_session_id=proposal_session_id,
            stream_callback=_cb,
        ))
        async for ev in _drain_until(step_task, _q):
            yield ev
        try:
            result = await step_task
        except Exception as e:
            logger.exception("[decision_maker] plan step task failed")
            yield _sse_event("error", {"message": format_user_error(e)})
            return

        step_result = result.get("step_result")
        next_cursor = result.get("next_cursor", cursor + 1)
        is_final = bool(result.get("is_final_step"))

        if step_result:
            completed_steps.append(step_result)
        cursor = next_cursor

        # Persist after each step so a crash only loses the current step
        plan_exec["cursor"] = cursor
        plan_exec["completed_steps"] = completed_steps
        plan_exec["status"] = "running"
        state["plan_execution"] = plan_exec
        ps.pipeline_state = state
        db.add(ps)
        try:
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        if is_final:
            break

    # ── Final step — clear state, stream aggregate markdown, save message ──
    state.pop("plan_execution", None)
    ps.pipeline_state = state
    db.add(ps)
    db.commit()

    final_md = _build_plan_aggregate_markdown(plan, completed_steps)
    yield _sse_event("plan_execution_complete", {
        "total_steps": total_steps,
        "completed_steps": sum(1 for s in completed_steps if s.get("status") == "completed"),
        "failed_steps": sum(1 for s in completed_steps if s.get("status") == "failed"),
        "skipped_steps": sum(1 for s in completed_steps if s.get("status") == "skipped"),
    })

    # Stream the aggregate in chunks
    chunk_size = 200
    for i in range(0, len(final_md), chunk_size):
        yield _sse_event("token", {"content": final_md[i:i + chunk_size]})
        await asyncio.sleep(0.005)

    yield _sse_event("agent_complete", {
        "agent_key": "decision_maker",
        "output_type": "plan_execution_result",
        "status": "completed",
    })

    # Save final assistant message
    try:
        assistant_msg = ProposalMessage(
            session_id=proposal_session_id,
            role="assistant",
            content=final_md,
            message_type="text",
            metadata_json=attach_run_cost(db, {
                "agents_used": ["decision_maker"],
                "output_type": "plan_execution_result",
                "plan_steps": [
                    {k: v for k, v in s.items() if k != "output"}
                    for s in completed_steps
                ],
            }),
        )
        db.add(assistant_msg)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    yield _sse_event("done", {
        "session_id": session_id,
        "output_type": "plan_execution_result",
        "latency_ms": int((time.time() - start_time) * 1000),
        # What this run cost. Sent live so the number appears with the answer;
        # it is also on the saved message, which is what survives a reload.
        "run_cost": run_cost_payload(db, current_run_id() or ""),
    })


async def stream_decision_maker_directly(
    message: str,
    display_message: Optional[str],
    session_id: str,
    tender_id: Optional[int],
    user_id: Optional[int],
    proposal_session_id: Optional[int],
    mode: str = "planning",
    approved_plan: Optional[dict] = None,
    change_feedback: Optional[str] = None,
    file_metadata: Optional[dict] = None,
    approved_action: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """Run the decision_maker directly (skipping the router classifier) and
    stream its events as SSE. Used by /decision/respond to resume a paused
    plan without re-running intent classification.

    Emits the same events as the decision_maker branch of the main streaming
    handler: ``session``, ``agent_start``, timeline events, optional tokens,
    ``agent_complete``, ``done``.
    """
    import json as _json
    import re as _re
    from app.services.langchain.graphs.decision_maker_agent import (
        run_decision_maker, _run_single_plan_step, _build_plan_aggregate_markdown,
    )
    from app.services.langchain.memory_service import save_conversation_turn
    from app.services.langchain.error_utils import format_user_error
    from app.models.proposal import ProposalMessage, ProposalSession

    db = _fresh_db()
    start_time = time.time()
    try:
        yield _sse_event("session", {"session_id": session_id})
        yield _sse_event("agent_start", {
            "agent_key": "decision_maker",
            "display_name": "Decision Maker",
        })

        # ── Execution mode: run ONE plan step at a time ────────────────────
        # Loads persistent state from proposal_sessions.pipeline_state and
        # increments a cursor on every call. Emits plan_step_progress +
        # optional artifact_created events, then pauses with
        # plan_step_awaiting_next so the UI can show a "Run next step"
        # button. On the final step emits plan_execution_complete.
        if mode == "execution" and proposal_session_id:
            async for ev in _drive_plan_step(
                db=db,
                session_id=session_id,
                proposal_session_id=proposal_session_id,
                user_id=user_id,
                tender_id=tender_id,
                file_metadata=file_metadata,
                approved_plan=approved_plan,
                start_time=start_time,
            ):
                yield ev
            return

        event_queue: asyncio.Queue = asyncio.Queue()

        def _sanitize(text: str) -> str:
            if not text:
                return text
            t = _re.sub(r"<think>[\s\S]*?</think>", "", text, flags=_re.IGNORECASE)
            t = _re.sub(r"<propose_plan[\s\S]*?</propose_plan>", "", t, flags=_re.IGNORECASE)
            t = _re.sub(r"<call_[a-z_]+[\s\S]*?</call_[a-z_]+>", "", t, flags=_re.IGNORECASE)
            t = _re.sub(r"<[a-z_][a-z0-9_]*>\s*\{[\s\S]*?\}\s*</[a-z_][a-z0-9_]*>", "", t, flags=_re.IGNORECASE)
            t = _re.sub(r"(?im)^.*\bI'?ll call (?:the\s+)?propose_plan.*$\n?", "", t)
            t = _re.sub(r"\n{3,}", "\n\n", t)
            return t.strip()

        def _cb(event_type: str, payload: dict) -> None:
            try:
                if event_type == "decision_thought" and isinstance(payload, dict):
                    content = payload.get("content")
                    if isinstance(content, str):
                        cleaned = _sanitize(content)
                        if not cleaned:
                            return
                        payload = {**payload, "content": cleaned}
                event_queue.put_nowait((event_type, payload))
            except Exception:
                pass

        _direct_capture = PendingActionCapture()
        _direct_grant = (
            ApprovalGrant(approved_action.get("tool"), approved_action.get("args") or {})
            if isinstance(approved_action, dict) and approved_action.get("tool")
            else None
        )
        _direct_run_id = uuid.uuid4().hex
        with run_id_scope(_direct_run_id), actor_scope(user_id), policy_scope(
            capture=_direct_capture, grant=_direct_grant, stream_callback=_cb,
        ):
          run_task = asyncio.create_task(run_decision_maker(
            db=_fresh_db(),
            message=message,
            session_id=session_id,
            tender_id=tender_id,
            user_id=user_id,
            file_metadata=file_metadata or {},
            proposal_session_id=proposal_session_id,
            stream_callback=_cb,
            mode=mode,
            approved_plan=approved_plan,
            change_feedback=change_feedback,
            approved_action=approved_action,
          ))

        while True:
            if run_task.done() and event_queue.empty():
                break
            try:
                ev_type, ev_payload = await asyncio.wait_for(event_queue.get(), timeout=0.25)
                yield _sse_event(ev_type, ev_payload)
            except asyncio.TimeoutError:
                continue

        result = await run_task
        output = (result or {}).get("output") or ""
        output_type = (result or {}).get("output_type", "decision_maker_trace")
        status = (result or {}).get("status", "completed")
        pending_plan = (result or {}).get("pending_plan")
        pending_action = (result or {}).get("pending_action")

        # Persist pending plan so another round of approval is possible
        if status == "pending_plan" and pending_plan and proposal_session_id:
            try:
                ps = db.query(ProposalSession).filter(ProposalSession.id == proposal_session_id).first()
                if ps:
                    state = dict(ps.pipeline_state or {})
                    state["pending_plan"] = {
                        "plan": pending_plan,
                        "original_message": display_message or message,
                        "file_metadata": file_metadata or {},
                        "tender_id": tender_id,
                        "proposed_at": time.time(),
                    }
                    ps.pipeline_state = state
                    db.add(ps)
                    db.commit()
            except Exception as e:
                logger.warning(f"Failed to persist pending plan (resume path): {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

        if status == "needs_confirmation" and pending_action:
            _persist_pending_action(
                db, proposal_session_id, pending_action,
                display_message or message, file_metadata, tender_id,
            )

        if status != "pending_plan":
            chunk_size = 200
            for i in range(0, len(output), chunk_size):
                yield _sse_event("token", {"content": output[i:i + chunk_size]})
                await asyncio.sleep(0.005)

        yield _sse_event("agent_complete", {
            "agent_key": "decision_maker",
            "output_type": output_type,
            "status": status,
            "trace": (result or {}).get("trace", []),
            "tool_calls": (result or {}).get("tool_calls", []),
            "pending_plan": pending_plan,
            "pending_action": pending_action,
        })

        # Persist assistant turn for history
        try:
            save_conversation_turn(
                db, session_id, "proposal_router", "assistant", output,
                metadata={
                    "routing": {"agents_used": ["decision_maker"]},
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "decision_trace": (result or {}).get("trace", []),
                    "decision_mode": mode,
                },
                output_type=output_type,
                routed_from="decision_maker",
            )
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        if proposal_session_id and output:
            try:
                meta: dict = {
                    "agents_used": ["decision_maker"],
                    "output_type": output_type,
                    "decision_mode": mode,
                }
                # Persist the structured plan so the DecisionPlanCard can be
                # rebuilt on page refresh (history only gives us `content`
                # otherwise, and the card falls back to plain markdown).
                if status == "pending_plan" and pending_plan:
                    meta["pending_plan"] = pending_plan
                    meta["plan_status"] = "pending"
                attach_run_cost(db, meta, _direct_run_id)
                assistant_msg = ProposalMessage(
                    session_id=proposal_session_id,
                    role="assistant",
                    content=output,
                    message_type="text",
                    metadata_json=meta,
                )
                db.add(assistant_msg)
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        yield _sse_event("done", {
            "session_id": session_id,
            "output_type": output_type,
            "agents_used": ["decision_maker"],
            "latency_ms": int((time.time() - start_time) * 1000),
            "run_cost": run_cost_payload(db, _direct_run_id),
        })

    except Exception as e:
        logger.error(f"stream_decision_maker_directly failed: {e}", exc_info=True)
        friendly = format_user_error(e)
        yield _sse_event("error", {"message": friendly})
        yield _sse_event("done", {"session_id": session_id, "error": friendly})
    finally:
        try:
            db.close()
        except Exception:
            pass


from app.core.actor_context import actor_scope  # noqa: E402
from app.core.run_context import current_run_id, run_id_scope  # noqa: E402
from app.services.run_cost_service import attach_run_cost, run_cost_payload  # noqa: E402
from app.services.langchain.tool_policy import (  # noqa: E402
    ApprovalGrant,
    PendingActionCapture,
    policy_scope,
)


def _persist_pending_action(
    db,
    proposal_session_id,
    pending_action: dict,
    original_message: str,
    file_metadata: dict,
    tender_id,
) -> None:
    """Park a gated tool call on the session so /action/respond can replay it.

    Mirrors how ``pending_plan`` is persisted. The execution context is stored
    alongside the call for the same reason it is there: without file_metadata
    and tender_id, the approved action re-runs blind.
    """
    if not (pending_action and proposal_session_id):
        return
    try:
        from app.models.proposal import ProposalSession as _PS
        ps = db.query(_PS).filter(_PS.id == proposal_session_id).first()
        if ps:
            state = dict(ps.pipeline_state or {})
            state["pending_action"] = {
                "action": pending_action,
                "original_message": original_message,
                "file_metadata": file_metadata or {},
                "tender_id": tender_id,
                "proposed_at": time.time(),
            }
            ps.pipeline_state = state
            db.add(ps)
            db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist pending action: {e}")
        try:
            db.rollback()
        except Exception:
            pass


async def stream_router_response(
    db: Session,
    message: str,
    session_id: Optional[str] = None,
    tender_id: Optional[int] = None,
    user_id: Optional[int] = None,
    proposal_session_id: Optional[int] = None,
    file_metadata: Optional[dict] = None,
    display_message: Optional[str] = None,
    approved_action: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """
    Stream the agent router response as SSE events.

    Uses a FRESH database session (not the endpoint's) to avoid inheriting
    a dirty/broken transaction state.  The endpoint's session may have been
    poisoned by a prior commit + Neon connection reset, so we create our own
    clean connection from the pool (validated by pool_pre_ping).

    Events emitted:
        - routing: Intent classification result
        - agent_start: Agent execution beginning
        - agent_complete: Agent finished with output_type
        - token: Streamed content chunks
        - done: Final response with metadata

    Yields:
        SSE-formatted strings (data: {...}\n\n)
    """
    # Use a fresh DB session — the endpoint's session may be dirty/stale
    # after its own commits (especially with Neon serverless PostgreSQL).
    db = _fresh_db()

    try:  # ← ensures db.close() runs when the generator finishes
        async for event in _stream_router_inner(
            db, message, session_id, tender_id, user_id,
            proposal_session_id, file_metadata, display_message,
            approved_action,
        ):
            yield event
    finally:
        db.close()


async def _stream_router_inner(
    db: Session,
    message: str,
    session_id: Optional[str] = None,
    tender_id: Optional[int] = None,
    user_id: Optional[int] = None,
    proposal_session_id: Optional[int] = None,
    file_metadata: Optional[dict] = None,
    display_message: Optional[str] = None,
    approved_action: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """Inner streaming logic — called with a clean DB session."""
    from app.services.langchain.graphs.agent_router_graph import (
        classify_intent_node,
        AGENT_DISPLAY_NAMES,
        AGENT_OUTPUT_TYPE_MAP,
    )
    from app.services.langchain.graphs.chat_agent_wrappers import (
        chat_document_analysis,
        chat_checklist_generation,
        chat_proposal_writing,
        chat_costing_research,
        chat_workspace_operations,
        chat_annexure_finder,
    )
    from app.services.langchain.memory_service import (
        save_conversation_turn,
        get_conversation_history,
    )

    sess_id = session_id or str(uuid.uuid4())
    start_time = time.time()

    # Constitution Rule 2: every log line emitted by this run carries
    # [run=...] so a conversation is greppable across graphs, tools and DB
    # writes. Opened at the coroutine/task boundaries below rather than around
    # this async generator — a run_id_scope spanning a `yield` can be resumed
    # in a different context and blow up on token reset.
    _run_id = uuid.uuid4().hex

    # One capture per run, shared by every agent and tool that executes inside
    # it, so a gated write surfaces the same way no matter which path ran.
    _run_capture = PendingActionCapture()
    _run_grant = (
        ApprovalGrant(approved_action.get("tool"), approved_action.get("args") or {})
        if isinstance(approved_action, dict) and approved_action.get("tool")
        else None
    )

    # Load conversation history (non-fatal on failure)
    history = []
    try:
        if session_id:
            turns = get_conversation_history(db, session_id, limit=20)
            history = [
                {"role": t.role, "content": t.content, "output_type": t.output_type}
                for t in turns
            ]

        # Save user turn (use display_message without file content for history)
        save_conversation_turn(
            db, sess_id, "proposal_router", "user", display_message or message,
            output_type=None, routed_from=None,
        )
    except Exception as e:
        logger.error(f"Failed to load history or save user turn: {e}", exc_info=True)
        history = []
        try:
            db.rollback()
        except Exception:
            pass

    # Emit session info
    yield _sse_event("session", {"session_id": sess_id})

    # Load session artifacts for context-aware routing
    session_artifacts = []
    if proposal_session_id:
        try:
            from app.services.artifact_service import list_artifacts
            artifacts = list_artifacts(db, proposal_session_id)
            session_artifacts = [
                {"type": a["artifact_type"], "title": a["title"], "agent_key": a.get("agent_key")}
                for a in artifacts
            ]
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    # ── Register uploaded PDFs as TenderDocuments so follow-up messages can
    # find them via _load_tender_pdfs even when no file is re-attached.
    if proposal_session_id:
        _pdf_paths = [
            ap["path"] if isinstance(ap, dict) else ap
            for ap in ((file_metadata or {}).get("attachment_paths") or [])
            if (isinstance(ap, dict) and ap.get("is_pdf") and ap.get("path"))
            or (isinstance(ap, str) and ap.lower().endswith(".pdf"))
        ]
        if _pdf_paths:
            try:
                from app.models.proposal import ProposalSession
                from app.models.tender import Tender, TenderDocument
                import os as _os
                import uuid as _uuid

                _ps = db.query(ProposalSession).filter(
                    ProposalSession.id == proposal_session_id
                ).first()
                if _ps:
                    _tid = _ps.tender_id or tender_id
                    if _tid:
                        existing_paths = {
                            row.file_path
                            for row in db.query(TenderDocument.file_path).filter(
                                TenderDocument.tender_id == _tid
                            ).all()
                        }
                        new_docs = [k for k in _pdf_paths if k not in existing_paths]
                        for key in new_docs:
                            db.add(TenderDocument(
                                tender_id=_tid,
                                file_name=_os.path.basename(key) or "uploaded.pdf",
                                file_path=key,
                                mime_type="application/pdf",
                                # Link extraction for a chat upload is tracked on its
                                # ChatAttachment; left at the column default
                                # ("pending"), this row stalled every later analysis
                                # of the tender for a minute in
                                # _wait_for_link_extraction. The upload route's own
                                # dual-write marks the same file "completed".
                                extraction_status="completed",
                            ))
                        if new_docs:
                            db.commit()
                        if not tender_id and _ps.tender_id:
                            tender_id = _ps.tender_id
                    else:
                        # No tender yet — create one and link it to the session
                        _new_tender = Tender(
                            portal="command_center",
                            tender_id=f"cc-{str(_uuid.uuid4())[:8]}",
                            title=_ps.title or f"Command Center #{_ps.id}",
                            status="open",
                            workflow_status="proposal_draft",
                            workspace_enabled=True,
                        )
                        db.add(_new_tender)
                        db.flush()
                        for key in _pdf_paths:
                            db.add(TenderDocument(
                                tender_id=_new_tender.id,
                                file_name=_os.path.basename(key) or "uploaded.pdf",
                                file_path=key,
                                mime_type="application/pdf",
                                extraction_status="completed",  # see above
                            ))
                        _ps.tender_id = _new_tender.id
                        db.commit()
                        tender_id = _new_tender.id
                        logger.info(
                            f"[router] auto-created tender {tender_id} for session "
                            f"{proposal_session_id} ({len(_pdf_paths)} PDF(s))"
                        )
            except Exception:
                logger.exception("[router] failed to register PDFs as TenderDocuments")
                try:
                    db.rollback()
                except Exception:
                    pass

    # Step 1: Classify intent
    router_state = {
        "session_id": sess_id,
        "user_message": message,
        "tender_id": tender_id,
        "intent": "",
        "selected_agents": [],
        "agent_results": {},
        "final_response": "",
        "output_type": "general",
        "metadata": {},
        "errors": [],
        "conversation_history": history,
        "file_metadata": file_metadata or {},
        "session_artifacts": session_artifacts,
    }

    # chat_engine decides who answers. "master" (the default) sends every
    # message to the Master Agent — no classifier deciding whether the user
    # deserves it, which is how it produced three conversation turns in its
    # entire history. "router" restores the classify-then-dispatch path
    # exactly; the setting is read live from PlatformSetting, so reverting is
    # an admin-panel change, not a deploy. That matters, because "master" puts
    # the flagship model on every message.
    _engine_choice = "master"
    try:
        _engine_choice = str(
            get_effective_setting(db, "chat_engine", "master") or "master"
        ).lower()
    except Exception:
        pass

    if _engine_choice == "master":
        intent = "orchestrate_complex"
        selected_agents = ["decision_maker"]
        # The shared routing event below fires for this path too — one
        # emission, one shape, whichever engine answered.
        classification_result = {"metadata": {
            "classifier_reasoning": "Master Agent is the chat entrypoint (chat_engine=master).",
        }}
    else:
      try:
        classification_result = await classify_intent_node(router_state, db)
        intent = classification_result.get("intent", "general_query")
        selected_agents = classification_result.get("selected_agents", [])
      except Exception as e:
        logger.error(f"Intent classification failed in streaming handler: {e}", exc_info=True)
        friendly = format_user_error(e)
        try:
            db.rollback()
        except Exception:
            pass
        yield _sse_event("error", {"message": friendly})
        yield _sse_event("done", {"session_id": sess_id, "error": friendly})
        return

    # Surface the router's "why I picked this agent" reasoning to the
    # frontend. With extended thinking enabled the model thinks carefully
    # before committing; the `reasoning` field is its one-sentence final
    # justification. Showing it builds trust ("the system understood my
    # request") and helps users self-correct ("oh, it picked the wrong
    # one — let me rephrase").
    routing_meta = classification_result.get("metadata") or {}
    routing_reasoning = (
        (routing_meta.get("classification") or {}).get("reasoning")
        or routing_meta.get("classifier_reasoning")
        or ""
    )

    yield _sse_event("routing", {
        "intent": intent,
        "agents": selected_agents,
        "agent_names": [AGENT_DISPLAY_NAMES.get(a, a) for a in selected_agents],
        "reasoning": routing_reasoning,
    })

    # Step 2: Execute agents or handle general query.
    #
    # `general_assistant` is a seeded CustomAgent, so the classifier's
    # valid-agent set now contains it and it can be selected by name. It has no
    # entry in agent_dispatch, so without this it would fall through to the
    # generic custom-agent executor instead of the path built for it.
    if (
        intent == "general_query"
        or not selected_agents
        or selected_agents[0] == "general_assistant"
    ):
        # The conversational path. Every message the classifier cannot place as
        # a specialist job arrives here — which used to mean a bare llm.astream
        # with no tools and no tender_id, so "where did these rates come from"
        # was answered with invented sources. It now runs the general assistant,
        # which can search, read this tender, calculate, and hand off.
        yield _sse_event("agent_start", {
            "agent_key": "general_assistant",
            "display_name": "DRPL Assistant",
        })

        try:
            from app.services.langchain.graphs.general_assistant_agent import (
                run_general_assistant,
            )

            # The agent runs to completion in a task while this generator
            # drains its events, so tokens reach the browser as they are
            # produced rather than in one lump at the end.
            _queue: asyncio.Queue = asyncio.Queue()

            def _ga_callback(event_name: str, data: dict) -> None:
                try:
                    _queue.put_nowait((event_name, data))
                except Exception:
                    pass

            _ga_task = asyncio.create_task(run_general_assistant(
                db,
                display_message or message,
                tender_id=tender_id,
                session_id=sess_id,
                proposal_session_id=proposal_session_id,
                user_id=user_id,
                conversation_history=history,
                file_metadata=file_metadata,
                stream_callback=_ga_callback,
            ))

            while True:
                if _ga_task.done() and _queue.empty():
                    break
                try:
                    name, payload = await asyncio.wait_for(_queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                yield _sse_event(name, payload)

            ga_result = await _ga_task
            full_response = ga_result.get("output") or ""
            ga_sources = ga_result.get("sources") or []
            ga_tool_calls = ga_result.get("tool_calls") or []
            ga_status = ga_result.get("status") or "completed"
            ga_pending = ga_result.get("pending_action")

            if ga_status == "needs_confirmation" and ga_pending and proposal_session_id:
                _persist_pending_action(
                    db, proposal_session_id, ga_pending,
                    display_message or message, file_metadata, tender_id,
                )

            yield _sse_event("agent_complete", {
                "agent_key": "general_assistant",
                "output_type": "general",
                "status": ga_status,
                "tool_calls": ga_tool_calls,
                "sources": ga_sources,
                "pending_action": ga_pending,
            })

            # Save assistant turn
            total_latency = int((time.time() - start_time) * 1000)
            save_conversation_turn(
                db, sess_id, "proposal_router", "assistant", full_response,
                tool_calls=ga_tool_calls,
                metadata={
                    "latency_ms": total_latency,
                    "sources": ga_sources,
                },
                output_type="general",
                routed_from="general_assistant",
            )

            # ...and as a ProposalMessage. Every other branch does this; this
            # one never did, so general answers were streamed to the browser,
            # written to conversation history, and then vanished on reload —
            # the assistant looked like it had said nothing at all.
            if proposal_session_id and full_response:
                try:
                    from app.models.proposal import ProposalMessage
                    db.add(ProposalMessage(
                        session_id=proposal_session_id,
                        role="assistant",
                        content=full_response,
                        message_type="text",
                        metadata_json=attach_run_cost(db, {
                            "agents_used": ["general_assistant"],
                            "output_type": "general",
                            "tool_calls": ga_tool_calls,
                            "sources": ga_sources,
                        }, _run_id),
                    ))
                    db.commit()
                except Exception as e:
                    logger.warning(f"Failed to save general ProposalMessage: {e}")
                    try:
                        db.rollback()
                    except Exception:
                        pass

            yield _sse_event("done", {
                "session_id": sess_id,
                "output_type": "general",
                "agents_used": ["general_assistant"],
                "latency_ms": total_latency,
                "run_cost": run_cost_payload(db, _run_id),
            })

            # Auto-generate session title
            if proposal_session_id:
                try:
                    new_title = await _generate_session_title(db, display_message or message, proposal_session_id)
                    if new_title:
                        yield _sse_event("title_updated", {"title": new_title})
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Streaming general response failed: {e}")
            friendly = format_user_error(e)
            yield _sse_event("error", {"message": friendly})
            yield _sse_event("done", {"session_id": sess_id, "error": friendly})
            # Reset DB session so subsequent saves don't cascade-fail
            try:
                db.rollback()
            except Exception:
                pass

    elif selected_agents and selected_agents[0] == "decision_maker":
        # Master agent — stream its reasoning timeline (Thought/Action/Observation)
        # events live via an asyncio.Queue drained by this generator.
        agent_key = "decision_maker"
        display_name = AGENT_DISPLAY_NAMES.get(agent_key, "Decision Maker")

        yield _sse_event("agent_start", {
            "agent_key": agent_key,
            "display_name": display_name,
        })

        event_queue: asyncio.Queue = asyncio.Queue()

        import re as _re

        def _sanitize_thought(text: str) -> str:
            """Strip internal scratchpad markers from a decision_thought payload
            before streaming it to the user. Removes <think>...</think> blocks,
            text-leaked tool-call XML (especially <propose_plan>...</propose_plan>),
            and fenced ```json / ```xml bodies — the LLM is told to stop emitting
            these but sanitize defensively in case it slips.
            """
            if not text:
                return text
            t = _re.sub(r"<think>[\s\S]*?</think>", "", text, flags=_re.IGNORECASE)
            # Strip the propose_plan leak (and any sibling tool-as-XML leaks).
            # The plan is recovered server-side from the raw LLM text, so the
            # frontend doesn't need to see the markup.
            t = _re.sub(r"<propose_plan[\s\S]*?</propose_plan>", "", t, flags=_re.IGNORECASE)
            t = _re.sub(r"<call_[a-z_]+[\s\S]*?</call_[a-z_]+>", "", t, flags=_re.IGNORECASE)
            # Also strip standalone tool-syntax like <inspect_session>{...}</inspect_session>
            t = _re.sub(r"<[a-z_][a-z0-9_]*>\s*\{[\s\S]*?\}\s*</[a-z_][a-z0-9_]*>", "", t, flags=_re.IGNORECASE)
            # Drop common preamble lines that introduce the (now-removed) leak
            t = _re.sub(r"(?im)^.*\bI'?ll call (?:the\s+)?propose_plan.*$\n?", "", t)
            # Collapse >2 blank lines
            t = _re.sub(r"\n{3,}", "\n\n", t)
            return t.strip()

        def _stream_cb(event_type: str, payload: dict) -> None:
            # put_nowait is safe from any thread/context
            try:
                if event_type == "decision_thought" and isinstance(payload, dict):
                    content = payload.get("content")
                    if isinstance(content, str):
                        cleaned = _sanitize_thought(content)
                        if not cleaned:
                            # Nothing to show after sanitization — drop the event.
                            return
                        payload = {**payload, "content": cleaned}
                event_queue.put_nowait((event_type, payload))
            except Exception:
                pass

        # Kick the decision maker off as a task and drain events concurrently
        from app.services.langchain.graphs.decision_maker_agent import run_decision_maker
        try:
            try:
                db.rollback()
            except Exception:
                pass

            # Determine mode. Default is AUTONOMOUS (the master agent plans +
            # executes itself, asking only when blocked) unless the operator has
            # turned autonomy off, in which case new requests start in planning.
            # An explicit per-request override (e.g. mode="execution" when the
            # user resumes after approving a plan) always wins.
            from app.core.config import get_settings
            dm_mode = "autonomous" if get_settings().decision_maker_autonomous else "planning"
            dm_approved_plan: Optional[dict] = None
            dm_change_feedback: Optional[str] = None
            if isinstance(file_metadata, dict):
                # Command Center endpoint uses file_metadata as a general
                # side-channel for per-request overrides.
                ov = file_metadata.get("_decision_maker_override") or {}
                if isinstance(ov, dict):
                    dm_mode = ov.get("mode") or dm_mode
                    dm_approved_plan = ov.get("approved_plan")
                    dm_change_feedback = ov.get("change_feedback")

            # create_task copies the current context, so the task keeps this
            # binding after the with-block exits. Sub-agents invoked as tools
            # load their own tools inside that copied context and therefore
            # record into the same capture.
            with run_id_scope(_run_id), actor_scope(user_id), policy_scope(
                capture=_run_capture,
                grant=_run_grant,
                stream_callback=_stream_cb,
            ):
              run_task = asyncio.create_task(run_decision_maker(
                db=_fresh_db(),  # own session; master owns its connection lifetime
                message=display_message or message,
                session_id=sess_id,
                tender_id=tender_id,
                user_id=user_id,
                conversation_history=history,
                file_metadata=file_metadata or {},
                proposal_session_id=proposal_session_id,
                stream_callback=_stream_cb,
                mode=dm_mode,
                approved_plan=dm_approved_plan,
                change_feedback=dm_change_feedback,
              ))

            # Drain events until the run_task finishes (plus a brief final drain)
            result: Optional[dict] = None
            while True:
                if run_task.done() and event_queue.empty():
                    break
                try:
                    ev_type, ev_payload = await asyncio.wait_for(event_queue.get(), timeout=0.25)
                    yield _sse_event(ev_type, ev_payload)
                except asyncio.TimeoutError:
                    continue

            result = await run_task
            output = (result or {}).get("output") or ""
            output_type = (result or {}).get("output_type", "decision_maker_trace")
            status = (result or {}).get("status", "completed")
            pending_plan = (result or {}).get("pending_plan")
            pending_action = (result or {}).get("pending_action")

            # Pending-plan short-circuit: persist the plan to the session so
            # /decision/respond can resume later, then emit agent_complete
            # WITHOUT streaming the plan as tokens (the frontend will render
            # the structured approval card instead).
            if status == "pending_plan" and pending_plan and proposal_session_id:
                try:
                    from app.models.proposal import ProposalSession
                    ps = db.query(ProposalSession).filter(
                        ProposalSession.id == proposal_session_id
                    ).first()
                    if ps:
                        state = dict(ps.pipeline_state or {})
                        state["pending_plan"] = {
                            "plan": pending_plan,
                            "original_message": display_message or message,
                            # Preserve the full execution context — without this the
                            # approval path runs each plan step with an empty
                            # file_metadata and no tender_id, so every handler
                            # bails out with "no documents to analyze".
                            "file_metadata": file_metadata or {},
                            "tender_id": tender_id,
                            "proposed_at": time.time(),
                        }
                        ps.pipeline_state = state
                        db.add(ps)
                        db.commit()
                except Exception as e:
                    logger.warning(f"Failed to persist pending plan: {e}")
                    try:
                        db.rollback()
                    except Exception:
                        pass

            # Stream the final markdown as tokens so frontend can render progressively.
            # For pending_plan, skip streaming — the DecisionPlanCard renders from the
            # agent_complete payload's pending_plan field instead.
            if status == "needs_confirmation" and pending_action:
                _persist_pending_action(
                    db, proposal_session_id, pending_action,
                    display_message or message, file_metadata, tender_id,
                )

            if status != "pending_plan":
                chunk_size = 200
                for i in range(0, len(output), chunk_size):
                    yield _sse_event("token", {"content": output[i:i + chunk_size]})
                    await asyncio.sleep(0.005)

            yield _sse_event("agent_complete", {
                "agent_key": agent_key,
                "output_type": output_type,
                "status": status,
                "trace": (result or {}).get("trace", []),
                "tool_calls": (result or {}).get("tool_calls", []),
                "pending_plan": pending_plan,
                "pending_action": pending_action,
            })

            # Persist assistant turn + ProposalMessage with the full trace in metadata
            agents_used = [agent_key]
            final_response = output
            final_output_type = output_type
            total_latency = int((time.time() - start_time) * 1000)

            try:
                _turn_meta: dict = {
                    "routing": {"agents_used": agents_used},
                    "latency_ms": total_latency,
                    "decision_trace": (result or {}).get("trace", []),
                }
                # Mirror the structured plan into the AgentConversationHistory
                # turn so that on tab reload the history endpoint (which
                # prefers that table over ProposalMessage) still carries
                # enough data to rebuild the DecisionPlanCard.
                _pending = (result or {}).get("pending_plan")
                if (result or {}).get("status") == "pending_plan" and _pending:
                    _turn_meta["pending_plan"] = _pending
                    _turn_meta["plan_status"] = "pending"
                save_conversation_turn(
                    db, sess_id, "proposal_router", "assistant", final_response,
                    metadata=_turn_meta,
                    output_type=final_output_type,
                    routed_from=agent_key,
                )
            except Exception as e:
                logger.warning(f"Failed to save decision_maker assistant turn: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

            if proposal_session_id and final_response:
                try:
                    from app.models.proposal import ProposalMessage
                    meta: dict = {
                        "agents_used": agents_used,
                        "output_type": final_output_type,
                        "decision_trace": (result or {}).get("trace", []),
                        "decision_tool_calls": (result or {}).get("tool_calls", []),
                    }
                    # Persist the structured plan so the DecisionPlanCard can
                    # be rebuilt on page refresh.
                    _pending = (result or {}).get("pending_plan")
                    if (result or {}).get("status") == "pending_plan" and _pending:
                        meta["pending_plan"] = _pending
                        meta["plan_status"] = "pending"
                    attach_run_cost(db, meta, _run_id)
                    assistant_msg = ProposalMessage(
                        session_id=proposal_session_id,
                        role="assistant",
                        content=final_response,
                        message_type="text",
                        metadata_json=meta,
                    )
                    db.add(assistant_msg)
                    db.commit()
                except Exception as e:
                    logger.warning(f"Failed to save decision_maker ProposalMessage: {e}")
                    try:
                        db.rollback()
                    except Exception:
                        pass

            yield _sse_event("done", {
                "session_id": sess_id,
                "output_type": final_output_type,
                "agents_used": agents_used,
                "agent_display_names": [display_name],
                "latency_ms": total_latency,
                "run_cost": run_cost_payload(db, _run_id),
            })

            # Auto-generate session title (same as specialized branch)
            if proposal_session_id:
                try:
                    new_title = await _generate_session_title(db, display_message or message, proposal_session_id)
                    if new_title:
                        yield _sse_event("title_updated", {"title": new_title})
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"decision_maker streaming failed: {e}", exc_info=True)
            friendly = format_user_error(e)
            yield _sse_event("error", {"agent_key": agent_key, "message": friendly})
            yield _sse_event("done", {"session_id": sess_id, "error": friendly})
            try:
                db.rollback()
            except Exception:
                pass

    else:
        # Execute single specialized agent (router always selects exactly one).
        #
        # When the `command_center_use_execute_agent` PlatformSetting is True
        # (default), specialized agents go through the bridge shims which
        # delegate to ``execute_agent()`` — same path Agent Builder Test uses.
        # That creates AgentExecution rows for monitoring, surfaces the
        # agent's raw output without canned fallback, and uses any user-edited
        # prompt from the Agent Builder UI.
        #
        # Setting it to False reverts to the legacy chat_* path (LangGraph
        # state machines + parse_and_return). Useful as a kill-switch if
        # quality regresses.
        # get_effective_setting is imported at module level; a function-level
        # import here would make the name local to this whole function and
        # break the chat_engine read at the top with UnboundLocalError.
        _use_bridge = get_effective_setting(db, "command_center_use_execute_agent", True)
        if isinstance(_use_bridge, str):
            _use_bridge = _use_bridge.lower() in ("true", "1", "yes")

        if _use_bridge:
            from app.services.langchain.graphs.chat_agent_wrappers import (
                chat_document_analysis_via_execute_agent,
                chat_checklist_generation_via_execute_agent,
                chat_proposal_writing_via_execute_agent,
                chat_annexure_finder_via_execute_agent,
                chat_workspace_operations_via_execute_agent,
            )
            # All six specialized agents now route through execute_agent() —
            # creates AgentExecution rows for monitoring, raw output flows
            # through to the user, no canned-fallback substitutions, and
            # user-edited prompts from Agent Builder UI take effect via
            # resolve_system_prompt.
            agent_dispatch = {
                "deep_analyzer": chat_document_analysis_via_execute_agent,
                "checklist_generator": chat_checklist_generation_via_execute_agent,
                "proposal_creator": chat_proposal_writing_via_execute_agent,
                "workspace_manager": chat_workspace_operations_via_execute_agent,
                # costing_researcher needs the canonical path even when the
                # bridge is enabled — costing a large NIT is an
                # enumerate-everything task. The canonical path auto-parses the
                # NIT bidding schedule, builds a deterministic 1:1 skeleton, and
                # prices every row in batches (no row can be summarised/dropped).
                # The generic execute_agent (ReAct) path has no awareness of the
                # NIT schedule: it runs one freeform pass that collapses dense
                # spares schedules (A7/B7/C7/D7, 50–138 rows each) into a single
                # "A7-Summary" line. Same class as annexure_finder below.
                "costing_researcher": chat_costing_research,
                # annexure_finder needs the canonical path even when the bridge
                # is enabled — it uses Claude Vision (call_ai_with_documents) to
                # extract structured annexure JSON and persists each as a paired
                # (ChecklistItem, DocumentWorkspace) row. The generic
                # execute_agent (ReAct/chain_of_thought) path can't replicate
                # that: it just iterates tool calls, hits max_tokens trying to
                # enumerate everything in one response, and never persists.
                "annexure_finder": chat_annexure_finder,
            }
        else:
            agent_dispatch = {
                "deep_analyzer": chat_document_analysis,
                "checklist_generator": chat_checklist_generation,
                "proposal_creator": chat_proposal_writing,
                "costing_researcher": chat_costing_research,
                "workspace_manager": chat_workspace_operations,
                "annexure_finder": chat_annexure_finder,
            }

        agent_key = selected_agents[0]  # Always exactly one agent
        handler = agent_dispatch.get(agent_key)

        if not handler:
            # Try custom agent
            try:
                from app.services.langchain.graphs.chat_agent_wrappers import chat_custom_agent
                from app.models.agent_builder import CustomAgent
                custom_agent = db.query(CustomAgent).filter(
                    CustomAgent.agent_key == agent_key,
                    CustomAgent.is_enabled == True,
                ).first()
                if custom_agent:
                    handler = lambda db, message, tender_id, session_id, _key=agent_key, **kwargs: chat_custom_agent(
                        db=db, message=message, tender_id=tender_id,
                        session_id=session_id, agent_key=_key,
                    )
                    AGENT_DISPLAY_NAMES[agent_key] = custom_agent.display_name
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        display_name = AGENT_DISPLAY_NAMES.get(agent_key, agent_key)
        agents_used = []
        final_response = ""
        final_output_type = "general"

        if handler:
            yield _sse_event("agent_start", {
                "agent_key": agent_key,
                "display_name": display_name,
            })

            try:
                # Ensure DB session is clean before agent execution —
                # earlier operations (save_conversation_turn, classify_intent)
                # may have left the session in a dirty/failed state.
                try:
                    db.rollback()
                except Exception:
                    pass

                # Reliability events bubble up via a ContextVar set by the
                # SSE handler. The agent (deep into LangChain / call_ai)
                # emits ``assistant_continuing`` / ``assistant_resumed`` /
                # ``assistant_retried_empty`` / ``assistant_truncated`` /
                # ``assistant_empty_content`` / ``assistant_retried_no_thinking``
                # without knowing or caring about the SSE pipe — they land in
                # this queue and we drain them concurrently.
                from app.services.ai_service import set_streaming_callback
                reliability_queue: asyncio.Queue = asyncio.Queue()

                def _reliability_cb(event_type: str, payload: dict) -> None:
                    try:
                        reliability_queue.put_nowait((event_type, payload))
                    except Exception:
                        pass

                # Retry on transient errors (rate limit, overloaded). The queue
                # drain below covers reliability events; transient retries stay
                # at this layer because they require waiting + re-invoking.
                max_retries = 2

                async def _invoke_with_retry() -> dict:
                    for attempt in range(max_retries + 1):
                        try:
                            # Gate writes inside the specialist's own tools.
                            # Without this scope, the router's
                            # direct-to-specialist path writes unconfirmed
                            # while the orchestrator path is gated.
                            with run_id_scope(_run_id), actor_scope(user_id), policy_scope(
                                capture=_run_capture,
                                grant=_run_grant,
                                stream_callback=_reliability_cb,
                            ):
                                return await handler(
                                    db=db,
                                    message=message,
                                    tender_id=tender_id,
                                    session_id=sess_id,
                                    conversation_history=history,
                                    file_metadata=file_metadata or {},
                                    proposal_session_id=proposal_session_id,
                                )
                        except Exception as handler_err:
                            if is_transient_error(handler_err) and attempt < max_retries:
                                wait_seconds = 5 * (attempt + 1)  # 5s, then 10s
                                logger.warning(
                                    f"Agent {agent_key}: transient error on "
                                    f"attempt {attempt + 1}/{max_retries + 1}, "
                                    f"retrying in {wait_seconds}s"
                                )
                                # Funnel the retry notice through the same queue
                                # so the SSE drain renders it in order with any
                                # reliability events emitted before it.
                                _reliability_cb("retry", {
                                    "attempt": attempt + 1,
                                    "wait_seconds": wait_seconds,
                                    "message": "Service busy, retrying...",
                                })
                                await asyncio.sleep(wait_seconds)
                                continue
                            raise

                with set_streaming_callback(_reliability_cb):
                    handler_task = asyncio.create_task(_invoke_with_retry())
                    # Drain reliability events until the handler finishes AND
                    # the queue is empty.
                    while True:
                        if handler_task.done() and reliability_queue.empty():
                            break
                        try:
                            ev_type, ev_payload = await asyncio.wait_for(
                                reliability_queue.get(), timeout=0.25
                            )
                            yield _sse_event(ev_type, ev_payload)
                        except asyncio.TimeoutError:
                            continue
                    # Re-raise any handler exception now that we've drained.
                    result = await handler_task

                output = result.get("output") or ""
                output_type = result.get("output_type", "general")

                # Sanitize failed agent output — never stream raw errors to user.
                # The agent paths already run format_user_error against the
                # *original* exception (preserving its class) before returning,
                # so result["output"] is already safe. Only fall back to a
                # generic message when the handler returned status="failed"
                # with an empty output — re-wrapping result["error"] in a
                # plain Exception here would discard the real class
                # (RateLimitError, OperationalError, etc.) and always produce
                # the unhelpful "Something went wrong" generic string.
                if result.get("status") == "failed" and not output:
                    raw_err = result.get("error") or ""
                    output = format_user_error(Exception(raw_err)) if raw_err else (
                        "The request failed. Please try again."
                    )

                # Strip any leaked internal markers before streaming
                from app.services.langchain.graphs.chat_agent_wrappers import (
                    _strip_checklist_markers, _strip_file_content_markers,
                )
                output = _strip_checklist_markers(output)
                output = _strip_file_content_markers(output)

                # A specialist tried to write and was gated. Report it the same
                # way the orchestrator does so the frontend renders one card
                # regardless of which agent ran.
                _specialist_pending = (
                    dict(_run_capture) if _run_capture.is_pending() else None
                )
                if _specialist_pending:
                    result["status"] = "needs_confirmation"
                    output = _specialist_pending["summary"]
                    _persist_pending_action(
                        db, proposal_session_id, _specialist_pending,
                        display_message or message, file_metadata, tender_id,
                    )

                # Stream the output in chunks for better UX
                chunk_size = 100
                for i in range(0, len(output), chunk_size):
                    yield _sse_event("token", {"content": output[i:i + chunk_size]})
                    await asyncio.sleep(0.01)

                yield _sse_event("agent_complete", {
                    "agent_key": agent_key,
                    "output_type": output_type,
                    "status": result.get("status", "completed"),
                    "pending_action": _specialist_pending,
                })

                # Surface any new clarification requests the agent queued via the
                # `clarify` tool. The tool persisted PendingClarification rows; emit
                # an SSE event for each pending row keyed to this session so the
                # frontend can show a toast + modal.
                if proposal_session_id:
                    try:
                        from app.models.clarification import PendingClarification
                        pending = (
                            db.query(PendingClarification)
                            .filter(
                                PendingClarification.session_id == proposal_session_id,
                                PendingClarification.status == "pending",
                            )
                            .order_by(PendingClarification.created_at.desc())
                            .limit(3)
                            .all()
                        )
                        for row in pending:
                            yield _sse_event("clarification_request", {
                                "clarification_id": row.id,
                                "agent_key": row.agent_key,
                                "question": row.question,
                                "options": row.options or [],
                                "context": row.context or {},
                            })
                    except Exception as ce:
                        logger.debug(f"Clarification emission failed (non-fatal): {ce}")

                final_response = output
                final_output_type = output_type
                agents_used = [agent_key]

                # Auto-create artifact if output qualifies.
                #
                # The cost-breakdown path is special: it has TWO sources of
                # artifact data — the heuristic extractor (which infers an
                # artifact from output text + structured_data) AND the
                # `xlsx_artifact` dict the costing runner attaches when
                # `persist_from_agent_output` actually wrote a CostBreakdown
                # row. We previously emitted both, which let the badge appear
                # even when persistence failed (the "lying artifact card"
                # bug). Now: when the agent runner attaches xlsx_artifact /
                # cost_breakdown_id, we treat THAT as the canonical
                # persistence proof and skip the heuristic path. Otherwise we
                # fall back to the heuristic path with its payload guard
                # (which itself rejects empty line_items now). Either way,
                # the artifact_created SSE event reflects a row that exists.
                #
                # Never spawn an artifact from a FAILED / needs_input result — a
                # re-analysis that errored used to leak its error text into the
                # artifact panel as a bogus "artifact 3". The error still streams
                # as a normal assistant message; it just isn't an artifact card.
                _agent_status = result.get("status", "completed") if isinstance(result, dict) else "completed"
                if proposal_session_id and _agent_status not in ("failed", "needs_input"):
                    try:
                        from app.services.artifact_service import (
                            extract_artifact_from_output, create_artifact,
                        )

                        xlsx_info = (
                            result.get("xlsx_artifact") if isinstance(result, dict) else None
                        )
                        persistence_failure = (
                            result.get("persistence_failure") if isinstance(result, dict) else None
                        )
                        cost_breakdown_id = (
                            result.get("cost_breakdown_id") if isinstance(result, dict) else None
                        )

                        # Path A — costing runner confirmed real persistence.
                        # Emit the artifact_created event for the XLSX artifact
                        # it produced. No heuristic fallback in this case.
                        if xlsx_info and xlsx_info.get("artifact_id"):
                            yield _sse_event("artifact_created", {
                                "artifact_id": xlsx_info["artifact_id"],
                                "artifact_type": xlsx_info.get("artifact_type", "cost_breakdown_xlsx"),
                                "title": xlsx_info.get("title", "Cost Breakdown"),
                                "version": xlsx_info.get("version", 1),
                                "cost_breakdown_id": cost_breakdown_id,
                            })
                        # Path B — costing runner reported persistence failure.
                        # DO NOT spawn a heuristic artifact; Phase 3 will emit
                        # a typed `agent_error` event with the reason so the
                        # user sees a clean error bubble instead.
                        elif persistence_failure:
                            logger.info(
                                f"[artifact] suppressing artifact_created for {agent_key!r}: "
                                f"persistence_failure={persistence_failure.get('reason')!r}"
                            )
                        # Path C — non-costing agents (deep_analyzer, checklist
                        # generator, etc.) take the heuristic path. The
                        # payload guard added in Phase 1a still applies, so a
                        # cost_breakdown artifact that slipped through without
                        # `xlsx_artifact` AND without `persistence_failure`
                        # will be suppressed by the guard if line_items is
                        # empty.
                        else:
                            artifact_meta = extract_artifact_from_output(
                                agent_key=agent_key,
                                output=output,
                                output_type=output_type,
                                structured_data=result.get("structured_data"),
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
                                yield _sse_event("artifact_created", {
                                    "artifact_id": artifact.id,
                                    "artifact_type": artifact.artifact_type,
                                    "title": artifact.title,
                                    "version": artifact.version,
                                })
                    except Exception as ae:
                        logger.warning(f"Artifact creation failed for {agent_key}: {ae}")

                # Phase 3 — typed agent_warning / agent_error events.
                #
                # 1. agent_warning: code=context_trimmed when the pre-flight
                #    budget policy dropped any signals (annexed PDFs / training
                #    data / PDF extracts). The frontend renders this as a
                #    distinct amber bubble below the response, NOT inside it.
                # 2. agent_error: code=persistence_failed when the costing
                #    runner reported it could not write a CostBreakdown row.
                #    The frontend renders this as a red error bubble. No
                #    artifact card appears in this case (gated above).
                # 3. agent_error: code=context_overflow when the run failed
                #    even after the trim policy ran — the user needs to know
                #    the analysis was incomplete.
                try:
                    trim_notes = (
                        result.get("trim_notes") if isinstance(result, dict) else None
                    )
                    if trim_notes:
                        yield _sse_event("agent_warning", {
                            "code": "context_trimmed",
                            "severity": "warning",
                            "message": (
                                f"Trimmed input to fit context window "
                                f"({len(trim_notes)} reduction"
                                f"{'s' if len(trim_notes) != 1 else ''} applied)."
                            ),
                            "details": {"trim_notes": trim_notes},
                            "agent_key": agent_key,
                        })

                    persistence_failure = (
                        result.get("persistence_failure") if isinstance(result, dict) else None
                    )
                    if persistence_failure:
                        yield _sse_event("agent_error", {
                            "code": "persistence_failed",
                            "severity": "error",
                            "message": persistence_failure.get("message")
                                       or "Cost breakdown could not be saved.",
                            "details": {
                                "reason": persistence_failure.get("reason"),
                                "tender_id": persistence_failure.get("tender_id"),
                            },
                            "agent_key": agent_key,
                        })
                        # /health/costing telemetry — count this run as a
                        # persistence failure so the success-rate metric
                        # reflects reality. See plan Phase 4.
                        try:
                            from app.core.costing_metrics import record_persist_fail
                            record_persist_fail(persistence_failure.get("reason"))
                        except Exception:
                            pass

                    structured_data = (
                        result.get("structured_data") if isinstance(result, dict) else None
                    )
                    overflow_after_trim = (
                        isinstance(structured_data, dict)
                        and structured_data.get("_overflow_after_trim")
                    )
                    if overflow_after_trim:
                        yield _sse_event("agent_error", {
                            "code": "context_overflow",
                            "severity": "error",
                            "message": (
                                "The tender documents exceed Claude's context "
                                "window even after trimming. Please attach the "
                                "NIT PDF only (skip the Annexed Document) and "
                                "retry."
                            ),
                            "details": {"trim_notes": trim_notes or []},
                            "agent_key": agent_key,
                        })
                        try:
                            from app.core.costing_metrics import record_overflow
                            record_overflow()
                        except Exception:
                            pass
                except Exception as event_err:
                    logger.warning(
                        f"agent_error / agent_warning emission failed for "
                        f"{agent_key}: {event_err}"
                    )

                # Auto-activate the workspace after Checklist Generator or
                # Annexure Finder runs. Both populate ChecklistItem /
                # DocumentWorkspace rows during their own execution, but neither
                # creates the WorkspaceConfig row or sets tender.workspace_enabled
                # — without this, the user has populated workspace data but the
                # UI says it's empty. Idempotent: init_workspace() skips items
                # that already have a workspace row.
                if (
                    final_output_type in ("checklist", "annexures_extracted")
                    and tender_id
                ):
                    try:
                        from app.services.workspace_service import init_workspace
                        init_workspace(db, tender_id, user_id)
                        yield _sse_event("workspace_activated", {
                            "tender_id": tender_id,
                            "trigger": final_output_type,
                        })
                    except Exception as ws_err:
                        logger.warning(f"Auto init_workspace failed: {ws_err}")
                        try:
                            db.rollback()
                        except Exception:
                            pass

                # Auto-extract GEM file IDs from analysis output
                if final_output_type == "document_analysis" and tender_id and output:
                    try:
                        from app.services.gem_file_service import (
                            extract_gem_file_ids, create_pending_documents,
                            process_gem_file_ids_background,
                        )
                        gem_refs = extract_gem_file_ids(output)
                        if gem_refs:
                            count = create_pending_documents(db, tender_id, gem_refs, user_id)
                            if count > 0:
                                yield _sse_event("gem_files_found", {
                                    "count": count,
                                    "files": [r["name"] for r in gem_refs[:10]],
                                })
                            # Attempt downloads in background
                            asyncio.create_task(asyncio.to_thread(
                                process_gem_file_ids_background,
                                tender_id, output, user_id,
                            ))
                    except Exception as gfe:
                        logger.warning(f"GEM file ID extraction failed: {gfe}")

                # Auto-extract annexure/proforma/BOQ format templates from analysis
                if final_output_type == "document_analysis" and tender_id:
                    try:
                        from app.services.format_extraction_service import (
                            extract_formats_from_analysis,
                            create_format_templates_and_workspace_items,
                        )
                        formats = extract_formats_from_analysis(
                            output, result.get("structured_data"),
                        )
                        if formats:
                            fmt_result = create_format_templates_and_workspace_items(
                                db, tender_id, formats, user_id,
                            )
                            if fmt_result.get("templates_created", 0) > 0:
                                yield _sse_event("formats_extracted", {
                                    "templates_created": fmt_result["templates_created"],
                                    "workspace_items_updated": fmt_result["workspace_items_updated"],
                                    "format_names": [f["name"] for f in formats[:10]],
                                })
                    except Exception as fe:
                        logger.warning(f"Format extraction failed (non-fatal): {fe}")

            except Exception as e:
                # Promote to full traceback so the worker logs reveal the
                # actual root cause when the user sees the generic
                # "Something went wrong" message. Without exc_info=True
                # the user has no way to know whether it's a DB error,
                # an SDK kwarg mismatch, a parser failure, etc.
                logger.error(
                    f"Agent {agent_key} streaming failed: "
                    f"{type(e).__name__}: {e}",
                    exc_info=True,
                )
                friendly = format_user_error(e)
                yield _sse_event("error", {
                    "agent_key": agent_key,
                    "message": friendly,
                    # Surface the error class (not the message) to the frontend
                    # so devs can see in the browser's network tab which
                    # exception fired. Generic message stays user-facing.
                    "error_class": type(e).__name__,
                })
                # Reset DB session so subsequent saves don't cascade-fail
                try:
                    db.rollback()
                except Exception:
                    pass
        else:
            final_response = f"Agent '{agent_key}' is not available."
            final_output_type = "general"

        # Save assistant turn (rollback first in case the session is dirty)
        total_latency = int((time.time() - start_time) * 1000)
        routed_from = ",".join(agents_used)
        try:
            save_conversation_turn(
                db, sess_id, "proposal_router", "assistant", final_response,
                metadata={"routing": {"agents_used": agents_used}, "latency_ms": total_latency},
                output_type=final_output_type,
                routed_from=routed_from,
            )
        except Exception as e:
            logger.warning(f"Failed to save assistant conversation turn: {e}")
            try:
                db.rollback()
            except Exception:
                pass

        yield _sse_event("done", {
            "session_id": sess_id,
            "output_type": final_output_type,
            "agents_used": agents_used,
            "agent_display_names": [AGENT_DISPLAY_NAMES.get(a, a) for a in agents_used],
            "latency_ms": total_latency,
            "run_cost": run_cost_payload(db, _run_id),
        })

        # Auto-generate session title
        if proposal_session_id:
            try:
                new_title = await _generate_session_title(db, display_message or message, proposal_session_id)
                if new_title:
                    yield _sse_event("title_updated", {"title": new_title})
            except Exception:
                pass

        # Save assistant message to ProposalMessage for approval workflow
        if proposal_session_id and final_response:
            try:
                from app.models.proposal import ProposalMessage
                assistant_msg = ProposalMessage(
                    session_id=proposal_session_id,
                    role="assistant",
                    content=final_response,
                    message_type="text",
                    metadata_json=attach_run_cost(
                        db,
                        {"agents_used": agents_used, "output_type": final_output_type},
                        _run_id,
                    ),
                )
                db.add(assistant_msg)

                # Update pipeline state if relevant
                from app.models.proposal import ProposalSession
                cc_session = db.query(ProposalSession).filter(
                    ProposalSession.id == proposal_session_id
                ).first()
                if cc_session:
                    pipeline_state = cc_session.pipeline_state or {}
                    # Map output types to pipeline steps
                    output_step_map = {
                        "document_analysis": "analyze_documents",
                        "checklist": "generate_checklist",
                        "proposal_document": "generate_documents",
                        "cost_breakdown": "research_costing",
                        "workspace_operations": "workspace_setup",
                        "annexures_extracted": "extract_annexures",
                    }
                    step = output_step_map.get(final_output_type)
                    state_dirty = False
                    if step and not pipeline_state.get(step):
                        pipeline_state[step] = True
                        state_dirty = True
                    # Checklist Generator and Annexure Finder both populate
                    # workspace tables; mark workspace_setup so the green dot
                    # lights up on the Workspace button (auto-init also runs
                    # earlier in this handler, which sets tender.workspace_enabled).
                    if (
                        final_output_type in ("checklist", "annexures_extracted")
                        and not pipeline_state.get("workspace_setup")
                    ):
                        pipeline_state["workspace_setup"] = True
                        state_dirty = True
                    if state_dirty:
                        cc_session.pipeline_state = pipeline_state
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(cc_session, "pipeline_state")

                db.commit()
            except Exception as e:
                logger.warning(f"Failed to save ProposalMessage/pipeline state: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

        # Emit suggestions
        if proposal_session_id:
            try:
                from app.services.suggestion_service import generate_suggestions
                from app.models.proposal import ProposalSession
                cc_session = db.query(ProposalSession).filter(
                    ProposalSession.id == proposal_session_id
                ).first()
                if cc_session:
                    suggestions = generate_suggestions(
                        db=db,
                        session_id=proposal_session_id,
                        tender_id=cc_session.tender_id,
                        pipeline_state=cc_session.pipeline_state,
                        last_output_type=final_output_type,
                    )
                    yield _sse_event("suggestions", {"items": suggestions})
            except Exception as e:
                logger.warning(f"Failed to generate suggestions: {e}")

        # Background learning extraction
        if final_response and user_id:
            try:
                from app.services.learning_extraction_service import extract_and_store_learnings
                asyncio.create_task(extract_and_store_learnings(
                    agent_key="proposal_router",
                    user_input=display_message or message,
                    agent_output=final_response,
                    tender_id=tender_id,
                ))
            except Exception as e:
                logger.debug(f"Learning extraction launch failed: {e}")

        # Capture quality outputs as exemplar training data
        if final_response and final_output_type not in ("general", "clarification_needed"):
            try:
                from app.services.learning_extraction_service import capture_quality_output
                if agents_used and final_response:
                    asyncio.create_task(capture_quality_output(
                        agent_key=agents_used[0],
                        output=final_response,
                        output_type=final_output_type,
                        user_input=display_message or message,
                        tender_id=tender_id,
                    ))
            except Exception as e:
                logger.debug(f"Quality output capture launch failed: {e}")


def _sse_event(event_type: str, data: dict) -> str:
    """Format an SSE event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


_TITLE_FILLER_RE = re.compile(
    r"^(?:(?:hi|hello|hey|dear|ok(?:ay)?|so|please|pls|kindly|can you|could you|"
    r"would you|will you|i want you to|i need you to|i want to|i need to|help me(?: to)?|"
    r"let'?s)[\s,.!:]+)+",
    re.IGNORECASE,
)
_TITLE_MAX_WORDS = 8
_TITLE_MAX_CHARS = 60


def session_title_from(message: str, tender_title: str | None = None) -> str | None:
    """A short session title without a model call.

    This was a Luna call per new session (max_tokens=30 on a reasoning model,
    so the budget often went to reasoning and the title came back empty) and
    it was not metered. A session about a tender is best named by the tender;
    otherwise the first clause of the request, minus the pleasantries, is what
    a person would have typed as a title anyway.
    """
    from app.services.tender_enrichment_service import _is_default_title

    if tender_title and not _is_default_title(tender_title):
        t = " ".join(tender_title.split())
        return t if len(t) <= _TITLE_MAX_CHARS else t[: _TITLE_MAX_CHARS - 1].rstrip() + "…"
    text = " ".join((message or "").split())
    if not text:
        return None
    first = re.split(r"(?<=[.?!])\s", text, maxsplit=1)[0]
    first = _TITLE_FILLER_RE.sub("", first).strip(" ,.;:!?-\"'")
    if not first:
        return None
    words = first.split()
    title = " ".join(words[:_TITLE_MAX_WORDS])
    if len(words) > _TITLE_MAX_WORDS:
        title += "…"
    if len(title) > _TITLE_MAX_CHARS:
        title = title[: _TITLE_MAX_CHARS - 1].rstrip() + "…"
    return title[0].upper() + title[1:]


async def _generate_session_title(db: Session, message: str, proposal_session_id: int) -> str | None:
    """Name a session that still carries a default title (no model call)."""
    from app.models.proposal import ProposalSession as PS

    session = db.query(PS).filter(PS.id == proposal_session_id).first()
    if not session:
        return None

    # Only generate if title is still a default
    default_titles = {"New Session", "Standalone Session", "", None}
    if session.title not in default_titles:
        return None

    tender_title = None
    if session.tender_id:
        from app.models.tender import Tender
        tender_title = (
            db.query(Tender.title).filter(Tender.id == session.tender_id).scalar()
        )
    title = session_title_from(message, tender_title)

    if title:
        session.title = title
        db.commit()
        return title
    return None
