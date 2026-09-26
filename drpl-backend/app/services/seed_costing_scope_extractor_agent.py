"""
DRPL Backend — Seed: Costing Scope Extractor Agent

Registers the ``costing_scope_extractor`` CustomAgent so it appears in the
Admin Panel → Agent Builder. This agent runs as a focused token-reduction
stage BEFORE costing_researcher: it reads the per-document summaries from
tender_doc_analyzer and produces a compact structured scope JSON (~3-5K
chars) that the costing agent consumes instead of the full PDF.

The runtime extraction logic lives in
``app/services/langchain/graphs/costing_scope_extractor.py``. This seed only
makes the agent configurable via the UI (model, system prompt, max tokens).

Run:  python -m app.services.seed_costing_scope_extractor_agent
"""

import logging

from app.core.database import SessionLocal
from app.models.agent_builder import CustomAgent
from app.services.agent_builder_service import create_agent
from app.services.langchain.graphs.costing_scope_extractor import (
    COSTING_SCOPE_AGENT_KEY,
    COSTING_SCOPE_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

AGENT_KEY = COSTING_SCOPE_AGENT_KEY


AGENT_DATA = {
    "agent_key": AGENT_KEY,
    "display_name": "Costing Scope Extractor",
    "description": (
        "Token-reduction stage for the costing pipeline. Reads per-document "
        "summaries already produced by tender_doc_analyzer and derives a compact "
        "structured scope (pricing mechanism, tender type, equipment in scope, "
        "BoQ items, penalty clauses, payment terms). The downstream "
        "costing_researcher consumes this ~5K-char JSON instead of the full "
        "tender PDF — typical input-token reduction is 90%+ for AMC tenders."
    ),
    "category": "costing",
    "agent_type": "chain_of_thought",
    "system_prompt": COSTING_SCOPE_SYSTEM_PROMPT,
    # Haiku is sufficient — the input is already structured per-doc summaries,
    # and the output is a strict JSON object. No reasoning / thinking needed.
    # Override to Sonnet via Agent Builder if extraction quality drops on edge cases.
    "model": "claude-haiku-4-5",
    "provider": "anthropic",
    "temperature": 0.0,
    "max_tokens": 4096,
    "thinking_mode": "disabled",
    "effort": "low",
    "tools": [],
    "tags": ["costing", "extraction", "tender", "preprocessing", "token-reduction"],
    "is_system": True,
    "is_enabled": True,
    "is_published": True,
}


def seed_costing_scope_extractor(db=None):
    """Create or update the costing_scope_extractor agent record."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        existing = db.query(CustomAgent).filter(
            CustomAgent.agent_key == AGENT_KEY
        ).first()

        if existing:
            existing.display_name = AGENT_DATA["display_name"]
            existing.description = AGENT_DATA["description"]
            existing.tags = AGENT_DATA["tags"]
            existing.is_published = True

            # Phase 3d — only re-sync prompt when the user hasn't customized it.
            if not existing.is_user_customized:
                existing.system_prompt = AGENT_DATA["system_prompt"]
                existing.tools = AGENT_DATA.get("tools") or []
                print(
                    f"Agent '{AGENT_KEY}' already exists (id={existing.id}). "
                    f"Re-synced system_prompt + tools from code canonical."
                )
            else:
                print(
                    f"Agent '{AGENT_KEY}' (id={existing.id}) is user-customized — "
                    f"skipping system_prompt + tools re-sync to preserve admin edits."
                )

            db.commit()
            return existing

        agent = create_agent(db, AGENT_DATA, user_id=None)
        print(f"Created agent '{AGENT_KEY}' (id={agent.id})")
        return agent

    finally:
        if close_db:
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_costing_scope_extractor()
