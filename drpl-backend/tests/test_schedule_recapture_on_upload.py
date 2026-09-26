"""A re-uploaded document must re-capture the schedule.

The Liluah tender, second report. Told to re-upload after the annexure fix,
the bidder re-uploaded the NIT and three annexures into the same Command
Center session and asked for a costing that included them. The platform
costed only the NIT schedules — again.

Nothing new had gone wrong. `ensure_boq_parsed` found BOQItem rows already
on the tender — the NIT-only schedule captured under the old rules — and
returned them, because "rows exist and are not thin" was the whole of its
freshness test (its rows-per-page ratio is inert on R2). The re-upload had
changed nothing it looked at: the session kept its tender, and files uploaded
under their existing names create no new `TenderDocument` at all (the
dual-write de-duplicates by file name). The only trace that anything had
arrived was the new `ChatAttachment` rows, which nothing consulted.

The rule pinned here: a schedule is stale when a document that could carry
priced rows arrived after the schedule was captured — a schedule-bearing
`TenderDocument`, or a PDF attached to any session bound to the tender. It
fires once per change to the document set and then goes quiet, because a
successful re-capture writes rows newer than every upload.
"""

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app.models.chat_attachment import ChatAttachment
from app.models.costing_template import BOQItem
from app.models.document_analysis import DocumentExtractionResult
from app.models.proposal import ProposalSession
from app.models.tender import TenderDocument
from app.services import boq_parser_service as bp

_IDS = itertools.count(993_000)
NOW = datetime.now(timezone.utc)


@pytest.fixture
def tid() -> int:
    return next(_IDS)


def _doc(db, tid, name, *, uploaded_at, mime="application/pdf"):
    d = TenderDocument(tender_id=tid, file_name=name, file_path=f"t/{tid}/{name}",
                       mime_type=mime, document_type="chat_upload", uploaded_at=uploaded_at)
    db.add(d); db.commit(); db.refresh(d)
    return d


def _classify(db, doc, doc_type):
    db.add(DocumentExtractionResult(tender_id=doc.tender_id, document_id=doc.id,
                                    document_name=doc.file_name, extraction_type="per_doc",
                                    summary_json={"doc_type": doc_type}))
    db.commit()


def _schedule(db, tid, *, captured_at, rows=6, coded=True):
    """A captured schedule: `rows` BOQItem rows stamped at `captured_at`."""
    out = []
    for n in range(1, rows + 1):
        b = BOQItem(tender_id=tid, sr_no=n, description=f"NIT item {n}",
                    item_code=f"A{n}" if coded else None, created_at=captured_at)
        db.add(b); out.append(b)
    db.commit()
    return out


def _session(db, tid):
    s = ProposalSession(created_by=1, status="draft", title="s", tender_id=tid)
    db.add(s); db.commit(); db.refresh(s)
    return s


def _attach(db, session, name, *, created_at, file_type="application/pdf"):
    a = ChatAttachment(session_id=session.id, file_name=name, file_path=f"cc/{session.id}/{name}",
                       file_type=file_type, uploaded_by=1, created_at=created_at)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _rows(db, tid):
    return db.query(BOQItem).filter(BOQItem.tender_id == tid).all()


# ── the reported case ───────────────────────────────────────────────────────


def test_a_same_name_reupload_marks_the_schedule_stale(db, tid):
    """Liluah as reported. The documents were uploaded, the NIT-only schedule
    was captured, then the same files were uploaded again into the same
    session. No new TenderDocument exists; only the attachments are newer."""
    first_upload = NOW - timedelta(hours=2)
    captured = NOW - timedelta(hours=1)
    reupload = NOW - timedelta(minutes=5)

    nit = _doc(db, tid, "NIT.pdf", uploaded_at=first_upload)
    for a in ("Annexure-1.pdf", "Annexure-2.pdf", "Annexure-3.pdf"):
        _doc(db, tid, a, uploaded_at=first_upload)
    _classify(db, nit, "BOQ")
    _schedule(db, tid, captured_at=captured)          # NIT-only, 6 rows
    sess = _session(db, tid)
    for a in ("NIT.pdf", "Annexure-1.pdf", "Annexure-2.pdf", "Annexure-3.pdf"):
        _attach(db, sess, a, created_at=reupload)      # dual-write dedups: no new docs

    stale = bp._schedule_predates_latest_upload(db, tid, _rows(db, tid))

    assert stale is not None, "the re-upload was invisible to the schedule"
    captured_at, uploaded_at = stale
    assert uploaded_at > captured_at


def test_a_new_document_uploaded_after_capture_marks_it_stale(db, tid):
    """The other shape of re-upload: a new file name, so a TenderDocument."""
    _doc(db, tid, "NIT.pdf", uploaded_at=NOW - timedelta(hours=2))
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))
    _doc(db, tid, "Annexure-2.pdf", uploaded_at=NOW - timedelta(minutes=5))

    assert bp._schedule_predates_latest_upload(db, tid, _rows(db, tid)) is not None


# ── it goes quiet when it should ────────────────────────────────────────────


def test_a_current_schedule_is_not_stale(db, tid):
    """Nothing arrived after the capture: no re-extraction, ever."""
    _doc(db, tid, "NIT.pdf", uploaded_at=NOW - timedelta(hours=2))
    sess = _session(db, tid)
    _attach(db, sess, "NIT.pdf", created_at=NOW - timedelta(hours=2))
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))

    assert bp._schedule_predates_latest_upload(db, tid, _rows(db, tid)) is None


def test_the_first_costing_after_an_upload_does_not_loop(db, tid):
    """Upload, then capture: the capture is newer, so the very next check is
    quiet. This is the ordinary sequence and it must not re-parse."""
    sess = _session(db, tid)
    _attach(db, sess, "NIT.pdf", created_at=NOW - timedelta(minutes=10))
    _doc(db, tid, "NIT.pdf", uploaded_at=NOW - timedelta(minutes=10))
    _schedule(db, tid, captured_at=NOW - timedelta(minutes=1))

    assert bp._schedule_predates_latest_upload(db, tid, _rows(db, tid)) is None


def test_a_non_pdf_attachment_does_not_count(db, tid):
    """The parser reads PDFs. A re-uploaded spreadsheet changes nothing it
    would read, so it must not trigger an extraction."""
    _doc(db, tid, "NIT.pdf", uploaded_at=NOW - timedelta(hours=2))
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))
    sess = _session(db, tid)
    _attach(db, sess, "rates.xlsx", created_at=NOW, file_type="application/vnd.ms-excel")

    assert bp._schedule_predates_latest_upload(db, tid, _rows(db, tid)) is None


def test_a_late_drawing_set_does_not_count(db, tid):
    """Only documents the parser would read can make the schedule stale — the
    same exclude-on-positive-evidence rule as selection."""
    nit = _doc(db, tid, "NIT.pdf", uploaded_at=NOW - timedelta(hours=2))
    _classify(db, nit, "BOQ")
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))
    dwg = _doc(db, tid, "Drawings.pdf", uploaded_at=NOW)
    _classify(db, dwg, "drawing")

    assert bp._schedule_predates_latest_upload(db, tid, _rows(db, tid)) is None


def test_an_attachment_on_another_tenders_session_does_not_count(db, tid):
    _doc(db, tid, "NIT.pdf", uploaded_at=NOW - timedelta(hours=2))
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))
    other = _session(db, tid + 500_000)
    _attach(db, other, "NIT.pdf", created_at=NOW)

    assert bp._schedule_predates_latest_upload(db, tid, _rows(db, tid)) is None


def test_an_undatable_schedule_is_treated_as_current(db, tid):
    """A row with no created_at cannot be compared. Unknown must read as
    current, not stale — otherwise it re-extracts on every run."""
    _doc(db, tid, "NIT.pdf", uploaded_at=NOW)
    rows = _schedule(db, tid, captured_at=NOW - timedelta(hours=1))
    rows[0].created_at = None
    db.commit()

    assert bp._schedule_predates_latest_upload(db, tid, _rows(db, tid)) is None


def test_naive_and_aware_timestamps_compare_safely(db, tid):
    """SQLite hands back naive datetimes, Postgres aware ones. Both mean UTC
    and both must compare without raising."""
    _doc(db, tid, "NIT.pdf", uploaded_at=(NOW - timedelta(minutes=5)).replace(tzinfo=None))
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))

    assert bp._schedule_predates_latest_upload(db, tid, _rows(db, tid)) is not None


# ── the gate acts on it, and stops acting once the schedule is fresh ────────


@pytest.mark.asyncio
async def test_ensure_boq_parsed_recaptures_a_stale_schedule(db, tid, monkeypatch):
    """The whole path: a stale schedule makes `ensure_boq_parsed` call the
    forced re-capture, which is the one that snapshots and re-binds existing
    cost lines. A fresh schedule afterwards makes the next call a no-op."""
    calls: list[bool] = []

    async def fake_parse(_db, _tid, force=False):
        calls.append(force)
        # What a successful re-capture does: replace the rows with newer ones.
        _db.query(BOQItem).filter(BOQItem.tender_id == _tid).delete()
        fresh = [BOQItem(tender_id=_tid, sr_no=n, description=f"item {n}",
                         item_code=f"A{n}", created_at=datetime.now(timezone.utc))
                 for n in range(1, 31)]
        _db.add_all(fresh); _db.commit()
        return fresh

    monkeypatch.setattr(bp, "parse_boq_from_tender", fake_parse)

    _doc(db, tid, "NIT.pdf", uploaded_at=NOW - timedelta(hours=2))
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))
    sess = _session(db, tid)
    _attach(db, sess, "Annexure-2.pdf", created_at=NOW - timedelta(minutes=5))

    n = await bp.ensure_boq_parsed(db, tid)
    assert calls == [True], "a stale schedule must trigger the FORCED re-capture"
    assert n == 30

    n2 = await bp.ensure_boq_parsed(db, tid)
    assert calls == [True], "a freshly captured schedule must not re-capture again"
    assert n2 == 30


@pytest.mark.asyncio
async def test_ensure_boq_parsed_leaves_a_current_schedule_alone(db, tid, monkeypatch):
    calls: list[bool] = []

    async def fake_parse(_db, _tid, force=False):
        calls.append(force)
        return []

    monkeypatch.setattr(bp, "parse_boq_from_tender", fake_parse)
    _doc(db, tid, "NIT.pdf", uploaded_at=NOW - timedelta(hours=2))
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))

    assert await bp.ensure_boq_parsed(db, tid) == 6
    assert calls == []


@pytest.mark.asyncio
async def test_a_staleness_check_failure_never_blocks_costing(db, tid, monkeypatch):
    """Best-effort: if dating the schedule raises, costing proceeds on the
    rows it has rather than failing or re-extracting."""
    def boom(*a, **k):
        raise RuntimeError("clock broke")

    monkeypatch.setattr(bp, "_schedule_predates_latest_upload", boom)
    calls: list[bool] = []

    async def fake_parse(_db, _tid, force=False):
        calls.append(force)
        return []

    monkeypatch.setattr(bp, "parse_boq_from_tender", fake_parse)
    _doc(db, tid, "NIT.pdf", uploaded_at=NOW)
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))

    assert await bp.ensure_boq_parsed(db, tid) == 6
    assert calls == []


# ── one re-capture per tender at a time ─────────────────────────────────────
#
# Two costings on the same tender in the same window would both find the
# schedule stale and both delete-and-reinsert it: a full AI parse spent twice
# and, in one interleaving, duplicate rows. The race predates the staleness
# trigger (the "zero rows" trigger had it), but the trigger makes it reachable
# for tenders that already have a schedule, so it is guarded here with the
# same SET NX + TTL flag the cancel path uses.


class _FakeRedis:
    """SET NX / EXISTS / DELETE -- enough for the lock, and it records itself."""

    def __init__(self, *, release_after_polls: int | None = None):
        self.keys: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.sets: list[tuple] = []
        self._polls_left = release_after_polls

    def set(self, key, value, nx=False, ex=None):
        self.sets.append((key, nx, ex))
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def exists(self, key):
        if self._polls_left is not None:
            self._polls_left -= 1
            if self._polls_left <= 0:
                self.keys.pop(key, None)
        return 1 if key in self.keys else 0

    def delete(self, *keys):
        return sum(1 for k in keys if self.keys.pop(k, None) is not None)


def _stale_tender(db, tid):
    _doc(db, tid, "NIT.pdf", uploaded_at=NOW - timedelta(hours=2))
    _schedule(db, tid, captured_at=NOW - timedelta(hours=1))
    sess = _session(db, tid)
    _attach(db, sess, "Annexure-2.pdf", created_at=NOW - timedelta(minutes=5))


def _fake_parse_factory(calls, rows=30):
    async def fake_parse(_db, _tid, force=False):
        calls.append(force)
        _db.query(BOQItem).filter(BOQItem.tender_id == _tid).delete()
        fresh = [BOQItem(tender_id=_tid, sr_no=n, description=f"item {n}",
                         item_code=f"A{n}", created_at=datetime.now(timezone.utc))
                 for n in range(1, rows + 1)]
        _db.add_all(fresh); _db.commit()
        return fresh
    return fake_parse


@pytest.mark.asyncio
async def test_the_recapture_takes_the_lock_and_releases_it(db, tid, monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: r)
    calls: list[bool] = []
    monkeypatch.setattr(bp, "parse_boq_from_tender", _fake_parse_factory(calls))
    _stale_tender(db, tid)

    assert await bp.ensure_boq_parsed(db, tid) == 30
    assert calls == [True]
    key = bp._recapture_lock_key(tid)
    assert (key, True, bp._RECAPTURE_LOCK_TTL_SECONDS) in r.sets, "not SET NX with a TTL"
    assert key not in r.keys, "the lock was not released"


@pytest.mark.asyncio
async def test_the_lock_is_released_even_when_the_parse_raises(db, tid, monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: r)

    async def boom(_db, _tid, force=False):
        raise RuntimeError("model down")

    monkeypatch.setattr(bp, "parse_boq_from_tender", boom)
    _stale_tender(db, tid)

    assert await bp.ensure_boq_parsed(db, tid) == 6      # non-fatal, current rows
    assert bp._recapture_lock_key(tid) not in r.keys


@pytest.mark.asyncio
async def test_a_second_run_waits_for_the_first_and_uses_its_schedule(db, tid, monkeypatch):
    """The lock is already held (another worker is mid-parse). This run must
    not parse; it waits, and when the lock clears it reads what the other
    run wrote."""
    r = _FakeRedis(release_after_polls=2)
    r.keys[bp._recapture_lock_key(tid)] = "1"           # held by the other run
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: r)
    monkeypatch.setattr(bp, "_RECAPTURE_POLL_SECONDS", 0)
    calls: list[bool] = []
    monkeypatch.setattr(bp, "parse_boq_from_tender", _fake_parse_factory(calls))
    _stale_tender(db, tid)

    # Simulate the other run finishing: its fresh rows land while we wait.
    orig_exists = r.exists

    def exists_then_land(key):
        out = orig_exists(key)
        if not out:
            db.query(BOQItem).filter(BOQItem.tender_id == tid).delete()
            db.add_all([BOQItem(tender_id=tid, sr_no=n, description=f"other {n}",
                                item_code=f"B{n}", created_at=datetime.now(timezone.utc))
                        for n in range(1, 31)])
            db.commit()
        return out

    r.exists = exists_then_land

    assert await bp.ensure_boq_parsed(db, tid) == 30
    assert calls == [], "the second run must not parse while the first holds the lock"


@pytest.mark.asyncio
async def test_a_second_run_that_times_out_costs_from_what_exists(db, tid, monkeypatch):
    r = _FakeRedis()                                     # never released
    r.keys[bp._recapture_lock_key(tid)] = "1"
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: r)
    monkeypatch.setattr(bp, "_RECAPTURE_WAIT_SECONDS", 0)
    calls: list[bool] = []
    monkeypatch.setattr(bp, "parse_boq_from_tender", _fake_parse_factory(calls))
    _stale_tender(db, tid)

    assert await bp.ensure_boq_parsed(db, tid) == 6
    assert calls == []


@pytest.mark.asyncio
async def test_without_redis_the_recapture_runs_unlocked(db, tid, monkeypatch):
    """Local dev has no Redis. The lock must degrade to today's behaviour,
    not block the parse."""
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: None)
    calls: list[bool] = []
    monkeypatch.setattr(bp, "parse_boq_from_tender", _fake_parse_factory(calls))
    _stale_tender(db, tid)

    assert await bp.ensure_boq_parsed(db, tid) == 30
    assert calls == [True]


@pytest.mark.asyncio
async def test_a_redis_error_does_not_block_the_recapture(db, tid, monkeypatch):
    class Broken:
        def set(self, *a, **k): raise ConnectionError("redis away")
        def exists(self, *a, **k): raise ConnectionError("redis away")
        def delete(self, *a, **k): raise ConnectionError("redis away")

    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: Broken())
    calls: list[bool] = []
    monkeypatch.setattr(bp, "parse_boq_from_tender", _fake_parse_factory(calls))
    _stale_tender(db, tid)

    assert await bp.ensure_boq_parsed(db, tid) == 30
    assert calls == [True]
