"""The startup refresh that moves agent rows onto current models.

This writes to every agent row on the platform, so the interesting cases are
the ones where it should do nothing: an admin's deliberate model choice must
survive it, or the Agent Builder's model picker becomes decorative.
"""

import pytest

from app.models.agent_builder import CustomAgent
from app.models.platform_setting import PlatformSetting
from app.services.langchain.provider_config import PRICING
from app.services.seed_agent_models import (
    AGENT_TIERS,
    ALLOCATION_SETTING_KEY,
    ALLOCATION_VERSION,
    BULK,
    FLAGSHIP,
    PREFERRED_AGENT_MODELS,
    TIER_MODELS,
    WORKER,
    seed_agent_models,
    target_model,
)


@pytest.fixture
def rows(db):
    made = []

    def make(agent_key, model, provider="anthropic"):
        row = CustomAgent(
            agent_key=agent_key,
            display_name=agent_key,
            model=model,
            provider=provider,
            is_enabled=True,
        )
        db.add(row)
        db.commit()
        made.append(agent_key)
        return row

    yield make
    for key in made:
        db.query(CustomAgent).filter(CustomAgent.agent_key == key).delete()
    db.commit()


def _model_of(db, key):
    db.expire_all()
    return db.query(CustomAgent).filter(CustomAgent.agent_key == key).first().model


# ── the tier map ────────────────────────────────────────────────────────────


def test_every_tier_model_is_priced():
    """An unpriced model reports zero cost, which reads as free usage."""
    for provider, tiers in TIER_MODELS.items():
        for tier, model in tiers.items():
            assert model in PRICING, f"{provider}/{tier}={model} is unpriced"


def test_tiers_are_ordered_by_cost_within_each_provider():
    for provider, tiers in TIER_MODELS.items():
        flagship = PRICING[tiers[FLAGSHIP]]["output"]
        worker = PRICING[tiers[WORKER]]["output"]
        bulk = PRICING[tiers[BULK]]["output"]
        assert flagship >= worker >= bulk, provider


def test_master_agent_is_on_the_worker_tier():
    assert AGENT_TIERS["decision_maker"] == WORKER


def test_scoring_agents_are_on_the_bulk_tier():
    """Short, high-volume, structurally simple — they run on every tender."""
    for key in ("relevance", "risk", "classifier", "summary", "eligibility"):
        assert AGENT_TIERS[key] == BULK, key


def test_reasoning_agents_are_not_on_the_bulk_tier():
    for key in ("costing_researcher", "tender_doc_analyzer", "proposal_creator"):
        assert AGENT_TIERS[key] == WORKER, key


def test_unknown_agent_defaults_to_worker():
    """The safe middle: an unrecognised agent is more likely real work than a
    scoring stub."""
    assert target_model("some_agent_added_later", "anthropic") == TIER_MODELS[
        "anthropic"
    ][WORKER]


def test_provider_specific_targets():
    assert target_model("decision_maker", "openai") == "gpt-5.6-terra"
    assert target_model("relevance", "google") == "gemini-3.5-flash-lite"


def test_unknown_provider_falls_back_to_anthropic_tier():
    """Better a known-good ID than one invented for a provider we know nothing
    about."""
    assert target_model("relevance", "mystery-provider") == "claude-haiku-4-5"


# ── the refresh itself ──────────────────────────────────────────────────────


def test_supersededmodel_is_replaced(db, rows):
    rows("refresh_test_stale", "claude-sonnet-4-6")
    seed_agent_models(db)
    assert _model_of(db, "refresh_test_stale") == "claude-haiku-4-5"


def test_invalid_model_is_replaced(db, rows):
    """Two live rows carried gpt-5.4-mini / gpt-5-mini, which are not real
    model IDs — every call they made would have failed at the provider."""
    rows("refresh_test_invalid", "gpt-5.4-mini", provider="openai")
    seed_agent_models(db)
    assert _model_of(db, "refresh_test_invalid") == "gpt-5.6-terra"


def test_empty_model_is_filled_in(db, rows):
    rows("refresh_test_empty", None)
    seed_agent_models(db)
    assert _model_of(db, "refresh_test_empty") == "claude-haiku-4-5"


def test_admin_choice_of_another_current_model_is_respected(db, rows):
    """An admin who picked the flagship tier for a worker agent in Agent
    Builder keeps it. Without this the picker would be overwritten on every
    restart."""
    rows("refresh_test_admin_pick", "claude-opus-5")
    seed_agent_models(db)
    assert _model_of(db, "refresh_test_admin_pick") == "claude-opus-5"


def test_row_already_on_target_is_untouched(db, rows):
    rows("refresh_test_ontarget", "claude-haiku-4-5")
    result = seed_agent_models(db)
    changed = {c["agent_key"] for c in result["changes"]}
    assert "refresh_test_ontarget" not in changed


def test_refresh_is_idempotent(db, rows):
    rows("refresh_test_idem", "claude-sonnet-4-6")
    first = seed_agent_models(db)
    second = seed_agent_models(db)

    assert any(c["agent_key"] == "refresh_test_idem" for c in first["changes"])
    assert not any(c["agent_key"] == "refresh_test_idem" for c in second["changes"])


def test_refresh_can_be_disabled(db, rows, monkeypatch):
    from app.core.config import get_settings

    rows("refresh_test_disabled_flag", "claude-sonnet-4-6")
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_MODEL_REFRESH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        result = seed_agent_models(db)
        assert result.get("skipped") is True
        assert _model_of(db, "refresh_test_disabled_flag") == "claude-sonnet-4-6"
    finally:
        get_settings.cache_clear()


# ── below-tier correction ───────────────────────────────────────────────────


def test_unrecognised_model_is_moved_to_the_tier_target(db, rows):
    """The below-tier correction, in the only form left for Anthropic.

    This used to assert a worker-tier agent was raised off the bulk model.
    Worker and bulk are both Haiku since the two-model rollout, so there is no
    Anthropic model to be raised *to*; what still has to work is that a row on
    a model nothing recognises lands on its tier target rather than staying
    there.

    Uses a throwaway key rather than the real one: agent_key is unique and the
    real rows are created by other modules in the shared test database.
    """
    rows("refresh_test_below_tier", "claude-imaginary-9")  # unlisted -> WORKER
    seed_agent_models(db)
    assert _model_of(db, "refresh_test_below_tier") == "claude-haiku-4-5"


def test_a_deliberate_sonnet_choice_survives_the_refresh(db, rows):
    """No tier targets Sonnet any more, but the Agent Builder picker still
    offers it — and an admin who picks it must keep it.

    Without `RESPECTED_LEGACY_MODELS` the refresh would not recognise the
    model, would read it as below tier, and would quietly overwrite the choice
    on the next restart. The rollout moves everyone off Sonnet once; it does
    not take Sonnet away from an admin who asks for it back.
    """
    rows("refresh_test_legacy_sonnet", "claude-sonnet-5")
    seed_agent_models(db)
    assert _model_of(db, "refresh_test_legacy_sonnet") == "claude-sonnet-5"


def test_above_tier_model_is_left_alone(db, rows, monkeypatch):
    """An admin who paid up for a higher tier made a quality call."""
    monkeypatch.setitem(AGENT_TIERS, "refresh_test_above_tier", BULK)
    rows("refresh_test_above_tier", "claude-opus-5")
    seed_agent_models(db)
    assert _model_of(db, "refresh_test_above_tier") == "claude-opus-5"


def test_correctly_placed_bulk_agent_is_left_alone(db, rows, monkeypatch):
    monkeypatch.setitem(AGENT_TIERS, "refresh_test_bulk_ok", BULK)
    rows("refresh_test_bulk_ok", "claude-haiku-4-5")
    result = seed_agent_models(db)
    assert not any(
        c["agent_key"] == "refresh_test_bulk_ok" for c in result["changes"]
    )


def test_real_reasoning_agents_target_the_worker_tier():
    """Still the worker tier — the worker tier is now Haiku."""
    assert target_model("costing_researcher", "anthropic") == "claude-haiku-4-5"
    assert target_model("tender_doc_analyzer", "anthropic") == "claude-haiku-4-5"


def test_roster_mixes_anthropic_and_openai():
    """Two providers, two models. Sonnet and Terra left the roster with the
    two-model rollout; see `tests/test_haiku_luna_allocation.py` for the split
    itself."""
    providers = {provider for provider, _model in PREFERRED_AGENT_MODELS.values()}
    models = {model for _provider, model in PREFERRED_AGENT_MODELS.values()}

    assert providers == {"anthropic", "openai"}
    assert models == {"claude-haiku-4-5", "gpt-5.6-luna"}


def test_cost_balanced_allocation_runs_once(db, rows, monkeypatch):
    key = "refresh_test_cost_balanced"
    monkeypatch.setitem(AGENT_TIERS, key, BULK)
    monkeypatch.setitem(PREFERRED_AGENT_MODELS, key, ("openai", "gpt-5.6-luna"))
    row = rows(key, "claude-sonnet-5", provider="anthropic")
    marker = db.query(PlatformSetting).filter(
        PlatformSetting.key == ALLOCATION_SETTING_KEY
    ).first()
    if not marker:
        marker = PlatformSetting(
            key=ALLOCATION_SETTING_KEY,
            value="",
            value_type="string",
            category="ai",
            description="test",
            is_secret=False,
        )
        db.add(marker)
    else:
        marker.value = ""
    db.commit()

    seed_agent_models(db)
    db.refresh(row)
    db.refresh(marker)
    assert (row.provider, row.model) == ("openai", "gpt-5.6-luna")
    assert marker.value == ALLOCATION_VERSION

    # A later Agent Builder choice survives ordinary startup refreshes.
    row.provider = "anthropic"
    row.model = "claude-sonnet-5"
    db.commit()
    seed_agent_models(db)
    db.refresh(row)
    assert (row.provider, row.model) == ("anthropic", "claude-sonnet-5")


def test_tender_analyzer_is_no_longer_force_downgraded():
    """The haiku kill-switch must not pull it back below its tier."""
    from app.services.langchain.llm_factory import _HAIKU_TIER_AGENTS

    assert "tender_doc_analyzer" not in _HAIKU_TIER_AGENTS
