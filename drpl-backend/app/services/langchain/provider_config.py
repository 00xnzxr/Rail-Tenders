"""
DRPL Backend - Multi-Provider Configuration
Centralizes model mappings, pricing, provider detection, API key resolution,
and fallback chain construction for Claude, OpenAI, and Google Gemini.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


# --- Model Tier Mapping ---
# Maps each Claude model to its quality-equivalent on other providers.

MODEL_EQUIVALENTS: dict[str, dict[str, str]] = {
    # ── Current generation (verified against provider docs, 2026-09-02) ──
    # Tiers are matched on capability class, not on price: the flagship tier is
    # for judgement-heavy work, the mid tier for ordinary agent work, the small
    # tier for high-volume extraction where throughput matters more than depth.
    "claude-opus-5":              {"openai": "gpt-5.6-sol",   "google": "gemini-3.1-pro-preview"},
    "claude-sonnet-5":            {"openai": "gpt-5.6-terra",  "google": "gemini-3.7-flash"},
    "claude-haiku-4-5":           {"openai": "gpt-5.6-luna",   "google": "gemini-3.5-flash-lite"},

    # ── Superseded Claude models, kept so existing rows keep resolving ──
    # An agent still pinned to one of these gets a sensible cross-provider
    # equivalent rather than falling through to DEFAULT_FALLBACK_MODELS.
    "claude-opus-4-8":            {"openai": "gpt-5.6-sol",   "google": "gemini-3.1-pro-preview"},
    "claude-opus-4-7":            {"openai": "gpt-5.6-sol",   "google": "gemini-3.1-pro-preview"},
    "claude-opus-4-6":            {"openai": "gpt-5.6-sol",   "google": "gemini-3.1-pro-preview"},
    "claude-sonnet-4-6":          {"openai": "gpt-5.6-terra",  "google": "gemini-3.7-flash"},
    "claude-opus-4-5-20251101":   {"openai": "gpt-5.6-sol",   "google": "gemini-3.1-pro-preview"},
    "claude-sonnet-4-5-20250929": {"openai": "gpt-5.6-terra",  "google": "gemini-3.7-flash"},
    "claude-haiku-4-5-20251001":  {"openai": "gpt-5.6-luna",   "google": "gemini-3.5-flash-lite"},
    "claude-opus-4-1-20250805":   {"openai": "gpt-5.6-sol",   "google": "gemini-3.1-pro-preview"},
    "claude-sonnet-4-20250514":   {"openai": "gpt-5.6-terra",  "google": "gemini-3.7-flash"},
    "claude-opus-4-20250514":     {"openai": "gpt-5.6-sol",   "google": "gemini-3.1-pro-preview"},
    "claude-3-5-sonnet-20241022": {"openai": "gpt-5.6-terra",  "google": "gemini-3.7-flash"},
    "claude-3-haiku-20240307":    {"openai": "gpt-5.6-luna",   "google": "gemini-3.5-flash-lite"},
    "claude-3-opus-20240229":     {"openai": "gpt-5.6-sol",   "google": "gemini-3.1-pro-preview"},
}

# Default fallback models when the primary isn't in MODEL_EQUIVALENTS
DEFAULT_FALLBACK_MODELS = {
    "openai": "gpt-5.6-terra",
    "google": "gemini-3.7-flash",
    "anthropic": "claude-sonnet-5",
}


# --- Unified Pricing (per 1M tokens) ---

PRICING: dict[str, dict[str, float]] = {
    # USD per 1M tokens. Verified against each provider's own pricing page on
    # 2026-09-02 — these feed the cost estimates shown to admins, so a guessed
    # figure here is worse than a missing one.
    # ── Anthropic ──
    "claude-opus-5":              {"input": 5.0,   "output": 25.0},
    "claude-sonnet-5":            {"input": 2.0,   "output": 10.0},
    "claude-haiku-4-5":           {"input": 1.0,   "output": 5.0},
    "claude-fable-5":             {"input": 10.0,  "output": 50.0},
    "claude-opus-4-8":            {"input": 5.0,   "output": 25.0},
    "claude-opus-4-7":            {"input": 5.0,   "output": 25.0},
    "claude-opus-4-6":            {"input": 5.0,   "output": 25.0},
    "claude-sonnet-4-6":          {"input": 3.0,   "output": 15.0},
    # Superseded, priced as they were when current.
    "claude-opus-4-5-20251101":   {"input": 15.0,  "output": 75.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0,   "output": 15.0},
    "claude-haiku-4-5-20251001":  {"input": 1.0,   "output": 5.0},
    "claude-opus-4-1-20250805":   {"input": 15.0,  "output": 75.0},
    "claude-sonnet-4-20250514":   {"input": 3.0,   "output": 15.0},
    "claude-opus-4-20250514":     {"input": 15.0,  "output": 75.0},
    "claude-3-5-sonnet-20241022": {"input": 3.0,   "output": 15.0},
    "claude-3-haiku-20240307":    {"input": 0.25,  "output": 1.25},
    "claude-3-opus-20240229":     {"input": 15.0,  "output": 75.0},
    # ── OpenAI ──
    "gpt-5.6-sol":                {"input": 4.0,   "output": 20.0},
    "gpt-5.6-terra":              {"input": 2.0,   "output": 12.0},
    "gpt-5.6-luna":               {"input": 0.20,  "output": 1.20},
    # Superseded.
    "gpt-4o":                     {"input": 2.50,  "output": 10.0},
    "gpt-4o-mini":                {"input": 0.15,  "output": 0.60},
    "gpt-4.1":                    {"input": 2.0,   "output": 8.0},
    "gpt-4.1-mini":               {"input": 0.40,  "output": 1.60},
    "gpt-4.1-nano":               {"input": 0.10,  "output": 0.40},
    "o3":                         {"input": 2.0,   "output": 8.0},
    "o3-mini":                    {"input": 1.10,  "output": 4.40},
    "o4-mini":                    {"input": 1.10,  "output": 4.40},
    # ── Google Gemini ──
    # Gemini prices step up for prompts over 200k tokens; the figure here is the
    # under-200k rate, which is where effectively all of our calls sit.
    "gemini-3.1-pro-preview":     {"input": 2.0,   "output": 12.0},
    # 3.7-flash is $0.75/$3.75 through 2026-12-31, then doubles to $1.50/$7.50.
    "gemini-3.7-flash":           {"input": 0.75,  "output": 3.75},
    "gemini-3.5-flash-lite":      {"input": 0.30,  "output": 2.50},
    "gemini-2.5-pro":             {"input": 1.25,  "output": 10.0},
    "gemini-2.5-flash":           {"input": 0.15,  "output": 0.60},
    "gemini-2.0-flash":           {"input": 0.10,  "output": 0.40},
    # ── Self-hosted (RunPod serverless vLLM) ──
    # Zero is the honest entry, not a placeholder: RunPod bills GPU-seconds for
    # the endpoint, so there is no per-token price to state. Without the entry
    # the model would fall through to DEFAULT_PRICING and every call would be
    # reported to the admin dashboard at Sonnet's $3/$15, which is the one
    # number about a local model that must not be wrong.
    "qwen2.5-7b":                 {"input": 0.0,   "output": 0.0},
}

# Default pricing for unknown models
DEFAULT_PRICING = {"input": 3.0, "output": 15.0}


# --- Provider Capabilities ---

PROVIDER_CAPABILITIES: dict[str, set[str]] = {
    "anthropic": {
        "thinking", "effort", "compaction", "prompt_caching",
        "native_pdf", "tool_calling", "streaming",
    },
    "openai": {
        "tool_calling", "streaming", "structured_outputs", "json_mode",
        "prompt_caching",
    },
    "google": {
        "tool_calling", "streaming", "search_grounding",
        "code_execution", "structured_outputs",
    },
    # vLLM speaks the OpenAI chat API, and Qwen2.5-Instruct is tool-capable
    # through it. No vision: Qwen2.5-7B-Instruct is text-only, so nothing that
    # reads a PDF page can ever be pointed here.
    "runpod": {"tool_calling", "streaming", "json_mode"},
}


# --- Helper Functions ---

def detect_provider(model: str) -> str:
    """Detect the provider from a model name string."""
    if not model:
        return "anthropic"
    model_lower = model.lower()
    if model_lower.startswith("claude-"):
        return "anthropic"
    if model_lower.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if model_lower.startswith("gemini-"):
        return "google"
    if model_lower.startswith("qwen") or "/" in model:
        # Self-hosted names: what vLLM was told to serve the weights as
        # ("qwen2.5-7b"), or a bare Hugging Face repo id. No hosted provider
        # uses either shape.
        return "runpod"
    return "anthropic"  # default


def get_equivalent_model(model: str, target_provider: str) -> str:
    """Get the equivalent model on a different provider."""
    equivalents = MODEL_EQUIVALENTS.get(model)
    if equivalents and target_provider in equivalents:
        return equivalents[target_provider]
    return DEFAULT_FALLBACK_MODELS.get(target_provider, model)


def get_api_key(db: Optional[Session], provider: str) -> Optional[str]:
    """
    Resolve API key for a provider: PlatformSetting → env variable.
    """
    key_map = {
        "anthropic": ("anthropic_api_key", settings.anthropic_api_key),
        "openai":    ("openai_api_key",    settings.openai_api_key),
        "google":    ("google_api_key",     settings.google_api_key),
        # Chat key first, OCR key as the fallback -- see config.
        "runpod":    ("runpod_chat_api_key", settings.runpod_chat_api_key
                                             or settings.runpod_api_key),
    }

    setting_key, env_value = key_map.get(provider, (None, ""))

    if not setting_key:
        return None

    # Try PlatformSetting first
    if db:
        try:
            from app.models.platform_setting import PlatformSetting
            setting = db.query(PlatformSetting).filter(
                PlatformSetting.key == setting_key
            ).first()
            if setting and setting.value and setting.value.strip() and setting.value != "••••••••":
                return setting.value
        except Exception:
            pass

    # Fall back to env
    return env_value if env_value else None


#: Providers that are never failed over -- neither away from nor towards.
#:
#: `runpod` is a self-hosted endpoint, and both directions matter. Falling
#: BACK to it would silently answer a Claude-tier request with a 7B model;
#: falling FORWARD off it would silently spend money on a paid provider while
#: the operator believes the local model is serving the traffic. Either way
#: the bill or the quality moves without anyone choosing it, so a pinned
#: local model stays pinned and fails loudly instead.
NO_FAILOVER_PROVIDERS = frozenset({"runpod"})


def get_base_url(db: Optional[Session], provider: str) -> Optional[str]:
    """The OpenAI-compatible base URL for a self-hosted provider.

    Only `runpod` has one. A RunPod serverless vLLM endpoint exposes the
    OpenAI chat API at ``/v2/{endpoint_id}/openai/v1``, so the endpoint id is
    the whole address -- and with no endpoint id there is nothing to call,
    which is why this returns None rather than a URL that 404s.
    """
    if provider != "runpod":
        return None
    endpoint = ""
    if db is not None:
        try:
            from app.services.settings_service import get_setting_value
            endpoint = (get_setting_value(db, "runpod_chat_endpoint_id", "") or "").strip()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    if not endpoint:
        endpoint = (settings.runpod_chat_endpoint_id or "").strip()
    if not endpoint:
        return None
    return f"https://api.runpod.ai/v2/{endpoint}/openai/v1"


# Prompt-cache multipliers against a model's input rate. Anthropic bills a
# cache read at ~0.1x and a first cache write at 1.25x; OpenAI and Google price
# cached input at a discount in the same direction. These are the Anthropic
# figures, which is where our caching actually fires.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


#: Anthropic server-side web search: $10 per 1,000 searches, billed on top of
#: tokens. Micro-dollars per search, to sit beside the per-token rates.
WEB_SEARCH_MICRO_USD = 10_000.0


def web_search_requests_of(usage) -> int:
    """Searches an Anthropic response ran (usage.server_tool_use), else 0."""
    try:
        stu = getattr(usage, "server_tool_use", None)
        if stu is None and isinstance(usage, dict):
            stu = usage.get("server_tool_use")
        if stu is None:
            return 0
        n = getattr(stu, "web_search_requests", None)
        if n is None and isinstance(stu, dict):
            n = stu.get("web_search_requests")
        return int(n or 0)
    except (TypeError, ValueError):
        return 0


def estimate_cost(model: str, usage: dict) -> float:
    """Estimate API cost in USD for any provider.

    Prices the prompt-cache components when the caller supplies them.
    `DRPLCallbackHandler` has always collected `cache_read_input_tokens` and
    `cache_creation_input_tokens`, but this function used to ignore both and
    charge every token at the fresh-input rate. With the Master Agent's prompt
    cached on every turn, that overstated cost badly — tolerable while these
    numbers only decorated an admin dashboard, not once a user is blocked at a
    real $30 limit.

    Callers that pass no cache fields price exactly as they did before.
    """
    rates = PRICING.get(model, DEFAULT_PRICING)
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0

    searches = usage.get("web_search_requests", 0) or 0

    micro = (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_read * rates["input"] * CACHE_READ_MULTIPLIER
        + cache_write * rates["input"] * CACHE_WRITE_MULTIPLIER
        + searches * WEB_SEARCH_MICRO_USD
    )
    return round(micro / 1_000_000, 6)


def provider_supports(provider: str, capability: str) -> bool:
    """Check if a provider supports a given capability."""
    return capability in PROVIDER_CAPABILITIES.get(provider, set())


def build_fallback_chain(
    db: Optional[Session],
    primary_model: str,
    pinned_provider: Optional[str] = None,
) -> list[tuple[str, str, str]]:
    """
    Build an ordered list of (provider, model, api_key) for failover.

    Args:
        db: Database session for API key resolution.
        primary_model: The primary model name (e.g. "claude-sonnet-4-6").
        pinned_provider: If set to a specific provider, returns only that provider (no failover).

    Returns:
        List of (provider, model, api_key) tuples. At least 1 entry.
        Providers without API keys are skipped.
    """
    primary_provider = detect_provider(primary_model)

    # If pinned to a specific provider (not None, not "auto")
    if pinned_provider and pinned_provider not in (None, "", "auto"):
        api_key = get_api_key(db, pinned_provider)
        if not api_key:
            # A pin is a preference, not a suicide pact. An agent row pinned to
            # a provider the deployment has no key for used to raise here, so a
            # single unconfigured credential took down every agent carrying that
            # pin -- including the seeded roster. Drop the pin and fall through
            # to the ordinary auto chain, which skips keyless providers anyway.
            logger.warning(
                "No API key for pinned provider '%s'; falling back to the auto "
                "failover chain for model '%s'.", pinned_provider, primary_model,
            )
            pinned_provider = None
        elif pinned_provider == primary_provider:
            model = primary_model
        else:
            model = get_equivalent_model(primary_model, pinned_provider)
        if pinned_provider:
            return [(pinned_provider, model, api_key)]

    # Check if failover is enabled
    failover_enabled = True
    fallback_order_str = "openai,google"
    if db:
        try:
            from app.services.settings_service import get_effective_setting
            fe = get_effective_setting(db, "failover_enabled", "true")
            failover_enabled = str(fe).lower() in ("true", "1", "yes")
            fallback_order_str = get_effective_setting(
                db, "failover_providers", "openai,google"
            ) or "openai,google"
        except Exception:
            pass
    else:
        failover_enabled = getattr(settings, "failover_enabled", True)
        fallback_order_str = getattr(settings, "failover_providers", "openai,google")

    # Build chain: primary first
    chain: list[tuple[str, str, str]] = []

    primary_key = get_api_key(db, primary_provider)
    if primary_key:
        chain.append((primary_provider, primary_model, primary_key))

    # Add fallback providers
    if failover_enabled and primary_provider not in NO_FAILOVER_PROVIDERS:
        fallback_providers = [
            p.strip() for p in fallback_order_str.split(",")
            if p.strip()
            and p.strip() != primary_provider
            and p.strip() not in NO_FAILOVER_PROVIDERS
        ]
        for fp in fallback_providers:
            fp_key = get_api_key(db, fp)
            if fp_key:
                fp_model = get_equivalent_model(primary_model, fp)
                chain.append((fp, fp_model, fp_key))

    if not chain:
        raise ValueError(
            "No AI provider API keys configured. "
            "Set at least one of ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY "
            "in Platform Settings or .env file."
        )

    return chain
