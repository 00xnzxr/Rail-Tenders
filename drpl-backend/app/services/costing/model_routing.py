"""Which schedule rows the costing agent is asked to price.

The batched costing sent every row to the costing agent (sixty rows a call,
priced blind to the railway's rate). On a row that prints a published rate
its figure was a guess -- 0.15x-6.45x of the railway's estimate between the
tenth and ninetieth percentile on the Mid-Life NIT -- and only the firm's own
rate data could make it more than that. When the firm has none, those rows
now go to the platform's own cost build-up instead (costing/cost_buildup.py):
the schedule banner, verified wages and material prices, a strong model on a
few rows at a time, and the platform's own arithmetic. They still get the
per-row market research, and a verified market price still wins.

The agent keeps every row where its answer is used:

* a row with no published rate (its build-up is the only figure there is);
* an annexure component (priced from its printed basis, rolled up, never
  settled);
* a row with no quantity (settlement does not touch it);
* every row, when the firm has rate data the model could cite -- costing
  training data, or rate notes a person wrote into memory -- because then it
  can find the firm's real price;
* a row the firm's rate card could price (checked per row, the way the
  `ratecard_lookup` tool searches it).

Switch: `costing.model_skips_reference_rows` (default on). Nothing is
skipped when the platform's build-up cannot run (`costing.platform_buildup`
off, or no Anthropic key): then the agent prices every row, as before.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Words that say nothing about which part a row is.
_GENERIC_TERMS = frozenset({
    "the", "and", "for", "with", "from", "into", "per", "set", "sets", "nos", "each",
    "complete", "assembly", "arrangement", "type", "size", "item", "items", "supply",
    "providing", "fixing", "fitting", "fitment", "work", "works", "including", "etc",
    "drg", "drawing", "spec", "specification", "specifications", "rdso", "icf", "rcf",
    "coach", "coaches", "made", "make", "new", "old", "all", "other", "under", "over",
    "as", "of", "to", "in", "on", "or", "by", "at", "is", "be", "no",
})
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9./-]*", re.IGNORECASE)
_RATECARD_SCAN_LIMIT = 5000


@dataclass
class ModelRouting:
    model_rows: list[dict]
    reference_rows: list[dict] = field(default_factory=list)
    reason: str = ""

    @property
    def skipped(self) -> int:
        return len(self.reference_rows)


def _significant_terms(text: str) -> set[str]:
    out = set()
    for tok in _TOKEN_RE.findall((text or "").lower()):
        tok = tok.strip("./-")
        if len(tok) >= 3 and tok not in _GENERIC_TERMS:
            out.add(tok)
    return out


def _load_ratecard(db: Session) -> list[tuple[str, set[str]]]:
    """(part_no, significant terms) for the firm's rate-card items."""
    try:
        from app.models.ratecard import RatecardItem

        rows = (
            db.query(RatecardItem.part_no, RatecardItem.description)
            .limit(_RATECARD_SCAN_LIMIT)
            .all()
        )
    except Exception as e:
        logger.debug(f"[costing routing] rate card unreadable: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return []
    return [
        ((part_no or "").strip().lower(), _significant_terms(description or ""))
        for part_no, description in rows
    ]


def ratecard_could_price(description: str, ratecard: list[tuple[str, set[str]]]) -> bool:
    """True when the firm's rate card holds a plausible match for the row.

    Deliberately generous -- a part number named in the row, or two
    significant words shared with one rate-card item -- because a miss here
    sends a row the rate card could have priced away from the firm's own
    rates, while a false hit only costs the agent call the row had before.
    """
    if not ratecard:
        return False
    text = (description or "").lower()
    terms = _significant_terms(text)
    if not terms:
        return False
    need = min(2, len(terms))
    for part_no, item_terms in ratecard:
        if part_no and len(part_no) >= 4 and part_no in text:
            return True
        if len(terms & item_terms) >= need:
            return True
    return False


def has_personal_rate_memory(db: Session, actor=None) -> bool:
    """True when rate knowledge a person wrote is in the costing agent's reach.

    Counts memory written by the acting user or curated by a master_admin,
    for the costing agent or for every agent -- not what the platform
    captured from earlier runs (learnings, exemplars of past costings, the
    agent's own `memory_store`), whose rates are the model's or the railway's
    own figures coming back round.
    """
    try:
        from sqlalchemy import and_, not_, or_

        from app.core.actor_context import current_actor
        from app.models.agent_memory import AgentMemory
        from app.services.langchain.memory_service import (
            AUTO_CAPTURE_CONTEXT_PREFIXES,
            _master_admin_ids,
        )

        actor = actor if actor is not None else current_actor()
        owners = set(_master_admin_ids(db))
        if actor is not None and actor.user_id is not None:
            owners.add(actor.user_id)
        if not owners:
            return False
        not_auto = and_(*[
            not_(AgentMemory.context.startswith(p)) for p in AUTO_CAPTURE_CONTEXT_PREFIXES
        ])
        row = (
            db.query(AgentMemory.id)
            .filter(
                AgentMemory.created_by.in_(owners),
                or_(AgentMemory.agent_key.is_(None), AgentMemory.agent_key == "costing_researcher"),
                or_(AgentMemory.context.is_(None), not_auto),
            )
            .first()
        )
        return row is not None
    except Exception as e:
        logger.debug(f"[costing routing] memory check failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        # Unknown is treated as "may have rate data": the model keeps the rows.
        return True


def route_rows_for_model(
    db: Session,
    cost_rows: list[dict],
    *,
    published_rates: dict,
    quantities: dict,
    training_chars: int,
    buildup_available: bool = True,
) -> ModelRouting:
    """Split the costable rows into the ones the costing agent prices and the
    ones the platform's own cost build-up prices (`reference_rows`: they
    print a published rate to check the build-up against).

    `published_rates` / `quantities` map boq_item_id -> the skeleton line's
    tender_rate / quantity (the same values settlement reads).
    """
    from app.services.settings_service import get_setting_value

    try:
        enabled = bool(get_setting_value(db, "costing.model_skips_reference_rows", True))
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return ModelRouting(model_rows=list(cost_rows), reason="settings unreadable")
    if not enabled:
        return ModelRouting(model_rows=list(cost_rows), reason="switched off")
    if not buildup_available:
        # Nothing of the platform's own would price the skipped rows.
        return ModelRouting(model_rows=list(cost_rows),
                            reason="the platform's cost build-up is not available")
    if training_chars and training_chars > 0:
        return ModelRouting(model_rows=list(cost_rows),
                            reason="the firm's costing training data is loaded")
    if has_personal_rate_memory(db):
        return ModelRouting(model_rows=list(cost_rows),
                            reason="rate notes written by a person are in memory")

    ratecard = _load_ratecard(db)
    model_rows: list[dict] = []
    reference_rows: list[dict] = []
    for row in cost_rows:
        bid = row.get("boq_item_id")
        try:
            published = float(published_rates.get(bid)) if published_rates.get(bid) is not None else 0.0
        except (TypeError, ValueError):
            published = 0.0
        if (
            row.get("component_of")
            or bid is None
            or published <= 0
            or quantities.get(bid) is None
            or ratecard_could_price(row.get("description") or "", ratecard)
        ):
            model_rows.append(row)
        else:
            reference_rows.append(row)
    return ModelRouting(
        model_rows=model_rows,
        reference_rows=reference_rows,
        reason="the firm has no rate data of its own for these rows",
    )
