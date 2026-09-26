"""Archive listing, restore, and manual purge endpoints."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.core.auth import get_current_user, require_admin
from app.main import app
from app.models.tender import Tender

# Register models these routes touch so create_all builds their tables:
# platform_setting backs get_scoring_settings(), and delete_tenders_deep walks
# every child table (missing ones are skipped, but present ones must exist).
from app.models import platform_setting as _ps  # noqa: F401
from app.models import cost_breakdown as _cb  # noqa: F401

NOW = datetime.now(timezone.utc)
_n = 0


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: object()
    app.dependency_overrides[require_admin] = lambda: object()
    return TestClient(app)


def _mk(db, **kw):
    global _n
    _n += 1
    t = Tender(portal="ireps", tender_id=f"AR{_n}", title=f"arch {_n}", **kw)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_archive_list_returns_archived_with_purge_at():
    db = _session()
    try:
        client = _client(db)
        _mk(db, is_archived=True, archived_at=NOW - timedelta(days=2),
            archive_reason="past_due")
        _mk(db, is_archived=False)   # active — must not appear

        body = client.get("/api/tenders/archive").json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["archive_reason"] == "past_due"
        assert item["purge_at"] is not None
    finally:
        app.dependency_overrides.clear()


def test_archive_list_purge_at_is_null_for_non_past_due():
    """SAFETY INVARIANT: nothing auto-purges an auto_discard/manual row, so it
    must never show a countdown."""
    db = _session()
    try:
        client = _client(db)
        _mk(db, is_archived=True, archived_at=NOW, archive_reason="auto_discard")
        _mk(db, is_archived=True, archived_at=NOW, archive_reason="manual")
        items = client.get("/api/tenders/archive").json()["items"]
        assert len(items) == 2
        assert all(i["purge_at"] is None for i in items)
    finally:
        app.dependency_overrides.clear()


def test_archive_list_filters_by_reason():
    db = _session()
    try:
        client = _client(db)
        _mk(db, is_archived=True, archived_at=NOW, archive_reason="past_due")
        _mk(db, is_archived=True, archived_at=NOW, archive_reason="manual")
        body = client.get("/api/tenders/archive?reason=manual").json()
        assert body["total"] == 1
        assert body["items"][0]["archive_reason"] == "manual"
    finally:
        app.dependency_overrides.clear()


def test_restore_clears_archive_fields_and_marks_overridden():
    db = _session()
    try:
        client = _client(db)
        t = _mk(db, is_archived=True, archived_at=NOW, archive_reason="past_due")

        assert client.post(f"/api/tenders/{t.id}/restore").status_code == 200
        db.refresh(t)
        assert t.is_archived is False
        assert t.archived_at is None
        assert t.archive_reason is None
        assert t.segment_overridden is True
    finally:
        app.dependency_overrides.clear()


def test_restore_404s_for_unknown_tender():
    db = _session()
    try:
        client = _client(db)
        assert client.post("/api/tenders/999999/restore").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_purge_now_deletes():
    db = _session()
    try:
        client = _client(db)
        t = _mk(db, is_archived=True, archived_at=NOW, archive_reason="past_due")
        r = client.post("/api/tenders/archive/purge-now", json={"tender_ids": [t.id]})
        assert r.status_code == 200
        assert r.json()["deleted"] == 1
        assert db.query(Tender).filter(Tender.id == t.id).first() is None
    finally:
        app.dependency_overrides.clear()


def test_restore_survives_the_next_archive_sweep():
    """End-to-end proof the restore actually sticks.

    Archive a past-due tender exactly the way the sweep does, restore it via
    the real endpoint, then run the real sweep again over the real predicate
    (no mocks). Before the fix this re-archived the row within one tick and
    restarted its 7-day purge clock.
    """
    from app.services.tender_archive_service import (
        archive_candidates_query, run_archive_pass,
    )

    db = _session()
    try:
        client = _client(db)
        # Past-due, workflow_status "new", unassigned, no work artifacts:
        # a genuine archive candidate, so the test cannot pass vacuously.
        t = _mk(db, closing_date=NOW - timedelta(days=30), workflow_status="new")
        assert run_archive_pass(db, now=NOW, grace_days=3, batch_size=10) == 1
        db.refresh(t)
        assert t.is_archived is True and t.archive_reason == "past_due"

        assert client.post(f"/api/tenders/{t.id}/restore").status_code == 200

        # The very next sweep tick must not pick it back up.
        assert t.id not in {c.id for c in
                            archive_candidates_query(db, now=NOW, grace_days=3).all()}
        assert run_archive_pass(db, now=NOW, grace_days=3, batch_size=10) == 0
        db.refresh(t)
        assert t.is_archived is False
        assert t.archived_at is None
        assert t.archive_reason is None
    finally:
        app.dependency_overrides.clear()


def test_purge_now_refuses_a_non_archived_tender():
    """A wrong id in the body must not hard-delete a live tender."""
    db = _session()
    try:
        client = _client(db)
        live = _mk(db, is_archived=False)
        archived = _mk(db, is_archived=True, archived_at=NOW, archive_reason="past_due")

        r = client.post("/api/tenders/archive/purge-now",
                        json={"tender_ids": [live.id, archived.id]})
        assert r.status_code == 200
        assert r.json()["deleted"] == 1      # only the archived one
        assert r.json()["skipped"] == 1
        assert db.query(Tender).filter(Tender.id == live.id).first() is not None
        assert db.query(Tender).filter(Tender.id == archived.id).first() is None
    finally:
        app.dependency_overrides.clear()


def test_purge_now_rejects_oversized_batch():
    db = _session()
    try:
        client = _client(db)
        r = client.post("/api/tenders/archive/purge-now",
                        json={"tender_ids": list(range(1, 60))})
        assert r.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_purge_now_rejects_empty_list():
    db = _session()
    try:
        client = _client(db)
        assert client.post("/api/tenders/archive/purge-now",
                           json={"tender_ids": []}).status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_purge_now_rejects_malformed_body():
    """Malformed input must be a 422 at the boundary, never a 500.

    Before `PurgeRequest`, `body: dict` let these through: a non-list
    `tender_ids` reached `set()`/`.in_()`, an unhashable element raised
    TypeError in `set(tender_ids)`, and string ids raised on Postgres.
    """
    db = _session()
    try:
        client = _client(db)
        for bad in (
            {"tender_ids": "12345"},          # str, not a list of ints
            {"tender_ids": [{"id": 1}]},      # unhashable element
            {"tender_ids": [[1, 2]]},         # unhashable element
            {"tender_ids": ["not-an-int"]},   # non-coercible id
            {},                               # missing field
            {"tender_ids": None},
        ):
            r = client.post("/api/tenders/archive/purge-now", json=bad)
            assert r.status_code == 422, f"{bad} -> {r.status_code}"
    finally:
        app.dependency_overrides.clear()


def test_archive_path_is_not_swallowed_by_tender_id_route():
    """Route-ordering guard: "/archive" must not be parsed as a tender id.

    If the literal routes ever drift below `@router.get("/{tender_id}")`, this
    fails with 422 (int coercion of "archive") instead of 200.
    """
    db = _session()
    try:
        client = _client(db)
        assert client.get("/api/tenders/archive").status_code == 200
        assert client.post("/api/tenders/archive/purge-now",
                           json={"tender_ids": [12345]}).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_bulk_archive_stamps_reason_and_timestamp():
    db = _session()
    try:
        client = _client(db)
        a = _mk(db)
        b = _mk(db)
        r = client.post("/api/tenders/bulk-archive",
                        json={"tender_ids": [a.id, b.id]})
        assert r.status_code == 200
        db.refresh(a)
        db.refresh(b)
        assert a.is_archived is True and b.is_archived is True
        assert a.archive_reason == "manual" and b.archive_reason == "manual"
        assert a.archived_at is not None
        # One `now` computed once for the whole batch, not per row.
        assert a.archived_at == b.archived_at
    finally:
        app.dependency_overrides.clear()
