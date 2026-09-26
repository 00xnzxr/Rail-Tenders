"""The haiku/luna roster: no agent and no shipped pin runs on Sonnet or Opus.

The platform ran a mixed roster — Opus for the Master and verbatim annexure
transcription, Sonnet for analysis and drafting, Terra for costing research.
This is the deliberate move to two models: Haiku 4.5 for work that has to be
right (the Master, tender analysis, PDF extraction, costing) and Luna for work
that is short and structurally simple (scoring, routing, checklists).

The tests here are drift tests, not behaviour tests. Nothing stops someone
adding a Sonnet pin back in a month; these fail when they do.
"""

import pytest

from app.core.config import Settings
from app.services.langchain.provider_config import PRICING
from app.services.seed_agent_models import (
    AGENT_TIERS,
    PREFERRED_AGENT_MODELS,
    target_model,
)

HAIKU = "claude-haiku-4-5"
LUNA = "gpt-5.6-luna"

#: Work that has to be right: the coordinator, everything that reads a tender
#: or a PDF, everything that produces a number or a document you submit.
HAIKU_AGENTS = (
    "decision_maker",
    "general_assistant",
    "tender_doc_analyzer",
    "deep_analyzer",
    "document_analyzer",
    "tender_analysis",
    "tender_pipeline",
    "costing_researcher",
    "doc-costing-analyst",
    "annexure_finder",
    "proposal_creator",
    "proposal",
    "doc-technical-writer",
    "doc-compliance-writer",
)

#: Short, high-volume, structurally simple: a score, a category, a routing
#: decision, a checklist row.
LUNA_AGENTS = (
    "proposal_router",
    "classifier",
    "relevance",
    "risk",
    "summary",
    "eligibility",
    "checklist",
    "checklist_generator",
    "workspace_manager",
    "doc-letter-writer",
    "costing_scope_extractor",
)


def test_master_agent_runs_on_haiku():
    """The one the user asked for by name."""
    assert PREFERRED_AGENT_MODELS["decision_maker"] == ("anthropic", HAIKU)


@pytest.mark.parametrize("key", HAIKU_AGENTS)
def test_work_that_has_to_be_right_runs_on_haiku(key):
    assert PREFERRED_AGENT_MODELS[key] == ("anthropic", HAIKU), key


@pytest.mark.parametrize("key", LUNA_AGENTS)
def test_simple_work_runs_on_luna(key):
    assert PREFERRED_AGENT_MODELS[key] == ("openai", LUNA), key


def test_the_roster_uses_only_haiku_and_luna():
    """Two models, deliberately. A third means someone added a pin."""
    assert {m for _p, m in PREFERRED_AGENT_MODELS.values()} == {HAIKU, LUNA}


def test_every_tiered_agent_is_rostered():
    """An agent with a tier but no roster entry keeps whatever it had.

    The rollout only rewrites rows it names, so a missing entry is silent — the
    agent simply stays on Sonnet and nobody finds out until the bill.
    """
    missing = sorted(set(AGENT_TIERS) - set(PREFERRED_AGENT_MODELS))
    assert missing == [], f"tiered but not rostered: {missing}"


def test_roster_agrees_with_the_tier_table():
    """The trap: a roster entry the drift pass disagrees with is undone.

    `seed_agent_models` applies the roster and then, in the same call, walks
    every row again and moves anything not on its tier's model. A roster entry
    the tier table would overwrite gets applied and reverted on one startup —
    which looks exactly like the rollout never ran.
    """
    for key, (provider, model) in PREFERRED_AGENT_MODELS.items():
        assert target_model(key, provider) == model, (
            f"{key}: roster says {model}, tier table says "
            f"{target_model(key, provider)}"
        )


def test_both_roster_models_are_priced():
    """An unpriced model reports zero cost, which reads as free usage — and the
    per-run cost figure this rollout is paired with would be a lie."""
    for model in (HAIKU, LUNA):
        assert model in PRICING, model


# ── the config pins, which no agent row covers ──────────────────────────────


def _shipped_model_defaults() -> dict[str, str]:
    """Every shipped setting whose value names a model."""
    settings = Settings()
    return {
        name: value
        for name, value in vars(settings).items()
        if name.endswith("_model") or name == "ai_model"
        if isinstance(value, str) and value
    }


#: The one shipped pin allowed to name a model above the two-model roster.
#:
#: `annexure_transcription_escalation_model` is not a tier choice: it is the
#: model the SECOND attempt uses, and the second attempt only happens when the
#: first has already been rejected for descriptive placeholders, an unusable
#: envelope or an empty body. A clean form still costs exactly one Haiku call,
#: so the rollout's economics are intact — and the alternative, which shipped
#: for nine Liluah reports, was retrying the same model that had just failed at
#: the one thing it is documented to be weakest at.
_ALLOWED_ABOVE_ROSTER = {"annexure_transcription_escalation_model"}


def test_no_shipped_pin_names_sonnet_or_opus():
    """`master_agent_model`, `ai_model`, the analyzer synthesis pass and the
    annexure transcription pass are pinned in config, not in an agent row —
    the roster cannot reach them."""
    offenders = {
        name: value
        for name, value in _shipped_model_defaults().items()
        if ("sonnet" in value or "opus" in value)
        and name not in _ALLOWED_ABOVE_ROSTER
    }
    assert offenders == {}, offenders


def test_the_annexure_escalation_is_the_only_pin_above_the_roster():
    """And it escalates FROM Haiku, not instead of it. If the first-attempt pin
    ever drifts up, the exception above stops being an exception."""
    pins = _shipped_model_defaults()
    assert pins["annexure_transcription_model"] == HAIKU
    assert "sonnet" in pins["annexure_transcription_escalation_model"]
    above = {
        name for name, value in pins.items()
        if "sonnet" in value or "opus" in value
    }
    assert above == _ALLOWED_ABOVE_ROSTER, above


def test_master_agent_model_pin_is_haiku():
    assert Settings().master_agent_model == HAIKU


def test_annexure_transcription_pin_is_haiku():
    """Verbatim legal transcription was pinned to Opus. Moved on request; the
    `[Name of Bidder]` placeholder failure this guards against is checked at
    runtime by `quality_tools.diagnose_tender_outputs`."""
    assert Settings().annexure_transcription_model == HAIKU


def test_analyzer_synthesis_pin_is_haiku():
    assert Settings().tender_analyzer_synthesis_model == HAIKU
