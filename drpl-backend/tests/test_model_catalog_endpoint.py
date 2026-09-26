"""The model catalog the Agent Builder renders from.

This endpoint exists because the frontend carried its own hardcoded copies of
the model list, provider list and per-model output ceilings. The drift was
silent rather than loud: an agent saved as `claude-opus-5` rendered as
`claude-sonnet-4-6` because the new ID was missing from the frontend array, and
the Max Tokens hint showed an 8,192 fallback for a model that supports 128,000.

These tests pin the properties the UI depends on.
"""

import pytest

from app.api.routes.agent_builder import list_models
from app.services.langchain.model_limits import MODEL_MAX_OUTPUT_TOKENS
from app.services.seed_agent_models import TIER_MODELS


@pytest.fixture(scope="module")
def catalog():
    return list_models(current_user=None)


def _by_id(catalog, model_id):
    return next((m for m in catalog["models"] if m["id"] == model_id), None)


# ── completeness ────────────────────────────────────────────────────────────


def test_every_tier_model_is_offered(catalog):
    """If a model the platform assigns to agents is missing from the catalog,
    the dropdown cannot display it — the exact bug this replaces."""
    for provider, tiers in TIER_MODELS.items():
        for tier, model_id in tiers.items():
            entry = _by_id(catalog, model_id)
            assert entry is not None, f"{provider}/{tier} {model_id} not offered"
            assert entry["is_current"] is True
            # A model may serve more than one tier — Anthropic's worker and
            # bulk are both Haiku since the two-model rollout — so the catalog
            # can only carry one of them. It must be one the table agrees with.
            serves = {t for t, m in tiers.items() if m == model_id}
            assert entry["tier"] in serves, (
                f"{model_id} catalogued as {entry['tier']}, table says {serves}"
            )


def test_all_three_providers_are_offered(catalog):
    """The old frontend list had no Google option at all, and its OpenAI list
    was assigned but never rendered."""
    assert {"anthropic", "openai", "google"} <= set(catalog["providers"])


def test_the_self_hosted_provider_is_offered_too(catalog):
    """`runpod` has to reach the Agent Builder dropdown from the same catalog
    as everything else — the frontend carries no provider list of its own, so
    a provider missing here is a provider an admin cannot see or monitor."""
    assert "runpod" in catalog["providers"]
    entry = _by_id(catalog, "qwen2.5-7b")
    assert entry is not None and entry["provider"] == "runpod"


def test_providers_are_derived_correctly(catalog):
    assert _by_id(catalog, "claude-opus-5")["provider"] == "anthropic"
    assert _by_id(catalog, "gpt-5.6-sol")["provider"] == "openai"
    assert _by_id(catalog, "gemini-3.7-flash")["provider"] == "google"


def test_superseded_models_are_still_offered(catalog):
    """An agent pinned to an old model must still display its real value
    rather than silently rendering as something else."""
    old = _by_id(catalog, "claude-sonnet-4-6")
    assert old is not None
    assert old["is_current"] is False


# ── the numbers the UI shows ────────────────────────────────────────────────


def test_output_ceilings_match_the_runtime_table(catalog):
    """The UI validates Max Tokens against this; a mismatch either blocks a
    legal value or lets through one the provider will reject."""
    for entry in catalog["models"]:
        if entry["id"] in MODEL_MAX_OUTPUT_TOKENS:
            assert entry["max_output_tokens"] == MODEL_MAX_OUTPUT_TOKENS[entry["id"]]


def test_flagship_output_ceiling_is_not_the_fallback(catalog):
    """claude-opus-5 was absent from the limits table, so it clamped to 8,192 —
    the Master Agent was silently capped at 8k output on every call."""
    assert _by_id(catalog, "claude-opus-5")["max_output_tokens"] == 128_000


def test_every_hosted_model_carries_pricing(catalog):
    """An unpriced hosted model reports zero cost, which reads as free usage."""
    for entry in catalog["models"]:
        if entry["provider"] == "runpod":
            continue      # self-hosted: see the next test
        assert entry["input_price_per_mtok"] > 0, entry["id"]
        assert entry["output_price_per_mtok"] > 0, entry["id"]


def test_a_self_hosted_model_is_priced_at_zero_deliberately(catalog):
    """Zero is the fact, not a missing entry: RunPod bills GPU-seconds, so
    there is no per-token price to state. It has to be an explicit 0 rather
    than absent, because absent falls through to DEFAULT_PRICING and would
    report every call on our own GPU at Sonnet's $3/$15."""
    from app.services.langchain.provider_config import PRICING

    entry = _by_id(catalog, "qwen2.5-7b")
    assert entry["input_price_per_mtok"] == 0.0
    assert entry["output_price_per_mtok"] == 0.0
    assert "qwen2.5-7b" in PRICING


# ── capability flags the UI gates its controls on ───────────────────────────


def test_current_claude_models_report_adaptive_thinking(catalog):
    for model_id in ("claude-opus-5", "claude-sonnet-5"):
        assert _by_id(catalog, model_id)["supports_adaptive_thinking"] is True


def test_current_claude_models_report_no_thinking_budget(catalog):
    """The UI hides the "Enabled (manual budget)" option on these. Offering it
    is not cosmetic — budget_tokens on these models is a hard 400."""
    for model_id in ("claude-opus-5", "claude-sonnet-5"):
        assert _by_id(catalog, model_id)["supports_thinking_budget"] is False


def test_current_claude_models_report_no_sampling(catalog):
    """The Temperature slider is disabled for these; the backend omits the
    parameter because sending it fails the request."""
    for model_id in ("claude-opus-5", "claude-sonnet-5"):
        assert _by_id(catalog, model_id)["supports_sampling"] is False


def test_haiku_reports_the_opposite_capabilities(catalog):
    haiku = _by_id(catalog, "claude-haiku-4-5")
    assert haiku["supports_thinking_budget"] is True
    assert haiku["supports_adaptive_thinking"] is False
    assert haiku["supports_sampling"] is True


def test_effort_levels_include_xhigh(catalog):
    assert "xhigh" in catalog["effort_levels"]


def test_max_effort_flag_is_a_subset_of_effort_support(catalog):
    for entry in catalog["models"]:
        if entry["supports_max_effort"]:
            assert entry["supports_effort"], entry["id"]
