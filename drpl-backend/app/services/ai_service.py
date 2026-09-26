"""
DRPL Backend - AI Intelligence Engine
Four AI agents: Analyst (classify), Relevance, Risk, Summary
Uses Anthropic Claude API for tender analysis.
Reads per-agent configuration from AgentConfig table.
Logs API usage to APIUsageLog table.
"""

import asyncio
import base64
import contextlib
import contextvars
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import httpx
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.config import get_settings
from app.models.tender import Tender
from app.models.agent_config import AgentConfig
from app.models.platform_setting import PlatformSetting
from app.models.api_usage import APIUsageLog
from app.services.langchain.model_limits import clamp_max_tokens

logger = logging.getLogger(__name__)
settings = get_settings()


# --- Reliability Wrapper Constants ---

# Maximum number of automatic continuations when stop_reason="max_tokens" is hit.
# Each continuation re-prompts the model to resume seamlessly. Prompt caching on
# the system prompt + document blocks makes this cheap (~90% input discount).
_MAX_CONTINUATIONS = 3

# Nudge sent when the model returns content:[] with a non-max_tokens stop_reason.
# This is the failure mode that produces blank costing/analysis outputs even
# when the same prompt works on claude.ai.
_EMPTY_RETRY_NUDGE = (
    "Your previous response was empty. Please respond now with the requested "
    "output. If anything in the request is unclear, make your best effort and "
    "answer in full."
)

# Continuation prompt — instructs the model to resume from where it stopped
# without re-emitting earlier content.
_CONTINUATION_NUDGE = (
    "Your previous response was cut off mid-output. Continue exactly from where "
    "you stopped. Do NOT repeat any content. Pick up at the next character or "
    "section and finish the response."
)

# Sentinel substrings used by _extract_text_from_response when the model burned
# its entire token budget on extended thinking. We use this to trigger a
# retry-without-thinking instead of a useless continuation.
_THINKING_BURNED_TOKENS_SENTINEL = "all available tokens for internal reasoning"


def _emit_reliability_event(
    streaming_callback: Optional[Callable[[str, dict], None]],
    event_type: str,
    payload: dict,
) -> None:
    """Best-effort emission of reliability events to the SSE pipeline.

    The callback is optional; if anything goes wrong we swallow the error
    rather than disturbing the underlying API call.
    """
    cb = streaming_callback or _streaming_callback_var.get()
    if not cb:
        return
    try:
        result = cb(event_type, payload)
        if asyncio.iscoroutine(result):
            # Fire-and-forget — we don't block the HTTP path on UI updates.
            asyncio.create_task(result)
    except Exception as e:
        logger.debug(f"streaming_callback failed for event {event_type}: {e}")


# --- ContextVar-based streaming callback ---
#
# The reliability events (continuing / resumed / retried_empty / etc.) are
# emitted from deep inside the call stack (`_call_anthropic`, `safe_ainvoke`,
# `DRPLCallbackHandler.on_llm_end`). Threading a `streaming_callback` parameter
# through every chat_* wrapper, run_*_node, and graph node would require ~30
# function-signature changes. Instead we put the callback in a ContextVar:
# the streaming SSE handler sets it once before invoking the agent, and any
# inner call that emits a reliability event reads it transparently.
#
# ContextVars propagate to tasks spawned via ``asyncio.create_task`` (which
# snapshots the current context at task creation), so parallel sub-agents
# inherit the same callback. Resetting on exit prevents leaks across requests.
_streaming_callback_var: contextvars.ContextVar[
    Optional[Callable[[str, dict], None]]
] = contextvars.ContextVar("drpl_streaming_callback", default=None)


@contextlib.contextmanager
def set_streaming_callback(cb: Optional[Callable[[str, dict], None]]):
    """Context manager that installs a streaming_callback for the duration of
    the ``with`` block.

    Usage::

        with set_streaming_callback(my_cb):
            await chat_costing_research(...)
            # all _call_anthropic / safe_ainvoke / DRPLCallbackHandler events
            # bubble up through my_cb without changing any function signature

    Implementation note — DO NOT use ``Token.reset()`` here. The streaming
    handler is an async generator (SSE) that yields control between
    ``await`` boundaries; asyncio can resume the coroutine in a different
    ``Context`` than where the with-block was entered. ``Token.reset()``
    raises ``ValueError: Token ... was created in a different Context``
    in that case, which masks any successful agent completion as a
    user-visible "Something went wrong" error.

    Save-and-restore via ``set()`` doesn't have that constraint. Tokens
    leak slightly (Python may warn at interpreter exit), but the leak is
    bounded by request count and harmless within a process lifetime.
    """
    previous = _streaming_callback_var.get()
    _streaming_callback_var.set(cb)
    try:
        yield
    finally:
        try:
            _streaming_callback_var.set(previous)
        except Exception:
            # Defensive — even set() can theoretically raise if the
            # interpreter is shutting down. Don't let cleanup hide a
            # real exception from the body of the with-block.
            pass


def get_streaming_callback() -> Optional[Callable[[str, dict], None]]:
    """Return the currently-installed streaming_callback (or None)."""
    return _streaming_callback_var.get()


# --- Config Helpers ---

def _get_agent_config(db: Session, agent_name: str) -> dict:
    """Get per-agent configuration from database.

    Resolution cascade (Phase 3c — fixed disconnect between Agent Builder UI
    and runtime):

      1. **CustomAgent table** (source of truth — what the Admin → Agent
         Builder UI edits). System agents like `costing_researcher`,
         `tender_doc_analyzer`, `checklist_generator`, `annexure_finder`,
         `decision_maker`, `proposal_router` etc. all live here.
      2. **AgentConfig table** (legacy — used by the original four analysis
         agents: classifier, relevance, risk, summary, eligibility, checklist,
         proposal, document_analyzer). Kept for backward compatibility.
      3. **Defaults** (model=None, max_tokens=1024 etc.) — caller will
         resolve via _get_effective_model → PlatformSetting `ai_model`.

    Before this fix, the runtime ONLY read AgentConfig, so editing a system
    agent's model in Agent Builder did nothing — the runtime silently fell
    back to the platform `ai_model` setting (which still held an older value
    like claude-sonnet-4-5-20250929 from a prior release, even though the
    Agent Builder UI showed claude-sonnet-4-6).
    """
    # Step 1 — CustomAgent (Agent Builder UI source of truth)
    try:
        from app.models.agent_builder import CustomAgent
        custom = db.query(CustomAgent).filter(CustomAgent.agent_key == agent_name).first()
    except Exception as e:
        # Defensive: if CustomAgent table is missing for some reason (very
        # early bootstrap, broken migration), fall through to AgentConfig.
        logger.debug(f"CustomAgent lookup failed for '{agent_name}' (non-fatal): {e}")
        custom = None

    if custom is not None:
        # CustomAgent.langchain_config can stash thinking_mode / thinking_budget_tokens / effort
        lc_config = custom.langchain_config or {}
        if isinstance(lc_config, str):
            try:
                lc_config = json.loads(lc_config)
            except (json.JSONDecodeError, TypeError):
                lc_config = {}
        if not isinstance(lc_config, dict):
            lc_config = {}
        result = {
            "model": custom.model,
            "provider": getattr(custom, "provider", None),
            "temperature": custom.temperature if custom.temperature is not None else 0.7,
            "max_tokens": custom.max_tokens or 4096,
            "system_prompt_override": None,  # CustomAgent.system_prompt is the prompt itself; not an override
            "is_enabled": getattr(custom, "is_enabled", True),
            "thinking_mode": (
                getattr(custom, "thinking_mode", None)
                or lc_config.get("thinking_mode")
            ),
            "thinking_budget_tokens": (
                getattr(custom, "thinking_budget_tokens", None)
                or lc_config.get("thinking_budget_tokens")
            ),
            "effort": getattr(custom, "effort", None) or lc_config.get("effort"),
            "_source": "custom_agent",  # diagnostic — surfaces in logs
        }
        _apply_force_provider_override(db, result)
        return result

    # Step 2 — AgentConfig (legacy)
    config = db.query(AgentConfig).filter(AgentConfig.agent_name == agent_name).first()
    if config:
        result = {
            "model": config.ai_model,
            "provider": config.ai_provider,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "system_prompt_override": config.system_prompt_override,
            "is_enabled": config.is_enabled,
            "thinking_mode": getattr(config, "thinking_mode", None),
            "thinking_budget_tokens": getattr(config, "thinking_budget_tokens", None),
            "effort": getattr(config, "effort", None),
            "_source": "agent_config",
        }
    else:
        # Step 3 — Defaults (caller resolves model via _get_effective_model)
        result = {
            "model": None, "provider": None, "temperature": 0.7, "max_tokens": 1024,
            "system_prompt_override": None, "is_enabled": True,
            "thinking_mode": None, "thinking_budget_tokens": None, "effort": None,
            "_source": "defaults",
        }
    _apply_force_provider_override(db, result)
    return result


def _apply_force_provider_override(db: Optional[Session], cfg: dict) -> None:
    """Apply the platform-wide ``force_provider_override`` kill-switch in place.

    When set to a specific provider (anthropic/openai/google), every agent is
    routed through that provider regardless of its per-agent setting. The
    paired model field is cleared so downstream model resolution falls back to
    the platform default for the forced provider — otherwise we'd send a
    Claude model to OpenAI and get a 404.
    """
    if not db:
        return
    try:
        from app.services.settings_service import get_effective_setting
        forced = get_effective_setting(db, "force_provider_override", "auto")
        if not forced or str(forced).lower() in ("auto", "", "none"):
            return
        forced = str(forced).lower()
        if forced not in ("anthropic", "openai", "google"):
            return
        if cfg.get("provider") != forced:
            cfg["provider"] = forced
            # Clear the model so build_fallback_chain picks the right default
            # for the forced provider via DEFAULT_FALLBACK_MODELS.
            cfg["model"] = None
            cfg["_force_override"] = forced
    except Exception:
        # Non-fatal — if the setting machinery fails, fall through to per-agent config
        pass


# --- Thinking & Effort Helpers ---

# Per-model thinking/effort capabilities. Verified 2026-09-02.
#
# Getting these wrong is not a soft failure. A model missing from
# ADAPTIVE_THINKING_MODELS falls through to thinking-disabled, and on the
# Opus 5 family disabled thinking makes the model occasionally write a tool
# call into its visible text instead of emitting a tool_use block — the call
# never runs, no error is raised, and in an agentic loop that text pollutes
# every later turn. Sending budget_tokens to a model that dropped it is a
# hard 400.

# Adaptive thinking — {"type": "adaptive"}, no budget.
ADAPTIVE_THINKING_MODELS = {
    "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
}

# Manual extended thinking — {"type": "enabled", "budget_tokens": N}.
# budget_tokens is REMOVED (400) on Fable 5, Opus 5, Sonnet 5, Opus 4.8 and
# Opus 4.7; it survives on Opus 4.6 / Sonnet 4.6 only as a transitional escape
# hatch. Never add a current model to this set.
EXTENDED_THINKING_MODELS = {
    "claude-opus-4-6", "claude-sonnet-4-6",
    "claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929",
    "claude-opus-4-1-20250805",
    "claude-sonnet-4-20250514", "claude-opus-4-20250514",
    "claude-haiku-4-5-20251001", "claude-haiku-4-5",
}

# output_config.effort
EFFORT_SUPPORTED_MODELS = {
    "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
    "claude-opus-4-5-20251101",
}

# Valid effort levels. "xhigh" arrived with Opus 4.7 and sits between high and max.
VALID_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}

# Models where 'xhigh' / 'max' effort are accepted.
MAX_EFFORT_MODELS = {
    "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
}

# Models that REJECT temperature / top_p / top_k with a 400. Sampling params
# were removed across the 4.6+ family; passing one through is a hard failure,
# not a silently-ignored field.
NO_SAMPLING_PARAM_MODELS = {
    "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
    # The gpt-5.6 family rejects it the same way: "Unsupported parameter:
    # 'temperature' is not supported with this model." The set was
    # Anthropic-only until eleven agents were rostered onto Luna, at which
    # point every one of their calls would have 400'd.
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
}


def supports_sampling_params(model: str) -> bool:
    """False when temperature/top_p/top_k must be omitted from the request."""
    return model not in NO_SAMPLING_PARAM_MODELS


def _resolve_thinking_config(model: str, agent_config: dict, db: Session = None) -> Optional[dict]:
    """
    Resolve the thinking configuration for an API call.

    Returns a dict like {"type": "adaptive"} or {"type": "enabled", "budget_tokens": N}
    or None if thinking should be disabled.
    """
    from app.services.settings_service import get_effective_setting

    # Get thinking mode: per-agent → platform setting → default
    thinking_mode = agent_config.get("thinking_mode")
    if not thinking_mode and db:
        thinking_mode = get_effective_setting(db, "thinking_mode", "auto")
    thinking_mode = thinking_mode or "auto"

    if thinking_mode == "disabled":
        return None

    if thinking_mode == "adaptive":
        if model in ADAPTIVE_THINKING_MODELS:
            return {"type": "adaptive"}
        # Fallback: adaptive not supported, try enabled with budget
        budget = agent_config.get("thinking_budget_tokens")
        if not budget and db:
            budget = get_effective_setting(db, "thinking_budget_tokens", 10000)
        budget = int(budget or 10000)
        if model in EXTENDED_THINKING_MODELS:
            return {"type": "enabled", "budget_tokens": budget}
        return None

    if thinking_mode == "enabled":
        budget = agent_config.get("thinking_budget_tokens")
        if not budget and db:
            budget = get_effective_setting(db, "thinking_budget_tokens", 10000)
        budget = int(budget or 10000)
        if model in EXTENDED_THINKING_MODELS:
            return {"type": "enabled", "budget_tokens": budget}
        return None

    # "auto" mode: use adaptive for 4.6 models, disabled for others
    if model in ADAPTIVE_THINKING_MODELS:
        return {"type": "adaptive"}
    return None


def _resolve_effort(model: str, agent_config: dict, db: Session = None) -> Optional[str]:
    """
    Resolve the effort level for an API call.

    Returns "low", "medium", "high", "max", or None.
    """
    from app.services.settings_service import get_effective_setting

    effort = agent_config.get("effort")
    if not effort and db:
        effort = get_effective_setting(db, "effort_level", None)

    if not effort or effort not in VALID_EFFORT_LEVELS:
        return None  # Use API default (high)

    # 'max' only works on Opus 4.6
    if effort == "max" and model not in MAX_EFFORT_MODELS:
        return "high"

    if model not in EFFORT_SUPPORTED_MODELS:
        return None  # Effort not supported on this model

    return effort


def _extract_text_from_response(data: dict) -> str:
    """
    Extract text from a Claude API response, handling thinking blocks.

    With extended thinking enabled, response.content may contain:
    - {"type": "thinking", "thinking": "...", "signature": "..."}
    - {"type": "text", "text": "..."}

    Returns only the text content.
    """
    content = data.get("content", [])
    text_parts = []
    for block in content:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))

    stop_reason = data.get("stop_reason", "")
    if not text_parts and stop_reason == "max_tokens":
        logger.warning("[DRPL] AI response hit max_tokens with no text output - all tokens consumed by thinking")
        return "[Error: The AI used all available tokens for internal reasoning and could not produce a response. Try increasing max_tokens or disabling extended thinking for this agent.]"

    return "\n".join(text_parts) if text_parts else ""


def _get_effective_model(db: Session, agent_config: dict) -> str:
    """Resolve model: agent override → platform setting → .env."""
    if agent_config.get("model"):
        return agent_config["model"]
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "ai_model").first()
    if setting:
        return setting.value
    return settings.ai_model


def _get_effective_api_key(db: Session) -> str:
    """Resolve API key: platform setting → .env."""
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "anthropic_api_key").first()
    if setting and setting.value and setting.value != "••••••••" and setting.value.strip():
        return setting.value
    return settings.anthropic_api_key


def _log_usage(db: Session, provider: str, model: str, agent_name: str, usage: dict, elapsed_ms: int, success: bool, error_msg: str = None):
    """Log API usage on a fresh SessionLocal — never poisons the caller's session.

    Telemetry must not share the request session: a long LLM call can leave the
    request connection dropped by Neon, and committing on it both fails and
    poisons every subsequent ORM access on the caller. The `db` arg is kept for
    signature compatibility but is intentionally unused.

    `run_id` comes from the ambient `run_id_scope` for the same reason, and
    gives the ledger a second axis: what one run cost, not just what a user
    spent this month. A run is the unit a user recognises.

    `user_id` comes from the ambient actor rather than a parameter. This is the
    ledger a per-user budget is enforced against, and the callers that spend the
    most — every graph building a `DRPLCallbackHandler` — have no user id to
    pass. A missing actor records the row unattributed (seeders, the archive
    sweep, scheduled jobs) rather than guessing an owner.
    """
    from app.core.actor_context import current_actor
    from app.core.database import SessionLocal
    from app.core.run_context import current_run_id

    actor = current_actor()
    fresh = SessionLocal()
    try:
        log = APIUsageLog(
            provider=provider,
            model=model,
            agent_name=agent_name,
            tokens_input=usage.get("input_tokens", 0),
            tokens_output=usage.get("output_tokens", 0),
            cost_estimate=_estimate_cost(model, usage),
            user_id=actor.user_id if actor else None,
            run_id=current_run_id(),
            success=success,
            error_message=error_msg,
            response_time_ms=elapsed_ms,
        )
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)
        if cache_read or cache_create:
            if hasattr(log, "cache_read_tokens"):
                log.cache_read_tokens = cache_read
            if hasattr(log, "cache_creation_tokens"):
                log.cache_creation_tokens = cache_create
        fresh.add(log)
        fresh.commit()
    except Exception as e:
        logger.warning(f"_log_usage: failed: {type(e).__name__}: {e}")
        try:
            fresh.rollback()
        except Exception:
            pass
    finally:
        try:
            fresh.close()
        except Exception:
            pass


def _estimate_cost(model: str, usage: dict) -> float:
    """Estimate API cost in USD (delegates to centralized multi-provider pricing)."""
    from app.services.langchain.provider_config import estimate_cost
    return estimate_cost(model, usage)


# --- AI Call Helper ---

async def call_ai(
    system_prompt: str,
    user_prompt: str,
    db: Session = None,
    agent_name: str = None,
    model_override: Optional[str] = None,
    max_tokens_override: Optional[int] = None,
    temperature_override: Optional[float] = None,
    streaming_callback: Optional[Callable[[str, dict], None]] = None,
    force_cache_system: bool = False,
) -> str:
    """Call the AI provider with optional per-agent configuration and automatic failover.

    Args:
        model_override: When set, overrides the agent/platform model (e.g. force Haiku for
            cheap per-doc extraction or Sonnet for synthesis). Provider is auto-detected.
        max_tokens_override: When set, overrides the agent/platform max_tokens.
        temperature_override: When set, overrides the agent/platform temperature. Use 0.1
            for extraction tasks where determinism matters (tender_doc_analyzer per-doc
            and synthesis paths) so the same tender produces stable output across runs.
        streaming_callback: Optional fn(event_type, payload) for reliability
            events (continuing / resumed / retried_empty). Currently honored by
            the Anthropic path; OpenAI/Gemini paths ignore it.
        force_cache_system: When True, marks the system prompt cacheable
            (cache_control: ephemeral) even if it's under the 4000-char length
            heuristic. Use for callers with a short-but-stable, high-frequency
            system prompt (e.g. the auto-scoring digest) where prompt caching
            still pays off despite not tripping the default length check.
    """
    agent_config = _get_agent_config(db, agent_name) if db and agent_name else {}

    # Check if agent is enabled
    if agent_config.get("is_enabled") is False:
        return _get_disabled_default(agent_name)

    # Use system prompt override if set
    if agent_config.get("system_prompt_override"):
        system_prompt = agent_config["system_prompt_override"]

    # Apply model/tokens overrides
    if model_override:
        agent_config["model"] = model_override
        from app.services.langchain.provider_config import detect_provider
        agent_config["provider"] = detect_provider(model_override)
    if max_tokens_override:
        agent_config["max_tokens"] = max_tokens_override
    if temperature_override is not None:
        agent_config["temperature"] = temperature_override

    provider = agent_config.get("provider") or settings.ai_provider
    if provider == "anthropic":
        return await _call_anthropic(
            system_prompt, user_prompt, db, agent_name, agent_config,
            streaming_callback=streaming_callback,
            force_cache_system=force_cache_system,
        )
    elif provider == "openai":
        return await _call_openai(system_prompt, user_prompt, db, agent_name, agent_config)
    elif provider == "google":
        return await _call_gemini(system_prompt, user_prompt, db, agent_name, agent_config)
    elif provider == "runpod":
        return await _call_runpod(system_prompt, user_prompt, db, agent_name, agent_config)
    else:
        raise ValueError(f"Unknown AI provider: {provider}")


def _configured_temperature(agent_config: Optional[dict], default: float = 0.7) -> float:
    """The agent's temperature, where 0.0 is a value and not "unset".

    ``get("temperature") or 0.7`` turned every deliberate 0.0 -- the BOQ
    number transcription and its grounding repair, verbatim annexure
    transcription, per-doc analysis -- into 0.7 on the wire, so two runs of
    one PDF could put a cell in a different field.
    """
    value = (agent_config or {}).get("temperature")
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_disabled_default(agent_name: str) -> str:
    """Return sensible default when an agent is disabled."""
    defaults = {
        "classifier": "Other",
        "relevance": "0.5",
        "risk": "0.5",
        "summary": "AI analysis is disabled for this agent.",
        "eligibility": '{"eligible": false, "score": 0.5, "notes": "Agent disabled"}',
        "checklist": "[]",
    }
    return defaults.get(agent_name, "Agent disabled.")


async def _call_anthropic(
    system_prompt: str,
    user_prompt: str,
    db: Session = None,
    agent_name: str = None,
    agent_config: dict = None,
    content_blocks: Optional[list[dict]] = None,
    streaming_callback: Optional[Callable[[str, dict], None]] = None,
    force_cache_system: bool = False,
) -> str:
    """
    Call Anthropic Claude API with per-agent config, extended thinking, effort, and usage logging.

    Includes a reliability wrapper that:
      - Auto-continues on stop_reason="max_tokens" (up to _MAX_CONTINUATIONS times)
      - Retries once on empty content with a non-max_tokens stop_reason
      - Falls back to no-thinking when thinking consumes the entire token budget
      - Emits assistant_continuing / assistant_resumed / assistant_retried_empty
        events to streaming_callback so the frontend can show indicators

    Args:
        system_prompt: System prompt string.
        user_prompt: User prompt string (ignored if content_blocks provided).
        db: Optional DB session for config/logging.
        agent_name: Optional agent name for logging.
        agent_config: Optional per-agent config overrides.
        content_blocks: Optional multimodal content array (for native PDF support).
            When provided, this replaces the user_prompt string as the message content.
        streaming_callback: Optional fn(event_type, payload) for SSE events
            surfaced to the frontend during continuation/retry.
    """
    api_key = _get_effective_api_key(db) if db else settings.anthropic_api_key
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured. Set it in Platform Settings or .env")

    model = _get_effective_model(db, agent_config or {}) if db else settings.ai_model
    temperature = _configured_temperature(agent_config)
    # Clamp max_tokens to the model's actual completion ceiling so a drifted
    # agent config (e.g. 200000 on a 128k-completion model) can't crash the call.
    max_tokens = clamp_max_tokens(model, (agent_config or {}).get("max_tokens") or 1024)

    # Build message content: multimodal blocks or plain text
    if content_blocks:
        message_content = content_blocks
    else:
        message_content = user_prompt

    # Resolve thinking and effort configuration
    thinking_config = _resolve_thinking_config(model, agent_config or {}, db)
    effort_level = _resolve_effort(model, agent_config or {}, db)

    # Resolve context management (compaction, tool clearing, thinking clearing, caching)
    from app.services.context_service import resolve_context_config
    context_config = resolve_context_config(db, model, agent_config or {}) if db else {
        "context_management": None, "beta_headers": [], "cache_control": None, "prompt_caching_enabled": False,
    }

    # Cache large system prompts so repeated tender analyses get a ~90% input-token
    # discount on the hot prompt text. 4000 chars ~= 1000 tokens (Anthropic's min
    # cacheable block is 1024 tokens). prompt_caching_enabled comes from
    # context_config; default-on when resolve_context_config isn't available.
    cache_system_prompt = (
        context_config.get("prompt_caching_enabled", True)
        and system_prompt
        and (force_cache_system or len(system_prompt) > 4000)
    )
    if cache_system_prompt:
        system_field = [{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        system_field = system_prompt

    # Build the request body
    request_body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_field,
        "messages": [{"role": "user", "content": message_content}],
    }

    # Add thinking configuration if resolved
    if thinking_config:
        request_body["thinking"] = thinking_config
        # When thinking is enabled, temperature must not be explicitly set (API uses default)
        # Extended thinking with budget_tokens requires max_tokens to be > budget_tokens
        if thinking_config.get("type") == "enabled":
            budget = thinking_config.get("budget_tokens", 0)
            if max_tokens <= budget:
                request_body["max_tokens"] = budget + max_tokens  # Ensure room for both
        elif thinking_config.get("type") == "adaptive":
            # Adaptive thinking shares max_tokens between thinking and output.
            # Ensure enough room for both by boosting max_tokens.
            # The model needs space for thinking + the actual response text.
            request_body["max_tokens"] = max(max_tokens, 16000)
        # Re-clamp in case the thinking budget pushed us over the model ceiling.
        request_body["max_tokens"] = clamp_max_tokens(model, request_body["max_tokens"])
    else:
        # Only set temperature when thinking is not enabled — and only when
        # the model takes one at all. Newer families removed sampling params
        # and a request carrying one is a hard 400, which is what annexure
        # transcription was dying of once per annexure.
        if supports_sampling_params(model):
            request_body["temperature"] = temperature

    # Add effort parameter if resolved
    if effort_level:
        request_body["output_config"] = {"effort": effort_level}

    # Add context management (compaction + context editing) if configured
    if context_config.get("context_management"):
        request_body["context_management"] = context_config["context_management"]

    # Add top-level prompt caching (automatic cache breakpoint on last cacheable block)
    if context_config.get("prompt_caching_enabled") and context_config.get("cache_control"):
        request_body["cache_control"] = context_config["cache_control"]

    # Build headers (including beta headers for compaction/context editing)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    beta_headers = context_config.get("beta_headers", [])
    if beta_headers:
        headers["anthropic-beta"] = ",".join(beta_headers)

    # Longer timeout for thinking, document blocks, and large inputs
    timeout = 60.0
    input_size = len(system_prompt or "") + len(user_prompt or "") + sum(
        len(str(b)) for b in (content_blocks or [])
    )
    if input_size > 50_000:      # Large input (>~12k tokens)
        timeout = 300.0
    if content_blocks:
        timeout = 300.0
    if thinking_config:
        timeout = max(timeout, 180.0)  # Thinking can take longer
    if max_tokens > 4096:
        timeout = max(timeout, 300.0)  # High output token requests need more time
    if max_tokens > 16384:
        # Very large outputs (e.g. tender_synthesis at 20K tokens) routinely
        # take 6-9 min on Sonnet 4.6 at typical streaming rates. 300s is not
        # enough — the call dies mid-stream with httpx.ReadTimeout and the
        # caller has to fall back to an empty-markdown handler. 900s gives a
        # generous ceiling without leaving sockets open forever on real hangs.
        timeout = max(timeout, 900.0)

    # Retry-with-backoff for transient 5xx (Anthropic 503/529 during peak load).
    # 502/504 also retried for upstream gateway hiccups. 429 stays in its own
    # branch below (handled by provider fallback). 1s/2s backoff between
    # attempts; total worst-case extra latency ~3s before raising.
    _TRANSIENT_5XX = {502, 503, 504, 529}
    _MAX_ATTEMPTS = 3

    async with httpx.AsyncClient(timeout=timeout) as client:

        async def _send(rb: dict) -> tuple[httpx.Response, int]:
            """POST to Anthropic with 5xx retry. Returns (response, elapsed_ms)."""
            t_start = time.time()
            resp = None
            for attempt in range(_MAX_ATTEMPTS):
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=rb,
                )
                if resp.status_code not in _TRANSIENT_5XX:
                    break
                if attempt + 1 == _MAX_ATTEMPTS:
                    break  # exhausted retries — fall through to error handling
                wait_s = 2 ** attempt  # 1s, 2s
                logger.warning(
                    f"[ai_service] transient {resp.status_code} from Anthropic "
                    f"(agent={agent_name}, attempt {attempt + 1}/{_MAX_ATTEMPTS - 1}), "
                    f"retrying in {wait_s}s"
                )
                await asyncio.sleep(wait_s)
            return resp, int((time.time() - t_start) * 1000)

        def _raise_for_non_200(resp: httpx.Response, elapsed: int) -> None:
            error_detail = f"HTTP {resp.status_code}"
            try:
                error_data = resp.json()
                error_msg = error_data.get("error", {}).get("message", "")
                if error_msg:
                    error_detail = error_msg
            except Exception:
                pass
            if db and agent_name:
                _log_usage(db, "anthropic", model, agent_name, {}, elapsed, False, error_detail)
            raise Exception(f"Anthropic API error: {error_detail}")

        # --- First call ---
        response, elapsed_ms = await _send(request_body)

        # A 429 is usually seconds long. Wait it out on the same model before
        # handing the request to another provider: same answer quality, and
        # the prompt does not leave for a second vendor.
        for rl_attempt in range(_RATE_LIMIT_RETRIES):
            if response.status_code != 429:
                break
            wait_s = _rate_limit_wait_s(response, rl_attempt)
            logger.warning(
                f"[ai_service] 429 from Anthropic (agent={agent_name}); "
                f"retrying on the same model in {wait_s:.0f}s "
                f"({rl_attempt + 1}/{_RATE_LIMIT_RETRIES})"
            )
            await asyncio.sleep(wait_s)
            response, elapsed_ms = await _send(request_body)

        if response.status_code == 429:
            if content_blocks:
                # The fallback providers get text only. A vision request is
                # the document; answering it without the document returned
                # [] forms or an invented summary as a success. Fail loudly
                # so the caller's retry path runs instead.
                if db and agent_name:
                    _log_usage(db, "anthropic", model, agent_name, {}, elapsed_ms, False,
                               "Rate limit - document request not failed over")
                raise Exception(
                    "AI rate limit hit while reading the documents. Try again in a moment."
                )
            if db and agent_name:
                _log_usage(db, "anthropic", model, agent_name, {}, elapsed_ms, False, "Rate limit - trying fallback")
            # Try fallback providers before giving up
            fallback_result = await _call_with_fallback(
                system_prompt, user_prompt, db, agent_name, agent_config, content_blocks
            )
            if fallback_result is not None:
                return fallback_result
            raise Exception("AI rate limit hit on all providers. Try again in a moment.")

        if response.status_code != 200:
            _raise_for_non_200(response, elapsed_ms)

        data = response.json()
        if db and agent_name:
            _log_usage(db, "anthropic", model, agent_name, data.get("usage", {}), elapsed_ms, True)

        accumulated_text = _extract_text_from_response(data)
        stop_reason = data.get("stop_reason", "")

        # --- Recovery 1: thinking burned the entire output budget ---
        # _extract_text_from_response returns a sentinel error string when the
        # model produced no text blocks because extended thinking consumed
        # every available output token. Continuing won't help (model will keep
        # thinking); retry without thinking instead.
        if (
            _THINKING_BURNED_TOKENS_SENTINEL in accumulated_text
            and request_body.get("thinking")
        ):
            logger.warning(
                f"[ai_service] {agent_name}: thinking consumed all tokens — "
                f"retrying without thinking"
            )
            _emit_reliability_event(streaming_callback, "assistant_retried_no_thinking", {
                "agent": agent_name,
                "reason": "thinking_burned_budget",
            })
            no_think_body = {**request_body}
            no_think_body.pop("thinking", None)
            if supports_sampling_params(model):
                no_think_body["temperature"] = temperature
            response, elapsed_ms = await _send(no_think_body)
            if response.status_code == 200:
                data = response.json()
                if db and agent_name:
                    _log_usage(db, "anthropic", model, agent_name, data.get("usage", {}), elapsed_ms, True)
                accumulated_text = _extract_text_from_response(data)
                stop_reason = data.get("stop_reason", "")
            else:
                # Surface the original sentinel error rather than hiding behind
                # a secondary failure.
                logger.warning(
                    f"[ai_service] retry-without-thinking also failed: HTTP {response.status_code}"
                )

        # --- Recovery 2: auto-continue on stop_reason=max_tokens ---
        # Up to _MAX_CONTINUATIONS sequential requests, each picking up where
        # the previous response stopped. Because the system prompt + content
        # blocks are cached (cache_control=ephemeral), continuations are cheap
        # on input and only cost output tokens.
        current_messages = list(request_body["messages"])
        continuations = 0
        while (
            stop_reason == "max_tokens"
            and continuations < _MAX_CONTINUATIONS
            and accumulated_text
            and _THINKING_BURNED_TOKENS_SENTINEL not in accumulated_text
        ):
            continuations += 1
            _emit_reliability_event(streaming_callback, "assistant_continuing", {
                "agent": agent_name,
                "attempt": continuations,
                "max_attempts": _MAX_CONTINUATIONS,
                "reason": "max_tokens",
            })

            # Append the partial assistant text + a continuation nudge.
            current_messages = current_messages + [
                {"role": "assistant", "content": accumulated_text},
                {"role": "user", "content": _CONTINUATION_NUDGE},
            ]
            cont_body = {**request_body, "messages": current_messages}
            # Disable thinking on continuations: the model has already produced
            # the output structure; we just need it to keep writing.
            cont_body.pop("thinking", None)
            if supports_sampling_params(model):
                cont_body["temperature"] = temperature

            cont_response, cont_elapsed = await _send(cont_body)
            if cont_response.status_code != 200:
                logger.warning(
                    f"[ai_service] continuation #{continuations} failed: "
                    f"HTTP {cont_response.status_code} — stopping"
                )
                break

            cont_data = cont_response.json()
            if db and agent_name:
                _log_usage(
                    db, "anthropic", model, agent_name,
                    cont_data.get("usage", {}), cont_elapsed, True,
                )
            cont_text = _extract_text_from_response(cont_data)
            if not cont_text:
                # Empty continuation — bail, don't loop forever.
                break
            accumulated_text += cont_text
            stop_reason = cont_data.get("stop_reason", "")

        if continuations > 0 and stop_reason != "max_tokens":
            _emit_reliability_event(streaming_callback, "assistant_resumed", {
                "agent": agent_name,
                "continuations": continuations,
            })
        elif continuations >= _MAX_CONTINUATIONS and stop_reason == "max_tokens":
            logger.warning(
                f"[ai_service] {agent_name}: still truncated after "
                f"{_MAX_CONTINUATIONS} continuations — surfacing partial output"
            )

        # --- Recovery 3: empty content with a benign stop_reason ---
        # Rare but reproducible (especially on costing): the API returns
        # content:[] and stop_reason="end_turn" with no usable output. The same
        # prompt works on claude.ai. Single retry with a polite nudge usually
        # produces a real answer.
        text_stripped = (accumulated_text or "").strip()
        is_empty_genuine = (
            not text_stripped
            and stop_reason not in ("max_tokens",)
            and continuations == 0
        )
        if is_empty_genuine:
            logger.warning(
                f"[ai_service] {agent_name}: empty content (stop_reason={stop_reason}) "
                f"— retrying once with nudge"
            )
            _emit_reliability_event(streaming_callback, "assistant_retried_empty", {
                "agent": agent_name,
                "stop_reason": stop_reason,
            })
            retry_messages = list(request_body["messages"]) + [
                {"role": "user", "content": _EMPTY_RETRY_NUDGE},
            ]
            retry_body = {**request_body, "messages": retry_messages}
            retry_response, retry_elapsed = await _send(retry_body)
            if retry_response.status_code == 200:
                retry_data = retry_response.json()
                if db and agent_name:
                    _log_usage(
                        db, "anthropic", model, agent_name,
                        retry_data.get("usage", {}), retry_elapsed, True,
                    )
                retry_text = _extract_text_from_response(retry_data)
                if retry_text.strip():
                    accumulated_text = retry_text
            else:
                logger.warning(
                    f"[ai_service] empty-retry failed: HTTP {retry_response.status_code}"
                )

        return accumulated_text


_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_MAX_WAIT_S = 20.0


def _rate_limit_wait_s(response, attempt: int) -> float:
    """Seconds to wait after a 429: the server's retry-after, else backoff."""
    try:
        header = response.headers.get("retry-after")
        if header is not None:
            return max(1.0, min(float(header), _RATE_LIMIT_MAX_WAIT_S))
    except (TypeError, ValueError, AttributeError):
        pass
    return min(5.0 * (2 ** attempt), _RATE_LIMIT_MAX_WAIT_S)


# --- Native PDF Support ---

def _build_pdf_content_blocks(
    file_path: str,
    cache_enabled: bool = True,
) -> list[dict]:
    """
    Build Claude document content blocks from a PDF file (base64-encoded).

    Args:
        file_path: Path to the PDF file.
        cache_enabled: Whether to add prompt caching control.

    Returns:
        List containing a single document content block dict.
    """
    with open(file_path, "rb") as f:
        pdf_bytes = f.read()

    b64_data = base64.b64encode(pdf_bytes).decode("utf-8")

    block = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64_data,
        },
    }

    if cache_enabled:
        block["cache_control"] = {"type": "ephemeral"}

    return [block]


def _limit_document_cache_breakpoints(content_blocks: list[dict]) -> None:
    """Keep ``cache_control`` on the last document block only (in place)."""
    doc_idx = [i for i, b in enumerate(content_blocks)
               if isinstance(b, dict) and b.get("type") == "document"
               and "cache_control" in b]
    for i in doc_idx[:-1]:
        content_blocks[i].pop("cache_control", None)


def _document_capable_model(
    db: Session, agent_name: Optional[str], agent_config: dict, resolved_model: str,
) -> str:
    """Native PDF blocks are an Anthropic request shape; send them to Anthropic.

    ``call_ai_with_documents`` always posts to api.anthropic.com, but the
    model comes from the agent's roster row -- and the rows pinned to OpenAI
    (checklist_generator, the document writers) sent ``gpt-5.6-luna`` there.
    Every such call was a guaranteed failure that the caller swallowed before
    falling back to a text-only answer that never saw the document. The
    platform's own Anthropic model reads the PDF instead, and the agent's
    roster pin still governs its text-only calls.
    """
    from app.services.langchain.provider_config import detect_provider, get_api_key

    if detect_provider(resolved_model) == "anthropic":
        return resolved_model
    # Without an Anthropic key there is nothing to promote the call to: the
    # Anthropic request shape is unusable, so leave the model where it is and
    # let `call_ai_with_documents` send the PDFs on the provider that can
    # actually read them. Rewriting to a Claude model here would guarantee a
    # 401 on every document call instead.
    try:
        if not get_api_key(db, "anthropic"):
            return resolved_model
    except Exception:
        pass
    fallback = settings.ai_model
    if db is not None:
        try:
            row = db.query(PlatformSetting).filter(PlatformSetting.key == "ai_model").first()
            if row and row.value and detect_provider(row.value) == "anthropic":
                fallback = row.value
        except Exception:
            pass
    logger.info(
        "[ai_service] %s is pinned to %s, which cannot read native PDF blocks; "
        "reading the documents on %s", agent_name, resolved_model, fallback,
    )
    agent_config["model"] = fallback
    agent_config["provider"] = "anthropic"
    return fallback


def _get_pdf_page_count(file_path: str) -> int:
    """Get the number of pages in a PDF file using pdfplumber."""
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


async def call_ai_with_documents(
    system_prompt: str,
    user_prompt: str,
    document_paths: Optional[list[str]] = None,
    db: Session = None,
    agent_name: str = None,
    model_override: Optional[str] = None,
    max_tokens_override: Optional[int] = None,
    max_pages_override: Optional[int] = None,
    thinking_mode_override: Optional[str] = None,
    temperature_override: Optional[float] = None,
    streaming_callback: Optional[Callable[[str, dict], None]] = None,
) -> str:
    """
    Call Claude AI with native PDF document support.

    Sends PDF files as base64 document blocks for full visual + text understanding.
    Falls back to plain text call if native PDF is disabled or files are too large.

    Args:
        system_prompt: System prompt string.
        user_prompt: User prompt text (placed after document blocks).
        document_paths: List of local filesystem paths to PDFs. Must be real
            paths on disk — `TenderDocument.file_path` values are StorageService
            keys, NOT local paths. Callers with a key must materialize it first
            via ``with storage.as_local_file(key, suffix=".pdf") as local_path:``.
        db: Database session for settings and logging.
        agent_name: Agent name for logging.
        model_override: When set, overrides the agent/platform model
            (e.g. force Haiku 4.5 for cheap per-doc vision extraction).
        max_tokens_override: When set, overrides the agent/platform max_tokens.
        max_pages_override: When set, overrides the platform max-pages setting.

    Returns:
        AI response text.
    """
    # Check if native PDF is enabled
    if db:
        from app.services.settings_service import get_effective_setting
        native_enabled = get_effective_setting(db, "claude_pdf_native_enabled", True)
        if isinstance(native_enabled, str):
            native_enabled = native_enabled.lower() in ("true", "1", "yes")
        max_pages = int(get_effective_setting(db, "claude_pdf_max_pages", 200) or 200)
        cache_enabled = get_effective_setting(db, "claude_pdf_cache_enabled", True)
        if isinstance(cache_enabled, str):
            cache_enabled = cache_enabled.lower() in ("true", "1", "yes")
    else:
        native_enabled = True
        max_pages = 100
        cache_enabled = True

    if max_pages_override:
        max_pages = max_pages_override

    if not native_enabled or not document_paths:
        return await call_ai(
            system_prompt, user_prompt, db, agent_name,
            model_override=model_override,
            max_tokens_override=max_tokens_override,
            temperature_override=temperature_override,
            streaming_callback=streaming_callback,
        )

    # Build content blocks: documents first, then text prompt
    content_blocks = []
    skipped_docs = []

    for path in document_paths:
        if not os.path.exists(path):
            logger.warning(f"PDF file not found: {path}")
            skipped_docs.append(path)
            continue

        # Check file size (32MB API limit)
        file_size = os.path.getsize(path)
        if file_size > 32 * 1024 * 1024:
            logger.warning(f"PDF too large for native processing ({file_size} bytes): {path}")
            skipped_docs.append(path)
            continue

        # Check page count
        page_count = _get_pdf_page_count(path)
        if page_count > max_pages:
            logger.warning(f"PDF has {page_count} pages (max {max_pages}): {path}")
            skipped_docs.append(path)
            continue

        try:
            doc_blocks = _build_pdf_content_blocks(path, cache_enabled)
            content_blocks.extend(doc_blocks)
        except Exception as e:
            logger.error(f"Failed to build PDF content block for {path}: {e}")
            skipped_docs.append(path)

    if not content_blocks:
        # All documents failed — fall back to plain text
        logger.info("No valid PDF documents for native processing, falling back to text")
        return await call_ai(system_prompt, user_prompt, db, agent_name)

    # One cache breakpoint for the whole document prefix. A breakpoint caches
    # everything before it, so marking only the LAST document caches exactly
    # what marking each one did -- while a breakpoint per PDF, plus the
    # system block and the top-level marker, passed Anthropic's cap of four
    # on any three-PDF tender and the request failed with a 400 before a
    # token was read (and then paid again on every fallback).
    _limit_document_cache_breakpoints(content_blocks)

    # Add the text prompt after document blocks
    content_blocks.append({"type": "text", "text": user_prompt})

    # Resolve agent config for proper max_tokens, temperature, etc.
    agent_config = _get_agent_config(db, agent_name) if db and agent_name else {}

    # Apply per-call overrides (used by tender analyzer v2 to force Haiku for
    # cheap per-doc extraction without touching the underlying agent record).
    if model_override:
        agent_config["model"] = model_override
        from app.services.langchain.provider_config import detect_provider
        agent_config["provider"] = detect_provider(model_override)
    if max_tokens_override:
        agent_config["max_tokens"] = max_tokens_override
    if temperature_override is not None:
        agent_config["temperature"] = temperature_override
    if thinking_mode_override is not None:
        # Force a specific thinking_mode for this call. "disabled" is the
        # escape hatch when an upstream agent (e.g. annexure_finder) has
        # adaptive thinking on but max_tokens too small to fit thinking +
        # output — the model returns stop_reason=max_tokens with no text.
        agent_config["thinking_mode"] = thinking_mode_override

    # Clamp max_tokens to a sensible range for document analysis:
    #   - at least 8192 (structured JSON output needs room)
    #   - at most the model's completion ceiling (prevents 200000-style crashes)
    resolved_model = _get_effective_model(db, agent_config) if db else agent_config.get("model") or settings.ai_model
    resolved_model = _document_capable_model(db, agent_name, agent_config, resolved_model)
    requested = int(agent_config.get("max_tokens") or 0)
    agent_config["max_tokens"] = clamp_max_tokens(resolved_model, max(requested, 8192))

    # Call with document blocks. Native PDF is an Anthropic request shape, so
    # it only goes to Anthropic when this deployment actually holds that key;
    # otherwise OpenAI reads the same PDFs through the Responses API's
    # `input_file` part, which is native too (not text extraction).
    from app.services.langchain.provider_config import get_api_key as _get_api_key

    anthropic_key = ""
    try:
        anthropic_key = _get_api_key(db, "anthropic") or ""
    except Exception:
        anthropic_key = ""

    if not anthropic_key:
        if _get_api_key(db, "openai"):
            return await _call_openai_with_documents(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                document_paths=[p for p in (document_paths or [])
                                if p not in skipped_docs and os.path.exists(p)],
                db=db,
                agent_name=agent_name,
                agent_config=agent_config,
            )
        logger.warning(
            "[ai_service] no provider can read native PDFs (no Anthropic or "
            "OpenAI key); answering %s from the text prompt alone", agent_name,
        )
        return await call_ai(
            system_prompt, user_prompt, db, agent_name,
            model_override=model_override,
            max_tokens_override=max_tokens_override,
            temperature_override=temperature_override,
            streaming_callback=streaming_callback,
        )

    return await _call_anthropic(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        db=db,
        agent_name=agent_name,
        agent_config=agent_config,
        content_blocks=content_blocks,
        streaming_callback=streaming_callback,
    )


async def _call_openai_with_documents(
    system_prompt: str,
    user_prompt: str,
    document_paths: list[str],
    db: Session = None,
    agent_name: str = None,
    agent_config: dict = None,
) -> str:
    """Read PDFs natively on OpenAI, the way ``_call_anthropic`` does on Claude.

    The Responses API takes a PDF as an ``input_file`` content part carrying a
    base64 data URL, and the model sees the rendered pages -- not text pulled
    out by a parser. That distinction is the whole reason this exists: the
    tender analyser and the BOQ extractor are built on the assumption that the
    model can look at a scanned schedule, and a pdfplumber fallback silently
    turns that into guesswork on exactly the documents that matter most.

    Measured against gpt-5.6-terra on a live Eastern Railway NIT (2026-09-26):
    the tender number, the advertised value and the EMD all came back correct
    from the PDF alone.
    """
    import httpx

    from app.services.langchain.provider_config import get_api_key, get_equivalent_model

    api_key = get_api_key(db, "openai")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not configured. Set it in Platform Settings or .env")

    agent_config = agent_config or {}
    primary_model = _get_effective_model(db, agent_config) if db else (
        agent_config.get("model") or settings.ai_model
    )
    model = get_equivalent_model(primary_model, "openai") if (
        primary_model or ""
    ).lower().startswith("claude-") else (primary_model or settings.ai_model)
    max_tokens = clamp_max_tokens(model, max(int(agent_config.get("max_tokens") or 0), 8192))
    temperature = _configured_temperature(agent_config)

    content: list[dict] = []
    for path in document_paths:
        try:
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("utf-8")
        except OSError as exc:
            logger.warning("[ai_service] could not read %s for OpenAI: %s", path, exc)
            continue
        content.append({
            "type": "input_file",
            "filename": os.path.basename(path) or "document.pdf",
            "file_data": f"data:application/pdf;base64,{b64}",
        })
    if not content:
        return await call_ai(system_prompt, user_prompt, db, agent_name)
    content.append({"type": "input_text", "text": user_prompt})

    body: dict = {
        "model": model,
        "store": False,
        "input": [
            {"type": "message", "role": "developer", "content": system_prompt},
            {"type": "message", "role": "user", "content": content},
        ],
        "max_output_tokens": max_tokens,
    }
    # The gpt-5.6 family rejects `temperature` outright -- see
    # NO_SAMPLING_PARAM_MODELS.
    if temperature is not None and supports_sampling_params(model):
        body["temperature"] = temperature

    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body,
            )
        elapsed_ms = int((time.time() - start_time) * 1000)
        if response.status_code >= 400:
            detail = response.text[:500]
            if db and agent_name:
                _log_usage(db, "openai", model, agent_name, {}, elapsed_ms, False, detail)
            raise Exception(f"OpenAI document API error {response.status_code}: {detail}")

        data = response.json()
        text = "".join(
            part.get("text", "")
            for item in data.get("output", []) or []
            for part in (item.get("content") or [])
            if isinstance(part, dict) and part.get("type") in ("output_text", "text")
        )
        usage_raw = data.get("usage") or {}
        usage = {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
        }
        if db and agent_name:
            _log_usage(db, "openai", model, agent_name, usage, elapsed_ms, True)
        logger.info(
            "[ai_service] OpenAI read %d document(s) on %s for %s (%d ms)",
            len(document_paths), model, agent_name, elapsed_ms,
        )
        return text
    except Exception as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        if db and agent_name:
            _log_usage(db, "openai", model, agent_name, {}, elapsed_ms, False, str(exc)[:500])
        raise


async def _call_runpod(
    system_prompt: str,
    user_prompt: str,
    db: Session = None,
    agent_name: str = None,
    agent_config: dict = None,
) -> str:
    """Call the self-hosted model on the RunPod serverless vLLM endpoint.

    vLLM speaks the OpenAI chat API, so this is one POST. Two things differ
    from `_call_openai` on purpose:

    * The model is the agent's OWN model, never `get_equivalent_model`. There
      is no hosted equivalent of a model you are running yourself, and
      substituting one would silently move the work (and the bill) to a paid
      provider under a name the endpoint does not serve.
    * No configuration means a clear error, not a fallback. A local endpoint
      that quietly stops being called is the failure nobody notices.

    Usage is logged under provider "runpod" so the admin dashboard counts these
    calls alongside every other agent's — at the $0/token price the pricing
    table states for a model we host ourselves.
    """
    import httpx

    from app.services.langchain.provider_config import get_api_key, get_base_url

    api_key = get_api_key(db, "runpod")
    base_url = get_base_url(db, "runpod")
    if not api_key:
        raise ValueError(
            "No RunPod API key configured. Set runpod_chat_api_key in Platform "
            "Settings (or RUNPOD_CHAT_API_KEY in .env)."
        )
    if not base_url:
        raise ValueError(
            "No RunPod chat endpoint configured. Set runpod_chat_endpoint_id in "
            "Platform Settings (or RUNPOD_CHAT_ENDPOINT_ID in .env)."
        )

    cfg = agent_config or {}
    model = (cfg.get("model") or settings.runpod_chat_model or "").strip()
    if not model:
        raise ValueError("No model configured for the runpod provider.")
    temperature = cfg.get("temperature")
    max_tokens = clamp_max_tokens(model, cfg.get("max_tokens") or 1024)

    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        body["temperature"] = temperature

    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=settings.runpod_chat_timeout_s) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        resp.raise_for_status()
        data = resp.json()
        text = (data["choices"][0]["message"].get("content") or "").strip()
        raw = data.get("usage") or {}
        usage = {
            "input_tokens": raw.get("prompt_tokens", 0) or 0,
            "output_tokens": raw.get("completion_tokens", 0) or 0,
        }
        elapsed_ms = int((time.time() - start_time) * 1000)
        if db and agent_name:
            _log_usage(db, "runpod", model, agent_name, usage, elapsed_ms, True)
        return text
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        if db and agent_name:
            _log_usage(db, "runpod", model, agent_name, {}, elapsed_ms, False, str(e)[:500])
        # Name the likely cause per status, because the two ways this endpoint
        # refuses a request look nothing alike and both were measured:
        #
        #   403  the key is not scoped to THIS endpoint. RunPod keys are
        #        per-endpoint on this account -- the OCR key and the chat key
        #        each return 403 on the other's endpoint -- so the
        #        runpod_api_key fallback lands here rather than working.
        #   500  the model name is not what the endpoint serves. The dashboard
        #        displays "Qwen/Qwen2.5-7B-Instruct"; the endpoint answers to
        #        "qwen2.5-7b" and 500s on the other.
        #
        # Blaming the model name for a 403 sends someone to the wrong setting,
        # which is worse than saying nothing.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            hint = (
                f"the API key is not authorised for endpoint "
                f"{_runpod_endpoint_hint(db)!r}. RunPod keys are scoped per "
                f"endpoint: set runpod_chat_api_key to this endpoint's own key "
                f"(the OCR key will not work here)."
            )
        elif status is not None and status >= 500:
            hint = (
                f"the endpoint refused the request. Check that runpod_chat_model "
                f"({model!r}) is exactly what it serves -- a Hugging Face repo id "
                f"returns 500 where the served name works."
            )
        else:
            hint = (
                f"check runpod_chat_endpoint_id, runpod_chat_api_key and "
                f"runpod_chat_model ({model!r})."
            )
        raise Exception(
            f"Self-hosted model call failed ({type(e).__name__}: {e}) -- {hint}"
        ) from e


def _runpod_endpoint_hint(db) -> str:
    """The endpoint id, for an error message. Never the key."""
    try:
        from app.services.langchain.provider_config import get_base_url
        url = get_base_url(db, "runpod") or ""
        return url.split("/v2/")[1].split("/")[0] if "/v2/" in url else "(unset)"
    except Exception:
        return "(unknown)"


async def _call_openai(
    system_prompt: str,
    user_prompt: str,
    db: Session = None,
    agent_name: str = None,
    agent_config: dict = None,
) -> str:
    """Call OpenAI Responses API with config resolution and usage logging."""
    from app.services.langchain.provider_config import get_api_key, get_equivalent_model
    from app.services.openai_agents.responses_client import call_openai_responses

    api_key = get_api_key(db, "openai")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not configured. Set it in Platform Settings or .env")

    # Resolve model
    primary_model = _get_effective_model(db, agent_config or {}) if db else settings.ai_model
    model = get_equivalent_model(primary_model, "openai")
    temperature = _configured_temperature(agent_config)
    max_tokens = clamp_max_tokens(model, (agent_config or {}).get("max_tokens") or 4096)

    start_time = time.time()

    try:
        result = await call_openai_responses(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            api_key=api_key,
            model=model,
            # None omits it: the gpt-5.6 family 400s on `temperature`.
            temperature=temperature if supports_sampling_params(model) else None,
            max_tokens=max_tokens,
        )

        elapsed_ms = result.get("elapsed_ms", int((time.time() - start_time) * 1000))
        usage = result.get("usage", {})

        if db and agent_name:
            _log_usage(db, "openai", model, agent_name, usage, elapsed_ms, True)

        return result["text"]

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        error_str = str(e)

        if db and agent_name:
            _log_usage(db, "openai", model, agent_name, {}, elapsed_ms, False, error_str[:500])

        if "rate limit" in error_str.lower() or "429" in error_str:
            raise Exception(f"OpenAI rate limit hit: {error_str}")
        raise Exception(f"OpenAI API error: {error_str}")


async def _call_gemini(
    system_prompt: str,
    user_prompt: str,
    db: Session = None,
    agent_name: str = None,
    agent_config: dict = None,
) -> str:
    """Call Google Gemini API via the google-genai SDK with usage logging."""
    from app.services.langchain.provider_config import get_api_key, get_equivalent_model

    api_key = get_api_key(db, "google")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not configured. Set it in Platform Settings or .env")

    # Resolve model
    primary_model = _get_effective_model(db, agent_config or {}) if db else settings.ai_model
    model = get_equivalent_model(primary_model, "google")
    temperature = _configured_temperature(agent_config)
    max_tokens = clamp_max_tokens(model, (agent_config or {}).get("max_tokens") or 4096)

    start_time = time.time()

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )

        response = client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=config,
        )

        elapsed_ms = int((time.time() - start_time) * 1000)

        result_text = response.text or ""

        # Extract usage if available
        usage_meta = getattr(response, "usage_metadata", None)
        normalized_usage = {
            "input_tokens": getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0,
            "output_tokens": getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0,
        }

        if db and agent_name:
            _log_usage(db, "google", model, agent_name, normalized_usage, elapsed_ms, True)

        return result_text

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        error_str = str(e)

        if db and agent_name:
            _log_usage(db, "google", model, agent_name, {}, elapsed_ms, False, error_str[:500])

        # Re-raise with clear provider context
        if "429" in error_str or "ResourceExhausted" in type(e).__name__:
            raise Exception(f"Gemini rate limit hit: {error_str}")
        raise Exception(f"Gemini API error: {error_str}")


async def _call_with_fallback(
    system_prompt: str,
    user_prompt: str,
    db: Session = None,
    agent_name: str = None,
    agent_config: dict = None,
    content_blocks: Optional[list[dict]] = None,
) -> Optional[str]:
    """
    Try fallback providers (OpenAI then Gemini) when Claude hits rate limits.
    Returns None if all fallbacks fail.
    For document blocks: falls back to text-only since native PDF is Claude-only.
    """
    from app.services.langchain.provider_config import get_api_key

    # If we have content blocks (native PDF), extract text for fallback providers
    if content_blocks:
        # content_blocks may contain document blocks + a text block
        # Use the text prompt only for non-Claude providers
        logger.info("[FALLBACK] Native PDF blocks not supported on fallback provider, using text-only")

    # Try OpenAI
    openai_key = get_api_key(db, "openai")
    if openai_key:
        try:
            logger.warning("[FALLBACK] Anthropic rate limited, trying OpenAI")
            return await _call_openai(system_prompt, user_prompt, db, agent_name, agent_config)
        except Exception as e:
            logger.warning(f"[FALLBACK] OpenAI also failed: {e}")

    # Try Gemini
    gemini_key = get_api_key(db, "google")
    if gemini_key:
        try:
            logger.warning("[FALLBACK] Trying Gemini")
            return await _call_gemini(system_prompt, user_prompt, db, agent_name, agent_config)
        except Exception as e:
            logger.warning(f"[FALLBACK] Gemini also failed: {e}")

    return None


# --- Tender Context Builder ---

def _build_tender_context(tender: Tender) -> str:
    """Build a text description of a tender for AI prompts."""
    parts = [
        f"Title: {tender.title}",
        f"Portal: {tender.portal}",
        f"Tender ID: {tender.tender_id}",
        f"Department: {tender.department or 'Not specified'}",
        f"Organisation: {tender.organisation or 'Not specified'}",
        f"Description: {tender.description or 'No description available'}",
        f"Estimated Value: {tender.estimated_value or 'Not specified'} {tender.currency or 'INR'}",
        f"EMD Amount: {tender.emd_amount or 'Not specified'}",
        f"Opening Date: {tender.opening_date or 'Not specified'}",
        f"Closing Date: {tender.closing_date or 'Not specified'}",
        f"Status: {tender.status}",
    ]
    return "\n".join(parts)


# --- AI Agent Functions ---

async def classify_tender(tender: Tender, db: Session = None) -> str:
    """Analyst Agent: Classify tender into a category."""
    system_prompt = """You are an AI analyst for DRPL Manufacturing, a company specializing in
mechanical and electrical engineering for Indian Railways. Classify the given tender into exactly
ONE of these categories: Mechanical, Electrical, Civil, IT/Software, Materials/Supplies, Consulting, Other.
Respond with ONLY the category name, nothing else."""

    from app.services.redaction_service import redact_tender_context
    user_prompt = redact_tender_context(_build_tender_context(tender), db)

    try:
        result = await call_ai(system_prompt, user_prompt, db, "classifier")
        category = result.strip()
        valid = ["Mechanical", "Electrical", "Civil", "IT/Software", "Materials/Supplies", "Consulting", "Other"]
        return category if category in valid else "Other"
    except Exception as e:
        logger.error(f"classify_tender failed for tender {tender.id}: {e}")
        return "Other"


async def score_relevance(
    tender: Tender,
    db: Session = None,
    extra_system_context: str = "",
) -> float:
    """Relevance Agent: Score how relevant this tender is to DRPL (0-1).

    `extra_system_context` is an optional string appended to the system prompt
    — used by Phase 7 to inject the admin-configured Tender Scope Profile
    (keyword groups, exclusions, target ministries, value range).
    """
    system_prompt = """You are a relevance scoring agent for DRPL Manufacturing. DRPL specializes in:
- Mechanical engineering for Indian Railways (locomotive components, bogies, couplings, braking systems)
- Electrical engineering for railways (traction motors, transformers, signaling equipment, power systems)
- Annual Maintenance Contracts (AMC) for railway equipment
- Supply of mechanical and electrical spare parts

Score the tender's relevance to DRPL on a scale of 0.0 to 1.0:
- 0.9-1.0: Perfect match (railway mechanical/electrical core work)
- 0.7-0.8: Strong match (related mechanical/electrical or railway work)
- 0.4-0.6: Moderate match (partially relevant industry or scope)
- 0.1-0.3: Weak match (different domain but some overlap)
- 0.0: No relevance

Respond with ONLY a number between 0.0 and 1.0, nothing else."""

    if extra_system_context:
        system_prompt = system_prompt + "\n\n" + extra_system_context

    from app.services.redaction_service import redact_tender_context
    user_prompt = redact_tender_context(_build_tender_context(tender), db)

    try:
        from app.services.auto_scoring_settings import get_scoring_settings
        _scoring_model = get_scoring_settings(db)["auto_scoring_model"] if db else "claude-haiku-4-5"
        result = await call_ai(system_prompt, user_prompt, db, "relevance", model_override=_scoring_model)
        score = float(result.strip())
        return max(0.0, min(1.0, score))
    except Exception as e:
        logger.error(f"score_relevance failed for tender {tender.id}: {e}")
        return 0.5


async def assess_risk(tender: Tender, db: Session = None) -> float:
    """Risk Agent: Assess risk level of the tender (0=low risk, 1=high risk)."""
    system_prompt = """You are a risk assessment agent for DRPL Manufacturing, evaluating government tenders.
Assess the risk level considering:
- Tight deadlines (closing soon, short execution periods)
- High EMD/security deposit requirements relative to tender value
- Unclear or vague scope of work
- Complex technical requirements that may be hard to fulfill
- Unusually low estimated values suggesting compressed margins
- Multiple corrigenda or amendments suggesting instability

Score risk from 0.0 (very low risk) to 1.0 (very high risk).
Respond with ONLY a number between 0.0 and 1.0, nothing else."""

    from app.services.redaction_service import redact_tender_context
    user_prompt = redact_tender_context(_build_tender_context(tender), db)

    try:
        result = await call_ai(system_prompt, user_prompt, db, "risk")
        score = float(result.strip())
        return max(0.0, min(1.0, score))
    except Exception as e:
        logger.error(f"assess_risk failed for tender {tender.id}: {e}")
        return 0.5


async def summarize_tender(tender: Tender, db: Session = None) -> str:
    """Summary Agent: Generate a 2-3 sentence executive summary."""
    system_prompt = """You are a tender analyst for DRPL Manufacturing. Write a concise 2-3 sentence
executive summary of the tender. Highlight: what is being procured, key requirements or quantities,
and whether it aligns with DRPL's mechanical/electrical railway capabilities. Be factual and specific."""

    from app.services.redaction_service import redact_tender_context
    user_prompt = redact_tender_context(_build_tender_context(tender), db)

    try:
        result = await call_ai(system_prompt, user_prompt, db, "summary")
        return result.strip()[:500]
    except Exception as e:
        logger.error(f"summarize_tender failed for tender {tender.id}: {e}")
        return "AI summary unavailable."


async def check_eligibility(tender_text: str, db: Session = None) -> dict:
    """Eligibility Agent: Check if DRPL is eligible for a tender."""
    system_prompt = """You are an eligibility assessment agent for DRPL Manufacturing. DRPL specializes in:
- Mechanical engineering for Indian Railways (locomotive components, bogies, couplings, braking systems)
- Electrical engineering for railways (traction motors, transformers, signaling equipment)
- Annual Maintenance Contracts (AMC) for railway equipment

Assess whether DRPL is eligible for this tender based on their capabilities.

Respond with ONLY a valid JSON object:
{
  "eligible": true/false,
  "score": 0.0 to 1.0,
  "notes": "Brief explanation of eligibility assessment"
}"""

    try:
        from app.services.auto_scoring_settings import get_scoring_settings
        _scoring_model = get_scoring_settings(db)["auto_scoring_model"] if db else "claude-haiku-4-5"
        result = await call_ai(system_prompt, tender_text, db, "eligibility", model_override=_scoring_model)
        result = result.strip()
        if result.startswith("```"):
            result = result.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(result)
        return {
            "eligible": data.get("eligible", False),
            "score": max(0.0, min(1.0, float(data.get("score", 0.5)))),
            "notes": data.get("notes", ""),
        }
    except Exception as e:
        logger.error(f"check_eligibility failed: {e}")
        return {"eligible": False, "score": 0.5, "notes": "Assessment unavailable"}


async def parse_document_checklist(
    tender_text: str,
    document_text: str = "",
    db: Session = None,
    *,
    document_label: str = "Tender Notice Document Content",
    max_document_chars: int = 4000,
) -> list[dict]:
    """Checklist Agent: Extract required documents list from tender info."""
    system_prompt = """You are a document requirements analyst for government tender submissions in India.
Given a tender description, extract a structured list of ALL documents required for submission.

For each document, provide:
- name: Short name of the document
- description: Brief description of what is needed
- is_required: true if mandatory, false if optional

Respond with ONLY a valid JSON array. Common documents include: EMD, PAN Card, GST Registration,
Company Registration, Technical Bid Documents, Financial Bid, Experience Certificates, etc."""

    user_prompt = f"Tender Information:\n{tender_text}"
    if document_text:
        user_prompt += f"\n\n{document_label}:\n{document_text[:max_document_chars]}"

    try:
        result = await call_ai(system_prompt, user_prompt, db, "checklist")
        result = result.strip()
        if result.startswith("```"):
            result = result.split("\n", 1)[1].rsplit("```", 1)[0]
        items = json.loads(result)
        if isinstance(items, list):
            return items
        return []
    except Exception as e:
        logger.error(f"parse_document_checklist failed: {e}")
        return []


# --- Master Functions ---

_QUICK_ANALYSIS_AGENTS = ("classifier", "relevance", "risk", "summary")
_VALID_CATEGORIES = (
    "Mechanical", "Electrical", "Civil", "IT/Software", "Materials/Supplies", "Consulting", "Other",
)

_QUICK_ANALYSIS_SYSTEM = """You are a tender analyst for DRPL Manufacturing. DRPL specializes in:
- Mechanical engineering for Indian Railways (locomotive components, bogies, couplings, braking systems)
- Electrical engineering for railways (traction motors, transformers, signaling equipment, power systems)
- Annual Maintenance Contracts (AMC) for railway equipment
- Supply of mechanical and electrical spare parts

For the tender the user gives you, produce four judgements.

1. category -- exactly ONE of: Mechanical, Electrical, Civil, IT/Software, Materials/Supplies, Consulting, Other.
2. relevance -- relevance to DRPL, 0.0 to 1.0:
   0.9-1.0 perfect match (railway mechanical/electrical core work); 0.7-0.8 strong match
   (related mechanical/electrical or railway work); 0.4-0.6 moderate (partially relevant);
   0.1-0.3 weak (different domain, some overlap); 0.0 no relevance.
3. risk -- 0.0 (very low) to 1.0 (very high), considering: tight deadlines (closing soon, short
   execution periods); high EMD/security deposit relative to value; unclear or vague scope;
   complex technical requirements that may be hard to fulfil; unusually low estimated value
   suggesting compressed margins; multiple corrigenda or amendments suggesting instability.
4. summary -- a concise 2-3 sentence executive summary: what is being procured, key
   requirements or quantities, and whether it aligns with DRPL's mechanical/electrical
   railway capabilities. Factual and specific.

Respond with ONLY this JSON object, no prose, no code fence:
{"category": "<category>", "relevance": <number>, "risk": <number>, "summary": "<summary>"}"""


def _clamp01(value, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _quick_analysis_customised(db: Session) -> bool:
    """True when an admin has disabled or re-prompted one of the four agents.

    The combined call replaces four hard-coded prompts; an admin's legacy
    AgentConfig override or a disabled agent must still be honoured, so the
    separate calls run instead.
    """
    if db is None:
        return False
    for name in _QUICK_ANALYSIS_AGENTS:
        try:
            cfg = _get_agent_config(db, name)
        except Exception:
            continue
        if cfg.get("is_enabled") is False or cfg.get("system_prompt_override"):
            return True
    return False


async def quick_analyze(tender: Tender, db: Session = None) -> Optional[dict]:
    """Category, relevance, risk and summary in ONE call, or None on failure.

    These were four sequential calls, each re-sending the same tender context
    with a one-line task. One call reads the context once and answers all
    four; the caller falls back to the separate calls on any failure, so a
    malformed reply costs a retry, never a wrong field.
    """
    from app.services.redaction_service import redact_tender_context
    user_prompt = redact_tender_context(_build_tender_context(tender), db)
    try:
        from app.services.auto_scoring_settings import get_scoring_settings
        model = get_scoring_settings(db)["auto_scoring_model"] if db else "claude-haiku-4-5"
        raw = await call_ai(_QUICK_ANALYSIS_SYSTEM, user_prompt, db, "summary",
                            model_override=model, max_tokens_override=1024)
        text = (raw or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        category = str(data.get("category") or "").strip()
        summary = str(data.get("summary") or "").strip()
        if data.get("relevance") is None or data.get("risk") is None or not summary:
            raise ValueError("incomplete quick analysis")
        return {
            "ai_category": category if category in _VALID_CATEGORIES else "Other",
            "ai_relevance_score": _clamp01(data.get("relevance")),
            "ai_risk_score": _clamp01(data.get("risk")),
            "ai_summary": summary[:500],
        }
    except Exception as e:
        logger.warning(f"quick_analyze failed for tender {tender.id}; using separate calls: {e}")
        return None


async def analyze_tender(db: Session, tender_id: int) -> dict:
    """Category, relevance, risk and summary for one tender, written to the row."""
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise ValueError(f"Tender {tender_id} not found")

    combined = None if _quick_analysis_customised(db) else await quick_analyze(tender, db)
    if combined is not None:
        for field, value in combined.items():
            setattr(tender, field, value)
    else:
        tender.ai_category = await classify_tender(tender, db)
        tender.ai_relevance_score = await score_relevance(tender, db)
        tender.ai_risk_score = await assess_risk(tender, db)
        tender.ai_summary = await summarize_tender(tender, db)
    tender.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(tender)

    return {
        "tender_id": tender.id,
        "ai_category": tender.ai_category,
        "ai_relevance_score": tender.ai_relevance_score,
        "ai_risk_score": tender.ai_risk_score,
        "ai_summary": tender.ai_summary,
    }


async def analyze_batch(db: Session, batch_size: int = 10, use_batch_api: bool = False) -> dict:
    """
    Analyze a batch of unprocessed tenders.

    Args:
        db: Database session.
        batch_size: Number of tenders to process.
        use_batch_api: If True, use Claude's Message Batches API (50% cost discount, async).
                       If False, process sequentially (immediate results).

    Returns:
        For sequential: {"analyzed": N, "remaining": M, "errors": K}
        For batch API: {"batch_id": "msgbatch_xxx", "total_requests": N, "remaining": M, "mode": "batch_api"}
    """
    remaining = db.query(Tender).filter(Tender.ai_category == None).count()

    if use_batch_api:
        # Use the Batches API for 50% cost discount (async processing)
        from app.services.batch_service import create_tender_analysis_batch
        try:
            batch = await create_tender_analysis_batch(db, batch_size=batch_size)
            return {
                "batch_id": batch.batch_id,
                "total_requests": batch.total_requests,
                "remaining": max(0, remaining - (batch.total_requests // 4)),
                "mode": "batch_api",
                "status": batch.status,
            }
        except ValueError as e:
            return {"analyzed": 0, "remaining": remaining, "errors": 1, "error": str(e)}
        except Exception as e:
            logger.error(f"Batch API creation failed: {e}")
            return {"analyzed": 0, "remaining": remaining, "errors": 1, "error": str(e)}

    # Sequential processing (original behavior)
    unanalyzed = db.query(Tender).filter(
        Tender.ai_category == None,
    ).limit(batch_size).all()

    analyzed = 0
    errors = 0

    for tender in unanalyzed:
        try:
            await analyze_tender(db, tender.id)
            analyzed += 1
        except Exception as e:
            logger.error(f"Batch analysis failed for tender {tender.id}: {e}")
            errors += 1

    return {
        "analyzed": analyzed,
        "remaining": max(0, remaining - analyzed),
        "errors": errors,
    }


def get_ai_stats(db: Session) -> dict:
    """Get aggregate AI analysis statistics."""
    total_analyzed = db.query(Tender).filter(Tender.ai_category != None).count()
    total_unanalyzed = db.query(Tender).filter(Tender.ai_category == None).count()

    avg_relevance = db.query(func.avg(Tender.ai_relevance_score)).filter(
        Tender.ai_relevance_score != None,
    ).scalar()

    high_relevance = db.query(Tender).filter(
        Tender.ai_relevance_score != None,
        Tender.ai_relevance_score > 0.7,
    ).count()

    categories = db.query(
        Tender.ai_category, func.count(Tender.id)
    ).filter(
        Tender.ai_category != None,
    ).group_by(Tender.ai_category).all()

    category_distribution = {cat: count for cat, count in categories}

    return {
        "total_analyzed": total_analyzed,
        "total_unanalyzed": total_unanalyzed,
        "avg_relevance": round(avg_relevance, 2) if avg_relevance else None,
        "category_distribution": category_distribution,
        "high_relevance_count": high_relevance,
    }
