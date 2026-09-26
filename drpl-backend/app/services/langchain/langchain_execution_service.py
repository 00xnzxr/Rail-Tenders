"""
DRPL Backend - LangChain Execution Service
Advanced execution path for agents using LangChain with real tool calling.
Parallel to the existing agent_execution_service (which uses simple call_ai).

Agents with agent_type="react" or "tool_use" route through this service.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from sqlalchemy.orm import Session

from app.models.agent_builder import CustomAgent, AgentExecution
from app.services.langchain.llm_factory import get_chat_model_for_custom_agent
from app.services.langchain.callback_handler import DRPLCallbackHandler
from app.services.langchain.tools.tool_loader import load_tools_for_agent
from app.services.langchain.error_utils import is_transient_error, format_user_error
from app.services.langchain.memory_service import (
    save_conversation_turn,
    get_conversation_history,
)

logger = logging.getLogger(__name__)

# Shared helper — every agent path uses the same fresh-session finalizer to
# guarantee AgentExecution rows reach a terminal state even when the main
# request session has been poisoned by a tool's failed DB call.
from app.services.agent_execution_finalizer import finalize_execution_row as _finalize_execution_row


async def execute_langchain_agent(
    db: Session,
    agent: CustomAgent,
    input_data: dict,
    user_id: Optional[int] = None,
    parent_execution_id: Optional[int] = None,
    session_id: Optional[str] = None,
    save_turn: bool = True,
    context_budget: Optional[int] = None,
) -> dict:
    """
    Execute a custom agent via LangChain with real tool calling.

    This is the advanced execution path for agents with agent_type in
    ("react", "tool_use"). It creates a ReAct agent with the agent's
    configured tools and runs it with the LangChain runtime.

    Args:
        db: Database session
        agent: The CustomAgent record to execute
        input_data: Dict with the user input (must contain 'message' or 'text')
        user_id: User triggering the execution
        parent_execution_id: For multi-agent orchestration tracing
        session_id: Optional conversation session ID for history

    Returns:
        Dict with output, metrics, tool_calls, execution_id, status
    """
    # Create execution record
    execution = AgentExecution(
        agent_id=agent.id,
        agent_version=agent.current_version,
        trigger="langchain",
        input_summary=json.dumps(input_data)[:2000],
        status="running",
        parent_execution_id=parent_execution_id,
        user_id=user_id,
    )
    db.add(execution)
    db.flush()
    # Capture as a plain int immediately. After a long LLM call, the request
    # session may be poisoned by a dead Neon connection; reading `execution.id`
    # later would trigger an expired-attribute lazy-load on that session and
    # raise PendingRollbackError. The local int survives session poisoning and
    # is used by every subsequent reference (finalize calls, return payloads,
    # logging, etc.).
    execution_id = execution.id

    start_time = time.time()
    sess_id = session_id or str(uuid.uuid4())
    # Tracks whether the AgentExecution row has been moved out of "running"
    # by either the success or failure path. The `finally` block uses this
    # as a last-resort signal to mark the row as "cancelled" via a fresh
    # session — guards against asyncio.CancelledError + any new code path
    # that exits without finalizing.
    finalized = False

    try:
        # Create LLM
        llm = get_chat_model_for_custom_agent(db, agent)

        # Load tools. The router session goes with them: session-scoped tools
        # (`clarify`, `conversation_history_search`) are built with whatever is
        # passed here, and this path — the Master delegating to an
        # admin-registered agent that has no purpose-built wrapper — was
        # handing them nothing.
        tools = load_tools_for_agent(db, agent, router_session_id=sess_id)

        # Tools the Master Agent granted for this single call. Additive and
        # de-duplicated by name — a grant widens what this run can do without
        # touching the agent's saved configuration, so nothing leaks into the
        # next call. They pass through the same write gate as any other tool.
        extra_keys = []
        if isinstance(input_data, dict):
            extra_keys = input_data.get("_extra_tool_keys") or []
        if extra_keys:
            from app.services.langchain.tools.tool_loader import load_tools_by_keys

            have = {t.name for t in tools}
            for extra in load_tools_by_keys(
                db, list(extra_keys), agent_key=agent.agent_key
            ):
                if extra.name not in have:
                    tools.append(extra)
                    have.add(extra.name)
            logger.info(
                "Agent '%s' running with %d tools (%d granted for this call)",
                agent.agent_key, len(tools), len(extra_keys),
            )

        # Load MCP server tools if assigned
        if getattr(agent, 'mcp_servers', None):
            try:
                from app.services.mcp.mcp_client import load_mcp_tools_for_agent as load_mcp_tools
                mcp_tools = await load_mcp_tools(db, server_names=agent.mcp_servers)
                tools.extend(mcp_tools)
                if mcp_tools:
                    logger.info(f"Agent '{agent.agent_key}': loaded {len(mcp_tools)} MCP tools from {agent.mcp_servers}")
            except Exception as e:
                logger.warning(f"Failed to load MCP tools for agent '{agent.agent_key}': {e}")

        # Create callback handler
        callback = DRPLCallbackHandler(
            db=db,
            agent_name=agent.agent_key,
            execution_id=execution_id,
        )

        # Build ReAct agent
        from langgraph.prebuilt import create_react_agent

        # Extract user text early (needed for context assembly memory retrieval)
        user_text = _extract_user_text(input_data)

        # Assemble context: training datasets + auto-retrieved memories + instructions
        from app.services.context_assembly_service import (
            assemble_agent_context,
            DEFAULT_CONTEXT_BUDGET,
        )
        # Caller-supplied budget (e.g. chat costing path caps at 80K to stay
        # under Claude's 200K-token ceiling). Also accept it via input_data
        # for callers that route through execute_agent's input_data convention.
        _effective_context_budget = context_budget
        if _effective_context_budget is None and isinstance(input_data, dict):
            _effective_context_budget = input_data.get("context_budget")
        if not (
            isinstance(_effective_context_budget, int)
            and _effective_context_budget > 0
        ):
            _effective_context_budget = DEFAULT_CONTEXT_BUDGET
        ctx = assemble_agent_context(
            db=db,
            agent=agent,
            user_input=user_text,
            tender_id=input_data.get("tender_id") if isinstance(input_data, dict) else None,
            context_budget=_effective_context_budget,
        )
        system_prompt = ctx.enriched_system_prompt or "You are a helpful AI assistant."
        if ctx.memories_injected > 0:
            logger.info(f"Agent '{agent.agent_key}': injected {ctx.memories_injected} memories, {ctx.training_chars} training chars")

        # Structured output: build a separate chain for post-processing.
        # Do NOT wrap the LLM with with_structured_output() here — create_react_agent()
        # needs a raw BaseChatModel to call .bind_tools() internally.
        structured_output_model = None
        if agent.output_schema:
            try:
                from app.services.openai_agents.schema_utils import json_schema_to_pydantic
                structured_output_model = json_schema_to_pydantic(agent.output_schema)
                logger.debug(f"Agent '{agent.agent_key}': structured output will be applied post-execution")
            except Exception as e:
                logger.warning(f"Failed to build structured output model for '{agent.agent_key}': {e}")

        lc_config = agent.langchain_config or {}
        if isinstance(lc_config, str):
            try:
                lc_config = json.loads(lc_config)
            except (json.JSONDecodeError, TypeError):
                lc_config = {}
        max_iterations = lc_config.get("max_iterations", 10) if isinstance(lc_config, dict) else 10

        react_agent = create_react_agent(
            model=llm,
            tools=tools,
            prompt=SystemMessage(content=system_prompt),
        )

        # Get conversation history for context
        history_messages = []
        if session_id:
            history = get_conversation_history(db, session_id, limit=20)
            for turn in history:
                if turn.role == "user":
                    history_messages.append(HumanMessage(content=turn.content))
                elif turn.role == "assistant":
                    history_messages.append(AIMessage(content=turn.content))

        # Build input message (user_text already extracted above for context assembly)
        history_messages.append(HumanMessage(content=user_text))

        # Save user turn to conversation history (only when this service owns the
        # conversation; when called from a router/wrapper, set save_turn=False to
        # avoid duplicate rows — the router saves the canonical turn instead).
        if save_turn:
            save_conversation_turn(db, sess_id, agent.agent_key, "user", user_text)

        # Invoke agent (with retry-on-transient-error)
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                result = await react_agent.ainvoke(
                    {"messages": history_messages},
                    config={
                        "callbacks": [callback],
                        "recursion_limit": max_iterations * 2 + 5,
                    },
                )
                break  # success
            except Exception as invoke_err:
                if is_transient_error(invoke_err) and attempt < max_retries:
                    wait_seconds = 5 * (attempt + 1)  # 5s, then 10s
                    logger.warning(
                        f"Agent '{agent.agent_key}': transient error on attempt "
                        f"{attempt + 1}/{max_retries + 1}, retrying in {wait_seconds}s — {invoke_err}"
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                raise  # non-transient or retries exhausted → outer except block

        elapsed_ms = int((time.time() - start_time) * 1000)

        # Extract output — concatenate ALL AIMessage content across iterations.
        # A ReAct agent produces multiple AIMessages (one per iteration).
        # Each may contain analysis sections + tool calls. We must collect
        # all text content, not just the last message.
        messages = result.get("messages", [])
        final_message_parts = []
        tool_call_details = []

        for msg in messages:
            if isinstance(msg, AIMessage) and hasattr(msg, 'content') and msg.content:
                # content may be a string or a list of content blocks (multimodal)
                raw = msg.content
                if isinstance(raw, list):
                    # Extract text from content block dicts
                    text = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in raw
                    ).strip()
                else:
                    text = str(raw).strip()
                if text:
                    final_message_parts.append(text)
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_call_details.append({
                        "tool": tc.get("name", "unknown"),
                        "input": tc.get("args", {}),
                    })

        final_message = "\n\n".join(final_message_parts)

        # Strip any leaked CHECKLIST_JSON_START/END markers from output
        from app.services.langchain.graphs.chat_agent_wrappers import _strip_checklist_markers
        final_message = _strip_checklist_markers(final_message)

        # Post-process: apply structured output if schema was defined
        if structured_output_model and final_message:
            try:
                structured_llm = llm.with_structured_output(structured_output_model)
                structured_result = await structured_llm.ainvoke(
                    f"Convert the following into the required JSON schema:\n\n{final_message}"
                )
                if hasattr(structured_result, 'model_dump'):
                    import json as _json
                    final_message = _json.dumps(structured_result.model_dump(), indent=2)
                elif isinstance(structured_result, dict):
                    import json as _json
                    final_message = _json.dumps(structured_result, indent=2)
                else:
                    final_message = str(structured_result)
                logger.debug(f"Agent '{agent.agent_key}': structured output applied successfully")
            except Exception as e:
                logger.warning(f"Structured output post-processing failed for '{agent.agent_key}': {e}")

        # Save assistant turn (skipped when save_turn=False — the router owns it)
        if save_turn:
            save_conversation_turn(
                db, sess_id, agent.agent_key, "assistant", final_message,
                tool_calls=tool_call_details,
            )

        # Update execution record via a FRESH session — never touch the request
        # session after ainvoke. The request `db` is unsafe at this point for
        # two reasons:
        #   1. Long-running ainvoke (5+ min) can leave the connection dead even
        #      with TCP keepalives, because Neon's PgBouncer drops server-side
        #      idle connections regardless of socket-level keepalives.
        #   2. Any tool that called `self.db.rollback()` during ainvoke (e.g.
        #      after a transient query failure) has expired all ORM attributes
        #      on session-attached objects. A subsequent `db.commit()` would
        #      trigger `get_history` → expired-attribute load → SELECT on the
        #      potentially-dead connection → fail.
        # `_finalize_execution_row` opens its own SessionLocal with a freshly
        # checked-out (pre-pinged) connection, so the UPDATE always lands.
        metrics = callback.get_summary()
        finalized = _finalize_execution_row(
            execution_id,
            status="completed",
            output_summary=final_message,
            latency_ms=elapsed_ms,
            tokens_input=metrics["total_tokens_input"],
            tokens_output=metrics["total_tokens_output"],
            cost_estimate=metrics["total_cost"],
            metadata_json={
                "tool_calls": tool_call_details,
                "session_id": sess_id,
                "errors": metrics.get("errors", []),
            },
        )
        # Discard any pending state on the request session so the caller
        # (streaming_handler / router) gets a clean session for its own writes.
        try:
            db.rollback()
        except Exception:
            pass

        # Fire-and-forget: auto-extract learnings from this interaction
        if getattr(agent, 'learning_enabled', True) and final_message:
            try:
                from app.services.learning_extraction_service import extract_and_store_learnings
                asyncio.create_task(extract_and_store_learnings(
                    agent_key=agent.agent_key,
                    user_input=user_text,
                    agent_output=final_message,
                    tender_id=input_data.get("tender_id") if isinstance(input_data, dict) else None,
                    execution_id=execution_id,
                ))
            except Exception as e:
                logger.debug(f"Learning extraction launch failed: {e}")

        return {
            "output": final_message,
            "tokens_input": metrics["total_tokens_input"],
            "tokens_output": metrics["total_tokens_output"],
            "cost_estimate": metrics["total_cost"],
            "latency_ms": elapsed_ms,
            "execution_id": execution_id,
            "session_id": sess_id,
            "tool_calls": tool_call_details,
            "status": "completed",
        }

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(f"LangChain agent execution failed for '{agent.agent_key}': {e}", exc_info=True)
        # Use a fresh session for the row update — the main `db` may be in
        # PendingRollbackError if the failure originated in a tool's DB call,
        # in which case the older try/commit/reset-to-running dance left the
        # row stuck at status="running" forever. The fresh session has its
        # own connection so the UPDATE always lands.
        finalized = _finalize_execution_row(
            execution_id,
            status="failed",
            error_message=str(e),
            latency_ms=elapsed_ms,
        )
        try:
            db.rollback()
        except Exception:
            pass
        user_message = format_user_error(e)
        return {
            "output": user_message,
            "tokens_input": 0,
            "tokens_output": 0,
            "cost_estimate": 0.0,
            "latency_ms": elapsed_ms,
            "execution_id": execution_id,
            "session_id": sess_id,
            "tool_calls": [],
            "status": "failed",
            "error": str(e),  # raw error kept for internal logging/debugging
        }
    finally:
        # Last-resort safety net for paths that bypassed both branches —
        # e.g. asyncio.CancelledError (BaseException, not caught by `except
        # Exception`), or a failure between the success path's metric build
        # and its commit. `finalized` is set True by both happy paths above
        # once the row is in a terminal state; if it's still False here, the
        # row is still "running" and we mark it "cancelled" via a fresh
        # session.
        try:
            if not finalized:
                _finalize_execution_row(
                    execution_id,
                    status="cancelled",
                    error_message=(
                        "Request cancelled before completion "
                        "(likely client disconnect or worker timeout)"
                    ),
                    latency_ms=int((time.time() - start_time) * 1000),
                )
                logger.warning(
                    f"LangChain agent '{agent.agent_key}': execution "
                    f"{execution_id} cancelled mid-run — marked status=cancelled"
                )
        except Exception as cleanup_err:
            logger.warning(
                f"Failed to mark execution {execution_id} "
                f"as cancelled: {cleanup_err}"
            )


async def run_tender_pipeline(
    db: Session,
    tender_id: int,
    user_id: Optional[int] = None,
    letterhead_template_id: Optional[int] = None,
    signature_ids: Optional[list[int]] = None,
    steps: Optional[list[str]] = None,
) -> dict:
    """
    Run the full multi-step tender processing pipeline.

    Steps (all run by default):
      1. analyze_documents — Deep document analysis
      2. generate_checklist — Extract required documents
      3. generate_documents — Generate each document
      4. research_costing — Cost estimation and research

    Args:
        db: Database session
        tender_id: Tender to process
        user_id: User triggering the pipeline
        letterhead_template_id: Apply this letterhead to generated docs
        signature_ids: Apply these signatures to generated docs
        steps: Optional subset of steps to run

    Returns:
        Complete pipeline state with results from each step
    """
    from app.services.langchain.graphs.tender_pipeline_graph import (
        build_tender_pipeline,
        TenderPipelineState,
    )

    # Create parent execution record for the pipeline
    execution = AgentExecution(
        agent_id=0,  # Pipeline doesn't have a single agent
        trigger="pipeline",
        input_summary=json.dumps({"tender_id": tender_id, "steps": steps})[:2000],
        status="running",
        user_id=user_id,
    )
    db.add(execution)
    db.flush()
    # Capture as plain int — survives a poisoned session (see execute_langchain_agent).
    execution_id = execution.id

    start_time = time.time()
    finalized = False

    try:
        pipeline = build_tender_pipeline(db)

        initial_state: TenderPipelineState = {
            "tender_id": tender_id,
            "documents_text": {},
            "analysis_result": {},
            "negative_keywords": [],
            "checklist_items": [],
            "generated_documents": [],
            "costing_data": {},
            "execution_id": execution_id,
            "letterhead_template_id": letterhead_template_id,
            "signature_ids": signature_ids or [],
            "user_id": user_id,
            "current_step": "initializing",
            "errors": [],
            "completed_steps": [],
        }

        # Run the pipeline
        final_state = await pipeline.ainvoke(initial_state)

        elapsed_ms = int((time.time() - start_time) * 1000)

        # Update execution record
        _output_summary = json.dumps({
            "completed_steps": final_state.get("completed_steps", []),
            "checklist_count": len(final_state.get("checklist_items", [])),
            "docs_generated": len(final_state.get("generated_documents", [])),
            "errors_count": len(final_state.get("errors", [])),
        })
        _pipeline_metadata = {
            "pipeline_steps": final_state.get("completed_steps", []),
            "errors": final_state.get("errors", []),
        }
        # Same rationale as execute_langchain_agent: never commit on the
        # request session after a long-running ainvoke. Always go through a
        # fresh SessionLocal so a dropped Neon/PgBouncer connection or
        # session-expired ORM attributes can't strand the row at "running".
        finalized = _finalize_execution_row(
            execution_id,
            status="completed",
            output_summary=_output_summary,
            latency_ms=elapsed_ms,
            metadata_json=_pipeline_metadata,
        )
        try:
            db.rollback()
        except Exception:
            pass

        return {
            "tender_id": tender_id,
            "execution_id": execution_id,
            "status": "completed" if not final_state.get("errors") else "completed_with_errors",
            "completed_steps": final_state.get("completed_steps", []),
            "analysis_result": final_state.get("analysis_result", {}),
            "negative_keywords": final_state.get("negative_keywords", []),
            "checklist_items": final_state.get("checklist_items", []),
            "generated_documents": final_state.get("generated_documents", []),
            "costing_data": final_state.get("costing_data", {}),
            "errors": final_state.get("errors", []),
            "latency_ms": elapsed_ms,
        }

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(f"Tender pipeline failed for tender {tender_id}: {e}")
        finalized = _finalize_execution_row(
            execution_id,
            status="failed",
            error_message=str(e),
            latency_ms=elapsed_ms,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return {
            "tender_id": tender_id,
            "execution_id": execution_id,
            "status": "failed",
            "error": str(e),
            "latency_ms": elapsed_ms,
        }
    finally:
        if not finalized:
            try:
                _finalize_execution_row(
                    execution_id,
                    status="cancelled",
                    error_message="Pipeline cancelled before completion",
                    latency_ms=int((time.time() - start_time) * 1000),
                )
            except Exception:
                pass


def _extract_user_text(input_data: dict) -> str:
    """Extract the user message from input_data dict."""
    if isinstance(input_data, dict):
        # Resolve the user's query text from common key names
        user_query = None
        if "message" in input_data:
            user_query = str(input_data["message"])
        elif "query" in input_data:
            user_query = str(input_data["query"])
        elif "text" in input_data:
            user_query = str(input_data["text"])
        elif "input" in input_data:
            user_query = str(input_data["input"])

        # Append uploaded document text when present
        if "uploaded_documents" in input_data:
            doc_text = str(input_data["uploaded_documents"])
            if user_query:
                return f"{user_query}\n\n[UPLOADED DOCUMENTS]\n{doc_text}"
            return f"[UPLOADED DOCUMENTS]\n{doc_text}"

        if user_query is not None:
            return user_query

        return json.dumps(input_data, indent=2, default=str)
    return str(input_data)
