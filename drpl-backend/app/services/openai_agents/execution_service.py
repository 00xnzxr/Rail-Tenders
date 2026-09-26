"""
DRPL - OpenAI Agents SDK Execution Service
Parallel execution engine to LangChain — uses the OpenAI Agents SDK
for agent orchestration with handoffs, guardrails, tracing, and hosted tools.

Agents with agent_type="openai_agent" route through this service.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Optional, AsyncGenerator

from sqlalchemy.orm import Session

from app.models.agent_builder import CustomAgent, AgentExecution, AgentTool
from app.services.langchain.provider_config import get_api_key, get_equivalent_model

logger = logging.getLogger(__name__)

# Default max turns to prevent infinite agent loops
DEFAULT_MAX_TURNS = 10


async def execute_openai_agent(
    db: Session,
    agent: CustomAgent,
    input_data: dict,
    user_id: Optional[int] = None,
    parent_execution_id: Optional[int] = None,
    session_id: Optional[str] = None,
) -> dict:
    """
    Execute an agent via the OpenAI Agents SDK.

    This is the advanced execution path for agents with agent_type="openai_agent".
    It builds an OpenAI SDK Agent with DRPL tool bridging, hosted tools,
    optional handoffs, guardrails, and structured output.

    Args:
        db: Database session
        agent: The CustomAgent record to execute
        input_data: Dict with user input (must contain 'message', 'query', 'text', or 'input')
        user_id: User triggering the execution
        parent_execution_id: For multi-agent orchestration tracing
        session_id: Optional conversation session ID for history

    Returns:
        Dict with output, metrics, tool_calls, execution_id, status
    """
    from agents import Agent, Runner, RunConfig, trace

    # Create execution record
    execution = AgentExecution(
        agent_id=agent.id,
        agent_version=agent.current_version,
        trigger="openai_agents",
        input_summary=json.dumps(input_data)[:2000],
        status="running",
        parent_execution_id=parent_execution_id,
        user_id=user_id,
    )
    db.add(execution)
    db.flush()

    start_time = time.time()
    sess_id = session_id or str(uuid.uuid4())
    finalized = False
    from app.services.agent_execution_finalizer import finalize_execution_row

    try:
        # Resolve OpenAI API key
        api_key = get_api_key(db, "openai")
        if not api_key:
            raise ValueError("OpenAI API key not configured. Set it in Platform Settings or .env")

        # Resolve model — use agent's model or map from Claude equivalent
        model = _resolve_openai_model(agent)

        # Extract user text
        user_text = _extract_user_text(input_data)

        # Run input guardrail before proceeding
        from app.services.openai_agents.guardrails import check_input_guardrail
        input_check = await check_input_guardrail(user_text)
        if not input_check["passed"]:
            elapsed_ms = int((time.time() - start_time) * 1000)
            finalized = finalize_execution_row(
                execution.id,
                status="blocked",
                error_message=f"Input guardrail: {input_check['reason']}",
                latency_ms=elapsed_ms,
            )
            return {
                "output": None,
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_estimate": 0.0,
                "latency_ms": elapsed_ms,
                "execution_id": execution.id,
                "session_id": sess_id,
                "tool_calls": [],
                "status": "blocked",
                "error": input_check["reason"],
            }

        # Assemble context (training datasets + memories + instructions)
        from app.services.context_assembly_service import assemble_agent_context
        ctx = assemble_agent_context(
            db=db,
            agent=agent,
            user_input=user_text,
            tender_id=input_data.get("tender_id") if isinstance(input_data, dict) else None,
        )
        system_prompt = ctx.enriched_system_prompt or "You are a helpful AI assistant."
        if ctx.memories_injected > 0:
            logger.info(
                f"Agent '{agent.agent_key}': injected {ctx.memories_injected} memories, "
                f"{ctx.training_chars} training chars"
            )

        # Build tools: bridge DRPL tools + hosted tools + MCP tools
        tools = _build_tools(db, agent)

        # Apply human-in-the-loop approval wrappers if configured
        orch_config = agent.orchestration_config or {}
        if isinstance(orch_config, dict):
            approval_tool_names = set(orch_config.get("requires_approval", []))
            if approval_tool_names and tools:
                from app.services.openai_agents.human_in_the_loop import build_approval_tool_wrapper
                tools = [
                    build_approval_tool_wrapper(
                        t, execution.id, agent.agent_key, user_id, approval_tool_names,
                    )
                    for t in tools
                ]

        # Build output type if structured output schema is defined
        output_type = None
        if agent.output_schema:
            from app.services.openai_agents.schema_utils import json_schema_to_pydantic
            try:
                output_type = json_schema_to_pydantic(agent.output_schema)
            except Exception as e:
                logger.warning(f"Failed to build output_type from schema: {e}")

        # Build handoff agents if orchestration_config has handoffs
        handoffs = _build_handoffs(db, agent)

        # Build guardrails from agent config
        from app.services.openai_agents.guardrails import build_guardrails_for_agent
        guardrails = build_guardrails_for_agent(agent)

        # Resolve model settings from agent config
        model_settings = _build_model_settings(agent)

        # Build the SDK Agent
        agent_kwargs = {
            "name": agent.display_name or agent.agent_key,
            "instructions": system_prompt,
            "model": model,
            "tools": tools,
            "model_settings": model_settings,
        }

        if output_type:
            agent_kwargs["output_type"] = output_type

        if handoffs:
            agent_kwargs["handoffs"] = handoffs

        # Attach guardrails
        if guardrails.get("input_guardrails"):
            agent_kwargs["input_guardrails"] = guardrails["input_guardrails"]
        if guardrails.get("output_guardrails"):
            agent_kwargs["output_guardrails"] = guardrails["output_guardrails"]

        sdk_agent = Agent(**agent_kwargs)

        # Resolve max_turns from langchain_config or default
        max_turns = _get_max_turns(agent)

        # Get conversation history for context + response chaining
        history_input, previous_response_id = _build_history_input(db, sess_id, user_text)
        if previous_response_id:
            logger.debug(f"Chaining to previous response: {previous_response_id}")

        # Save user turn
        from app.services.langchain.memory_service import save_conversation_turn
        save_conversation_turn(db, sess_id, agent.agent_key, "user", user_text)

        # Build run config
        run_config = _private_run_config(RunConfig, max_turns)

        # Run the agent with tracing
        with trace(agent.agent_key, disabled=True):
            result = await Runner.run(
                sdk_agent,
                input=history_input,
                max_turns=max_turns,
                run_config=run_config,
            )

        elapsed_ms = int((time.time() - start_time) * 1000)

        # Extract output
        if output_type and hasattr(result, "final_output_as"):
            try:
                final_output = result.final_output_as(output_type)
                output_text = json.dumps(final_output.model_dump(), indent=2, default=str)
            except Exception:
                output_text = str(result.final_output)
        else:
            output_text = str(result.final_output)

        # Run output guardrail
        from app.services.openai_agents.guardrails import check_output_guardrail
        output_check = await check_output_guardrail(output_text, agent.output_schema)
        guardrail_warnings = []
        if not output_check["passed"]:
            guardrail_warnings.append(output_check["reason"])
            logger.warning(f"Output guardrail warning for '{agent.agent_key}': {output_check['reason']}")

        # Extract tool calls from run result
        tool_call_details = _extract_tool_calls(result)

        # Extract token usage
        usage = _extract_usage(result)

        # Save assistant turn with response_id for chaining
        response_id = _get_last_response_id(result)
        save_conversation_turn(
            db, sess_id, agent.agent_key, "assistant", output_text,
            tool_calls=tool_call_details,
            metadata={"openai_response_id": response_id},
        )

        # Estimate cost
        from app.services.langchain.provider_config import estimate_cost
        cost = estimate_cost(model, usage)

        # Update execution record. Try main session first; fall back to a
        # fresh session if the main one can't commit so the row never
        # sticks at status="running".
        execution.output_summary = output_text[:2000]
        execution.status = "completed"
        execution.tokens_input = usage.get("input_tokens", 0)
        execution.tokens_output = usage.get("output_tokens", 0)
        execution.cost_estimate = cost
        execution.latency_ms = elapsed_ms
        _success_metadata = {
            "tool_calls": tool_call_details,
            "session_id": sess_id,
            "model": model,
            "engine": "openai_agents_sdk",
            "handoffs_used": len(handoffs) > 0,
            "max_turns": max_turns,
            "guardrail_warnings": guardrail_warnings,
            "openai_response_id": response_id,
        }
        execution.metadata_json = _success_metadata
        try:
            db.commit()
            db.refresh(execution)
            finalized = True
        except Exception as commit_err:
            logger.warning(
                f"OpenAI agent '{agent.agent_key}': main-session commit "
                f"failed on success path ({commit_err}) — finalizing via "
                f"fresh session"
            )
            try:
                db.rollback()
            except Exception:
                pass
            finalized = finalize_execution_row(
                execution.id,
                status="completed",
                output_summary=output_text,
                latency_ms=elapsed_ms,
                tokens_input=usage.get("input_tokens", 0),
                tokens_output=usage.get("output_tokens", 0),
                cost_estimate=cost,
                metadata_json=_success_metadata,
            )

        # Fire-and-forget: auto-extract learnings
        if getattr(agent, "learning_enabled", True) and output_text:
            try:
                from app.services.learning_extraction_service import extract_and_store_learnings
                asyncio.create_task(extract_and_store_learnings(
                    agent_key=agent.agent_key,
                    user_input=user_text,
                    agent_output=output_text,
                    tender_id=input_data.get("tender_id") if isinstance(input_data, dict) else None,
                    execution_id=execution.id,
                ))
            except Exception as e:
                logger.debug(f"Learning extraction launch failed: {e}")

        return {
            "output": output_text,
            "tokens_input": usage.get("input_tokens", 0),
            "tokens_output": usage.get("output_tokens", 0),
            "cost_estimate": cost,
            "latency_ms": elapsed_ms,
            "execution_id": execution.id,
            "session_id": sess_id,
            "tool_calls": tool_call_details,
            "status": "completed",
            "guardrail_warnings": guardrail_warnings,
        }

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(f"OpenAI agent execution failed for '{agent.agent_key}': {e}")
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
            "session_id": sess_id,
            "tool_calls": [],
            "status": "failed",
            "error": str(e),
        }
    finally:
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


async def stream_openai_agent(
    db: Session,
    agent: CustomAgent,
    input_data: dict,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    Stream an OpenAI agent execution as SSE events.

    Events emitted:
        - session: Session ID
        - agent_start: Agent execution beginning
        - token: Streamed content chunks
        - tool_call: Tool invocation details
        - agent_complete: Agent finished
        - done: Final metadata
        - error: Error details

    Yields:
        SSE-formatted strings (event: type\\ndata: {...}\\n\\n)
    """
    from agents import Agent, Runner, RunConfig, trace

    sess_id = session_id or str(uuid.uuid4())
    start_time = time.time()

    yield _sse_event("session", {"session_id": sess_id})

    # Create execution record
    execution = AgentExecution(
        agent_id=agent.id,
        agent_version=agent.current_version,
        trigger="openai_agents_stream",
        input_summary=json.dumps(input_data)[:2000],
        status="running",
        user_id=user_id,
    )
    db.add(execution)
    db.flush()

    finalized = False
    from app.services.agent_execution_finalizer import finalize_execution_row

    try:
        api_key = get_api_key(db, "openai")
        if not api_key:
            raise ValueError("OpenAI API key not configured")

        model = _resolve_openai_model(agent)
        user_text = _extract_user_text(input_data)

        # Input guardrail
        from app.services.openai_agents.guardrails import check_input_guardrail
        input_check = await check_input_guardrail(user_text)
        if not input_check["passed"]:
            yield _sse_event("error", {"message": input_check["reason"]})
            yield _sse_event("done", {"session_id": sess_id, "error": input_check["reason"]})
            finalized = finalize_execution_row(
                execution.id,
                status="blocked",
                error_message=input_check["reason"],
            )
            return

        # Context assembly
        from app.services.context_assembly_service import assemble_agent_context
        ctx = assemble_agent_context(
            db=db, agent=agent, user_input=user_text,
            tender_id=input_data.get("tender_id") if isinstance(input_data, dict) else None,
        )
        system_prompt = ctx.enriched_system_prompt or "You are a helpful AI assistant."

        # Build tools, handoffs, guardrails, settings
        tools = _build_tools(db, agent)
        handoffs = _build_handoffs(db, agent)
        model_settings = _build_model_settings(agent)
        from app.services.openai_agents.guardrails import build_guardrails_for_agent
        guardrails = build_guardrails_for_agent(agent)

        # Output type
        output_type = None
        if agent.output_schema:
            from app.services.openai_agents.schema_utils import json_schema_to_pydantic
            try:
                output_type = json_schema_to_pydantic(agent.output_schema)
            except Exception:
                pass

        # Build agent
        agent_kwargs = {
            "name": agent.display_name or agent.agent_key,
            "instructions": system_prompt,
            "model": model,
            "tools": tools,
            "model_settings": model_settings,
        }
        if output_type:
            agent_kwargs["output_type"] = output_type
        if handoffs:
            agent_kwargs["handoffs"] = handoffs
        if guardrails.get("input_guardrails"):
            agent_kwargs["input_guardrails"] = guardrails["input_guardrails"]
        if guardrails.get("output_guardrails"):
            agent_kwargs["output_guardrails"] = guardrails["output_guardrails"]

        sdk_agent = Agent(**agent_kwargs)
        max_turns = _get_max_turns(agent)
        history_input, previous_response_id = _build_history_input(db, sess_id, user_text)

        # Save user turn
        from app.services.langchain.memory_service import save_conversation_turn
        save_conversation_turn(db, sess_id, agent.agent_key, "user", user_text)

        yield _sse_event("agent_start", {
            "agent_key": agent.agent_key,
            "display_name": agent.display_name or agent.agent_key,
            "model": model,
        })

        run_config = _private_run_config(RunConfig, max_turns)

        # Run with streaming via Runner.run_streamed()
        with trace(agent.agent_key, disabled=True):
            streamed_result = Runner.run_streamed(
                sdk_agent,
                input=history_input,
                max_turns=max_turns,
                run_config=run_config,
            )

            full_output = ""
            tool_call_details = []

            async for event in streamed_result.stream_events():
                event_type = getattr(event, "type", "")

                if event_type == "raw_response_event":
                    # Extract streaming text delta
                    raw_data = getattr(event, "data", None)
                    if raw_data:
                        delta_type = getattr(raw_data, "type", "")
                        if delta_type == "response.output_text.delta":
                            delta = getattr(raw_data, "delta", "")
                            if delta:
                                full_output += delta
                                yield _sse_event("token", {"content": delta})

                elif event_type == "run_item_stream_event":
                    item = getattr(event, "item", None)
                    if item:
                        item_type = getattr(item, "type", "")
                        if "function_call" in item_type or "tool_call" in item_type:
                            tc = {
                                "tool": getattr(item, "name", "unknown"),
                                "input": getattr(item, "arguments", ""),
                            }
                            tool_call_details.append(tc)
                            yield _sse_event("tool_call", tc)
                        elif "function_call_output" in item_type:
                            output_val = getattr(item, "output", "")
                            if tool_call_details:
                                tool_call_details[-1]["output"] = str(output_val)[:500]

        elapsed_ms = int((time.time() - start_time) * 1000)

        # If no streamed text, get from final result
        if not full_output:
            final_result = streamed_result.result
            if final_result:
                full_output = str(final_result.final_output)

        # Extract usage from final result
        usage = {"input_tokens": 0, "output_tokens": 0}
        if hasattr(streamed_result, "result") and streamed_result.result:
            usage = _extract_usage(streamed_result.result)

        response_id = None
        if hasattr(streamed_result, "result") and streamed_result.result:
            response_id = _get_last_response_id(streamed_result.result)

        # Save assistant turn
        save_conversation_turn(
            db, sess_id, agent.agent_key, "assistant", full_output,
            tool_calls=tool_call_details,
            metadata={"openai_response_id": response_id},
        )

        # Update execution
        from app.services.langchain.provider_config import estimate_cost
        cost = estimate_cost(model, usage)

        execution.output_summary = full_output[:2000]
        execution.status = "completed"
        execution.tokens_input = usage.get("input_tokens", 0)
        execution.tokens_output = usage.get("output_tokens", 0)
        execution.cost_estimate = cost
        execution.latency_ms = elapsed_ms
        _stream_metadata = {
            "tool_calls": tool_call_details,
            "session_id": sess_id,
            "model": model,
            "engine": "openai_agents_sdk_stream",
            "openai_response_id": response_id,
        }
        execution.metadata_json = _stream_metadata
        try:
            db.commit()
            finalized = True
        except Exception as commit_err:
            logger.warning(
                f"OpenAI agent stream '{agent.agent_key}': main-session commit "
                f"failed on success path ({commit_err}) — finalizing via fresh session"
            )
            try:
                db.rollback()
            except Exception:
                pass
            finalized = finalize_execution_row(
                execution.id,
                status="completed",
                output_summary=full_output,
                latency_ms=elapsed_ms,
                tokens_input=usage.get("input_tokens", 0),
                tokens_output=usage.get("output_tokens", 0),
                cost_estimate=cost,
                metadata_json=_stream_metadata,
            )

        yield _sse_event("agent_complete", {
            "agent_key": agent.agent_key,
            "status": "completed",
        })

        yield _sse_event("done", {
            "session_id": sess_id,
            "execution_id": execution.id,
            "tokens_input": usage.get("input_tokens", 0),
            "tokens_output": usage.get("output_tokens", 0),
            "cost_estimate": cost,
            "latency_ms": elapsed_ms,
        })

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(f"OpenAI agent streaming failed for '{agent.agent_key}': {e}")
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
        yield _sse_event("error", {"message": str(e)})
        yield _sse_event("done", {"session_id": sess_id, "error": str(e)})
    finally:
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


# --- Helpers ---


def _sse_event(event_type: str, data: dict) -> str:
    """Format an SSE event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _resolve_openai_model(agent: CustomAgent) -> str:
    """Resolve the OpenAI model to use for this agent."""
    model = agent.model or "gpt-4o-mini"

    # If the model is already an OpenAI model, use it directly
    if model.startswith("gpt-") or model.startswith("o3") or model.startswith("o4"):
        return model

    # Map from Claude model to OpenAI equivalent
    return get_equivalent_model(model, "openai")


def _build_model_settings(agent: CustomAgent) -> dict:
    """Build ModelSettings dict from agent config."""
    from agents import ModelSettings

    temperature = agent.temperature if agent.temperature is not None else 0.7
    max_tokens = agent.max_tokens or 4096

    # store=False: the history is resent in full on every run (the response
    # id is read but never chained), so a stored response buys nothing and
    # leaves the tender text in OpenAI's response store.
    try:
        return ModelSettings(temperature=temperature, max_tokens=max_tokens, store=False)
    except TypeError:  # an SDK older than the store field
        return ModelSettings(temperature=temperature, max_tokens=max_tokens)


def _private_run_config(run_config_cls, max_turns: int):
    """A RunConfig that does not upload the run to OpenAI Traces.

    Tracing is on by default in the Agents SDK and sends every model input,
    output and tool result -- tender documents, company rates -- to the
    OpenAI traces dashboard. ``max_turns`` is a ``Runner.run`` argument, not
    a RunConfig field: passing it here raised TypeError on the SDK this
    service installs, so every openai_agent run failed before its first
    call. It is accepted and ignored here for the callers' symmetry.
    """
    try:
        return run_config_cls(tracing_disabled=True, trace_include_sensitive_data=False)
    except TypeError:  # an SDK older than these fields
        return run_config_cls()


def _get_max_turns(agent: CustomAgent) -> int:
    """Resolve max_turns from agent's langchain_config or default."""
    lc_config = agent.langchain_config or {}
    if isinstance(lc_config, str):
        try:
            lc_config = json.loads(lc_config)
        except (json.JSONDecodeError, TypeError):
            lc_config = {}
    if isinstance(lc_config, dict):
        return lc_config.get("max_iterations", DEFAULT_MAX_TURNS)
    return DEFAULT_MAX_TURNS


def _extract_user_text(input_data: dict) -> str:
    """Extract the user message from input_data dict."""
    if isinstance(input_data, dict):
        for key in ("message", "query", "text", "input"):
            if key in input_data:
                user_text = str(input_data[key])
                if "uploaded_documents" in input_data:
                    doc_text = str(input_data["uploaded_documents"])
                    return f"{user_text}\n\n[UPLOADED DOCUMENTS]\n{doc_text}"
                return user_text

        if "uploaded_documents" in input_data:
            return f"[UPLOADED DOCUMENTS]\n{input_data['uploaded_documents']}"

        return json.dumps(input_data, indent=2, default=str)
    return str(input_data)


def _build_tools(db: Session, agent: CustomAgent) -> list:
    """Build the combined tool list: bridged DRPL tools + hosted tools + MCP tools."""
    from app.services.langchain.tools.tool_loader import load_tools_for_agent
    from app.services.openai_agents.tool_bridge import bridge_drpl_tools
    from app.services.openai_agents.hosted_tools import is_hosted_tool, get_hosted_tools

    all_tools = []

    # Separate hosted tool keys from DRPL tool IDs
    hosted_keys = []
    tools_config = agent.tools or []

    for tool_entry in tools_config:
        tool_id = tool_entry.get("tool_id") if isinstance(tool_entry, dict) else tool_entry
        agent_tool = db.query(AgentTool).filter(AgentTool.id == tool_id).first()
        if agent_tool and is_hosted_tool(agent_tool.tool_key):
            hosted_keys.append(agent_tool.tool_key)

    # Load and bridge DRPL LangChain tools (excluding hosted tools)
    drpl_tools = load_tools_for_agent(db, agent)
    if drpl_tools:
        bridged = bridge_drpl_tools(drpl_tools)
        all_tools.extend(bridged)

    # Load MCP server tools if assigned
    if getattr(agent, "mcp_servers", None):
        try:
            from app.services.mcp.mcp_client import load_mcp_tools_for_agent as load_mcp_tools
            import asyncio
            # load_mcp_tools is async — get or create event loop
            try:
                loop = asyncio.get_running_loop()
                # We're in an async context but can't await here directly
                # Use a thread to run the async function
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    mcp_tools = pool.submit(
                        asyncio.run,
                        load_mcp_tools(db, server_names=agent.mcp_servers),
                    ).result()
            except RuntimeError:
                mcp_tools = asyncio.run(load_mcp_tools(db, server_names=agent.mcp_servers))

            if mcp_tools:
                mcp_bridged = bridge_drpl_tools(mcp_tools)
                all_tools.extend(mcp_bridged)
                logger.info(
                    f"Agent '{agent.agent_key}': loaded {len(mcp_tools)} MCP tools "
                    f"from {agent.mcp_servers}"
                )
        except Exception as e:
            logger.warning(f"Failed to load MCP tools for agent '{agent.agent_key}': {e}")

    # Add OpenAI hosted tools
    if hosted_keys:
        hosted = get_hosted_tools(hosted_keys)
        all_tools.extend(hosted)

    return all_tools


def _build_handoffs(db: Session, agent: CustomAgent) -> list:
    """Build handoff agents from orchestration_config."""
    from agents import Agent

    handoffs = []
    orch_config = agent.orchestration_config
    if not orch_config or not isinstance(orch_config, dict):
        return handoffs

    handoff_entries = orch_config.get("handoffs", [])
    if not handoff_entries:
        return handoffs

    for entry in handoff_entries:
        agent_key = entry.get("agent_key")
        if not agent_key:
            continue

        target = db.query(CustomAgent).filter(
            CustomAgent.agent_key == agent_key,
            CustomAgent.is_enabled == True,
        ).first()

        if not target:
            logger.warning(f"Handoff target agent '{agent_key}' not found or disabled")
            continue

        # Build a sub-agent for the handoff target
        target_model = _resolve_openai_model(target)
        target_tools = _build_tools(db, target)
        target_settings = _build_model_settings(target)

        # Assemble context for target agent
        from app.services.context_assembly_service import assemble_agent_context
        target_ctx = assemble_agent_context(db=db, agent=target, user_input="")
        target_prompt = target_ctx.enriched_system_prompt or target.system_prompt or ""

        target_sdk_agent = Agent(
            name=target.display_name or target.agent_key,
            instructions=target_prompt,
            model=target_model,
            tools=target_tools,
            model_settings=target_settings,
        )
        handoffs.append(target_sdk_agent)
        logger.debug(f"Added handoff target: {target.agent_key}")

    return handoffs


def _build_history_input(
    db: Session, session_id: str, user_text: str,
) -> tuple[str | list, Optional[str]]:
    """
    Build input with conversation history for the agent.

    Returns:
        Tuple of (input_for_runner, previous_response_id)
        - input_for_runner: either a string or list of message dicts
        - previous_response_id: OpenAI response ID for conversation chaining (if available)
    """
    from app.services.langchain.memory_service import get_conversation_history
    from app.services.openai_agents.responses_client import extract_response_id_from_metadata

    history = get_conversation_history(db, session_id, limit=20)
    if not history:
        return user_text, None

    # Look for the last response_id in conversation metadata
    previous_response_id = None
    for turn in reversed(history):
        if turn.role == "assistant":
            metadata = getattr(turn, "metadata_json", None) or {}
            prev_id = extract_response_id_from_metadata(metadata)
            if prev_id:
                previous_response_id = prev_id
                break

    # Build input as list of message dicts for the SDK
    messages = []
    for turn in history:
        if turn.role == "user":
            messages.append({"role": "user", "content": turn.content})
        elif turn.role == "assistant":
            messages.append({"role": "assistant", "content": turn.content})

    # Add the current user message
    messages.append({"role": "user", "content": user_text})
    return messages, previous_response_id


def _extract_tool_calls(result) -> list[dict]:
    """Extract tool call details from a RunResult."""
    tool_calls = []
    try:
        for item in result.new_items:
            item_type = getattr(item, "type", "")
            if "function_call" in item_type or "tool_call" in item_type:
                tool_calls.append({
                    "tool": getattr(item, "name", "unknown"),
                    "input": getattr(item, "arguments", ""),
                })
            elif "function_call_output" in item_type or "tool_output" in item_type:
                if tool_calls:
                    tool_calls[-1]["output"] = str(getattr(item, "output", ""))[:500]
    except Exception as e:
        logger.debug(f"Could not extract tool calls from result: {e}")
    return tool_calls


def _extract_usage(result) -> dict:
    """Extract token usage from RunResult."""
    usage = {"input_tokens": 0, "output_tokens": 0}
    try:
        if hasattr(result, "raw_responses") and result.raw_responses:
            for resp in result.raw_responses:
                resp_usage = getattr(resp, "usage", None)
                if resp_usage:
                    usage["input_tokens"] += getattr(resp_usage, "input_tokens", 0)
                    usage["output_tokens"] += getattr(resp_usage, "output_tokens", 0)
    except Exception as e:
        logger.debug(f"Could not extract usage from result: {e}")
    return usage


def _get_last_response_id(result) -> Optional[str]:
    """Get the last response ID for conversation chaining."""
    try:
        if hasattr(result, "raw_responses") and result.raw_responses:
            last = result.raw_responses[-1]
            return getattr(last, "id", None)
    except Exception:
        pass
    return None
