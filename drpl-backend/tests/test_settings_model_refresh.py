"""Platform settings must not silently outlive a model upgrade.

`PlatformSetting` rows beat the config defaults in `_get_effective_model`, so a
stale row here undoes a model change no matter what `config.py` says. Two were
doing exactly that: `ai_model` sat on the cheapest tier and `gemini_search_model`
on a superseded Gemini, both winning over the current defaults.
"""

import pytest

from app.models.platform_setting import PlatformSetting
from app.services.langchain.provider_config import PRICING
from app.services.settings_service import (
    DEFAULT_SETTINGS,
    _KNOWN_STALE_VALUES,
    _forward_migrate_known_stale,
    seed_defaults,
)
from app.services.seed_agent_models import TIER_MODELS

CURRENT_MODELS = {m for tiers in TIER_MODELS.values() for m in tiers.values()}

#: Settings whose value is a model id, and the tier that setting should sit on.
MODEL_SETTINGS = {
    "ai_model": "worker",
    "advisor_model": "flagship",
    "pdf_vision_model": "bulk",
    "gemini_search_model": "worker",
}


def _default(key):
    return next(d for d in DEFAULT_SETTINGS if d["key"] == key)


# ── the shipped defaults ────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(MODEL_SETTINGS))
def test_default_names_a_current_model(key):
    value = _default(key)["value"]
    assert value in CURRENT_MODELS, f"{key} default {value!r} is not a current model"


@pytest.mark.parametrize("key", sorted(MODEL_SETTINGS))
def test_default_is_priced(key):
    assert _default(key)["value"] in PRICING


def test_advisor_is_not_weaker_than_the_default_worker():
    """The advisor tool must be at least as capable as the model asking for the
    advice; an invalid pair is rejected by the API, not silently downgraded."""
    advisor = PRICING[_default("advisor_model")["value"]]
    worker = PRICING[_default("ai_model")["value"]]
    assert advisor["output"] >= worker["output"]


def test_platform_default_matches_the_worker_tier():
    """`ai_model` is what an agent with no model of its own runs on, so it has
    to agree with the tier table or the two disagree about the same agent.

    This used to assert the default was *above* the bulk tier. Since the
    two-model rollout Anthropic's worker and bulk tiers are both Haiku, so the
    remaining claim is agreement, not height."""
    assert _default("ai_model")["value"] == TIER_MODELS["anthropic"]["worker"]


def test_no_default_carries_a_date_suffix():
    for key in MODEL_SETTINGS:
        value = _default(key)["value"]
        assert not value[-9:].lstrip("-").isdigit(), f"{key}={value} is date-suffixed"


# ── forward migration of existing rows ──────────────────────────────────────


def test_every_migration_target_is_current():
    """A migration that lands on another stale value just moves the problem."""
    for key, mapping in _KNOWN_STALE_VALUES.items():
        if key not in MODEL_SETTINGS:
            continue
        for stale, target in mapping.items():
            assert target in CURRENT_MODELS, f"{key}: {stale} -> {target} is not current"


def test_migrations_are_not_circular():
    """A stale value must never also be a target, or the pass would flap."""
    for key, mapping in _KNOWN_STALE_VALUES.items():
        overlap = set(mapping) & set(mapping.values())
        assert not overlap, f"{key} maps {overlap} both from and to"


@pytest.fixture
def setting(db):
    made = []

    def make(key, value, description="", value_type="string"):
        db.query(PlatformSetting).filter(PlatformSetting.key == key).delete()
        row = PlatformSetting(
            key=key, value=value, value_type=value_type,
            category="ai", description=description, is_secret=False,
        )
        db.add(row)
        db.commit()
        made.append(key)
        return row

    yield make
    for key in made:
        db.query(PlatformSetting).filter(PlatformSetting.key == key).delete()
    db.commit()


def _value(db, key):
    db.expire_all()
    return db.query(PlatformSetting).filter(PlatformSetting.key == key).first().value


def test_superseded_model_is_migrated(db, setting):
    setting("advisor_model", "claude-opus-4-7")
    _forward_migrate_known_stale(db)
    assert _value(db, "advisor_model") == "claude-opus-5"


def test_sonnet_platform_default_is_migrated_to_haiku(db, setting):
    """The row that would otherwise undo the two-model rollout.

    A `PlatformSetting` beats the config default in `_get_effective_model`, so
    a live `ai_model` row still saying sonnet-5 keeps every unconfigured agent
    on Sonnet however `config.py` is changed. This migration is what actually
    moves the platform. (It replaces the previous test, which asserted the
    opposite direction: haiku was raised to sonnet.)"""
    setting("ai_model", "claude-sonnet-5")
    _forward_migrate_known_stale(db)
    assert _value(db, "ai_model") == "claude-haiku-4-5"


def test_date_suffixed_id_is_normalised(db, setting):
    setting("pdf_vision_model", "claude-haiku-4-5-20251001")
    _forward_migrate_known_stale(db)
    assert _value(db, "pdf_vision_model") == "claude-haiku-4-5"


def test_a_current_value_is_left_alone(db, setting):
    """An admin who already picked a current model keeps it."""
    setting("advisor_model", "claude-opus-5")
    _forward_migrate_known_stale(db)
    assert _value(db, "advisor_model") == "claude-opus-5"


def test_an_unrecognised_value_is_left_alone(db, setting):
    """Only values we KNOW are stale are migrated; anything else is a choice."""
    setting("ai_model", "claude-fable-5")
    _forward_migrate_known_stale(db)
    assert _value(db, "ai_model") == "claude-fable-5"


def test_migration_is_idempotent(db, setting):
    setting("advisor_model", "claude-opus-4-7")
    _forward_migrate_known_stale(db)
    _forward_migrate_known_stale(db)
    assert _value(db, "advisor_model") == "claude-opus-5"


# ── descriptions ────────────────────────────────────────────────────────────


def test_stale_description_is_refreshed(db, setting):
    """Descriptions are documentation, not configuration. A stale one is the
    sentence an admin reads before deciding whether to change a setting —
    agent_models_tier still described switching to "Sonnet 4.6" long after both
    the models and the mechanism had changed."""
    setting("agent_models_tier", "haiku", description="Something long out of date.")

    seed_defaults(db)

    db.expire_all()
    row = db.query(PlatformSetting).filter(
        PlatformSetting.key == "agent_models_tier"
    ).first()
    assert row.description == _default("agent_models_tier")["description"]
    assert "4.6" not in row.description


def test_description_refresh_does_not_touch_the_value(db, setting):
    """An admin's chosen value must survive a documentation refresh."""
    setting("agent_models_tier", "sonnet", description="stale")

    seed_defaults(db)

    assert _value(db, "agent_models_tier") == "sonnet"


def test_no_default_description_mentions_a_superseded_model():
    """The descriptions an admin reads must not name models we no longer run."""
    superseded = ("Sonnet 4.6", "sonnet-4-6", "opus-4-7", "gemini-2.5", "Haiku 4.5 (")
    problems = [
        d["key"]
        for d in DEFAULT_SETTINGS
        for bad in superseded
        if bad in (d.get("description") or "")
    ]
    assert not problems, f"settings describing superseded models: {problems}"


def test_the_stale_migration_runs_at_startup():
    """It only ran when someone opened Admin -> Settings.

    `seed_defaults` — the only caller — is invoked from `app/seed.py` and the
    admin-settings routes, never at boot. So a live `ai_model` row on a
    superseded model kept winning over the config default until a human
    happened to load a page, which is not a migration, it is a coincidence.
    That is how the two-model rollout shipped with every unconfigured agent
    still on Sonnet.

    Only the forward migration is called at startup, not the full
    `seed_defaults`: rewriting known-stale values is idempotent and safe, while
    inserting every default row at boot is a different and larger decision.
    """
    import inspect

    from app import main

    assert "_forward_migrate_known_stale" in inspect.getsource(main)
