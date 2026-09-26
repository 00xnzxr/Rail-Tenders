"""
DRPL Backend - Seed: Costing Researcher Agent
Creates the 'costing_researcher' CustomAgent database record so it appears
in the Admin Panel → Agent Builder.

The agent's actual execution logic lives in enhanced_costing_agent.py.
This record makes it configurable via the UI: system prompt, model, tools,
training dataset assignments, memory, and testing.

Run:  python -m app.services.seed_costing_researcher_agent
"""

import logging
from app.core.database import SessionLocal
from app.models.agent_builder import CustomAgent
from app.services.agent_builder_service import create_agent

# Import the canonical runtime prompt so the Admin Panel record stays in
# lockstep with what the costing pipeline actually executes. Previously this
# file held a divergent older copy, which meant admins edited a prompt that
# the agent never used at runtime.
from app.services.langchain.graphs.costing_agent import (
    COSTING_AGENT_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

AGENT_KEY = "costing_researcher"

SYSTEM_PROMPT = COSTING_AGENT_SYSTEM_PROMPT

AGENT_DATA = {
    "agent_key": AGENT_KEY,
    "display_name": "Costing Researcher",
    "description": (
        "Produces detailed, itemised COST estimates (no margin baked in) for Indian Railways "
        "government tenders. Outputs a single rate per line — the user adds margin / overhead / "
        "GST themselves in the cost-breakdown editor. Mandatory web research via "
        "anonymizing_web_search for any item not covered by training data; every line cites a "
        "real source (URL, DRPL training row, DSR item, or build-up formula). Supports training "
        "data injection (rate cards, DSR tables), interactive clarification in chat mode, "
        "automatic anonymisation before web searches, and crew travel cost delegation."
    ),
    "category": "costing",
    "agent_type": "react",
    "system_prompt": SYSTEM_PROMPT,
    "model": "gpt-5.6-terra",
    "provider": "openai",
    "temperature": 0.1,
    # NOTE: this seeded value is NOT the runtime ceiling. The costing nodes
    # (enhanced_costing_agent.py:_build_costing_agent) call
    # compute_dynamic_max_tokens(model, requested=None), which resolves to the
    # model's full output ceiling (Sonnet 4.6 = 64K). Large tenders (>
    # costing.batch_size BOQ rows) are costed in batches so no single
    # completion has to emit the whole schedule — see run_costing_batched_node.
    # This field is kept for Agent Builder display / non-costing callers.
    "max_tokens": 32000,
    # Thinking is OFF by default (Phase 3a cost cut). Costing reasoning is
    # mostly tool-use orchestration (training -> web -> calculator), which
    # doesn't need extended thinking. Re-enable via the
    # `costing_thinking_enabled=true` PlatformSetting kill-switch — the
    # runtime call in enhanced_costing_agent.py reads that setting and
    # passes thinking_mode_override accordingly.
    "thinking_mode": "disabled",
    "effort": "medium",
    "tools": [],          # Tools are loaded programmatically by enhanced_costing_agent.py
    "tags": ["costing", "rate-research", "dsr", "railways", "tender", "estimation"],
    "is_system": True,    # Marks it as a built-in system agent (not user-created)
    "is_enabled": True,
    "is_published": True,
}


def seed_costing_researcher(db=None):
    """Create or update the costing_researcher agent record."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        existing = db.query(CustomAgent).filter(
            CustomAgent.agent_key == AGENT_KEY
        ).first()

        if existing:
            # Always refresh non-customizable metadata.
            existing.display_name = AGENT_DATA["display_name"]
            existing.description = AGENT_DATA["description"]
            existing.tags = AGENT_DATA["tags"]
            existing.is_published = True

            # Phase 3d — only re-sync system_prompt + tools when the user has
            # NOT customized them in the Admin → Agent Builder UI. Otherwise
            # the user's edits would be silently overwritten on every backend
            # restart. The canonical registry stays the source of truth for
            # everyone who hasn't customized; user-customized agents preserve
            # their edits across restarts.
            # Only when they actually differ — an unconditional write on every
            # boot is DB churn that also makes the log claim a re-sync happened
            # when nothing changed, which is how a real drift goes unnoticed.
            if not existing.is_user_customized:
                _tools = AGENT_DATA.get("tools") or []
                if (
                    existing.system_prompt != AGENT_DATA["system_prompt"]
                    or (existing.tools or []) != _tools
                ):
                    existing.system_prompt = AGENT_DATA["system_prompt"]
                    # Tools are programmatic for costing — empty list in seed.
                    existing.tools = _tools
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
    seed_costing_researcher()
