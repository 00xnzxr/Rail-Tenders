"""Annexure pass 1 runs the discovery prompt, and the seeder stops churning.

`SYSTEM_AGENTS` seeded annexure_finder with the legacy combined EXTRACTION
prompt while the registry's canonical has been ANNEXURE_DISCOVERY_PROMPT since
the two-pass split. The seeder then saw the row differ from its canonical,
flagged it as an admin customisation, and `resolve_system_prompt` handed pass
1 the body-emitting prompt: ~14k output tokens per PDF and a truncated form
list. "Reset to Default" was undone by the next Agent Builder page load.
"""

from datetime import datetime, timezone

import pytest

from app.models.agent_builder import CustomAgent
from app.services.agent_builder_service import SYSTEM_AGENTS, seed_system_agents
from app.services.langchain.graphs import annexure_finder_agent as afa


@pytest.fixture(autouse=True)
def _leave_no_seeded_rows(db):
    """seed_system_agents commits; remove what it created so later tests that
    insert their own system rows (the roster tests) start from what they
    expect."""
    keys = [a["agent_key"] for a in SYSTEM_AGENTS]
    before = {k for (k,) in db.query(CustomAgent.agent_key)
              .filter(CustomAgent.agent_key.in_(keys)).all()}
    yield
    db.rollback()
    created = set(keys) - before
    if created:
        from app.models.agent_builder import AgentVersion
        ids = [i for (i,) in db.query(CustomAgent.id)
               .filter(CustomAgent.agent_key.in_(created)).all()]
        if ids:
            db.query(AgentVersion).filter(AgentVersion.agent_id.in_(ids)).delete(
                synchronize_session=False)
        db.query(CustomAgent).filter(CustomAgent.agent_key.in_(created)).delete(
            synchronize_session=False)
        db.commit()


def _row(db):
    return db.query(CustomAgent).filter(CustomAgent.agent_key == "annexure_finder").first()


def test_the_seed_carries_the_discovery_prompt():
    entry = next(a for a in SYSTEM_AGENTS if a["agent_key"] == "annexure_finder")
    assert entry["_system_prompt_attr"] == "ANNEXURE_DISCOVERY_PROMPT"


def test_a_row_holding_the_legacy_seed_is_healed(db):
    seed_system_agents(db)
    row = _row(db)
    row.system_prompt = afa.ANNEXURE_EXTRACTION_PROMPT
    row.is_user_customized = True
    db.commit()

    seed_system_agents(db)
    db.refresh(row)
    assert row.system_prompt.strip() == afa.ANNEXURE_DISCOVERY_PROMPT.strip()
    assert row.is_user_customized is False


def test_pass_one_resolves_to_the_discovery_prompt(db):
    seed_system_agents(db)
    from app.services.langchain.canonical_registry import resolve_system_prompt

    resolved = resolve_system_prompt(db, "annexure_finder")
    text = resolved[0] if isinstance(resolved, tuple) else resolved
    assert text.strip() == afa.ANNEXURE_DISCOVERY_PROMPT.strip()


def test_a_real_admin_edit_is_still_preserved(db):
    seed_system_agents(db)
    row = _row(db)
    row.system_prompt = afa.ANNEXURE_DISCOVERY_PROMPT + "\nAlso list the drawings index."
    row.is_user_customized = True
    db.commit()
    seed_system_agents(db)
    db.refresh(row)
    assert row.system_prompt.endswith("Also list the drawings index.")
    assert row.is_user_customized is True
    row.system_prompt = afa.ANNEXURE_DISCOVERY_PROMPT
    row.is_user_customized = False
    db.commit()


def test_a_second_seed_writes_nothing(db):
    seed_system_agents(db)
    marker = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = db.query(CustomAgent).filter(
        CustomAgent.agent_key.in_([a["agent_key"] for a in SYSTEM_AGENTS]),
        CustomAgent.is_user_customized.is_(False),
    ).all()
    for r in rows:
        r.updated_at = marker
    db.commit()
    seed_system_agents(db)
    for r in rows:
        db.refresh(r)
        stamp = r.updated_at if r.updated_at.tzinfo else r.updated_at.replace(tzinfo=timezone.utc)
        assert stamp == marker, f"{r.agent_key} was rewritten by an idempotent seed"
