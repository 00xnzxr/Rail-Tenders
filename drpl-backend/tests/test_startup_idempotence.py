"""Booting twice must change nothing.

The web process re-runs every seeder on import, and Railway restarts it on
every deploy, every crash and every health-check failure. A seeder that writes
unconditionally therefore does not run once — it runs for the lifetime of the
deployment, and what looks like a one-time sync becomes per-restart churn.

`seed_tender_analyzer` had exactly this: it rewrote `system_prompt` and `tools`
and incremented `current_version` on every boot, so a row nobody had touched
climbed a version per restart. Worse, the list it wrote was the canonical
default, which is precisely the fingerprint `seed_agent_tools` clears — the two
seeders spent every boot undoing each other, and `current_version` stopped
meaning "the prompt changed" and started counting process starts.

This is the same defect class as the workspace migration that used to
re-announce itself every boot (5fd75df). The invariant is worth pinning
directly: a seeder run against its own output is a no-op.
"""

import pytest

from app.models.agent_builder import CustomAgent


@pytest.fixture
def seeded(db):
    """The system agents, seeded once."""
    from app.services.seed_costing_researcher_agent import seed_costing_researcher
    from app.services.seed_tender_analyzer_agent import seed_tender_analyzer

    seed_costing_researcher(db=db)
    seed_tender_analyzer(db=db)
    db.commit()
    yield db


def _snapshot(db, agent_key):
    row = db.query(CustomAgent).filter(CustomAgent.agent_key == agent_key).first()
    assert row is not None, f"{agent_key} was not seeded"
    return {
        "version": row.current_version,
        "prompt": row.system_prompt,
        "tools": list(row.tools or []),
    }


def test_reseeding_the_analyzer_changes_nothing(seeded):
    """Three more boots must leave the row exactly as it was."""
    from app.services.seed_tender_analyzer_agent import seed_tender_analyzer

    before = _snapshot(seeded, "tender_doc_analyzer")
    for _ in range(3):
        seed_tender_analyzer(db=seeded)
        seeded.commit()

    assert _snapshot(seeded, "tender_doc_analyzer") == before


def test_reseeding_the_costing_researcher_changes_nothing(seeded):
    from app.services.seed_costing_researcher_agent import seed_costing_researcher

    before = _snapshot(seeded, "costing_researcher")
    for _ in range(3):
        seed_costing_researcher(db=seeded)
        seeded.commit()

    assert _snapshot(seeded, "costing_researcher") == before


def test_the_version_only_moves_when_the_prompt_does(seeded):
    """The number has to mean something, or it is noise in the audit trail."""
    from app.services.seed_tender_analyzer_agent import seed_tender_analyzer

    row = seeded.query(CustomAgent).filter(
        CustomAgent.agent_key == "tender_doc_analyzer"
    ).first()
    before = row.current_version
    row.system_prompt = "something an older deploy wrote"
    seeded.commit()

    seed_tender_analyzer(db=seeded)
    seeded.commit()
    seeded.refresh(row)

    assert row.current_version == before + 1

    seed_tender_analyzer(db=seeded)
    seeded.commit()
    seeded.refresh(row)

    assert row.current_version == before + 1  # and no further


def test_a_customized_agent_is_never_overwritten(seeded):
    """An admin's edit has to survive every restart after it."""
    from app.services.seed_tender_analyzer_agent import seed_tender_analyzer

    row = seeded.query(CustomAgent).filter(
        CustomAgent.agent_key == "tender_doc_analyzer"
    ).first()
    row.is_user_customized = True
    row.system_prompt = "an admin wrote this"
    seeded.commit()

    seed_tender_analyzer(db=seeded)
    seeded.commit()
    seeded.refresh(row)

    assert row.system_prompt == "an admin wrote this"


def test_the_seeder_does_not_re_arm_the_tool_release_pass(seeded):
    """`seed_agent_tools` clears an assignment matching the canonical default,
    reading it as the old backfill's. A seeder that rewrites exactly that list
    on every boot puts the two in a permanent tug-of-war."""
    from app.services.langchain.canonical_registry import CANONICAL_AGENTS
    from app.services.seed_tender_analyzer_agent import seed_tender_analyzer

    row = seeded.query(CustomAgent).filter(
        CustomAgent.agent_key == "tender_doc_analyzer"
    ).first()
    row.tools = []          # what the release pass leaves behind
    seeded.commit()

    seed_tender_analyzer(db=seeded)
    seeded.commit()
    seeded.refresh(row)

    assert row.tools in ([], None), (
        "the seeder rewrote the canonical tool list the release pass had just "
        "cleared — that is the loop"
    )
    # And the canonical belt is still what the agent runs with, because
    # `resolve_tool_keys` falls back to the registry when the column is empty.
    assert CANONICAL_AGENTS["tender_doc_analyzer"]["default_tools"]


def test_the_analyzers_runtime_belt_survives_an_empty_column(seeded):
    """The column being empty must not mean the agent reaches everything —
    `resolve_tool_keys` answers from the registry."""
    from app.services.langchain.canonical_registry import resolve_tool_keys

    row = seeded.query(CustomAgent).filter(
        CustomAgent.agent_key == "tender_doc_analyzer"
    ).first()
    row.tools = []
    seeded.commit()

    keys, source = resolve_tool_keys(seeded, "tender_doc_analyzer")
    assert source == "default"
    assert set(keys) == {
        "document_reader", "memory_retrieve", "memory_store",
        "semantic_search", "tender_lookup", "web_search",
    }
