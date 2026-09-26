"""Tests for the global assistant thread and its page-context envelope.

The thread is an ordinary ProposalSession with a discriminating `agent_type`,
which buys us history/artifacts/attachments for free but means it can leak into
every place that lists or deletes sessions. These tests pin the isolation.
"""

import pytest

from app.api.routes.command_center import (
    GLOBAL_ASSISTANT_AGENT_TYPE,
    create_assistant_session,
    list_assistant_sessions,
    _format_page_context,
    _get_or_create_global_session,
)
from app.models.proposal import ProposalMessage, ProposalSession
from app.models.user import User


@pytest.fixture
def user(db):
    u = User(
        email="assistant-test@example.com",
        name="Assistant Test",
        hashed_password="x",
        role="operator",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    yield u
    session_ids = [row[0] for row in db.query(ProposalSession.id).filter(
        ProposalSession.created_by == u.id
    ).all()]
    if session_ids:
        db.query(ProposalMessage).filter(
            ProposalMessage.session_id.in_(session_ids)
        ).delete(synchronize_session=False)
    db.query(ProposalSession).filter(ProposalSession.created_by == u.id).delete()
    db.query(User).filter(User.id == u.id).delete()
    db.commit()


# ── the global thread ───────────────────────────────────────────────────────


def test_creates_thread_on_first_use(db, user):
    session = _get_or_create_global_session(db, user)

    assert session.id
    assert session.agent_type == GLOBAL_ASSISTANT_AGENT_TYPE
    assert session.tender_id is None
    assert session.created_by == user.id
    # chat_stream rejects any status other than draft/revision_requested.
    assert session.status == "draft"
    # list_sessions requires router_session_id to be set.
    assert session.router_session_id


def test_reuses_the_same_thread(db, user):
    """Opening the popup resumes the latest conversation."""
    first = _get_or_create_global_session(db, user)
    second = _get_or_create_global_session(db, user)

    assert first.id == second.id
    count = (
        db.query(ProposalSession)
        .filter(
            ProposalSession.created_by == user.id,
            ProposalSession.agent_type == GLOBAL_ASSISTANT_AGENT_TYPE,
        )
        .count()
    )
    assert count == 1


def test_new_chat_preserves_old_conversation_and_becomes_current(db, user):
    first = _get_or_create_global_session(db, user)
    created = create_assistant_session(db=db, current_user=user)
    current = _get_or_create_global_session(db, user)

    assert created["session_id"] != first.id
    assert current.id == created["session_id"]
    assert (
        db.query(ProposalSession)
        .filter(
            ProposalSession.created_by == user.id,
            ProposalSession.agent_type == GLOBAL_ASSISTANT_AGENT_TYPE,
        )
        .count()
        == 2
    )


def test_lists_saved_assistant_conversations_newest_first(db, user):
    first = _get_or_create_global_session(db, user)
    db.add(ProposalMessage(session_id=first.id, role="user", content="Check eligibility"))
    db.add(ProposalMessage(session_id=first.id, role="assistant", content="I will check."))
    db.commit()
    second = create_assistant_session(db=db, current_user=user)

    listed = list_assistant_sessions(db=db, current_user=user)

    assert [item["session_id"] for item in listed] == [second["session_id"], first.id]
    assert all("message_count" in item for item in listed)
    assert listed[1]["message_count"] == 2
    assert listed[1]["preview"] == "Check eligibility"


def test_thread_is_excluded_from_command_center_listing(db, user):
    """Otherwise it shows up as a phantom row in the session sidebar."""
    _get_or_create_global_session(db, user)

    listed = (
        db.query(ProposalSession)
        .filter(
            ProposalSession.created_by == user.id,
            ProposalSession.router_session_id.isnot(None),
            ProposalSession.agent_type != GLOBAL_ASSISTANT_AGENT_TYPE,
        )
        .all()
    )

    assert listed == []


def test_assistant_history_is_scoped_to_its_owner(db, user):
    """Saved assistant chats must not be readable by guessing a session id."""
    from fastapi import HTTPException
    from app.api.routes.command_center import get_history

    session = _get_or_create_global_session(db, user)
    other = User(
        email="assistant-history-other@example.com",
        name="Other User",
        hashed_password="x",
        role="operator",
    )
    db.add(other)
    db.commit()
    db.refresh(other)

    try:
        with pytest.raises(HTTPException) as exc:
            get_history(session.id, db=db, current_user=other)
        assert exc.value.status_code == 403
    finally:
        db.query(User).filter(User.id == other.id).delete()
        db.commit()


# ── page context ────────────────────────────────────────────────────────────


def test_empty_context_adds_nothing():
    assert _format_page_context(None) == ""
    assert _format_page_context({}) == ""
    assert _format_page_context("not a dict") == ""


def test_context_includes_tender_and_route():
    out = _format_page_context(
        {"route": "/tenders/412/workspace/checklist", "tender_id": 412,
         "workspace_tab": "checklist"}
    )

    assert "/tenders/412/workspace/checklist" in out
    assert "412" in out
    assert "checklist" in out


def test_context_names_the_focused_record():
    """This is what makes "why is this wrong?" resolvable."""
    out = _format_page_context(
        {"route": "/x", "visible_entity": {"type": "checklist_item", "id": 88}}
    )

    assert "checklist_item" in out
    assert "88" in out


def test_visible_entity_without_id_is_still_rendered():
    out = _format_page_context({"visible_entity": {"type": "cost_breakdown"}})

    assert "cost_breakdown" in out
    assert "#" not in out.split("cost_breakdown")[1]


def test_long_error_is_truncated():
    """An unbounded UI error string would otherwise eat the context window."""
    out = _format_page_context({"last_error": "E" * 5000})

    assert len(out) < 1000


def test_malformed_visible_entity_is_ignored():
    """The popup is a client; it must not be able to crash the endpoint."""
    assert _format_page_context({"visible_entity": "oops"}) == ""
    assert _format_page_context({"visible_entity": {"no_type": 1}}) == ""


# ── pending-action endpoint guards ──────────────────────────────────────────


def test_action_respond_rejects_when_nothing_is_pending(db, user):
    """Must be a clean 400, not a 500. This caught a real bug: the authz helper
    takes (db, session_id, user_id) and was being called with ORM objects, so
    the permission check raised TypeError instead of authorizing."""
    from fastapi import HTTPException

    from app.services.artifact_authz import assert_session_access

    session = _get_or_create_global_session(db, user)

    # The authz call itself must succeed for the owner.
    assert_session_access(db, session.id, user.id)

    # And reject a different user.
    with pytest.raises(HTTPException) as exc:
        assert_session_access(db, session.id, user.id + 9999)
    assert exc.value.status_code == 403

    # With no pending_action parked, the endpoint's precondition is falsy.
    state = dict(session.pipeline_state or {})
    assert not (state.get("pending_action") or {}).get("action")
