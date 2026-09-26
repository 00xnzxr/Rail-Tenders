"""BOQ parsing must narrate itself, and must not parse the same file twice.

A costing run on a 149-page tender took nine minutes. The first event the UI
could show arrived eight minutes in, so for almost the whole run the screen
said "DRPL is working" and nothing else — no way to tell a working platform
from a hung one.

Roughly half that time was avoidable: re-sending in the same session uploads
the files again, so the tender held two byte-identical copies of the NIT and TD
and the parser worked through all four.
"""

import hashlib

import pytest

from app.services import boq_parser_service as bps


@pytest.fixture
def captured():
    """Collect what the SSE pipe would receive."""
    from app.services.ai_service import set_streaming_callback

    events: list = []
    with set_streaming_callback(lambda t, p: events.append((t, p))):
        yield events


# ── progress ────────────────────────────────────────────────────────────────


def test_progress_reaches_the_stream(captured):
    bps._progress("Reading the price schedule (part 2 of 9)")

    assert captured, "nothing reached the stream — the UI would stay silent"
    event_type, payload = captured[0]
    assert event_type == "agent_status"
    assert payload["message"] == "Reading the price schedule (part 2 of 9)"
    assert payload["phase"] == "tool_running"


def test_progress_clears_itself(captured):
    """A progress line that never clears sticks on screen after the step ends."""
    bps._progress("", run_id="boq-chunks", done=True)

    _, payload = captured[0]
    assert payload["phase"] == "tool_done"
    assert payload["run_id"] == "boq-chunks"


def test_progress_message_is_human(captured):
    """The user reads this. No tool names, no snake_case, no page indices."""
    bps._progress("Reading tender document 1 of 2")

    message = captured[0][1]["message"]
    assert "_" not in message
    assert message[0].isupper()


def test_progress_never_breaks_a_parse():
    """Emission is best-effort: a failing callback must not abort the run."""
    from app.services.ai_service import set_streaming_callback

    def _boom(event_type, payload):
        raise RuntimeError("stream is gone")

    with set_streaming_callback(_boom):
        bps._progress("still fine")  # must not raise


def test_progress_without_a_listener_is_a_noop():
    """Parsing also runs outside a request, e.g. from the worker."""
    bps._progress("no listener here")  # must not raise


# ── duplicate documents ─────────────────────────────────────────────────────


def test_digest_matches_for_identical_content(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"identical bytes")
    b.write_bytes(b"identical bytes")

    assert bps._file_digest(str(a)) == bps._file_digest(str(b))


def test_digest_differs_for_different_content(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"one")
    b.write_bytes(b"two")

    assert bps._file_digest(str(a)) != bps._file_digest(str(b))


def test_digest_is_the_real_hash(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"content")
    assert bps._file_digest(str(f)) == hashlib.sha256(b"content").hexdigest()


def test_digest_returns_none_when_unreadable():
    """A hashing failure should cost us the de-duplication, not the parse."""
    assert bps._file_digest("/nonexistent/nope.pdf") is None


def test_large_file_is_hashed_in_blocks(tmp_path):
    """The real files are multi-megabyte; hashing must not read them whole."""
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x" * (3 << 20))

    assert bps._file_digest(str(f)) == hashlib.sha256(b"x" * (3 << 20)).hexdigest()


# ── the chunk loop reports its position ─────────────────────────────────────


@pytest.mark.asyncio
async def test_chunk_loop_reports_progress(captured, monkeypatch):
    """The chunk loop is where the minutes go; each part must announce itself."""

    async def _fake_extract(db, chunk_pages, carry, *, max_retries):
        return []

    monkeypatch.setattr(bps, "_extract_chunk_with_retry", _fake_extract)

    pages = [{"page_num": i, "text": f"page {i}"} for i in range(1, 7)]
    await bps._walk_pages_chunked(
        None,
        pages,
        [None] * 6,
        list(range(6)),
        pages_per_chunk=2,
        max_retries=0,
    )

    messages = [p.get("message") for t, p in captured if p.get("message")]
    assert any("part 1 of 3" in m for m in messages), messages
    assert any("part 3 of 3" in m for m in messages), messages
    # And it clears when the stage ends.
    assert any(p["phase"] == "tool_done" for _, p in captured)
