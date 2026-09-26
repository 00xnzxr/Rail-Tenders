"""
DRPL Backend - Context Management Service
Multi-provider context window management for Claude, OpenAI, and Gemini:
1. Context Windows — Model sizes and capabilities across all providers
2. Compaction — Server-side conversation summarization (Claude beta: compact-2026-01-12)
3. Context Editing — Tool result clearing & thinking block clearing (Claude beta)
4. Prompt Caching — Claude (cache_control) and OpenAI (automatic)
5. Token Counting — Anthropic API, tiktoken (OpenAI), or estimation (Gemini)
"""

import json
import logging
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.platform_setting import PlatformSetting

logger = logging.getLogger(__name__)
settings = get_settings()

# -------------------------------------------------------------------
# 1. Context Window Sizes (per model)
# -------------------------------------------------------------------

MODEL_CONTEXT_WINDOWS = {
    # 1M context window models (Claude 4.6)
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    # 200k context window models (can be extended to 1M with beta header for some)
    "claude-opus-4-5-20251101": 200_000,
    "claude-sonnet-4-5-20250929": 200_000,   # 1M with beta header context-1m-2025-08-07
    "claude-haiku-4-5-20251001": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-opus-4-1-20250805": 200_000,
    "claude-sonnet-4-20250514": 200_000,      # 1M with beta header context-1m-2025-08-07
    "claude-opus-4-20250514": 200_000,
    # Legacy models
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    "claude-3-haiku-20240307": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1-nano": 1_047_576,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    # Google Gemini
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
}

# Models that support context awareness (track remaining tokens)
CONTEXT_AWARE_MODELS = {"claude-sonnet-4-6", "claude-sonnet-4-5-20250929", "claude-haiku-4-5", "claude-haiku-4-5-20251001"}

# Models that support compaction (beta)
COMPACTION_MODELS = {"claude-opus-4-6", "claude-sonnet-4-6"}

# Models that support context editing (beta)
CONTEXT_EDITING_MODELS = {
    "claude-opus-4-6", "claude-sonnet-4-6",
    "claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929",
    "claude-opus-4-1-20250805",
    "claude-sonnet-4-20250514", "claude-opus-4-20250514",
    "claude-haiku-4-5-20251001",
}

# Minimum cacheable tokens per model
CACHE_MIN_TOKENS = {
    "claude-opus-4-6": 4096,
    "claude-opus-4-5-20251101": 4096,
    "claude-haiku-4-5": 4096,
    "claude-haiku-4-5-20251001": 4096,
    "claude-sonnet-4-6": 2048,
    "claude-haiku-3-5": 2048,
    "claude-opus-4-1-20250805": 1024,
    "claude-sonnet-4-5-20250929": 1024,
    "claude-sonnet-4-20250514": 1024,
    "claude-opus-4-20250514": 1024,
    # OpenAI (automatic caching, 50% discount on cache reads)
    "gpt-4o": 1024,
    "gpt-4o-mini": 1024,
    "gpt-4.1": 1024,
    "gpt-4.1-mini": 1024,
    "gpt-4.1-nano": 1024,
    "o3": 1024,
    "o3-mini": 1024,
    "o4-mini": 1024,
}


def get_context_window_size(model: str) -> int:
    """Get the context window size for a model in tokens."""
    return MODEL_CONTEXT_WINDOWS.get(model, 200_000)


def get_model_context_info(model: str) -> dict:
    """Get comprehensive context info for a model."""
    from app.services.langchain.provider_config import detect_provider
    return {
        "model": model,
        "provider": detect_provider(model),
        "context_window": get_context_window_size(model),
        "supports_compaction": model in COMPACTION_MODELS,
        "supports_context_editing": model in CONTEXT_EDITING_MODELS,
        "context_aware": model in CONTEXT_AWARE_MODELS,
        "min_cache_tokens": CACHE_MIN_TOKENS.get(model, 1024),
        "supports_caching": model in CACHE_MIN_TOKENS,
    }


def get_all_models_context_info() -> list[dict]:
    """Get context info for all known models."""
    return [get_model_context_info(m) for m in MODEL_CONTEXT_WINDOWS]


# -------------------------------------------------------------------
# 2. Compaction Configuration
# -------------------------------------------------------------------

def build_compaction_config(
    enabled: bool = True,
    trigger_tokens: int = 150_000,
    instructions: Optional[str] = None,
    pause_after: bool = False,
) -> Optional[dict]:
    """
    Build compaction configuration for the context_management.edits array.

    Compaction is a beta feature (compact-2026-01-12) for Claude 4.6 models.
    Automatically summarizes conversation when input tokens exceed trigger threshold.

    Args:
        enabled: Whether to include compaction.
        trigger_tokens: Token threshold to trigger compaction (min 50,000).
        instructions: Custom summarization prompt (replaces default).
        pause_after: Whether to pause after generating the compaction summary.

    Returns:
        Compaction edit config dict, or None if disabled.
    """
    if not enabled:
        return None

    config = {
        "type": "compact_20260112",
        "trigger": {"type": "input_tokens", "value": max(50_000, trigger_tokens)},
    }

    if pause_after:
        config["pause_after_compaction"] = True

    if instructions:
        config["instructions"] = instructions

    return config


# -------------------------------------------------------------------
# 3. Context Editing Configuration
# -------------------------------------------------------------------

def build_tool_clearing_config(
    enabled: bool = True,
    trigger_tokens: int = 100_000,
    keep_tool_uses: int = 5,
    clear_at_least_tokens: Optional[int] = None,
    exclude_tools: Optional[list[str]] = None,
    clear_tool_inputs: bool = False,
) -> Optional[dict]:
    """
    Build tool result clearing config for context_management.edits.

    Clears old tool results when conversation context grows beyond threshold.
    Beta feature: context-management-2025-06-27.

    Args:
        enabled: Whether to enable tool clearing.
        trigger_tokens: Token threshold to trigger clearing.
        keep_tool_uses: Number of recent tool uses to keep.
        clear_at_least_tokens: Minimum tokens to clear per activation.
        exclude_tools: Tool names to never clear.
        clear_tool_inputs: Also clear tool call inputs (not just results).
    """
    if not enabled:
        return None

    config = {
        "type": "clear_tool_uses_20250919",
        "trigger": {"type": "input_tokens", "value": trigger_tokens},
        "keep": {"type": "tool_uses", "value": keep_tool_uses},
    }

    if clear_at_least_tokens:
        config["clear_at_least"] = {"type": "input_tokens", "value": clear_at_least_tokens}

    if exclude_tools:
        config["exclude_tools"] = exclude_tools

    if clear_tool_inputs:
        config["clear_tool_inputs"] = True

    return config


def build_thinking_clearing_config(
    enabled: bool = True,
    keep: str = "last",  # "all", "last", or number
    keep_value: int = 1,
) -> Optional[dict]:
    """
    Build thinking block clearing config for context_management.edits.

    Manages thinking blocks in conversations with extended thinking enabled.
    Default API behavior keeps only last turn's thinking.

    Args:
        enabled: Whether to enable thinking clearing.
        keep: "all" to preserve all, "last" for last N turns.
        keep_value: Number of thinking turns to keep (when keep="last").
    """
    if not enabled:
        return None

    config = {"type": "clear_thinking_20251015"}

    if keep == "all":
        config["keep"] = "all"
    else:
        config["keep"] = {"type": "thinking_turns", "value": keep_value}

    return config


def build_context_management(
    model: str,
    compaction_config: Optional[dict] = None,
    tool_clearing_config: Optional[dict] = None,
    thinking_clearing_config: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """
    Build the full context_management object and required beta headers.

    Returns:
        Tuple of (context_management dict or None, list of beta header strings).
    """
    edits = []
    betas = []

    if compaction_config and model in COMPACTION_MODELS:
        edits.append(compaction_config)
        betas.append("compact-2026-01-12")

    if tool_clearing_config and model in CONTEXT_EDITING_MODELS:
        edits.append(tool_clearing_config)
        if "context-management-2025-06-27" not in betas:
            betas.append("context-management-2025-06-27")

    if thinking_clearing_config and model in CONTEXT_EDITING_MODELS:
        edits.append(thinking_clearing_config)
        if "context-management-2025-06-27" not in betas:
            betas.append("context-management-2025-06-27")

    if not edits:
        return None, betas

    return {"edits": edits}, betas


# -------------------------------------------------------------------
# 4. Prompt Caching Configuration
# -------------------------------------------------------------------

def build_cache_control(
    ttl: str = "5m",
) -> dict:
    """
    Build a cache_control object for content blocks.

    Args:
        ttl: Cache TTL — "5m" (default, 1.25x cost) or "1h" (2x cost, better for infrequent).
    """
    config = {"type": "ephemeral"}
    if ttl == "1h":
        config["ttl"] = "1h"
    return config


def should_enable_caching(
    model: str,
    estimated_tokens: int = 0,
) -> bool:
    """Check if caching should be enabled based on model and token count."""
    min_tokens = CACHE_MIN_TOKENS.get(model, 1024)
    return estimated_tokens >= min_tokens


# -------------------------------------------------------------------
# 5. Token Counting
# -------------------------------------------------------------------

async def count_tokens(
    db: Session,
    model: str,
    messages: list[dict],
    system: Optional[str] = None,
    tools: Optional[list[dict]] = None,
    thinking: Optional[dict] = None,
) -> dict:
    """
    Count tokens for a message across providers.

    - Anthropic: Free Token Counting API (/v1/messages/count_tokens)
    - OpenAI: Local tiktoken library (free, fast)
    - Gemini: Character-based estimation (~4 chars per token)

    Returns:
        {"input_tokens": N} or {"input_tokens": N, "error": "..."}
    """
    from app.services.langchain.provider_config import detect_provider
    provider = detect_provider(model)

    if provider == "anthropic":
        return await _count_tokens_anthropic(db, model, messages, system, tools, thinking)
    elif provider == "openai":
        return _count_tokens_openai(model, messages, system)
    else:
        return _estimate_tokens(messages, system)


async def _count_tokens_anthropic(
    db: Session,
    model: str,
    messages: list[dict],
    system: Optional[str] = None,
    tools: Optional[list[dict]] = None,
    thinking: Optional[dict] = None,
) -> dict:
    """Count tokens via the free Anthropic Token Counting API."""
    from app.services.ai_service import _get_effective_api_key

    api_key = _get_effective_api_key(db)
    if not api_key:
        return {"input_tokens": 0, "error": "API key not configured"}

    request_body = {
        "model": model,
        "messages": messages,
    }

    if system:
        request_body["system"] = system
    if tools:
        request_body["tools"] = tools
    if thinking:
        request_body["thinking"] = thinking

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages/count_tokens",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=request_body,
            )

            if response.status_code != 200:
                return {"input_tokens": 0, "error": f"API error: {response.status_code}"}

            data = response.json()
            return {"input_tokens": data.get("input_tokens", 0)}

    except Exception as e:
        logger.error(f"Token counting failed: {e}")
        return {"input_tokens": 0, "error": str(e)}


def _count_tokens_openai(
    model: str,
    messages: list[dict],
    system: Optional[str] = None,
) -> dict:
    """Count tokens locally using tiktoken for OpenAI models."""
    try:
        import tiktoken
    except ImportError:
        return _estimate_tokens(messages, system)

    try:
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")

        total = 0
        if system:
            total += len(encoding.encode(system)) + 4  # system message overhead

        for msg in messages:
            total += 4  # per-message overhead
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(encoding.encode(content))
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        total += len(encoding.encode(block.get("text", "")))
            role = msg.get("role", "")
            total += len(encoding.encode(role))

        total += 2  # reply priming
        return {"input_tokens": total}

    except Exception as e:
        logger.error(f"tiktoken counting failed: {e}")
        return _estimate_tokens(messages, system)


def _estimate_tokens(
    messages: list[dict],
    system: Optional[str] = None,
) -> dict:
    """Estimate tokens using character-based heuristic (~4 chars per token)."""
    total_chars = 0
    if system:
        total_chars += len(system)
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total_chars += len(block.get("text", ""))
    estimated = total_chars // 4
    return {"input_tokens": estimated, "note": "Estimated (character-based)"}


# -------------------------------------------------------------------
# Settings Resolution
# -------------------------------------------------------------------

def _get_context_setting(db: Session, key: str, default=None):
    """Get a platform setting value."""
    from app.services.settings_service import get_effective_setting
    return get_effective_setting(db, key, default)


def resolve_context_config(
    db: Session,
    model: str,
    agent_config: dict = None,
) -> dict:
    """
    Resolve the full context management configuration for an API call.

    Combines platform settings, per-agent overrides, and model capabilities.
    Non-Claude providers get an empty config (all features are Claude-specific).

    Returns dict with keys:
        - context_management: dict or None (for request body)
        - beta_headers: list of beta header strings
        - cache_control: dict or None (for top-level or block-level caching)
        - prompt_caching_enabled: bool
    """
    # Compaction, context editing, and thinking clearing are Claude-only beta features.
    # OpenAI supports automatic prompt caching (no explicit config needed).
    from app.services.langchain.provider_config import detect_provider
    provider = detect_provider(model)
    if provider != "anthropic":
        return {
            "context_management": None,
            "beta_headers": [],
            "cache_control": None,
            "prompt_caching_enabled": provider == "openai",
        }

    agent_config = agent_config or {}

    # --- Compaction ---
    compaction_enabled = agent_config.get("compaction_enabled")
    if compaction_enabled is None:
        compaction_enabled = _get_context_setting(db, "compaction_enabled", False)
    if isinstance(compaction_enabled, str):
        compaction_enabled = compaction_enabled.lower() in ("true", "1", "yes")

    compaction_trigger = int(agent_config.get("compaction_trigger_tokens") or
                            _get_context_setting(db, "compaction_trigger_tokens", 150000) or 150000)

    compaction_instructions = agent_config.get("compaction_instructions") or \
                              _get_context_setting(db, "compaction_instructions", None)

    compaction_config = build_compaction_config(
        enabled=bool(compaction_enabled),
        trigger_tokens=compaction_trigger,
        instructions=compaction_instructions,
    )

    # --- Tool Result Clearing ---
    tool_clearing_enabled = agent_config.get("tool_clearing_enabled")
    if tool_clearing_enabled is None:
        tool_clearing_enabled = _get_context_setting(db, "tool_clearing_enabled", False)
    if isinstance(tool_clearing_enabled, str):
        tool_clearing_enabled = tool_clearing_enabled.lower() in ("true", "1", "yes")

    tool_clearing_trigger = int(agent_config.get("tool_clearing_trigger_tokens") or
                                _get_context_setting(db, "tool_clearing_trigger_tokens", 100000) or 100000)

    tool_clearing_keep = int(agent_config.get("tool_clearing_keep_uses") or
                             _get_context_setting(db, "tool_clearing_keep_uses", 5) or 5)

    tool_clearing_config = build_tool_clearing_config(
        enabled=bool(tool_clearing_enabled),
        trigger_tokens=tool_clearing_trigger,
        keep_tool_uses=tool_clearing_keep,
    )

    # --- Thinking Block Clearing ---
    thinking_clearing_enabled = agent_config.get("thinking_clearing_enabled")
    if thinking_clearing_enabled is None:
        thinking_clearing_enabled = _get_context_setting(db, "thinking_clearing_enabled", False)
    if isinstance(thinking_clearing_enabled, str):
        thinking_clearing_enabled = thinking_clearing_enabled.lower() in ("true", "1", "yes")

    thinking_clearing_config = build_thinking_clearing_config(
        enabled=bool(thinking_clearing_enabled),
    )

    # --- Build context_management ---
    context_management, beta_headers = build_context_management(
        model=model,
        compaction_config=compaction_config,
        tool_clearing_config=tool_clearing_config,
        thinking_clearing_config=thinking_clearing_config,
    )

    # --- Prompt Caching ---
    prompt_caching_enabled = agent_config.get("prompt_caching_enabled")
    if prompt_caching_enabled is None:
        prompt_caching_enabled = _get_context_setting(db, "prompt_caching_enabled", True)
    if isinstance(prompt_caching_enabled, str):
        prompt_caching_enabled = prompt_caching_enabled.lower() in ("true", "1", "yes")

    cache_ttl = agent_config.get("cache_ttl") or _get_context_setting(db, "cache_ttl", "5m") or "5m"

    cache_control = build_cache_control(cache_ttl) if prompt_caching_enabled else None

    return {
        "context_management": context_management,
        "beta_headers": beta_headers,
        "cache_control": cache_control,
        "prompt_caching_enabled": bool(prompt_caching_enabled),
    }
