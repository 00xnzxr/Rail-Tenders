"""Models that reject `temperature` must never be sent one.

Sampling params were removed across the newer model families. Passing one is a
hard 400, not a silently-ignored field, so every request builder has to consult
`supports_sampling_params` before setting it.

Two gaps were live at once:

- The set listed only Anthropic models. The two-model rollout moved eleven
  agents onto `gpt-5.6-luna`, which rejects `temperature` exactly as Opus 5
  does — every one of those agents would have 400'd on every call.
- `ai_service`'s own request builders never consulted the guard at all. It was
  wired into the LangChain path (`llm_factory`, `model_factory`) and the Agent
  Builder catalog route, so the omission was invisible from those call sites —
  but `call_ai` is a real path, and this is what
  `annexure_finder` was dying of: "Anthropic API error: `temperature` is
  deprecated for this model", once per annexure.
"""

import inspect

import pytest

from app.services.ai_service import (
    NO_SAMPLING_PARAM_MODELS,
    supports_sampling_params,
)

#: Every model the platform can actually be configured onto, from the tier
#: table and the roster — the set has to be right for all of them, not just
#: the ones someone remembered.
REJECTS_TEMPERATURE = (
    "claude-opus-5",
    "claude-sonnet-5",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
)

ACCEPTS_TEMPERATURE = (
    "claude-haiku-4-5",
    "claude-3-haiku-20240307",
    # vLLM honours temperature, and `_call_runpod` sends it — a deterministic
    # probe (temperature 0.0) that silently sampled would stop being a probe.
    "qwen2.5-7b",
)


@pytest.mark.parametrize("model", REJECTS_TEMPERATURE)
def test_models_that_reject_temperature_are_listed(model):
    assert not supports_sampling_params(model), model


@pytest.mark.parametrize("model", ACCEPTS_TEMPERATURE)
def test_models_that_accept_temperature_are_not_listed(model):
    """Over-listing is not free: omitting temperature from a model that honours
    it silently changes that agent's output."""
    assert supports_sampling_params(model), model


def test_every_rostered_model_is_classified_deliberately():
    """A model an agent can be rostered onto must be a considered case here,
    not an accident of whoever last edited the set."""
    from app.services.seed_agent_models import PREFERRED_AGENT_MODELS, TIER_MODELS

    rostered = {m for _p, m in PREFERRED_AGENT_MODELS.values()}
    tiered = {m for tiers in TIER_MODELS.values() for m in tiers.values()}
    known = set(REJECTS_TEMPERATURE) | set(ACCEPTS_TEMPERATURE)

    unclassified = sorted((rostered | tiered) - known - {
        # Google models are sent temperature through a different client and are
        # out of scope for this set.
        "gemini-3.1-pro-preview", "gemini-3.7-flash", "gemini-3.5-flash-lite",
    })
    assert unclassified == [], f"unclassified by this test: {unclassified}"


@pytest.mark.parametrize("builder", ["_call_anthropic", "_call_openai"])
def test_request_builders_consult_the_guard(builder):
    """The guard existed and neither of these called it.

    `_call_gemini` is deliberately not covered: no Google model is in the set,
    and it passes temperature through `GenerateContentConfig` rather than a
    JSON body. Adding a branch there would be dead code guarding a case that
    has never existed."""
    from app.services import ai_service

    fn = getattr(ai_service, builder, None)
    if fn is None:
        pytest.skip(f"{builder} does not exist")
    assert "supports_sampling_params" in inspect.getsource(fn), (
        f"{builder} sets temperature without asking whether the model takes one"
    )
