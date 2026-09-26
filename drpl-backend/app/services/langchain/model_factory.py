"""
DRPL Backend - Multi-Provider Model Factory
Creates the appropriate LangChain BaseChatModel for any supported provider.
Claude-specific features (thinking, effort, compaction) are only applied
to the Anthropic constructor — other providers get basic parameters only.
"""

import logging
from typing import Optional

from langchain_core.language_models import BaseChatModel

from app.services.langchain.model_limits import clamp_max_tokens

logger = logging.getLogger(__name__)


def create_chat_model(
    provider: str,
    model: str,
    api_key: str,
    temperature: Optional[float] = 0.7,
    max_tokens: int = 4096,
    model_kwargs: Optional[dict] = None,
    extra_headers: Optional[dict] = None,
    base_url: Optional[str] = None,
) -> BaseChatModel:
    """
    Create a LangChain BaseChatModel for the specified provider.

    Args:
        provider: "anthropic", "openai", "google", or "runpod"
        model: The model name (e.g. "claude-sonnet-4-6", "gpt-4o-mini", "gemini-2.5-flash")
        api_key: The API key for the provider
        temperature: Temperature setting (None = provider default / thinking mode)
        max_tokens: Maximum output tokens
        model_kwargs: Claude-specific kwargs (thinking, effort, compaction) — ignored for non-Anthropic
        extra_headers: Claude beta headers — ignored for non-Anthropic
        base_url: OpenAI-compatible endpoint, required for "runpod" and
            ignored by the hosted providers (see provider_config.get_base_url)

    Returns:
        A LangChain BaseChatModel instance ready for use
    """
    if provider == "anthropic":
        return _create_anthropic(model, api_key, temperature, max_tokens, model_kwargs, extra_headers)
    elif provider == "openai":
        return _create_openai(model, api_key, temperature, max_tokens)
    elif provider == "google":
        return _create_google(model, api_key, temperature, max_tokens)
    elif provider == "runpod":
        return _create_runpod(model, api_key, temperature, max_tokens, base_url)
    else:
        raise ValueError(f"Unsupported provider: {provider}")


def _create_anthropic(
    model: str,
    api_key: str,
    temperature: Optional[float],
    max_tokens: int,
    model_kwargs: Optional[dict] = None,
    extra_headers: Optional[dict] = None,
) -> BaseChatModel:
    """Create a ChatAnthropic instance with full Claude feature support."""
    from langchain_anthropic import ChatAnthropic

    chat_kwargs: dict = {
        "model": model,
        "anthropic_api_key": api_key,
        "max_tokens": clamp_max_tokens(model, max_tokens),
    }

    # Temperature is omitted in two cases, and both are hard 400s if you get
    # them wrong:
    #   1. Thinking is active — the long-standing API constraint.
    #   2. The model dropped sampling params entirely (the 4.6+ family). This
    #      one applies even with thinking disabled, so the check below is NOT
    #      redundant with the thinking check: an agent configured
    #      thinking_mode="disabled" on Opus 5 would otherwise send
    #      temperature=0.7 and fail every call.
    from app.services.ai_service import supports_sampling_params

    thinking = (model_kwargs or {}).get("thinking")
    if not thinking and temperature is not None and supports_sampling_params(model):
        chat_kwargs["temperature"] = temperature

    if model_kwargs:
        chat_kwargs["model_kwargs"] = model_kwargs

    if extra_headers:
        chat_kwargs["default_headers"] = extra_headers

    return ChatAnthropic(**chat_kwargs)


def _create_openai(
    model: str,
    api_key: str,
    temperature: Optional[float],
    max_tokens: int,
) -> BaseChatModel:
    """Create a ChatOpenAI instance with basic parameters."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise ImportError(
            "langchain-openai is not installed. Run: pip install langchain-openai"
        )

    chat_kwargs: dict = {
        "model": model,
        "openai_api_key": api_key,
        "max_tokens": clamp_max_tokens(model, max_tokens),
    }

    if temperature is not None:
        chat_kwargs["temperature"] = temperature

    # langchain-openai moves some models (gpt-5.x among them) onto the
    # Responses API by itself, and that API stores every request by default.
    # Nothing here chains on a stored response, so keep the tender text out
    # of OpenAI's store on either API.
    if "store" in getattr(ChatOpenAI, "model_fields", {}):
        chat_kwargs["store"] = False

    return ChatOpenAI(**chat_kwargs)


def _create_runpod(
    model: str,
    api_key: str,
    temperature: Optional[float],
    max_tokens: int,
    base_url: Optional[str],
) -> BaseChatModel:
    """A self-hosted model on a RunPod serverless vLLM endpoint.

    vLLM serves the OpenAI chat API, so this is `ChatOpenAI` pointed at a
    different address -- no second client library, and every existing agent
    path works against it unchanged.

    Two things are deliberate. The missing base URL raises rather than
    defaulting to api.openai.com: without it the call would be answered by a
    paid provider under a model id it does not have, which is a silent wrong
    bill instead of a clear misconfiguration. And the timeout is a serverless
    COLD START (the sibling OCR endpoint measured 160 s cold, 1.5 s warm), not
    a latency budget -- a shorter one makes a scaled-to-zero endpoint look
    broken.
    """
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise ImportError(
            "langchain-openai is not installed. Run: pip install langchain-openai"
        )

    if not base_url:
        raise ValueError(
            "provider 'runpod' needs an endpoint: set runpod_chat_endpoint_id "
            "in Platform Settings (or RUNPOD_CHAT_ENDPOINT_ID in .env)."
        )

    from app.core.config import get_settings

    chat_kwargs: dict = {
        "model": model,
        "openai_api_key": api_key,
        "base_url": base_url,
        "max_tokens": clamp_max_tokens(model, max_tokens),
        "timeout": get_settings().runpod_chat_timeout_s,
        # One retry only. A cold endpoint is slow, not flaky, and the retry
        # budget belongs to the caller that knows whether the work is worth
        # waiting twice for.
        "max_retries": 1,
    }

    if temperature is not None:
        chat_kwargs["temperature"] = temperature

    # langchain-openai moves some models (gpt-5.x among them) onto the
    # Responses API by itself, and that API stores every request by default.
    # Nothing here chains on a stored response, so keep the tender text out
    # of OpenAI's store on either API.
    if "store" in getattr(ChatOpenAI, "model_fields", {}):
        chat_kwargs["store"] = False

    return ChatOpenAI(**chat_kwargs)


def _create_google(
    model: str,
    api_key: str,
    temperature: Optional[float],
    max_tokens: int,
) -> BaseChatModel:
    """Create a ChatGoogleGenerativeAI instance with basic parameters."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    chat_kwargs: dict = {
        "model": model,
        "google_api_key": api_key,
        "max_output_tokens": clamp_max_tokens(model, max_tokens),
    }

    if temperature is not None:
        chat_kwargs["temperature"] = temperature

    return ChatGoogleGenerativeAI(**chat_kwargs)
