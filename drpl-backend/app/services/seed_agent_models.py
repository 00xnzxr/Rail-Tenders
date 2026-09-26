"""Bring every registered agent onto a current model, tiered by its work.

Config defaults only cover the code paths that read `Settings`. Agents
configured in Agent Builder carry their own `CustomAgent.model`, and those rows
outlive any config change — so after a model refresh the platform ends up with
current defaults and a database full of superseded ones. Two rows were worse
than superseded: `gpt-5.4-mini` and `gpt-5-mini` are not real model IDs at all,
and every call they made would have failed at the provider.

Tiering is by the work the agent does, not by what it happened to be set to:

- **flagship** — judgement across the whole platform, or verbatim legal
  transcription where a paraphrase is a submission risk.
- **worker** — ordinary agent work: analysis, drafting, costing research.
- **bulk** — short, high-volume, structurally simple calls (scoring a tender
  0-1, picking a category, a two-sentence summary). These run constantly and
  the work does not reward a larger model.

Idempotent, and safe to run on every startup: a row already on its tier's model
is left alone, and the function reports how many rows it actually changed.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.agent_builder import CustomAgent
from app.models.platform_setting import PlatformSetting

logger = logging.getLogger(__name__)

FLAGSHIP = "flagship"
WORKER = "worker"
BULK = "bulk"

#: Low to high. Used to decide whether an existing model is *below* what the
#: agent's work needs.
_TIER_RANK = {BULK: 0, WORKER: 1, FLAGSHIP: 2}

#: Per-provider model for each tier. Verified against provider docs 2026-09-02.
TIER_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        # Worker and bulk are the same model on purpose. The platform runs two
        # models now — Haiku for work that has to be right, Luna for work that
        # is short and structurally simple — so there is no longer a middle
        # tier for Anthropic to name. Flagship stays Opus so an admin who
        # deliberately picks it in Agent Builder still outranks the target and
        # keeps it.
        FLAGSHIP: "claude-opus-5",
        WORKER: "claude-haiku-4-5",
        BULK: "claude-haiku-4-5",
    },
    "openai": {
        FLAGSHIP: "gpt-5.6-sol",
        WORKER: "gpt-5.6-terra",
        BULK: "gpt-5.6-luna",
    },
    "google": {
        FLAGSHIP: "gemini-3.1-pro-preview",
        WORKER: "gemini-3.7-flash",
        BULK: "gemini-3.5-flash-lite",
    },
    # One model, so every tier names it. This entry is not an invitation to
    # roster work onto a 7B model -- it is what stops the startup refresh from
    # doing the opposite. `target_model` falls back to Anthropic's map for an
    # unknown provider, so without this the local agent's model would be
    # rewritten to claude-haiku-4-5 on every boot and the local endpoint would
    # quietly stop being called.
    "runpod": {
        FLAGSHIP: "qwen2.5-7b",
        WORKER: "qwen2.5-7b",
        BULK: "qwen2.5-7b",
    },
}

#: Models no tier targets any more, mapped to the rank they still count as.
#: Without this the haiku/luna rollout would make the Agent Builder picker
#: decorative for anyone who chooses Sonnet: the refresh would not recognise
#: the model, would read it as below tier, and would quietly overwrite it on
#: the next restart. The rollout moves everyone off Sonnet once; it does not
#: take Sonnet away from an admin who asks for it back.
RESPECTED_LEGACY_MODELS: dict[str, str] = {
    "claude-sonnet-5": FLAGSHIP,
}


#: agent_key -> tier. Anything not listed defaults to WORKER: an unrecognised
#: agent is more likely to be real work than a scoring stub, and the worker
#: tier is the safe middle.
AGENT_TIERS: dict[str, str] = {
    # ── Worker ──
    # The coordinator delegates domain work, so Sonnet is the quality ceiling
    # here rather than the flagship Opus model on every chat turn.
    "decision_maker": WORKER,
    # Routine conversation is high-volume and can delegate specialist work.
    "general_assistant": BULK,
    "tender_doc_analyzer": WORKER,
    "deep_analyzer": WORKER,
    "document_analyzer": WORKER,
    "tender_analysis": WORKER,
    "costing_researcher": WORKER,
    "doc-costing-analyst": BULK,
    "proposal_creator": WORKER,
    "proposal": WORKER,
    "doc-technical-writer": WORKER,
    "doc-letter-writer": BULK,
    "doc-compliance-writer": WORKER,
    "checklist_generator": BULK,
    "checklist": BULK,
    "workspace_manager": BULK,
    "tender_pipeline": WORKER,
    # ── Bulk ──
    # Vision discovery over many pages; the verbatim transcription pass that
    # follows it is separately pinned to the flagship tier in config.
    "annexure_finder": BULK,
    # Reduces a tender to a compact scope blob for the costing agent.
    "costing_scope_extractor": BULK,
    # Scoring and classification: short outputs, called on every tender.
    "relevance": BULK,
    "risk": BULK,
    "classifier": BULK,
    "summary": BULK,
    "eligibility": BULK,
    # A 1-2 sentence routing decision on every chat message.
    "proposal_router": BULK,
}

# The two-model roster. Haiku 4.5 for work that has to be right — the Master,
# anything that reads a tender or a PDF, anything that produces a number or a
# document you submit. Luna for work that is short and structurally simple: a
# score, a category, a routing decision, a checklist row.
#
# This replaces the mixed cost-balanced roster (Opus for the Master, Sonnet for
# analysis and drafting, Terra for costing research). Every tiered agent is
# named here: the rollout only rewrites rows it names, so a missing entry is
# silent — the agent stays on its old model and nobody finds out until the bill.
#
# A one-time rollout for existing rows, then Agent Builder choices remain in
# control on later startups.
ALLOCATION_SETTING_KEY = "agent_model_allocation_version"
# Bumped with the OpenAI-primary rollout below so the roster is rewritten once
# on the next boot; a stale marker would leave every agent on Anthropic.
ALLOCATION_VERSION = "openai-terra-luna-v1"

# The Anthropic account behind this deployment has no credit balance, so the
# work tier moves to OpenAI's equivalent rather than the Claude model it was
# authored against. `gpt-5.6-terra` is what MODEL_EQUIVALENTS already names as
# the worker-tier match; `_LUNA` was always OpenAI and is unchanged. Point
# _HAIKU back at ("anthropic", "claude-haiku-4-5") and bump ALLOCATION_VERSION
# again to restore the Claude roster once that account is funded.
_HAIKU = ("openai", "gpt-5.6-terra")
_LUNA = ("openai", "gpt-5.6-luna")

PREFERRED_AGENT_MODELS: dict[str, tuple[str, str]] = {
    # ── Haiku: the Master, tender analysis, PDF extraction, costing, drafting ──
    "decision_maker": _HAIKU,
    "general_assistant": _HAIKU,
    "tender_doc_analyzer": _HAIKU,
    "deep_analyzer": _HAIKU,
    "document_analyzer": _HAIKU,
    "tender_analysis": _HAIKU,
    "tender_pipeline": _HAIKU,
    "costing_researcher": _HAIKU,
    "doc-costing-analyst": _HAIKU,
    "annexure_finder": _HAIKU,
    "proposal_creator": _HAIKU,
    "proposal": _HAIKU,
    "doc-technical-writer": _HAIKU,
    "doc-compliance-writer": _HAIKU,
    # ── Luna: scoring, classification, routing, checklists, short letters ──
    "proposal_router": _LUNA,
    "classifier": _LUNA,
    "relevance": _LUNA,
    "risk": _LUNA,
    "summary": _LUNA,
    "eligibility": _LUNA,
    "checklist": _LUNA,
    "checklist_generator": _LUNA,
    "workspace_manager": _LUNA,
    "doc-letter-writer": _LUNA,
    "costing_scope_extractor": _LUNA,
}


def target_model(agent_key: str, provider: str | None) -> str:
    """The model this agent should be on, for its provider."""
    tier = AGENT_TIERS.get(agent_key, WORKER)
    models = TIER_MODELS.get((provider or "anthropic").lower())
    if models is None:
        # Unknown provider — leave Anthropic's tier as the sane default rather
        # than inventing an ID for a provider we know nothing about.
        models = TIER_MODELS["anthropic"]
    return models[tier]


def _current_models() -> set[str]:
    return {
        m for tier in TIER_MODELS.values() for m in tier.values()
    } | set(RESPECTED_LEGACY_MODELS)


def _tier_of_model(model: str) -> str | None:
    """Which tier a model belongs to, across every provider."""
    for tiers in TIER_MODELS.values():
        for tier, m in tiers.items():
            if m == model:
                return tier
    return RESPECTED_LEGACY_MODELS.get(model)


def seed_agent_models(db: Session) -> dict:
    """Move every agent row onto its tier's current model.

    Returns ``{"updated": N, "checked": M, "changes": [...]}``.
    """
    settings = get_settings()
    if not settings.agent_model_refresh_enabled:
        logger.info("[DRPL] Agent model refresh disabled; skipping.")
        return {"updated": 0, "checked": 0, "changes": [], "skipped": True}

    current = _current_models()
    changes: list[dict] = []

    agents = db.query(CustomAgent).all()
    allocation_marker = db.query(PlatformSetting).filter(
        PlatformSetting.key == ALLOCATION_SETTING_KEY
    ).first()
    apply_allocation = not allocation_marker or allocation_marker.value != ALLOCATION_VERSION

    if apply_allocation:
        for agent in agents:
            preferred = PREFERRED_AGENT_MODELS.get(agent.agent_key)
            if not preferred:
                continue
            provider, model = preferred
            have = (agent.model or "").strip()
            old_provider = (agent.provider or "anthropic").lower()
            if have == model and old_provider == provider:
                continue
            agent.model = model
            agent.provider = provider
            changes.append({
                "agent_key": agent.agent_key,
                "from": have or None,
                "to": model,
                "provider_from": old_provider,
                "provider_to": provider,
            })
            db.add(agent)

        if allocation_marker:
            allocation_marker.value = ALLOCATION_VERSION
        else:
            allocation_marker = PlatformSetting(
                key=ALLOCATION_SETTING_KEY,
                value=ALLOCATION_VERSION,
                value_type="string",
                category="ai",
                description=(
                    "Internal rollout marker for the cost-balanced per-agent "
                    "model roster. Updated automatically; do not edit."
                ),
                is_secret=False,
            )
            db.add(allocation_marker)

    for agent in agents:
        want = target_model(agent.agent_key, agent.provider)
        have = (agent.model or "").strip()

        # Already correct.
        if have == want:
            continue

        # On some other current model. An admin who picked a HIGHER tier than
        # the work strictly needs made a quality call — respect it. A model
        # BELOW the agent's tier is the mismatch this refresh exists to fix:
        # costing_researcher and tender_doc_analyzer had been pushed down to
        # the bulk tier for cost, and they produce the rates you bid on and the
        # go/no-go call respectively.
        if have and have in current:
            have_rank = _TIER_RANK.get(_tier_of_model(have), -1)
            want_rank = _TIER_RANK[AGENT_TIERS.get(agent.agent_key, WORKER)]
            if have_rank >= want_rank:
                continue

        agent.model = want
        changes.append(
            {"agent_key": agent.agent_key, "from": have or None, "to": want}
        )
        db.add(agent)

    if changes or apply_allocation:
        db.commit()
    if changes:
        for c in changes:
            logger.info(
                "[DRPL] Agent model refresh: %s %s -> %s",
                c["agent_key"], c["from"] or "(unset)", c["to"],
            )
    logger.info(
        "[DRPL] Agent model refresh: %d of %d agents updated.",
        len(changes), len(agents),
    )
    return {"updated": len(changes), "checked": len(agents), "changes": changes}
