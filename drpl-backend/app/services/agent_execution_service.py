"""
DRPL Backend - Agent Execution Service
Runtime execution engine for custom agents.
Loads agent config, calls AI, logs executions, and provides analytics.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.agent_builder import CustomAgent, AgentExecution
from app.services.ai_service import call_ai, _get_effective_api_key, _get_effective_model, _log_usage, _estimate_cost

logger = logging.getLogger(__name__)


async def execute_agent(
    db: Session,
    agent_key_or_id,
    input_data: dict,
    user_id: Optional[int] = None,
    parent_execution_id: Optional[int] = None,
    session_id: Optional[str] = None,
    context_budget: Optional[int] = None,
) -> dict:
    """
    Execute a custom agent against the provided input data.

    Loads the agent configuration, constructs the prompt, calls the AI provider,
    logs the execution to AgentExecution, and returns the result.

    Args:
        db: Database session.
        agent_key_or_id: Either the agent_key (str) or agent id (int).
        input_data: Dict containing the user input / context for the agent.
        user_id: ID of the user triggering the execution.
        parent_execution_id: For multi-agent orchestration tracing.
        session_id: Conversation session id (e.g. router session). When set,
            the LangChain runner loads prior turns from
            AgentConversationHistory so multi-turn refinement works
            ("agent gives analysis → user asks for breakdown → agent
            produces line items"). Falls back to ``input_data["session_id"]``
            so callers can pass it via either path.

    Returns:
        Dict with keys: output, tokens_input, tokens_output, cost_estimate,
        latency_ms, execution_id, status.
    """
    # Allow callers to pass session_id either as a kwarg or inside input_data
    # (the chat_*_via_execute_agent shims use the latter convention).
    if session_id is None and isinstance(input_data, dict):
        session_id = input_data.get("session_id")
    # Same pattern for context_budget — chat wrappers (e.g.
    # chat_costing_research_via_execute_agent) cap injected training data
    # below the default 500K-char budget to keep total input under Claude's
    # 200K-token ceiling. See context_assembly_service.assemble_agent_context.
    if context_budget is None and isinstance(input_data, dict):
        context_budget = input_data.get("context_budget")
    # Resolve agent
    if isinstance(agent_key_or_id, int):
        agent = db.query(CustomAgent).filter(CustomAgent.id == agent_key_or_id).first()
    else:
        agent = db.query(CustomAgent).filter(CustomAgent.agent_key == str(agent_key_or_id)).first()

    if not agent:
        raise ValueError(f"Agent not found: {agent_key_or_id}")

    if not agent.is_enabled:
        raise ValueError(f"Agent '{agent.agent_key}' is disabled")

    # Auto-upgrade to react when tools are configured but the declared mode
    # would not bind them.
    #
    # `chain_of_thought` skips tool binding by design. `orchestrator` had no
    # execution path at all — it fell through to the simple single-call branch
    # below, so an orchestrator agent's tools were silently ignored. That was
    # not theoretical: doc-costing-analyst is an orchestrator with 15 tools
    # configured, none of which were ever reachable. An agent configured with
    # tools must be able to use them, whatever mode it declares.
    effective_type = agent.agent_type
    if effective_type in ("chain_of_thought", "orchestrator") and agent.tools:
        logger.info(
            f"Agent '{agent.agent_key}' has {len(agent.tools)} tools configured "
            f"but agent_type='{effective_type}' — upgrading to 'react' so they "
            f"are actually bound"
        )
        effective_type = "react"

    # Route to LangChain execution for react/tool_use agents.
    # ─────────────────────────────────────────────────────────────────────────
    # IMPORTANT: We do NOT create an AgentExecution row here for ReAct agents.
    # `execute_langchain_agent` creates its own row with trigger="langchain"
    # and properly updates it on completion / failure. Creating a duplicate
    # row here leaves orphaned status="running" rows in monitoring forever
    # (they'd never be updated because we'd return langchain_agent's result
    # without touching ours). The OpenAI Agents path is handled the same way.
    # ─────────────────────────────────────────────────────────────────────────
    if effective_type in ("react", "tool_use"):
        from app.services.langchain.langchain_execution_service import execute_langchain_agent
        # save_turn defaults to True; the chat_*_via_execute_agent shims
        # in streaming_handler already record the canonical user turn via
        # save_conversation_turn before invoking us, so duplicate saving
        # would inflate history. We let input_data signal that explicitly.
        save_turn = True
        if isinstance(input_data, dict) and input_data.get("_skip_save_turn"):
            save_turn = False
        return await execute_langchain_agent(
            db=db,
            agent=agent,
            input_data=input_data,
            user_id=user_id,
            parent_execution_id=parent_execution_id,
            session_id=session_id,
            save_turn=save_turn,
            context_budget=context_budget,
        )

    # Route to OpenAI Agents SDK for openai_agent type — also creates its
    # own execution row, so don't create one here.
    if agent.agent_type == "openai_agent":
        from app.services.openai_agents.execution_service import execute_openai_agent
        return await execute_openai_agent(
            db=db,
            agent=agent,
            input_data=input_data,
            user_id=user_id,
            parent_execution_id=parent_execution_id,
        )

    # Simple chain_of_thought path — this function owns the execution row
    # and is responsible for updating it. Create it now.
    execution = AgentExecution(
        agent_id=agent.id,
        agent_version=agent.current_version,
        trigger="api" if parent_execution_id else "manual",
        input_summary=json.dumps(input_data)[:2000] if input_data else None,
        status="running",
        parent_execution_id=parent_execution_id,
        user_id=user_id,
    )
    db.add(execution)
    db.flush()

    # Build user prompt first (needed for context assembly memory retrieval)
    user_prompt = _build_user_prompt(input_data)

    # Assemble context: training datasets + auto-retrieved memories + instructions
    from app.services.context_assembly_service import (
        assemble_agent_context,
        DEFAULT_CONTEXT_BUDGET,
    )
    _effective_context_budget = (
        context_budget if isinstance(context_budget, int) and context_budget > 0
        else DEFAULT_CONTEXT_BUDGET
    )
    ctx = assemble_agent_context(
        db=db,
        agent=agent,
        user_input=user_prompt,
        tender_id=input_data.get("tender_id") if isinstance(input_data, dict) else None,
        context_budget=_effective_context_budget,
    )
    system_prompt = ctx.enriched_system_prompt
    if ctx.memories_injected > 0:
        logger.info(f"Agent '{agent.agent_key}': injected {ctx.memories_injected} memories, {ctx.training_chars} training chars")

    start_time = time.time()
    finalized = False
    from app.services.agent_execution_finalizer import finalize_execution_row

    try:
        # call_ai reads CustomAgent fields via _get_agent_config and routes to
        # the correct provider (anthropic/openai/google) based on agent.provider.
        output_text = await call_ai(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            db=db,
            agent_name=agent.agent_key,
        )

        elapsed_ms = int((time.time() - start_time) * 1000)

        # Estimate tokens (rough heuristic when exact counts unavailable)
        tokens_input = _estimate_token_count(system_prompt + user_prompt)
        tokens_output = _estimate_token_count(output_text)
        model = agent.model or "claude-sonnet-5"
        cost = _estimate_cost(model, {
            "input_tokens": tokens_input,
            "output_tokens": tokens_output,
        })

        # Update execution record. Try the main session first (fast path);
        # fall back to a fresh session when the main session can't commit
        # so the row never sticks at status="running".
        execution.output_summary = output_text[:2000] if output_text else None
        execution.status = "completed"
        execution.tokens_input = tokens_input
        execution.tokens_output = tokens_output
        execution.latency_ms = elapsed_ms
        execution.cost_estimate = cost
        try:
            db.commit()
            db.refresh(execution)
            finalized = True
        except Exception as commit_err:
            logger.warning(
                f"Agent '{agent.agent_key}': main-session commit failed on "
                f"success path ({commit_err}) — finalizing via fresh session"
            )
            try:
                db.rollback()
            except Exception:
                pass
            finalized = finalize_execution_row(
                execution.id,
                status="completed",
                output_summary=output_text or "",
                latency_ms=elapsed_ms,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                cost_estimate=cost,
            )

        # Fire-and-forget: auto-extract learnings from this interaction
        if getattr(agent, 'learning_enabled', True) and output_text:
            try:
                import asyncio
                from app.services.learning_extraction_service import extract_and_store_learnings
                asyncio.create_task(extract_and_store_learnings(
                    agent_key=agent.agent_key,
                    user_input=user_prompt,
                    agent_output=output_text,
                    tender_id=input_data.get("tender_id") if isinstance(input_data, dict) else None,
                    execution_id=execution.id,
                ))
            except Exception as e:
                logger.debug(f"Learning extraction launch failed: {e}")

        return {
            "output": output_text,
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "cost_estimate": cost,
            "latency_ms": elapsed_ms,
            "execution_id": execution.id,
            "status": "completed",
        }

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(f"Agent execution failed for '{agent.agent_key}': {e}", exc_info=True)
        print(f"[DRPL ERROR] Agent execution failed for '{agent.agent_key}': {type(e).__name__}: {e}", flush=True)
        finalized = finalize_execution_row(
            execution.id,
            status="failed",
            error_message=str(e),
            latency_ms=elapsed_ms,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return {
            "output": None,
            "tokens_input": 0,
            "tokens_output": 0,
            "cost_estimate": 0.0,
            "latency_ms": elapsed_ms,
            "execution_id": execution.id,
            "status": "failed",
            "error": str(e),
        }
    finally:
        # Last-resort safety net for paths that bypassed both branches
        # (e.g. asyncio.CancelledError, which inherits from BaseException).
        if not finalized:
            try:
                finalize_execution_row(
                    execution.id,
                    status="cancelled",
                    error_message="Request cancelled before completion",
                    latency_ms=int((time.time() - start_time) * 1000),
                )
            except Exception:
                pass


def _build_user_prompt(input_data: dict) -> str:
    """Convert the input_data dict into a formatted user prompt string."""
    if not input_data:
        return ""
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
            # Truncate very large document text to stay within model context limits
            MAX_DOC_CHARS = 600_000  # ~150k tokens
            if len(doc_text) > MAX_DOC_CHARS:
                doc_text = doc_text[:MAX_DOC_CHARS] + \
                    f"\n\n[... Document truncated from {len(doc_text):,} to {MAX_DOC_CHARS:,} characters to fit context window ...]"
            if user_query:
                return f"{user_query}\n\n[UPLOADED DOCUMENTS]\n{doc_text}"
            return f"[UPLOADED DOCUMENTS]\n{doc_text}"

        if user_query is not None:
            return user_query

    # Fallback: JSON-serialize the full input for unrecognized shapes
    return json.dumps(input_data, indent=2, default=str)


def _estimate_token_count(text: str) -> int:
    """Rough token estimate: ~4 characters per token for English text."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# --- Query helpers ---

def get_execution(db: Session, execution_id: int) -> Optional[AgentExecution]:
    """Get a single execution record by ID."""
    return db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()


def get_agent_executions(db: Session, agent_id: int, limit: int = 50) -> list[AgentExecution]:
    """Get recent executions for an agent, newest first."""
    return (
        db.query(AgentExecution)
        .filter(AgentExecution.agent_id == agent_id)
        .order_by(AgentExecution.created_at.desc())
        .limit(limit)
        .all()
    )


def get_execution_stats(db: Session, agent_id: int) -> dict:
    """
    Aggregate execution statistics for an agent.

    Returns:
        Dict with total_executions, success_rate, avg_latency_ms,
        total_cost, total_tokens_input, total_tokens_output.
    """
    base = db.query(AgentExecution).filter(AgentExecution.agent_id == agent_id)

    total = base.count()
    if total == 0:
        return {
            "total_executions": 0,
            "success_rate": 0.0,
            "avg_latency_ms": 0,
            "total_cost": 0.0,
            "total_tokens_input": 0,
            "total_tokens_output": 0,
        }

    completed = base.filter(AgentExecution.status == "completed").count()

    agg = db.query(
        func.avg(AgentExecution.latency_ms),
        func.sum(AgentExecution.cost_estimate),
        func.sum(AgentExecution.tokens_input),
        func.sum(AgentExecution.tokens_output),
    ).filter(AgentExecution.agent_id == agent_id).first()

    return {
        "total_executions": total,
        "success_rate": round(completed / total, 4) if total > 0 else 0.0,
        "avg_latency_ms": int(agg[0] or 0),
        "total_cost": round(float(agg[1] or 0), 6),
        "total_tokens_input": int(agg[2] or 0),
        "total_tokens_output": int(agg[3] or 0),
    }
