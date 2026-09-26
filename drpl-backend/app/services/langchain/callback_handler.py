"""
DRPL Backend - LangChain Callback Handler
Intercepts LangChain lifecycle events to log usage to the existing
APIUsageLog and AgentExecution tables for cost/token tracking.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from sqlalchemy.orm import Session

from app.services.ai_service import _log_usage, get_streaming_callback
from app.services.langchain.provider_config import estimate_cost, detect_provider

logger = logging.getLogger(__name__)


# Map raw tool names → short user-facing status messages. Anything not in
# the map falls back to "Using <tool_name>…" so new tools degrade gracefully.
_TOOL_STATUS_MESSAGES: dict[str, str] = {
    "web_search": "Searching the web",
    "web_search_tool": "Searching the web",
    "semantic_search": "Searching tender documents",
    "semantic_search_tool": "Searching tender documents",
    "document_reader": "Reading documents",
    "document_reader_tool": "Reading documents",
    "document_generator": "Generating document",
    "document_generator_tool": "Generating document",
    "cost_calculator": "Calculating costs",
    "cost_calculator_tool": "Calculating costs",
    "costing_training_retrieval": "Looking up costing examples",
    "costing_training_retrieval_tool": "Looking up costing examples",
    "memory": "Recalling related context",
    "memory_tool": "Recalling related context",
    "clarify": "Preparing clarification",
    "clarify_tool": "Preparing clarification",
}


def _humanize_tool(name: str) -> str:
    """What the user sees while this tool runs.

    Delegates to the shared vocabulary in `activity_labels` so the Master
    Agent's timeline and a specialist's progress line describe the same work
    the same way. The old local map covered 7 of 25 tools and derived the rest
    mechanically, which is how users ended up watching "Using anonymizing web
    search" and "Using call costing researcher".
    """
    from app.services.langchain.activity_labels import describe_tool

    if name in _TOOL_STATUS_MESSAGES:
        return _TOOL_STATUS_MESSAGES[name]
    return describe_tool(name)



def _usage_from_messages(response) -> tuple[dict, str]:
    """(usage, model) from the generations' messages, for streamed results.

    ``usage_metadata.input_tokens`` counts cache reads and writes too; the
    ledger prices fresh input, cache reads and cache writes separately, so
    they are split back out here.
    """
    usage: dict = {}
    model = ""
    try:
        for gen_list in response.generations or []:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue
                meta = getattr(msg, "response_metadata", None) or {}
                model = model or meta.get("model_name") or meta.get("model") or ""
                um = getattr(msg, "usage_metadata", None) or {}
                if not um:
                    continue
                details = um.get("input_token_details") or {}
                c_read = int(details.get("cache_read") or 0)
                c_write = int(details.get("cache_creation") or 0)
                total_in = int(um.get("input_tokens") or 0)
                usage["input_tokens"] = usage.get("input_tokens", 0) + max(0, total_in - c_read - c_write)
                usage["output_tokens"] = usage.get("output_tokens", 0) + int(um.get("output_tokens") or 0)
                usage["cache_read_input_tokens"] = usage.get("cache_read_input_tokens", 0) + c_read
                usage["cache_creation_input_tokens"] = usage.get("cache_creation_input_tokens", 0) + c_write
    except Exception:
        return {}, model
    return usage, model

class DRPLCallbackHandler(BaseCallbackHandler):
    """
    Custom LangChain callback handler that logs AI usage and tool calls
    to the DRPL database for monitoring and cost tracking.
    """

    def __init__(
        self,
        db: Session,
        agent_name: str = "langchain_agent",
        execution_id: Optional[int] = None,
        streaming_callback: Optional[Callable[[str, dict], None]] = None,
    ):
        super().__init__()
        self.db = db
        self.agent_name = agent_name
        self.execution_id = execution_id
        # When set, reliability events (truncation, empty) are surfaced to the
        # SSE pipeline so the frontend can render a "Continuing response..."
        # indicator. Best-effort — failures here never disturb the LLM call.
        self.streaming_callback = streaming_callback

        # Accumulate metrics across the entire agent run
        self.total_tokens_input = 0
        self.total_tokens_output = 0
        self.total_cost = 0.0
        # Anthropic prompt-cache telemetry — first-cache-write costs 1.25x
        # input price; subsequent cache reads cost ~0.1x. Tracking both lets
        # us verify caching is actually firing (cache_read > 0 means saving).
        self.total_cache_creation_tokens = 0
        self.total_cache_read_tokens = 0
        # max_tokens truncation tracker — when stop_reason="max_tokens", the
        # agent's response was cut off mid-stream. Surfaces silent truncation
        # bugs (the parser then falls back to needs_user_input + raw prose).
        self.max_tokens_hits = 0
        self.tool_calls: list[dict] = []
        self.errors: list[str] = []
        self._llm_start_times: dict[UUID, float] = {}
        self._tool_start_times: dict[UUID, float] = {}

    # --- LLM Callbacks ---

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        started = time.time()
        self._llm_start_times[run_id] = started
        self._emit_event("agent_status", {
            "agent_key": self.agent_name,
            "phase": "llm_thinking",
            "run_id": str(run_id),
            "message": "Thinking",
            "started_at_ms": int(started * 1000),
        })

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        elapsed_ms = int((time.time() - self._llm_start_times.pop(run_id, time.time())) * 1000)

        # Extract token usage + cache metrics from LLM response.
        usage = {}
        cache_creation = 0
        cache_read = 0
        if response.llm_output:
            token_usage = response.llm_output.get("token_usage") or response.llm_output.get("usage", {})
            usage = {
                "input_tokens": token_usage.get("prompt_tokens", 0) or token_usage.get("input_tokens", 0),
                "output_tokens": token_usage.get("completion_tokens", 0) or token_usage.get("output_tokens", 0),
            }
            # Anthropic-specific cache fields — present when prompt caching fires.
            cache_creation = (
                token_usage.get("cache_creation_input_tokens", 0)
                or token_usage.get("cache_creation_tokens", 0)
                or 0
            )
            cache_read = (
                token_usage.get("cache_read_input_tokens", 0)
                or token_usage.get("cache_read_tokens", 0)
                or 0
            )
            usage["cache_creation_input_tokens"] = cache_creation
            usage["cache_read_input_tokens"] = cache_read

        model = ""
        if response.llm_output:
            model = response.llm_output.get("model_name", "") or response.llm_output.get("model", "")

        # A streamed call (astream_events -- every general-assistant turn)
        # returns no llm_output: usage and model live only on the message.
        # Reading llm_output alone logged those runs as provider "unknown",
        # 0 tokens, $0, so the monthly budget never saw them.
        if not (usage.get("input_tokens") or usage.get("output_tokens")) or not model:
            m_usage, m_model = _usage_from_messages(response)
            if m_usage and not (usage.get("input_tokens") or usage.get("output_tokens")):
                usage = m_usage
                cache_creation = m_usage.get("cache_creation_input_tokens", 0)
                cache_read = m_usage.get("cache_read_input_tokens", 0)
            if m_model and not model:
                model = m_model

        self.total_tokens_input += usage.get("input_tokens", 0)
        self.total_tokens_output += usage.get("output_tokens", 0)
        self.total_cache_creation_tokens += cache_creation
        self.total_cache_read_tokens += cache_read

        cost = estimate_cost(model, usage)
        self.total_cost += cost

        # Detect truncation — Anthropic surfaces stop_reason in the per-generation
        # response_metadata when streaming. Capture across both LangChain shapes.
        stop_reason = ""
        try:
            for gen_list in response.generations or []:
                for gen in gen_list:
                    md = getattr(gen, "generation_info", None) or {}
                    sr = md.get("stop_reason") or md.get("finish_reason")
                    if sr:
                        stop_reason = str(sr)
                    # ChatGeneration has .message with response_metadata
                    msg = getattr(gen, "message", None)
                    if msg is not None:
                        meta = getattr(msg, "response_metadata", None) or {}
                        sr2 = meta.get("stop_reason") or meta.get("finish_reason")
                        if sr2:
                            stop_reason = str(sr2)
        except Exception:
            stop_reason = ""

        if stop_reason == "max_tokens":
            self.max_tokens_hits += 1
            logger.warning(
                f"[{self.agent_name}] LLM hit stop_reason=max_tokens — output was truncated "
                f"({usage.get('output_tokens', 0)} output tokens emitted). "
                f"Increase max_tokens or shorten the prompt to avoid silent truncation."
            )
            self._emit_event("assistant_truncated", {
                "agent": self.agent_name,
                "output_tokens": usage.get("output_tokens", 0),
                "hits": self.max_tokens_hits,
            })

        # Detect empty content. This is the failure mode that produces blank
        # costing/analysis outputs — model responds with content:[] and a
        # benign stop_reason ("end_turn", "stop_sequence"). Surfaced to the
        # frontend so users know something went wrong rather than seeing a
        # silently-truncated "..." response.
        if (
            stop_reason
            and stop_reason != "max_tokens"
            and usage.get("output_tokens", 0) == 0
        ):
            self._emit_event("assistant_empty_content", {
                "agent": self.agent_name,
                "stop_reason": stop_reason,
            })

        # Detect provider dynamically from model name
        provider = detect_provider(model) if model else "unknown"

        # Log each LLM call individually
        try:
            _log_usage(
                self.db,
                provider=provider,
                model=model,
                agent_name=self.agent_name,
                usage=usage,
                elapsed_ms=elapsed_ms,
                success=True,
            )
        except Exception as e:
            logger.warning(f"Failed to log LLM usage: {e}")

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self.errors.append(f"LLM error: {type(error).__name__}: {str(error)[:500]}")
        logger.error(
            f"LangChain LLM error for {self.agent_name}: {type(error).__name__}: {error!r}",
            exc_info=error,
        )

    # --- Tool Callbacks ---

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        started = time.time()
        self._tool_start_times[run_id] = started
        tool_name = serialized.get("name", "unknown")
        logger.info(f"Tool started: {tool_name} for agent {self.agent_name}")
        self._emit_event("agent_status", {
            "agent_key": self.agent_name,
            "phase": "tool_running",
            "run_id": str(run_id),
            "tool": tool_name,
            "message": _humanize_tool(tool_name),
            "started_at_ms": int(started * 1000),
        })

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        elapsed_ms = int((time.time() - self._tool_start_times.pop(run_id, time.time())) * 1000)
        self.tool_calls.append({
            "run_id": str(run_id),
            "output_preview": str(output)[:500],
            "latency_ms": elapsed_ms,
        })
        self._emit_event("agent_status", {
            "agent_key": self.agent_name,
            "phase": "tool_done",
            "run_id": str(run_id),
            "elapsed_ms": elapsed_ms,
        })

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self.errors.append(f"Tool error: {type(error).__name__}: {str(error)[:500]}")
        logger.error(
            f"LangChain tool error for {self.agent_name}: {type(error).__name__}: {error!r}",
            exc_info=error,
        )

    # --- Chain Callbacks ---

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self.errors.append(f"Chain error: {type(error).__name__}: {str(error)[:500]}")
        logger.error(
            f"LangChain chain error for {self.agent_name}: {type(error).__name__}: {error!r}",
            exc_info=error,
        )

    # --- Event emission ---

    def _emit_event(self, event_type: str, payload: dict) -> None:
        """Best-effort emission of reliability events to the SSE pipeline."""
        # Fall back to the ContextVar — most callers never set a callback
        # directly on the handler; the streaming SSE handler installs it via
        # ``set_streaming_callback`` before invoking the agent, and we read
        # it here transparently.
        cb = self.streaming_callback or get_streaming_callback()
        if not cb:
            return
        try:
            result = cb(event_type, payload)
            if asyncio.iscoroutine(result):
                # The SSE callback may be async (the run_tasks worker uses an
                # async dispatcher). Fire-and-forget so the LLM lifecycle isn't
                # blocked on UI state propagation.
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(result)
                    else:
                        loop.run_until_complete(result)
                except RuntimeError:
                    # No event loop in this thread — discard safely.
                    pass
        except Exception as e:
            logger.debug(f"streaming_callback failed for event {event_type}: {e}")

    # --- Summary ---

    def get_summary(self) -> dict:
        """Return accumulated metrics for the entire agent run."""
        return {
            "total_tokens_input": self.total_tokens_input,
            "total_tokens_output": self.total_tokens_output,
            "total_cost": round(self.total_cost, 6),
            "cache_creation_tokens": self.total_cache_creation_tokens,
            "cache_read_tokens": self.total_cache_read_tokens,
            "max_tokens_hits": self.max_tokens_hits,
            "tool_calls_count": len(self.tool_calls),
            "tool_calls": self.tool_calls,
            "errors": self.errors,
        }
