"""Build + store the lean, prompt-cached scoring digest for the auto-scoring agent.

The digest is a compact (~800 token) system prompt: a fixed instruction/output
contract header plus a distilled body derived from the admin scope profile and
(when present) training-dataset summaries. It is regenerated only when its
inputs change, detected via a stored SHA-256 hash. Distillation is a single
Haiku call run here at seed time — never per tender.
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy.orm import Session

from app.models.platform_setting import PlatformSetting

log = logging.getLogger(__name__)

# Fixed header — the task, output contract, and rubric. Kept short but proper.
_HEADER = """You score how well a government tender fits DRPL's business.
DRPL does mechanical + electrical engineering and AMC work for Indian Railways
(traction, signaling, power systems, locomotive/coach components, spares).

Read the tender title and scope summary the user gives you and reply in EXACTLY
this format, nothing else:
MATCH: <integer 0-100>
REASON: <one short sentence>
(Add a third line, ELIGIBLE: yes|no, only when the user message asks for it.)

Rubric: 90-100 = core railway mechanical/electrical/AMC; 60-89 = adjacent or
partial fit; 40-59 = weak overlap; 0-39 = different domain. Use the relevance
reference below to judge fit."""


def _get_stored(db: Session, key: str) -> "str | None":
    row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    return row.value if row else None


def _set_stored(db: Session, key: str, value: str) -> None:
    row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    if row is None:
        row = PlatformSetting(
            key=key,
            value=value,
            value_type="string",
            category="auto_scoring",
        )
        db.add(row)
    else:
        row.value = value
    db.commit()


def build_digest_inputs(db: Session) -> str:
    """The raw text the digest is derived from — deterministic, used for hashing."""
    from app.services.scope_profile_service import (
        get_active_profile, build_relevance_prompt_context,
    )
    profile = get_active_profile(db)
    scope_ctx = build_relevance_prompt_context(profile) or ""
    # Training-dataset summaries are optional; include their parsed_content if the
    # tables exist and rows are present. Kept compact — names/tags + short excerpts.
    dataset_ctx = ""
    try:
        from app.models.training_dataset import TrainingDataset, TrainingDatasetFile
        datasets = db.query(TrainingDataset).filter(TrainingDataset.status == "active").order_by(TrainingDataset.id).all()
        parts = []
        for d in datasets:
            files = db.query(TrainingDatasetFile).filter(
                TrainingDatasetFile.dataset_id == d.id
            ).order_by(TrainingDatasetFile.id).all()
            excerpt = " ".join((f.parsed_content or f.raw_content or "")[:400] for f in files)
            parts.append(f"[{d.name}] tags={d.tags} :: {excerpt}")
        dataset_ctx = "\n".join(parts)
    except Exception:
        dataset_ctx = ""
    return (scope_ctx + "\n\n" + dataset_ctx).strip()


def _run_coro(coro):
    """Run an async coroutine whether or not an event loop is already running."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _distill(raw: str, db: "Session | None" = None) -> str:
    """Distill the raw inputs into a compact relevance reference (~600 tokens).

    Single synchronous Haiku call. Falls back to a truncated raw block on error
    so scoring is never blocked by distillation.
    """
    from app.core.config import get_settings
    from app.services.ai_service import call_ai
    settings = get_settings()
    system = ("Condense the following into a compact relevance reference for a "
              "tender-scoring agent: list the recurring buyers, departments, work "
              "categories, keywords, and the value band. Be terse — under 250 words. "
              "No preamble.")
    try:
        # With the session: the platform's API key (not only the env one) and
        # a row in api_usage_logs, which this call never had.
        out = _run_coro(call_ai(system, raw, db, "scoring_digest",
                                model_override=settings.auto_scoring_model))
        return out.strip() or raw[:1500]
    except Exception as e:  # noqa: BLE001
        log.warning("scoring digest distillation failed, using raw excerpt: %s", e)
        return raw[:1500]


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_scoring_system_prompt(db: Session) -> str:
    """Return the cached digest system prompt, rebuilding only if inputs changed."""
    raw = build_digest_inputs(db)
    current_hash = _hash(raw)
    stored_hash = _get_stored(db, "auto_scoring_digest_hash")
    stored_digest = _get_stored(db, "auto_scoring_digest")

    if stored_hash == current_hash and stored_digest:
        body = stored_digest
    else:
        body = _distill(raw, db)
        _set_stored(db, "auto_scoring_digest", body)
        _set_stored(db, "auto_scoring_digest_hash", current_hash)

    return _HEADER + "\n\nRelevance reference:\n" + body
