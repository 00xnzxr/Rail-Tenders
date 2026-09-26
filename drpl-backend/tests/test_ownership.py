"""The per-user firewall: who may read a work product.

Isolation is per individual user. Tenders and the expensive per-tender AI
output computed on them stay shared — walling those would bill the same PDFs
against every user's budget — while the work each person builds on top is
theirs alone. master_admin sees everything.
"""

import pytest
from fastapi import HTTPException

from app.core import ownership
from app.core.actor_context import Actor
from app.models.cost_breakdown import CostBreakdown
from app.models.proposal import ProposalSession
from app.models.tender import Tender
from app.models.document_analysis import TenderAnalysisSummary

ALICE = Actor(user_id=1, role="costing_research")
BOB = Actor(user_id=2, role="costing_research")
OWNER = Actor(user_id=9, role="master_admin")


def _session(db, owner_id, title="S"):
    s = ProposalSession(created_by=owner_id, title=title, status="draft")
    db.add(s); db.commit(); db.refresh(s)
    return s


def _tender(db):
    t = Tender(portal="ireps", tender_id=f"T{id(db)}", title="A tender")
    db.add(t); db.commit(); db.refresh(t)
    return t


# ------------------------------------------------------------- the registry --


def test_owner_column_is_declared_for_every_owned_model():
    for model, column in ownership.OWNED_MODELS.items():
        assert hasattr(model, column), f"{model.__name__} has no column {column!r}"


def test_shared_and_owned_do_not_overlap():
    """A model is either a fact about a tender or somebody's work. Never both."""
    overlap = {m.__name__ for m in ownership.OWNED_MODELS} & {
        m.__name__ for m in ownership.SHARED_MODELS
    }
    assert not overlap


def test_the_expensive_per_tender_output_is_shared():
    """Walling these would re-bill the same PDFs to every user's budget."""
    for model in (Tender, TenderAnalysisSummary):
        assert model in ownership.SHARED_MODELS
        assert model not in ownership.OWNED_MODELS


def test_personal_work_is_owned():
    for model in (ProposalSession, CostBreakdown):
        assert model in ownership.OWNED_MODELS


# --------------------------------------------------------------- scoping --


def test_scoped_query_returns_only_the_actors_rows(db):
    _session(db, ALICE.user_id, "alice's")
    _session(db, BOB.user_id, "bob's")
    rows = ownership.scoped_query(db, ProposalSession, ALICE).all()
    assert {r.created_by for r in rows} == {ALICE.user_id}


def test_master_admin_sees_everyones_rows(db):
    _session(db, ALICE.user_id)
    _session(db, BOB.user_id)
    rows = ownership.scoped_query(db, ProposalSession, OWNER).all()
    owners = {r.created_by for r in rows}
    assert ALICE.user_id in owners and BOB.user_id in owners


def test_no_actor_returns_nothing_rather_than_everything(db):
    """Fails closed. An unscoped read is the leak this module exists to stop."""
    _session(db, ALICE.user_id)
    assert ownership.scoped_query(db, ProposalSession, None).all() == []


def test_scoping_a_shared_model_is_a_programming_error(db):
    with pytest.raises(ValueError):
        ownership.scoped_query(db, Tender, ALICE)


# --------------------------------------------------------- single objects --


def test_owner_may_read_their_own(db):
    s = _session(db, ALICE.user_id)
    ownership.assert_can_read(s, ALICE)  # must not raise


def test_another_user_gets_404_not_403(db):
    """403 would confirm the row exists. The wall must not leak that."""
    s = _session(db, ALICE.user_id)
    with pytest.raises(HTTPException) as e:
        ownership.assert_can_read(s, BOB)
    assert e.value.status_code == 404


def test_master_admin_may_read_anything(db):
    s = _session(db, ALICE.user_id)
    ownership.assert_can_read(s, OWNER)


def test_missing_object_is_404(db):
    with pytest.raises(HTTPException) as e:
        ownership.assert_can_read(None, ALICE)
    assert e.value.status_code == 404


def test_shared_objects_are_readable_by_anyone(db):
    t = _tender(db)
    ownership.assert_can_read(t, ALICE)
    ownership.assert_can_read(t, BOB)


def test_unowned_legacy_rows_are_master_admin_only(db):
    """Rows predating the wall have no owner. Guessing one could hand a user
    someone else's work, so they stay visible to the owner only."""
    cb = CostBreakdown(tender_id=99, created_by=None)
    db.add(cb); db.commit()
    with pytest.raises(HTTPException):
        ownership.assert_can_read(cb, ALICE)
    ownership.assert_can_read(cb, OWNER)


# ----------------------------------------------------------- cost breakdowns --


def test_cost_breakdown_carries_an_owner(db):
    """It had none — keyed only by tender+version — so two costing researchers
    working the same tender would have seen each other's numbers."""
    assert ownership.OWNED_MODELS[CostBreakdown] == "created_by"
    cb = CostBreakdown(tender_id=1, created_by=ALICE.user_id)
    db.add(cb); db.commit()
    assert ownership.scoped_query(db, CostBreakdown, BOB).all() == []
    assert len(ownership.scoped_query(db, CostBreakdown, ALICE).all()) == 1
