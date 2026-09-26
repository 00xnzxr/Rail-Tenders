"""The Command Center history endpoint, on a session longer than its limit.

`get_conversation_history` now returns the most recent turns rather than the
oldest. That is the fix, and it moves a second thing that had been quietly
depending on the old behaviour: the endpoint matches chat attachments to user
turns positionally, walking `user_proposal_msgs` from index 0. Against a window
that is now the tail of the conversation, index 0 is the wrong end — the newest
turns would be handed the oldest messages' attachments, and a user reloading a
long session would find their files on the wrong messages.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.agent_memory import AgentConversationHistory
from app.models.chat_attachment import ChatAttachment
from app.models.proposal import ProposalMessage, ProposalSession
from app.models.user import User


@pytest.fixture
def long_session(db):
    """A session with 30 exchanges: 30 user turns, 30 assistant turns."""
    user = db.query(User).filter(User.email == "history-window@drpl.local").first()
    created_user = user is None
    if created_user:
        user = User(
            email="history-window@drpl.local", name="History Window",
            hashed_password="x", role="master_admin", is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    router_session_id = f"history-endpoint-{uuid.uuid4().hex[:8]}"
    session = ProposalSession(
        title="Long chat",
        router_session_id=router_session_id,
        created_by=user.id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(30):
        at = base + timedelta(minutes=i * 2)
        db.add(AgentConversationHistory(
            session_id=router_session_id, agent_key="proposal_router",
            role="user", content=f"question {i}", created_at=at,
        ))
        db.add(AgentConversationHistory(
            session_id=router_session_id, agent_key="proposal_router",
            role="assistant", content=f"answer {i}",
            created_at=at + timedelta(minutes=1),
        ))
        db.add(ProposalMessage(
            session_id=session.id, role="user",
            content=f"question {i}", created_at=at,
        ))
    db.commit()

    yield session, user

    db.query(ProposalMessage).filter(
        ProposalMessage.session_id == session.id
    ).delete()
    db.query(AgentConversationHistory).filter(
        AgentConversationHistory.session_id == router_session_id
    ).delete()
    db.rollback()
    db.query(ChatAttachment).filter(
        ChatAttachment.session_id == session.id
    ).delete()
    db.query(ProposalSession).filter(ProposalSession.id == session.id).delete()
    if created_user:
        db.query(User).filter(User.id == user.id).delete()
    db.commit()


def _history(db, session, user, limit):
    from app.api.routes.command_center import get_history

    return get_history(
        session_id=session.id, limit=limit, db=db, current_user=user,
    )


def test_the_endpoint_returns_the_end_of_a_long_conversation(db, long_session):
    """A user reloading a long chat must see what they last said."""
    session, user = long_session

    rows = _history(db, session, user, limit=10)

    assert len(rows) == 10
    assert rows[-1]["content"] == "answer 29"
    assert [r["created_at"] for r in rows] == sorted(r["created_at"] for r in rows)


def test_attachments_align_with_the_turns_in_the_window(db, long_session):
    """Walking both lists from index 0 puts files on the wrong messages."""
    session, user = long_session

    # Attach a file to the LAST user message in the session.
    last_msg = (
        db.query(ProposalMessage)
        .filter(ProposalMessage.session_id == session.id,
                ProposalMessage.role == "user")
        .order_by(ProposalMessage.created_at.desc())
        .first()
    )
    attachment = ChatAttachment(
        session_id=session.id, message_id=last_msg.id,
        file_name="boq.xlsx", file_path="uploads/boq.xlsx",
        file_type="xlsx", file_size=1234, uploaded_by=user.id,
    )
    db.add(attachment)
    db.commit()

    try:
        rows = _history(db, session, user, limit=10)
        with_files = [r for r in rows if r["attachments"]]

        assert len(with_files) == 1
        assert with_files[0]["content"] == "question 29"
        assert with_files[0]["attachments"][0]["file_name"] == "boq.xlsx"
    finally:
        db.query(ChatAttachment).filter(
            ChatAttachment.id == attachment.id
        ).delete()
        db.commit()


def test_a_session_inside_the_limit_is_unchanged(db, long_session):
    session, user = long_session

    rows = _history(db, session, user, limit=200)

    assert len(rows) == 60
    assert rows[0]["content"] == "question 0"
    assert rows[-1]["content"] == "answer 29"
