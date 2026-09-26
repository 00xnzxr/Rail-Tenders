"""
DRPL Backend - LangChain LLM Factory
Creates LangChain chat model instances with automatic provider failover.

Supports Claude (primary), OpenAI, and Google Gemini via a unified interface.
On rate limits (429), automatically retries with the next provider in the chain.

Uses the existing config cascade:
  Per-agent override -> Platform setting -> Environment variable -> Defaults

Claude-specific features (Extended Thinking, Effort, Compaction, Prompt Caching)
are preserved for Anthropic and gracefully skipped for other providers.
"""

import asyncio
import logging
from typing import Any, Callable, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.agent_builder import CustomAgent
from app.services.ai_service import (
    _get_agent_config,
    _get_effective_model,
    _get_effective_api_key,
    _resolve_thinking_config,
    _resolve_effort,
    _CONTINUATION_NUDGE,
    _EMPTY_RETRY_NUDGE,
    _MAX_CONTINUATIONS,
    get_streaming_callback,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _build_model_kwargs(
    model: str,
    agent_config: dict,
    db: Session = None,
) -> tuple[dict, dict]:
    """
    Build extra model_kwargs for ChatAnthropic, including thinking, effort,
    context management (compaction/editing), and prompt caching.

    Returns:
        Tuple of (model_kwargs dict, extra_headers dict).
    """
    kwargs = {}
    extra_headers = {}

    # Resolve thinking config
    thinking_config = _resolve_thinking_config(model, agent_config, db)
    if thinking_config:
        kwargs["thinking"] = thinking_config

    # Resolve effort
    effort = _resolve_effort(model, agent_config, db)
    if effort:
        kwargs["output_config"] = {"effort": effort}

    # Resolve context management (compaction, tool clearing, thinking clearing, caching)
    if db:
        from app.services.context_service import resolve_context_config
        context_config = resolve_context_config(db, model, agent_config)

        # `context_management` is an Anthropic beta feature (compact-2026-01-12 +
        # context-management-2025-06-27). The Python SDK accepts it as a
        # top-level field starting in v0.90+, but earlier versions reject it
        # with `AsyncMessages.create() got an unexpected keyword argument
        # 'context_management'`. Probe the SDK once and only forward the
        # kwarg when the installed version supports it natively. Users on
        # older SDKs simply don't get compaction in the LangChain path —
        # the raw HTTP path (`_call_anthropic`) still passes it through the
        # request body via httpx, which works regardless of SDK version.
        if context_config.get("context_management"):
            if _sdk_supports_context_management():
                kwargs["context_management"] = context_config["context_management"]
            else:
                logger.debug(
                    "anthropic SDK does not accept context_management as a "
                    "kwarg — skipping in LangChain path. Upgrade `anthropic` "
                    "to >=0.90 to enable compaction in agent runs."
                )

        if context_config.get("prompt_caching_enabled") and context_config.get("cache_control"):
            kwargs["cache_control"] = context_config["cache_control"]

        beta_headers = context_config.get("beta_headers", [])
        if beta_headers:
            extra_headers["anthropic-beta"] = ",".join(beta_headers)

    return kwargs, extra_headers


def _sdk_supports_context_management() -> bool:
    """Cached probe: does the installed Anthropic SDK accept ``context_management``
    as a top-level kwarg on ``messages.create``?

    Cached on the function object so we only inspect the signature once per
    process (it's stable for the SDK's lifetime).
    """
    cached = getattr(_sdk_supports_context_management, "_cached", None)
    if cached is not None:
        return cached
    try:
        import anthropic
        import inspect
        sig = inspect.signature(anthropic.AsyncAnthropic().messages.create)
        supported = "context_management" in sig.parameters
    except Exception:
        supported = False
    setattr(_sdk_supports_context_management, "_cached", supported)
    return supported


def _build_failover_or_direct(
    db: Session,
    model: str,
    temperature: Optional[float],
    max_tokens: int,
    extra_kwargs: dict,
    extra_headers: dict,
    pinned_provider: Optional[str] = None,
) -> BaseChatModel:
    """
    Build either a direct model or a FailoverChatModel depending on
    how many providers are available.
    """
    from app.services.langchain.provider_config import build_fallback_chain, get_base_url
    from app.services.langchain.model_factory import create_chat_model

    chain = build_fallback_chain(db, model, pinned_provider)

    if len(chain) == 1:
        # Single provider — return direct model (no wrapper overhead)
        provider, resolved_model, api_key = chain[0]

        # For non-Anthropic, ensure temperature is set (thinking constraint doesn't apply)
        temp = temperature
        if provider != "anthropic" and temp is None:
            temp = 0.7

        return create_chat_model(
            provider=provider,
            model=resolved_model,
            api_key=api_key,
            temperature=temp,
            max_tokens=max_tokens,
            model_kwargs=extra_kwargs if provider == "anthropic" else None,
            extra_headers=extra_headers if provider == "anthropic" else None,
            # None for every hosted provider; the address of the self-hosted
            # endpoint for `runpod`. Resolved here because this is where the
            # session is, and `build_fallback_chain` guarantees a self-hosted
            # provider only ever arrives as a chain of one.
            base_url=get_base_url(db, provider),
        )

    # Multiple providers — wrap in FailoverChatModel
    from app.services.langchain.failover_model import FailoverChatModel
    return FailoverChatModel(
        providers=chain,
        temperature=temperature,
        max_tokens=max_tokens,
        claude_kwargs=extra_kwargs,
        claude_headers=extra_headers,
    )


# Agents that get downgraded to Haiku 4.5 when the `agent_models_tier`
# PlatformSetting is "haiku" (the default after Phase 3a). These are extractive
# / single-pass tasks that don't need Sonnet's reasoning — flipping them to
# Haiku cuts their per-call cost ~75%. Flip `agent_models_tier="sonnet"` via
# PlatformSetting to revert without a code deploy.
#
# proposal_router (intent classification) was added 2026-05-04 — it's a
# 1-2 sentence routing decision called on every chat message; Sonnet on it
# was wasted latency. The kill-switch reverts router quality if regressions
# show up on ambiguous / multi-step requests.
# tender_doc_analyzer was removed on 2026-09-02: it produces the 10-section
# forensic report the go/no-go decision rests on, and running that on the bulk
# tier was the largest complexity/model mismatch on the platform. Its tier is
# now set from seed_agent_models.AGENT_TIERS like every other agent.
_HAIKU_TIER_AGENTS = {
    "deep_analyzer",
    "checklist_generator",
    "annexure_finder",
    "proposal_router",
}
_HAIKU_MODEL = "claude-haiku-4-5"


def _resolve_agent_tier_model(
    db: Session,
    agent_name: Optional[str],
    default_model: str,
    agent_config: Optional[dict] = None,
) -> str:
    """Apply the `agent_models_tier` kill-switch — but only for agents that
    have NOT been explicitly configured by the admin.

    When tier="haiku" (default) and the agent is in the Haiku tier set, AND
    the admin has not set a per-agent model override in Agent Builder, the
    agent runs on Haiku 4.5 to cut cost. As soon as the admin picks a model
    in Agent Builder for that agent, the explicit choice wins — the tier
    downgrade is skipped — because that selection reflects a quality
    requirement, not just a cost preference.

    Tier="sonnet" still reverts every tier-listed agent to its default-
    cascade model regardless of explicit overrides; that's the global
    one-click revert path.
    """
    if not agent_name or agent_name not in _HAIKU_TIER_AGENTS:
        return default_model
    # Explicit per-agent model override (Agent Builder UI write to
    # CustomAgent.model or AgentConfig.ai_model) always wins. The tier
    # downgrade is for agents still on the unconfigured cascade default.
    if agent_config and agent_config.get("model"):
        return default_model
    try:
        from app.services.settings_service import get_effective_setting
        tier = get_effective_setting(db, "agent_models_tier", "haiku")
    except Exception:
        return default_model
    if isinstance(tier, str) and tier.lower() == "haiku":
        return _HAIKU_MODEL
    return default_model


def get_chat_model(
    db: Session,
    agent_name: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    thinking_mode_override: Optional[str] = None,
    default_model: Optional[str] = None,
) -> BaseChatModel:
    """
    Create a chat model instance using the existing DRPL config cascade,
    with automatic provider failover on rate limits.

    Resolution order for each setting:
      1. Explicit parameter (if provided)
      2. Per-agent config from AgentConfig table
      3. Platform setting from PlatformSetting table
      4. Environment variable / .env file

    `thinking_mode_override` lets a caller force-disable (or force-enable)
    extended thinking for a specific call without touching the agent's
    persistent config. Used by the costing agent to disable thinking by
    default (with a `costing_thinking_enabled` PlatformSetting kill-switch).

    Returns a BaseChatModel — may be ChatAnthropic, ChatOpenAI,
    ChatGoogleGenerativeAI, or FailoverChatModel depending on config.
    """
    agent_config = _get_agent_config(db, agent_name) if agent_name else {}

    # Resolve model — agent_config["model"] from CustomAgent wins; else falls
    # through to PlatformSetting `ai_model` (default `claude-sonnet-5`).
    pre_tier_model = _get_effective_model(db, agent_config)
    # `default_model` lets a caller name its own tier default (the Master Agent
    # runs on the flagship tier, not the platform worker default) WITHOUT
    # overriding an admin's explicit choice: it applies only when the agent row
    # has no model of its own, so Agent Builder still wins.
    if default_model and not agent_config.get("model"):
        pre_tier_model = default_model
    # Apply Haiku-tier downgrade for extractive agents (kill-switch via PlatformSetting).
    # Pass agent_config so an explicit per-agent model override skips the downgrade —
    # admin-chosen models reflect quality requirements, not just cost.
    model = _resolve_agent_tier_model(db, agent_name, pre_tier_model, agent_config=agent_config)
    if thinking_mode_override is not None:
        # Stash on the agent_config dict so _resolve_thinking_config (called
        # inside _build_model_kwargs) sees the override before falling back
        # to the platform setting.
        agent_config = {**agent_config, "thinking_mode": thinking_mode_override}
    resolved_temp = temperature if temperature is not None else (
        0.7 if agent_config.get("temperature") in (None, "") else agent_config.get("temperature")
    )
    resolved_max = max_tokens if max_tokens is not None else (agent_config.get("max_tokens") or 4096)

    # Diagnostic — log the resolved config so the user can see in backend logs
    # exactly what each agent invocation is using. Critical for debugging the
    # "Agent Builder shows X but billing shows Y" disconnect.
    if agent_name:
        config_source = agent_config.get("_source", "unknown")
        thinking_label = (
            agent_config.get("thinking_mode") or "auto"
        ) if thinking_mode_override is None else thinking_mode_override
        tier_note = (
            f" (tier-downgraded from {pre_tier_model})"
            if model != pre_tier_model else ""
        )
        logger.info(
            f"[llm_factory] resolved agent='{agent_name}' model={model}{tier_note} "
            f"max_tokens={resolved_max} thinking_mode={thinking_label} "
            f"config_source={config_source}"
        )

    # Build thinking/effort/context kwargs (Claude-specific, skipped for others)
    extra_kwargs, extra_headers = _build_model_kwargs(model, agent_config, db)

    # When thinking is enabled, adjust max_tokens if budget_tokens would exceed it
    thinking = extra_kwargs.get("thinking")
    if thinking and thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens", 0)
        if resolved_max <= budget:
            resolved_max = budget + resolved_max

    # Omit temperature when thinking is active OR when the model dropped
    # sampling params altogether (the 4.6+ family rejects it with a 400 even
    # with thinking disabled). Belt-and-braces with the same check in
    # model_factory: this path also feeds the OpenAI/Google branches, where
    # temperature IS still valid, so the decision has to be per-model.
    from app.services.ai_service import supports_sampling_params

    effective_temp = (
        None if (thinking or not supports_sampling_params(model)) else resolved_temp
    )

    # Get pinned provider from agent config (None = auto failover)
    pinned_provider = agent_config.get("provider")

    return _build_failover_or_direct(
        db=db,
        model=model,
        temperature=effective_temp,
        max_tokens=resolved_max,
        extra_kwargs=extra_kwargs,
        extra_headers=extra_headers,
        pinned_provider=pinned_provider,
    )


async def safe_ainvoke(
    llm: BaseChatModel,
    messages: list[BaseMessage],
    *,
    agent_name: str = "langchain_agent",
    streaming_callback: Optional[Callable[[str, dict], None]] = None,
    max_continuations: int = _MAX_CONTINUATIONS,
    config: Optional[dict] = None,
) -> AIMessage:
    """Reliable wrapper for ``llm.ainvoke`` with auto-continuation and empty retry.

    Equivalent to the reliability wrapper baked into the raw httpx Anthropic
    path (``_call_anthropic``), but operates at the LangChain layer so agents
    that build a ``ChatAnthropic`` via :func:`get_chat_model` get the same
    semantics:

      * If the response's ``stop_reason`` is ``"max_tokens"``, append the
        partial output as an assistant message + a continuation nudge as a
        new user message, then re-invoke. Up to ``max_continuations`` rounds.
      * If the response is empty (no text content) with a non-truncation
        stop reason, append a polite nudge and retry once.

    The returned ``AIMessage`` carries the concatenated ``content`` and
    ``response_metadata`` from the final invocation, so callers that read
    ``message.content`` see one cohesive answer.

    Use this in place of ``await llm.ainvoke(messages)`` for plain text
    generation calls. For ``create_react_agent`` / agent executors, wrap
    the executor's call instead — those manage their own message loops.
    """

    def _stop_reason_of(msg: Any) -> str:
        if msg is None:
            return ""
        meta = getattr(msg, "response_metadata", None) or {}
        return str(
            meta.get("stop_reason")
            or meta.get("finish_reason")
            or ""
        )

    def _content_text(msg: Any) -> str:
        # ChatAnthropic returns a string; some backends return a list of dicts.
        c = getattr(msg, "content", "") if msg is not None else ""
        if isinstance(c, list):
            return "".join(
                block.get("text", "")
                for block in c
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return c or ""

    # Fall back to the ContextVar when the caller didn't pass an explicit
    # callback — the streaming SSE handler installs one before invoking the
    # agent so deep call sites (this helper, _call_anthropic, the LangChain
    # callback handler) all funnel events through the same place.
    effective_cb = streaming_callback or get_streaming_callback()

    def _emit(event_type: str, payload: dict) -> None:
        if not effective_cb:
            return
        try:
            result = effective_cb(event_type, payload)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as e:
            logger.debug(f"streaming_callback failed for {event_type}: {e}")

    invoke_kwargs = {"config": config} if config else {}

    # First call
    response = await llm.ainvoke(messages, **invoke_kwargs)
    accumulated_text = _content_text(response)
    stop_reason = _stop_reason_of(response)
    last_response = response

    # Auto-continue on truncation
    current_messages = list(messages)
    continuations = 0
    while (
        stop_reason == "max_tokens"
        and continuations < max_continuations
        and accumulated_text
    ):
        continuations += 1
        _emit("assistant_continuing", {
            "agent": agent_name,
            "attempt": continuations,
            "max_attempts": max_continuations,
            "reason": "max_tokens",
        })
        current_messages = current_messages + [
            AIMessage(content=accumulated_text),
            HumanMessage(content=_CONTINUATION_NUDGE),
        ]
        try:
            cont_response = await llm.ainvoke(current_messages, **invoke_kwargs)
        except Exception as e:
            logger.warning(
                f"[safe_ainvoke] {agent_name} continuation #{continuations} "
                f"failed: {type(e).__name__}: {e}"
            )
            break
        cont_text = _content_text(cont_response)
        if not cont_text:
            break
        accumulated_text += cont_text
        stop_reason = _stop_reason_of(cont_response)
        last_response = cont_response

    if continuations > 0 and stop_reason != "max_tokens":
        _emit("assistant_resumed", {
            "agent": agent_name,
            "continuations": continuations,
        })

    # Empty-content retry (only on first call, no continuations attempted)
    is_empty_genuine = (
        not (accumulated_text or "").strip()
        and stop_reason != "max_tokens"
        and continuations == 0
    )
    if is_empty_genuine:
        logger.warning(
            f"[safe_ainvoke] {agent_name}: empty content "
            f"(stop_reason={stop_reason!r}) — retrying once with nudge"
        )
        _emit("assistant_retried_empty", {
            "agent": agent_name,
            "stop_reason": stop_reason,
        })
        retry_messages = list(messages) + [HumanMessage(content=_EMPTY_RETRY_NUDGE)]
        try:
            retry_response = await llm.ainvoke(retry_messages, **invoke_kwargs)
            retry_text = _content_text(retry_response)
            if retry_text.strip():
                accumulated_text = retry_text
                last_response = retry_response
        except Exception as e:
            logger.warning(
                f"[safe_ainvoke] {agent_name} empty-retry failed: {type(e).__name__}: {e}"
            )

    # Return a single AIMessage carrying the merged text. Preserve the final
    # invocation's response_metadata so callers that inspect stop_reason,
    # token usage, etc. see the last segment's metadata.
    final_meta = getattr(last_response, "response_metadata", {}) or {}
    return AIMessage(content=accumulated_text, response_metadata=final_meta)


def get_chat_model_for_custom_agent(
    db: Session,
    agent: CustomAgent,
) -> BaseChatModel:
    """
    Create a chat model instance directly from a CustomAgent's configuration,
    with automatic provider failover on rate limits.
    Falls back to platform/env settings for missing values.
    """
    # Use agent's own model or fall back to config cascade
    model = agent.model or _get_effective_model(db, {})
    temperature = agent.temperature if agent.temperature is not None else 0.7
    max_tokens = agent.max_tokens if agent.max_tokens is not None else 4096

    # Apply langchain_config overrides if present
    lc_config = agent.langchain_config or {}
    if isinstance(lc_config, str):
        try:
            import json
            lc_config = json.loads(lc_config)
        except (json.JSONDecodeError, TypeError):
            lc_config = {}
    if not isinstance(lc_config, dict):
        lc_config = {}
    if "temperature" in lc_config:
        temperature = lc_config["temperature"]
    if "max_tokens" in lc_config:
        max_tokens = lc_config["max_tokens"]

    # Build agent_config dict for thinking/effort resolution
    agent_config = {
        "thinking_mode": getattr(agent, "thinking_mode", None),
        "thinking_budget_tokens": getattr(agent, "thinking_budget_tokens", None),
        "effort": getattr(agent, "effort", None),
    }
    # Also check langchain_config for thinking overrides
    if "thinking_mode" in lc_config:
        agent_config["thinking_mode"] = lc_config["thinking_mode"]
    if "thinking_budget_tokens" in lc_config:
        agent_config["thinking_budget_tokens"] = lc_config["thinking_budget_tokens"]
    if "effort" in lc_config:
        agent_config["effort"] = lc_config["effort"]

    extra_kwargs, extra_headers = _build_model_kwargs(model, agent_config, db)

    # Adjust max_tokens for thinking budget
    thinking = extra_kwargs.get("thinking")
    if thinking and thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens", 0)
        if max_tokens <= budget:
            max_tokens = budget + max_tokens

    # Only set temperature when thinking is NOT active
    effective_temp = None if thinking else temperature

    # Get pinned provider from agent (None/empty/"auto" = failover enabled)
    pinned_provider = getattr(agent, "provider", None)

    return _build_failover_or_direct(
        db=db,
        model=model,
        temperature=effective_temp,
        max_tokens=max_tokens,
        extra_kwargs=extra_kwargs,
        extra_headers=extra_headers,
        pinned_provider=pinned_provider,
    )
