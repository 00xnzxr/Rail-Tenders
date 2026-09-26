"""
DRPL Backend - Failover Chat Model
A LangChain BaseChatModel wrapper that automatically retries with fallback
providers when the primary provider hits rate limits (HTTP 429).

Design:
- Each instance is created per-request (no shared mutable state).
- Provider advancement is one-directional within a request.
- Next request starts fresh from the primary provider.
- When tools are bound, they are stored for re-binding on failover.
"""

import logging
from typing import Any, Iterator, AsyncIterator, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun, AsyncCallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult, ChatGenerationChunk

logger = logging.getLogger(__name__)


def _is_rate_limit_error(error: Exception) -> bool:
    """Check if an exception is a rate limit error from any provider."""
    error_str = str(error).lower()
    error_class = type(error).__name__

    # Anthropic: anthropic.RateLimitError, HTTP 429
    # OpenAI: openai.RateLimitError, HTTP 429
    # Google: google.api_core.exceptions.ResourceExhausted
    if error_class in ("RateLimitError", "ResourceExhausted"):
        return True
    if "429" in error_str:
        return True
    if "rate" in error_str and "limit" in error_str:
        return True
    if "resource" in error_str and "exhausted" in error_str:
        return True

    return False


def _strip_cache_control(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Drop Anthropic `cache_control` markers from content blocks.

    Callers mark stable prompt blocks cacheable for Anthropic. When the chain
    fails over to another provider those markers would be forwarded verbatim
    and rejected as an unknown field, so they are removed on the copy that
    goes to the fallback provider. The text itself is untouched.
    """
    out: list[BaseMessage] = []
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, list) and any(
            isinstance(b, dict) and "cache_control" in b for b in content
        ):
            cleaned = [
                {k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b
                for b in content
            ]
            m = m.model_copy(update={"content": cleaned})
        out.append(m)
    return out


class FailoverChatModel(BaseChatModel):
    """
    A chat model that wraps multiple providers with automatic failover.

    On rate limit errors, advances to the next provider in the chain,
    rebuilds the inner model, re-binds any tools, and retries.
    """

    # Configuration (immutable after construction)
    providers: list  # [(provider, model, api_key), ...]
    temperature: Optional[float] = 0.7
    max_tokens: int = 4096
    claude_kwargs: dict = {}   # model_kwargs for Anthropic (thinking, effort, etc.)
    claude_headers: dict = {}  # extra_headers for Anthropic (beta headers)

    # Internal state
    _current_index: int = 0
    _inner: Optional[BaseChatModel] = None
    _bound_tools: list = []
    _bind_tools_kwargs: dict = {}

    class Config:
        arbitrary_types_allowed = True
        underscore_attrs_are_private = True

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        # Build the initial inner model
        self._current_index = 0
        self._inner = self._build_inner(0)
        self._bound_tools = []
        self._bind_tools_kwargs = {}

    @property
    def _llm_type(self) -> str:
        return "drpl-failover"

    @property
    def _identifying_params(self) -> dict:
        provider, model, _ = self.providers[self._current_index]
        return {"provider": provider, "model": model, "fallback_count": len(self.providers)}

    def _build_inner(self, index: int) -> BaseChatModel:
        """Build the BaseChatModel for the provider at the given index."""
        from app.services.langchain.model_factory import create_chat_model

        provider, model, api_key = self.providers[index]

        # Only pass Claude-specific kwargs to Anthropic
        model_kwargs = self.claude_kwargs if provider == "anthropic" else None
        extra_headers = self.claude_headers if provider == "anthropic" else None

        # For non-Anthropic providers, always use temperature (thinking constraint doesn't apply)
        temperature = self.temperature
        if provider != "anthropic" and temperature is None:
            temperature = 0.7

        return create_chat_model(
            provider=provider,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=self.max_tokens,
            model_kwargs=model_kwargs,
            extra_headers=extra_headers,
        )

    def _messages_for_current(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """Messages as the current provider accepts them."""
        provider, _, _ = self.providers[self._current_index]
        return messages if provider == "anthropic" else _strip_cache_control(messages)

    def _record_rate_limit(self) -> None:
        """Emit a 429 metric for the current provider before advancing."""
        try:
            from app.core.llm_metrics import record_rate_limit
            provider, _, _ = self.providers[self._current_index]
            record_rate_limit(provider)
        except Exception:
            logger.debug("failover: 429 metric record failed", exc_info=True)

    def _advance_provider(self) -> bool:
        """
        Advance to the next provider in the chain.

        Returns True if advanced successfully, False if chain is exhausted.
        """
        next_index = self._current_index + 1
        if next_index >= len(self.providers):
            return False

        old_provider, old_model, _ = self.providers[self._current_index]
        new_provider, new_model, _ = self.providers[next_index]

        logger.warning(
            f"[FAILOVER] Rate limit hit on {old_provider}/{old_model}, "
            f"switching to {new_provider}/{new_model}"
        )

        self._current_index = next_index
        self._inner = self._build_inner(next_index)

        # Re-bind tools if they were bound
        if self._bound_tools:
            try:
                self._inner = self._inner.bind_tools(
                    self._bound_tools, **self._bind_tools_kwargs
                )
            except Exception as e:
                logger.warning(
                    f"[FAILOVER] Failed to re-bind tools on {new_provider}: {e}. "
                    f"Continuing without tool binding."
                )

        return True

    def bind_tools(self, tools: list, **kwargs: Any) -> "FailoverChatModel":
        """Bind tools to the model, storing them for re-binding on failover."""
        # Create a new FailoverChatModel with tools stored
        new_model = FailoverChatModel(
            providers=self.providers,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            claude_kwargs=self.claude_kwargs,
            claude_headers=self.claude_headers,
        )
        new_model._current_index = self._current_index
        new_model._bound_tools = list(tools)
        new_model._bind_tools_kwargs = dict(kwargs)

        # Bind tools on the inner model
        new_model._inner = self._inner.bind_tools(tools, **kwargs)

        return new_model

    # --- Synchronous Generation ---

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Generate with failover on rate limits."""
        while True:
            try:
                return self._inner._generate(self._messages_for_current(messages), stop=stop, run_manager=run_manager, **kwargs)
            except Exception as e:
                if _is_rate_limit_error(e):
                    self._record_rate_limit()
                    if self._advance_provider():
                        continue
                raise

    # --- Async Generation ---

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Async generate with failover on rate limits."""
        while True:
            try:
                return await self._inner._agenerate(self._messages_for_current(messages), stop=stop, run_manager=run_manager, **kwargs)
            except Exception as e:
                if _is_rate_limit_error(e):
                    self._record_rate_limit()
                    if self._advance_provider():
                        continue
                raise

    # --- Streaming ---

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Stream with failover — catches errors before first chunk."""
        while True:
            try:
                yield from self._inner._stream(self._messages_for_current(messages), stop=stop, run_manager=run_manager, **kwargs)
                return
            except Exception as e:
                if _is_rate_limit_error(e):
                    self._record_rate_limit()
                    if self._advance_provider():
                        continue
                raise

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Async stream with failover — catches errors before first chunk."""
        while True:
            try:
                async for chunk in self._inner._astream(
                    self._messages_for_current(messages), stop=stop, run_manager=run_manager, **kwargs
                ):
                    yield chunk
                return
            except Exception as e:
                if _is_rate_limit_error(e):
                    self._record_rate_limit()
                    if self._advance_provider():
                        continue
                raise
