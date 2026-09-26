"""Who may read what: the per-user firewall, in one place.

Isolation is **per individual user**. Two costing researchers do not see each
other's work. `master_admin` sees everything.

The split that matters is between a *fact about a tender* and *somebody's work*:

- **Shared.** The tender record — including one a user uploaded manually — its
  captured documents, and the expensive AI output computed on it: the analysis
  summary, checklists, extracted annexures. These are computed once and read by
  anyone who can see the tender. Walling them would make two users pay to
  analyse the same PDFs, billing the same work against two separate monthly
  budgets.
- **Owned.** What each person builds on top: Command Center sessions and their
  messages, cost breakdowns, generated documents, signatures, agent runs.

Two rules keep this honest rather than decorative:

1. **It fails closed.** No actor means no rows, and a row with no owner is
   readable only by `master_admin`. An unscoped read is precisely the leak this
   module exists to stop, so the ambiguous cases refuse rather than allow.
2. **A denied read is a 404, not a 403.** A 403 confirms the row exists, which
   tells one user something true about another user's data.

Adding a model with an owner column means adding it here. `tests/test_ownership_drift.py`
fails on one that is in neither map — the same shape as the tool-policy
classification tests, and for the same reason: the failure mode is silent.
"""

from __future__ import annotations

import logging
from typing import Optional, Type

from fastapi import HTTPException
from sqlalchemy.orm import Query, Session

from app.core.actor_context import Actor
from app.models.agent_run import AgentRun
from app.models.api_token import APIToken
from app.models.artifact import CommandCenterArtifact
from app.models.checklist import ChecklistItem
from app.models.cost_breakdown import CostBreakdown
from app.models.document_analysis import (
    CriticalClauseFlag,
    DocumentExtractionResult,
    TenderAnalysisSummary,
)
from app.models.letterhead import DigitalSignature, GeneratedDocument
from app.models.notification import Notification
from app.models.proposal import ProposalSession
from app.models.tender import Tender, TenderDocument
from app.models.workspace import DocumentWorkspace, WorkspaceConfig

logger = logging.getLogger(__name__)

MASTER_ADMIN = "master_admin"


#: Model → the column naming its owner. The codebase uses both `created_by` and
#: `user_id`; neither is wrong, so the registry records which applies.
OWNED_MODELS: dict[Type, str] = {
    ProposalSession: "created_by",
    CostBreakdown: "created_by",
    GeneratedDocument: "created_by",
    DigitalSignature: "user_id",
    AgentRun: "user_id",
    CommandCenterArtifact: "created_by",
    Notification: "user_id",
    APIToken: "user_id",
}


#: Facts about a tender, deliberately readable by everyone who can see it.
#: Listed explicitly so the drift test can tell "shared on purpose" from
#: "nobody classified this yet".
SHARED_MODELS: frozenset[Type] = frozenset({
    Tender,
    TenderDocument,
    TenderAnalysisSummary,
    DocumentExtractionResult,
    CriticalClauseFlag,
    ChecklistItem,
    WorkspaceConfig,
    DocumentWorkspace,
})


#: Rows reached only through an owned parent — scoping the parent scopes these.
#: Recorded so the drift test does not demand an owner column they should not
#: have: a message's owner is its session's owner, and duplicating that would
#: create two answers to one question.
CHILD_OF: dict[Type, tuple[Type, str, str]] = {}


def _owner_of(obj) -> Optional[int]:
    column = OWNED_MODELS.get(type(obj))
    return getattr(obj, column, None) if column else None


def is_owned(model: Type) -> bool:
    return model in OWNED_MODELS


def scoped_query(db: Session, model: Type, actor: Optional[Actor]) -> Query:
    """A query over ``model`` restricted to what ``actor`` may read.

    Raises ValueError for a shared model: scoping one is a programming error,
    and silently returning everything would hide the mistake.
    """
    if model not in OWNED_MODELS:
        raise ValueError(
            f"{model.__name__} is not user-owned — query it directly. "
            "If it should be walled, add it to OWNED_MODELS."
        )

    query = db.query(model)
    if actor is not None and (actor.role or "") == MASTER_ADMIN:
        return query
    if actor is None or actor.user_id is None:
        # Fail closed. Background work with no actor has no business reading
        # one user's private rows, and guessing an owner would be worse.
        logger.debug("ownership: unscoped read of %s refused (no actor)", model.__name__)
        return query.filter(False)

    column = getattr(model, OWNED_MODELS[model])
    return query.filter(column == actor.user_id)


def can_read(obj, actor: Optional[Actor]) -> bool:
    if obj is None:
        return False
    if type(obj) in SHARED_MODELS:
        return True
    if type(obj) not in OWNED_MODELS:
        # Unclassified: treat as owned-but-unowned, i.e. master_admin only.
        return actor is not None and (actor.role or "") == MASTER_ADMIN
    if actor is not None and (actor.role or "") == MASTER_ADMIN:
        return True
    if actor is None or actor.user_id is None:
        return False

    owner = _owner_of(obj)
    if owner is None:
        # Predates the wall. Guessing an owner could hand one user another's
        # work, so it stays with master_admin until an admin reassigns it.
        return False
    return owner == actor.user_id


def assert_can_read(obj, actor: Optional[Actor]) -> None:
    """404 when the actor may not read ``obj``.

    404 rather than 403 on purpose: a 403 tells the caller the row exists,
    which is itself a fact about another user's data.
    """
    if not can_read(obj, actor):
        raise HTTPException(status_code=404, detail="Not found")
