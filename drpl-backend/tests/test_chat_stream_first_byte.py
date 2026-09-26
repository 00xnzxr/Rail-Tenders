"""The chat stream must start before the slow work, not after it.

A Command Center session with two large tender PDFs attached came back blank.
The message was saved server-side, no error was logged, and both endpoints were
healthy — but `resolve_file_attachments` ran *before* the StreamingResponse was
returned. Measured on the real attachments: 21 seconds and ~79k tokens of
context, during which the browser received no headers and no bytes at all.
Nothing distinguished "working" from "hung", and any timeout or navigation in
that window dropped the connection and left the session looking empty.

These tests pin the ordering: bytes first, expensive work second, and the work
narrated while it happens.
"""

import asyncio

import pytest

from app.models.chat_attachment import ChatAttachment
from app.models.proposal import ProposalSession


@pytest.fixture
def session_with_attachment(db):
    """A session with one attachment row, owned by the stub user (id 1)."""
    s = ProposalSession(
        tender_id=None, created_by=1, status="draft",
        title="first-byte probe", agent_type="standalone", mode="standalone",
        router_session_id="probe-first-byte",
    )
    db.add(s)
    db.commit()
    db.refresh(s)

    att = ChatAttachment(
        session_id=s.id, file_name="big.pdf", file_path="/nonexistent/big.pdf",
        file_type="application/pdf", file_size=1, uploaded_by=1,
    )
    db.add(att)
    db.commit()
    db.refresh(att)

    yield s, att

    db.query(ChatAttachment).filter(ChatAttachment.session_id == s.id).delete()
    db.query(ProposalSession).filter(ProposalSession.id == s.id).delete()
    db.commit()


def _slow_resolver(seconds: float, marker: list):
    """Stands in for reading large PDFs."""

    async def _resolve(db, session_id, file_ids):
        marker.append("started")
        await asyncio.sleep(seconds)
        marker.append("finished")
        return "\n[file content]", {"has_files": True, "file_count": len(file_ids)}

    return _resolve


def test_attachment_work_happens_inside_the_stream(
    auth_client, session_with_attachment, monkeypatch
):
    """The attachment phase must be part of the streamed output.

    Note on what is NOT asserted here: true time-to-first-byte. Starlette's
    TestClient buffers the whole response instead of streaming it lazily, so it
    cannot observe when the first byte left the server. That was measured
    against the real 149-page attachments with curl — 1.5s to the first event
    after the fix, versus 21s of complete silence before it.

    What this test does pin is the observable consequence: in the broken
    version the resolver ran outside the generator, so the stream contained no
    trace of it at all. Now the phase is announced and closed within the body.
    """
    session, att = session_with_attachment
    marker: list = []
    monkeypatch.setattr(
        "app.api.routes.command_center.resolve_file_attachments",
        _slow_resolver(0.05, marker),
    )
    monkeypatch.setattr(
        "app.services.langchain.streaming_handler.stream_router_response",
        lambda **kwargs: _empty_stream(),
    )

    body = auth_client.post(
        f"/api/command-center/sessions/{session.id}/chat/stream",
        json={"message": "what is this?", "file_ids": [att.id]},
    ).text

    assert marker == ["started", "finished"], "the resolver did not run"
    # The stream opens, THEN reports the attachment work.
    assert body.index("event: session") < body.index("attached document")
    assert body.index("attached document") < body.index("tool_done")


def test_attachment_phase_is_narrated(auth_client, session_with_attachment, monkeypatch):
    """The user should see why the wait is happening, not a bare spinner."""
    session, att = session_with_attachment
    monkeypatch.setattr(
        "app.api.routes.command_center.resolve_file_attachments",
        _slow_resolver(0.05, []),
    )
    monkeypatch.setattr(
        "app.services.langchain.streaming_handler.stream_router_response",
        lambda **kwargs: _empty_stream(),
    )

    response = auth_client.post(
        f"/api/command-center/sessions/{session.id}/chat/stream",
        json={"message": "what is this?", "file_ids": [att.id]},
    )
    body = response.text

    assert "Reading 1 attached document" in body
    # And it must clear, or the progress line sticks forever.
    assert '"phase": "tool_done"' in body or '"phase":"tool_done"' in body


def test_no_attachment_phase_when_there_are_no_files(
    auth_client, session_with_attachment, monkeypatch
):
    """A plain message must not claim to be reading documents."""
    session, _ = session_with_attachment
    monkeypatch.setattr(
        "app.services.langchain.streaming_handler.stream_router_response",
        lambda **kwargs: _empty_stream(),
    )

    response = auth_client.post(
        f"/api/command-center/sessions/{session.id}/chat/stream",
        json={"message": "hello"},
    )

    assert "attached document" not in response.text


def test_a_failed_attachment_does_not_kill_the_run(
    auth_client, session_with_attachment, monkeypatch
):
    """An unreadable PDF should warn and continue, not end the conversation —
    the message is already saved and the user is waiting on an answer."""
    session, att = session_with_attachment

    async def _boom(db, session_id, file_ids):
        raise RuntimeError("corrupt pdf")

    monkeypatch.setattr(
        "app.api.routes.command_center.resolve_file_attachments", _boom
    )
    monkeypatch.setattr(
        "app.services.langchain.streaming_handler.stream_router_response",
        lambda **kwargs: _empty_stream(),
    )

    response = auth_client.post(
        f"/api/command-center/sessions/{session.id}/chat/stream",
        json={"message": "what is this?", "file_ids": [att.id]},
    )

    assert response.status_code == 200
    assert "could not read" in response.text
    # The run still proceeded past the attachment phase.
    assert '"phase": "tool_done"' in response.text or '"phase":"tool_done"' in response.text


async def _empty_stream():
    """A router stream that yields nothing, so tests end at the attachment phase."""
    return
    yield  # pragma: no cover — makes this an async generator
