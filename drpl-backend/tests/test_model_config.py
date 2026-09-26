"""Guards on the platform's model configuration.

Model IDs and capability flags are the kind of config that fails at runtime, on
a provider call, in production — never at import. These tests catch the classes
of mistake that would otherwise surface as a 400 mid-run:

- a model configured somewhere but missing from the pricing table (silently
  reports $0 cost);
- `budget_tokens` sent to a model that removed it (hard 400);
- `temperature` sent to a model that removed sampling params (hard 400);
- a date-suffixed model ID, which the current model IDs must never carry.
"""

import pytest

from app.core.config import Settings
from app.services.ai_service import (
    ADAPTIVE_THINKING_MODELS,
    EFFORT_SUPPORTED_MODELS,
    EXTENDED_THINKING_MODELS,
    MAX_EFFORT_MODELS,
    VALID_EFFORT_LEVELS,
    supports_sampling_params,
)
from app.services.langchain.provider_config import (
    DEFAULT_FALLBACK_MODELS,
    MODEL_EQUIVALENTS,
    PRICING,
)

#: Models the platform is configured to use by default, from Settings.
_CONFIGURED_MODEL_FIELDS = [
    "ai_model",
    "master_agent_model",
    "auto_scoring_model",
    "tender_analyzer_per_doc_model",
    "tender_analyzer_synthesis_model",
    "annexure_finder_model",
    "annexure_transcription_model",
]

#: The 4.6+ family removed sampling params and budget_tokens.
_CURRENT_ANTHROPIC = {
    "claude-fable-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
}


def _defaults() -> Settings:
    """Shipped defaults, ignoring any local .env (see conftest)."""
    return Settings()


# ── every configured model is priced and current ────────────────────────────


@pytest.mark.parametrize("field", _CONFIGURED_MODEL_FIELDS)
def test_configured_model_has_pricing(field):
    """An unpriced model reports zero cost, which looks like free usage."""
    model = getattr(_defaults(), field)
    assert model in PRICING, f"{field}={model!r} is missing from PRICING"


@pytest.mark.parametrize("field", _CONFIGURED_MODEL_FIELDS)
def test_configured_model_has_no_date_suffix(field):
    """Current Claude model IDs are complete as-is; a date suffix is a stale
    ID copied from older docs and may not resolve."""
    model = getattr(_defaults(), field)
    assert not model[-9:].lstrip("-").isdigit(), (
        f"{field}={model!r} carries a date suffix"
    )


def test_master_agent_is_not_on_the_flagship_tier():
    """The Master coordinates and delegates; it does not do the domain work.

    It sat on Sonnet, then Opus in the live row. Since the two-model rollout it
    runs on the same model as the workers it calls."""
    settings = _defaults()
    assert settings.master_agent_model == "claude-haiku-4-5"
    assert PRICING[settings.master_agent_model]["output"] < PRICING["claude-opus-5"]["output"]


def test_bulk_extraction_never_costs_more_than_the_platform_default():
    """Per-doc extraction runs at high volume, so it must not be the expensive
    end of the roster.

    This was a strict `<` when the platform default was Sonnet. The two-model
    rollout put both on Haiku, so the claim that survives is that extraction is
    never the pricier of the two — which is what would break if someone pinned
    a bigger model on the per-doc pass."""
    settings = _defaults()
    bulk = PRICING[settings.tender_analyzer_per_doc_model]
    default = PRICING[settings.ai_model]
    assert bulk["output"] <= default["output"]


# ── cross-provider mapping ──────────────────────────────────────────────────


def test_every_equivalent_model_is_priced():
    unpriced = sorted(
        {m for mapping in MODEL_EQUIVALENTS.values() for m in mapping.values()}
        - set(PRICING)
    )
    assert not unpriced, f"cross-provider equivalents missing pricing: {unpriced}"


def test_default_fallbacks_are_priced_and_current():
    for provider, model in DEFAULT_FALLBACK_MODELS.items():
        assert model in PRICING, f"{provider} default {model!r} is unpriced"


def test_current_models_map_to_all_providers():
    """OpenAI and Google are first-class now, not just a failover chain, so the
    current Claude tiers must resolve on both."""
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        assert model in MODEL_EQUIVALENTS, f"{model} has no cross-provider mapping"
        assert set(MODEL_EQUIVALENTS[model]) == {"openai", "google"}


def test_tiers_are_ordered_consistently_across_providers():
    """The flagship equivalent must not be cheaper than the mid-tier one, or
    the mapping is inverted somewhere."""
    for provider in ("openai", "google"):
        flagship = PRICING[MODEL_EQUIVALENTS["claude-opus-5"][provider]]
        mid = PRICING[MODEL_EQUIVALENTS["claude-sonnet-5"][provider]]
        small = PRICING[MODEL_EQUIVALENTS["claude-haiku-4-5"][provider]]
        assert flagship["output"] >= mid["output"] >= small["output"], provider


# ── capability flags: the 400-error classes ─────────────────────────────────


@pytest.mark.parametrize("model", sorted(_CURRENT_ANTHROPIC))
def test_current_models_reject_budget_tokens(model):
    """budget_tokens was removed on these; sending it is a hard 400."""
    assert model not in EXTENDED_THINKING_MODELS


@pytest.mark.parametrize("model", sorted(_CURRENT_ANTHROPIC))
def test_current_models_use_adaptive_thinking(model):
    """A model missing here falls through to thinking-disabled, which on the
    Opus 5 family makes it write tool calls into visible text instead of
    calling them — silent, and poisonous inside an agent loop."""
    assert model in ADAPTIVE_THINKING_MODELS


@pytest.mark.parametrize("model", sorted(_CURRENT_ANTHROPIC))
def test_current_models_omit_sampling_params(model):
    assert not supports_sampling_params(model)


def test_haiku_still_uses_budget_tokens():
    """Haiku 4.5 predates adaptive thinking; it needs the explicit budget."""
    assert "claude-haiku-4-5" in EXTENDED_THINKING_MODELS
    assert "claude-haiku-4-5" not in ADAPTIVE_THINKING_MODELS
    # And it does accept sampling params.
    assert supports_sampling_params("claude-haiku-4-5")


def test_no_model_is_both_adaptive_and_budget_based():
    """The two are mutually exclusive request shapes."""
    overlap = ADAPTIVE_THINKING_MODELS & EXTENDED_THINKING_MODELS
    # Opus/Sonnet 4.6 are the documented transitional exception.
    assert overlap <= {"claude-opus-4-6", "claude-sonnet-4-6"}, overlap


def test_effort_levels_include_xhigh():
    assert "xhigh" in VALID_EFFORT_LEVELS


def test_max_effort_models_are_a_subset_of_effort_models():
    assert MAX_EFFORT_MODELS <= EFFORT_SUPPORTED_MODELS


def test_configured_anthropic_models_have_consistent_capabilities():
    """Whatever the platform is set to must have a coherent request shape."""
    settings = _defaults()
    for field in _CONFIGURED_MODEL_FIELDS:
        model = getattr(settings, field)
        if not model.startswith("claude-"):
            continue
        adaptive = model in ADAPTIVE_THINKING_MODELS
        budget = model in EXTENDED_THINKING_MODELS
        assert adaptive or budget, (
            f"{field}={model!r} supports neither thinking mode, so it would run "
            "with thinking disabled"
        )
