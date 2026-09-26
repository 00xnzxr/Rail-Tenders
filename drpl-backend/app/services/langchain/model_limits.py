"""
DRPL Backend - Per-model max-output-token limits.

Maps each supported model to its actual *completion* (output) token ceiling.
This is distinct from the context window (total input+output) tracked in
context_service.py. Use `clamp_max_tokens` before sending requests to any
provider so that an overly-large agent configuration cannot crash the call.

Source: provider docs, verified 2026-09-02.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


MODEL_MAX_OUTPUT_TOKENS: dict[str, int] = {
    # ── Anthropic — current generation ──
    # 128K completion. Verified 2026-09-02. Anything missing here silently
    # clamps to DEFAULT_MAX_OUTPUT (8_192) — claude-opus-5 was absent, so the
    # Master Agent was capped at 8k output on every call.
    "claude-fable-5":             128_000,
    "claude-opus-5":              128_000,
    "claude-sonnet-5":            128_000,
    "claude-opus-4-8":            128_000,
    # Anthropic — Claude 4.x
    "claude-opus-4-7":            128_000,
    "claude-opus-4-6":            128_000,
    "claude-sonnet-4-6":          128_000,
    "claude-haiku-4-5-20251001":  32_000,
    "claude-haiku-4-5":           32_000,
    "claude-opus-4-5-20251101":   32_000,
    "claude-sonnet-4-5-20250929": 64_000,
    "claude-opus-4-1-20250805":   32_000,
    "claude-sonnet-4-20250514":   64_000,
    "claude-opus-4-20250514":     32_000,
    # Anthropic — legacy Claude 3.x
    "claude-3-5-sonnet-20241022":  8_192,
    "claude-3-haiku-20240307":     4_096,
    "claude-3-opus-20240229":      4_096,
    # OpenAI — GPT 4.x
    "gpt-4o":        16_384,
    "gpt-4o-mini":   16_384,
    "gpt-4.1":       32_768,
    "gpt-4.1-mini":  32_768,
    "gpt-4.1-nano":  32_768,
    "gpt-4-turbo":    4_096,
    "gpt-3.5-turbo":  4_096,
    # OpenAI — GPT 5.x (128k completion ceiling per API error response)
    # ── OpenAI — current generation (128K output, 1.05M context) ──
    "gpt-5.6-sol":      128_000,
    "gpt-5.6-terra":    128_000,
    "gpt-5.6-luna":     128_000,
    "gpt-5":            128_000,
    "gpt-5-mini":       128_000,
    "gpt-5-nano":       128_000,
    # OpenAI — reasoning models
    "o1":       100_000,
    "o1-pro":   100_000,
    "o1-mini":  100_000,
    "o3":       100_000,
    "o3-pro":   100_000,
    "o3-mini":  100_000,
    "o4-mini":  100_000,
    # Google Gemini
    "gemini-2.5-pro":        8_192,
    "gemini-2.5-flash":      8_192,
    "gemini-2.5-flash-lite": 8_192,
    "gemini-2.0-flash":      8_192,
    # Gemini 3.x output ceilings are not published on the models page; they
    # fall through to DEFAULT_MAX_OUTPUT rather than carry a guessed number.
    # Verify before assigning a Gemini model to a long-output agent.
    # ── Self-hosted (RunPod serverless vLLM) ──
    # Qwen2.5-7B-Instruct's native window is 32K, and on vLLM the prompt and
    # the completion share it. 8K leaves room for a real prompt; asking for
    # more than the server's max_model_len is a 400 from vLLM, not a clamp.
    "qwen2.5-7b": 8_192,
}

# Safe default for any model not listed above.
DEFAULT_MAX_OUTPUT = 8_192


def clamp_max_tokens(model: str, requested: int) -> int:
    """
    Clamp a requested max_tokens value to the model's actual completion ceiling.

    Args:
        model: The model name (e.g. "claude-sonnet-4-6", "gpt-4o-mini").
        requested: The max_tokens value configured on the agent / call.

    Returns:
        A value guaranteed to be within the model's supported range.
        Logs a warning if the requested value exceeded the limit.
    """
    limit = MODEL_MAX_OUTPUT_TOKENS.get(model, DEFAULT_MAX_OUTPUT)
    try:
        requested_int = int(requested or 0)
    except (TypeError, ValueError):
        requested_int = 0

    if requested_int <= 0:
        return limit

    if requested_int > limit:
        logger.warning(
            "Clamping max_tokens for %s: %d -> %d (model's completion ceiling)",
            model, requested_int, limit,
        )
        return limit

    return requested_int


def get_model_output_ceiling(model: str) -> int:
    """Return the model's hard completion-token ceiling (no clamp logic)."""
    return MODEL_MAX_OUTPUT_TOKENS.get(model, DEFAULT_MAX_OUTPUT)


def compute_dynamic_max_tokens(
    model: str,
    *,
    input_chars: int = 0,
    requested: Optional[int] = None,
    floor: int = 4_096,
    ceiling_fraction: float = 1.0,
    thinking_budget: int = 0,
) -> int:
    """Pick a sensible max_tokens for a single call.

    The agent builder lets an admin set a fixed ``max_tokens`` per agent, but
    most agents pick conservative numbers (4K–24K) that silently truncate on
    long tenders. Sonnet 4.6 for example supports 64K output tokens — leaving
    those on the table forces our reliability layer to do continuation rounds
    instead of finishing in one call.

    This helper resolves a target output budget per *call* using:

      * ``requested`` — if the caller already has an explicit budget (e.g.
        from a CustomAgent record), respect it but still clamp to the model
        ceiling.
      * ``input_chars`` — rough proxy for input size. Anthropic charges total
        tokens (input + output + thinking), and the context window is shared,
        so we leave room for input. ~4 chars per token; we leave a 5K-token
        safety margin on top.
      * ``thinking_budget`` — when extended thinking is enabled, the
        thinking_budget is consumed *out of* max_tokens. We bake it in so the
        model has the requested room to think AND produce output.
      * ``ceiling_fraction`` — caller can request "give me 80% of the
        ceiling" if they want to leave headroom for retries.

    Returns at least ``floor`` (default 4K) and at most the model's hard
    ceiling. Logs the chosen value for diagnostic visibility.
    """
    ceiling = MODEL_MAX_OUTPUT_TOKENS.get(model, DEFAULT_MAX_OUTPUT)

    # If the caller explicitly requested a budget, respect it (clamped).
    if requested:
        try:
            req = max(int(requested), floor)
        except (TypeError, ValueError):
            req = floor
        if thinking_budget:
            req = max(req, thinking_budget + floor)
        return min(req, ceiling)

    # Estimate input tokens from char count (4 chars per token is a common
    # heuristic for English; matches Anthropic's own rough guidance).
    estimated_input = int(input_chars / 4) if input_chars else 0
    safety_margin = 5_000  # ~5K tokens reserved for input growth + overhead

    target = int(ceiling * ceiling_fraction)
    if thinking_budget:
        # Thinking eats from max_tokens; ensure room for both.
        target = max(target, thinking_budget + floor)

    # When we know the input is enormous, pull max_tokens down so we don't
    # overshoot the context window. (Context windows are typically 200K
    # tokens, so this only matters for very large inputs.)
    if estimated_input > 0:
        # Anthropic context windows: opus-4-* and sonnet-4-* are 200K.
        # Reserve enough for input + safety, give the rest to output.
        context_window = 200_000
        available_for_output = context_window - estimated_input - safety_margin
        if available_for_output < target:
            target = max(available_for_output, floor)

    final = max(min(target, ceiling), floor)
    logger.debug(
        "compute_dynamic_max_tokens: model=%s input_chars=%d -> max_tokens=%d "
        "(ceiling=%d, requested=%s, thinking_budget=%d)",
        model, input_chars, final, ceiling, requested, thinking_budget,
    )
    return final


# --- Token Budget Protocol ---
#
# Appended to specialized-agent system prompts so the model self-monitors its
# output budget and prioritizes the most important sections under pressure.
# Combined with the reliability layer's auto-continuation, agents should both
# (a) prioritize correctly when budgets are tight, and (b) get the full ceiling
# when they need it.

_TOKEN_BUDGET_PROTOCOL_TEMPLATE = """
## Token Budget Protocol

You have approximately **{max_tokens} output tokens** for this response.

### Priority hierarchy (most important → least important)
{priority_hierarchy}

### Self-monitoring rules
- Before starting each new section, mentally estimate remaining tokens.
- If you have less than ~1500 tokens left, finish the highest-priority outstanding item in 2–3 concise bullets and STOP. Do not start new sections.
- A complete, decisive answer to the top 3 priorities is more valuable than a half-finished answer to all of them.
- Use bullet points and short paragraphs over long prose when the budget is tight.

### Continuation contract
- If your previous turn was cut off and a continuation request arrives, do NOT repeat earlier content. Pick up at the next character or section boundary and finish the response.
"""


def render_token_budget_protocol(
    *,
    max_tokens: int,
    priority_hierarchy: str,
) -> str:
    """Render the Token Budget Protocol block for inclusion in a system prompt.

    ``priority_hierarchy`` should be a numbered or bulleted markdown list
    describing the order in which the agent should prioritize sections of its
    output. Example for a tender analyzer::

        1. Eligibility GO/NO-GO determination
        2. Submission deadline and key dates
        3. Critical compliance requirements
        4. Negative keywords / rejection risks
        5. Document checklist + missing items
        6. Regulatory intelligence
        7. Prioritized next steps
    """
    return _TOKEN_BUDGET_PROTOCOL_TEMPLATE.format(
        max_tokens=max_tokens,
        priority_hierarchy=priority_hierarchy.strip(),
    )
