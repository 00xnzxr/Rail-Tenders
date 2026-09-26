"""A new user-owned table must be classified, not silently unwalled.

Same shape as `test_tool_policy.py`'s unclassified-tool test, for the same
reason: an unclassified *tool* demands a needless confirmation, which is
annoying and visible. An unclassified *table* leaks one user's work to another,
which is silent. This test is the thing that makes the registry load-bearing.

Do not add a model to PLATFORM_MODELS to make this pass. Ask whether the rows
belong to a person.
"""

import pytest

from app.core import ownership
from app.core.database import Base

#: Tables that carry an owner-shaped column but are not a user's work:
#: platform configuration and admin-authored records, where `created_by` means
#: "which admin wrote this", not "whose data is this".
PLATFORM_MODELS = {
    "AgentMemory", "CustomAgent", "AgentVersion", "AgentTool", "AgentExecution",
    "CostingTemplate", "ProposalTemplate", "Ratecard", "RedactionRule",
    "MCPServerConfig", "LetterheadTemplate", "TrainingDataset", "MessageBatch",
    "Workflow", "WorkflowVersion", "WorkflowExecution", "DocumentFormatTemplate",
    "APIUsageLog", "AuditLog", "UserBudget", "ScrapeLog", "TenderScopeProfile",
    # Admin-authored agent test fixtures, and extraction feedback — training
    # signal that improves extraction for everyone, not one person's work.
    "AgentTestCase", "ExtractionFeedback",
}

OWNER_COLUMNS = ("created_by", "user_id")


def _models_with_an_owner_column():
    out = []
    for mapper in Base.registry.mappers:
        model = mapper.class_
        cols = {c.key for c in mapper.columns}
        if cols & set(OWNER_COLUMNS):
            out.append(model)
    return out


@pytest.mark.parametrize(
    "model", _models_with_an_owner_column(), ids=lambda m: m.__name__
)
def test_every_model_with_an_owner_column_is_classified(model):
    name = model.__name__
    classified = (
        model in ownership.OWNED_MODELS
        or model in ownership.SHARED_MODELS
        or name in PLATFORM_MODELS
    )
    assert classified, (
        f"{name} has an owner column but is in neither OWNED_MODELS nor "
        f"SHARED_MODELS. Unclassified means unwalled: decide whether these rows "
        f"are a user's work (OWNED_MODELS), a fact about a tender "
        f"(SHARED_MODELS), or platform config (PLATFORM_MODELS here)."
    )


def test_owned_models_name_a_real_column():
    for model, column in ownership.OWNED_MODELS.items():
        cols = {c.key for c in model.__mapper__.columns}
        assert column in cols, f"{model.__name__}.{column} does not exist"


def test_owned_and_shared_are_disjoint():
    assert not (set(ownership.OWNED_MODELS) & set(ownership.SHARED_MODELS))


def test_platform_list_does_not_quietly_cover_an_owned_model():
    """Belt and braces: a model cannot be both a user's work and platform config."""
    owned = {m.__name__ for m in ownership.OWNED_MODELS}
    assert not (owned & PLATFORM_MODELS)
