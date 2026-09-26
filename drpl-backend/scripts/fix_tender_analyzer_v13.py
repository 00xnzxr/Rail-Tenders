"""
One-shot fix: reset the Tender Document Analyzer's drifted config.

Context: The live agent row had max_tokens=200000 (confused with Claude's 200k
input context window), which caused OpenAI-fallback calls to crash with
"max_tokens is too large: 200000 — this model supports at most 128000".

This script:
  1. Finds the tender-analyzer CustomAgent (by known agent_keys).
  2. Resets max_tokens to the seed value (16384).
  3. Pins provider to "anthropic" so failover never swaps it to OpenAI
     (OpenAI path can't handle PDFs + vision in this codebase).
  4. Resets model to "claude-sonnet-4-6".
  5. Bumps current_version + appends a version-history row so the fix is
     auditable in the UI (matches how AgentEditorPage creates versions).

Run once from the backend venv:
    python -m scripts.fix_tender_analyzer_v13

Safe to re-run — it only writes when values actually changed.
"""

import sys
from datetime import datetime, timezone

from app.core.database import SessionLocal
from app.models.agent_builder import CustomAgent, AgentVersion


TARGET_KEYS = ["deep_analyzer", "tender_doc_analyzer"]

CORRECT_VALUES = {
    "max_tokens": 16384,
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
}


def main() -> int:
    db = SessionLocal()
    try:
        agent = (
            db.query(CustomAgent)
            .filter(CustomAgent.agent_key.in_(TARGET_KEYS))
            .first()
        )
        if not agent:
            print(f"No agent found with agent_key in {TARGET_KEYS}.")
            print("If the analyzer has a different key in your DB, update TARGET_KEYS in this script.")
            return 1

        print(f"Found agent: id={agent.id} key={agent.agent_key} "
              f"display_name={agent.display_name!r}")
        print("Current config:")
        print(f"  max_tokens = {agent.max_tokens}")
        print(f"  provider   = {agent.provider}")
        print(f"  model      = {agent.model}")
        print(f"  version    = {agent.current_version}")

        changed = {}
        for field, correct in CORRECT_VALUES.items():
            current = getattr(agent, field)
            if current != correct:
                changed[field] = (current, correct)

        if not changed:
            print("\nAgent already matches the correct config. No changes made.")
            return 0

        print("\nPlanned changes:")
        for field, (before, after) in changed.items():
            print(f"  {field}: {before!r} -> {after!r}")

        for field, correct in CORRECT_VALUES.items():
            setattr(agent, field, correct)

        # Version history row so the fix is auditable in the Agent Editor UI.
        new_version = (agent.current_version or 1) + 1
        agent.current_version = new_version
        agent.updated_at = datetime.now(timezone.utc)

        db.add(AgentVersion(
            agent_id=agent.id,
            version_number=new_version,
            system_prompt=agent.system_prompt,
            tools=agent.tools,
            temperature=agent.temperature,
            max_tokens=agent.max_tokens,
            model=agent.model,
            orchestration_config=agent.orchestration_config,
            change_description=(
                "Automated fix: reset max_tokens to 16384, pinned provider to anthropic, "
                "restored model to claude-sonnet-4-6 (drifted via admin UI edit without validation)."
            ),
            created_by=None,
        ))

        db.commit()
        print(f"\nFixed agent id={agent.id}. New version: v{new_version}.")
        return 0
    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
