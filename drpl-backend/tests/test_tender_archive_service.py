"""Archive columns, sweep passes, and deep delete."""
from datetime import datetime, timezone, timedelta

from app.models.tender import Tender

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)

_n = 0


def _mk(db, *, closing_days_ago=None, workflow_status="new", assigned_to=None,
        is_archived=False, archived_at=None, archive_reason=None):
    """Insert a tender. closing_days_ago=4 means closing_date was 4 days before NOW."""
    global _n
    _n += 1
    closing = None if closing_days_ago is None else NOW - timedelta(days=closing_days_ago)
    t = Tender(portal="ireps", tender_id=f"ARC{_n}", title=f"archive test {_n}",
               closing_date=closing, workflow_status=workflow_status,
               assigned_to=assigned_to, is_archived=is_archived,
               archived_at=archived_at, archive_reason=archive_reason)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_archive_columns_exist_and_default_to_none(db):
    t = _mk(db)
    assert t.archived_at is None
    assert t.archive_reason is None


def test_archive_settings_defaults(db):
    from app.services.auto_scoring_settings import get_scoring_settings
    s = get_scoring_settings(db)
    assert s["archive_sweep_enabled"] is False   # ships OFF — deliberate
    assert s["archive_grace_days"] == 3
    assert s["archive_purge_days"] == 7


def test_archive_settings_are_db_overridable(db):
    from app.services.auto_scoring_settings import get_scoring_settings, set_scoring_setting
    set_scoring_setting(db, "archive_grace_days", 5)
    assert get_scoring_settings(db)["archive_grace_days"] == 5
    set_scoring_setting(db, "archive_grace_days", 3)   # restore


def test_delete_tenders_deep_removes_children(db):
    from app.models.tender import TenderDocument
    from app.models.checklist import ChecklistItem
    from app.models.cost_breakdown import CostBreakdown
    from app.models.message_batch import MessageBatchItem
    from app.models.proposal import ProposalSession, ProposalMessage
    from app.services.tender_archive_service import delete_tenders_deep

    t = _mk(db)
    db.add(TenderDocument(tender_id=t.id, file_name="a.pdf", file_path="tenders/1/a.pdf"))
    db.add(ChecklistItem(tender_id=t.id, item_name="Reg cert"))
    db.add(CostBreakdown(tender_id=t.id))
    db.add(MessageBatchItem(batch_id="msgbatch_test", custom_id="c1",
                             item_type="classifier", tender_id=t.id))
    session = ProposalSession(tender_id=t.id, created_by=1)
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = session.id   # captured before delete_tenders_deep expires `session`
    db.add(ProposalMessage(session_id=session_id, role="user", content="hello"))
    db.commit()

    assert delete_tenders_deep(db, [t.id]) == 1

    assert db.query(Tender).filter(Tender.id == t.id).first() is None
    assert db.query(TenderDocument).filter(TenderDocument.tender_id == t.id).count() == 0
    assert db.query(ChecklistItem).filter(ChecklistItem.tender_id == t.id).count() == 0
    assert db.query(CostBreakdown).filter(CostBreakdown.tender_id == t.id).count() == 0
    assert db.query(MessageBatchItem).filter(MessageBatchItem.tender_id == t.id).count() == 0
    assert db.query(ProposalSession).filter(ProposalSession.tender_id == t.id).count() == 0
    assert db.query(ProposalMessage).filter(ProposalMessage.session_id == session_id).count() == 0


def test_delete_tenders_deep_empty_list_is_noop(db):
    from app.services.tender_archive_service import delete_tenders_deep
    assert delete_tenders_deep(db, []) == 0


# --------------------------------------------------------------------------
# Archive pass
# --------------------------------------------------------------------------

def test_past_due_untouched_tender_is_archived(db):
    from app.services.tender_archive_service import run_archive_pass
    t = _mk(db, closing_days_ago=4)
    assert run_archive_pass(db, now=NOW, grace_days=3, batch_size=500) >= 1
    db.refresh(t)
    assert t.is_archived is True
    assert t.archive_reason == "past_due"
    assert t.archived_at is not None


def test_grace_boundary(db):
    """2 days past closing survives a 3-day grace; 4 days does not."""
    from app.services.tender_archive_service import archive_candidates_query
    young = _mk(db, closing_days_ago=2)
    old = _mk(db, closing_days_ago=4)
    ids = {t.id for t in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert young.id not in ids
    assert old.id in ids


def test_tender_with_no_closing_date_is_never_archived(db):
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=None)
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_assigned_tender_is_not_archived(db):
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10, assigned_to=1)
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_advanced_workflow_status_is_not_archived(db):
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10, workflow_status="proposal_draft")
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_tender_with_costing_is_not_archived(db):
    from app.models.cost_breakdown import CostBreakdown
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10)
    db.add(CostBreakdown(tender_id=t.id))
    db.commit()
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_tender_with_workspace_is_not_archived(db):
    from app.models.workspace import WorkspaceConfig
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10)
    db.add(WorkspaceConfig(tender_id=t.id))
    db.commit()
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id not in ids


def test_tender_with_only_documents_IS_archived(db):
    """Documents are auto-captured, not evidence of human work."""
    from app.models.tender import TenderDocument
    from app.services.tender_archive_service import archive_candidates_query
    t = _mk(db, closing_days_ago=10)
    db.add(TenderDocument(tender_id=t.id, file_name="nit.pdf", file_path="tenders/x/nit.pdf"))
    db.commit()
    ids = {x.id for x in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert t.id in ids


# --------------------------------------------------------------------------
# Purge pass — the safety invariant
# --------------------------------------------------------------------------

def test_purge_boundary(db):
    """6 days in archive survives a 7-day purge; 8 days does not."""
    from app.services.tender_archive_service import purge_candidates_query
    young = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=6),
                archive_reason="past_due")
    old = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=8),
              archive_reason="past_due")
    ids = {t.id for t in purge_candidates_query(db, now=NOW, purge_days=7).all()}
    assert young.id not in ids
    assert old.id in ids


def test_auto_discard_rows_are_never_purged(db):
    from app.services.tender_archive_service import purge_candidates_query
    t = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=90),
            archive_reason="auto_discard")
    ids = {x.id for x in purge_candidates_query(db, now=NOW, purge_days=7).all()}
    assert t.id not in ids


def test_manually_archived_rows_are_never_purged(db):
    from app.services.tender_archive_service import purge_candidates_query
    t = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=90),
            archive_reason="manual")
    ids = {x.id for x in purge_candidates_query(db, now=NOW, purge_days=7).all()}
    assert t.id not in ids


def test_legacy_archived_rows_are_never_purged(db):
    """Rows archived before this feature have archived_at=NULL, reason=NULL."""
    from app.services.tender_archive_service import purge_candidates_query
    t = _mk(db, is_archived=True, archived_at=None, archive_reason=None)
    ids = {x.id for x in purge_candidates_query(db, now=NOW, purge_days=7).all()}
    assert t.id not in ids


def test_run_purge_pass_deletes_the_row(db):
    from app.services.tender_archive_service import run_purge_pass
    t = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=8),
            archive_reason="past_due")
    tid = t.id
    assert run_purge_pass(db, now=NOW, purge_days=7, batch_size=500) >= 1
    assert db.query(Tender).filter(Tender.id == tid).first() is None


def test_run_purge_pass_spares_protected_rows(db):
    """End-to-end invariant: a real purge run leaves non-past_due rows alive."""
    from app.services.tender_archive_service import run_purge_pass
    auto = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=90),
               archive_reason="auto_discard")
    manual = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=90),
                 archive_reason="manual")
    legacy = _mk(db, is_archived=True, archived_at=None, archive_reason=None)
    doomed = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=90),
                 archive_reason="past_due")
    ids = (auto.id, manual.id, legacy.id, doomed.id)
    run_purge_pass(db, now=NOW, purge_days=7, batch_size=500)
    survivors = {r.id for r in db.query(Tender).filter(Tender.id.in_(ids)).all()}
    assert survivors == {auto.id, manual.id, legacy.id}


def test_run_archive_pass_respects_batch_size(db):
    """A backlog larger than batch_size is drained across calls, not in one txn."""
    from app.services.tender_archive_service import run_archive_pass
    for _ in range(3):
        _mk(db, closing_days_ago=20)
    assert run_archive_pass(db, now=NOW, grace_days=3, batch_size=2) == 2


# --------------------------------------------------------------------------
# Work-artifact probe: must fail CLOSED
# --------------------------------------------------------------------------

def test_total_probe_failure_archives_nothing(db, monkeypatch):
    """If EVERY work-artifact probe fails the guard is blind — archive nothing.

    Failing open here would flag every past-due new/unassigned tender,
    costings and all, each starting a purge clock.
    """
    from app.services import tender_archive_service as svc

    t = _mk(db, closing_days_ago=10)
    # Sanity: this tender IS a candidate while the probes work.
    assert t.id in {x.id for x in
                    svc.archive_candidates_query(db, now=NOW, grace_days=3).all()}

    monkeypatch.setattr(svc, "WORK_ARTIFACT_TABLES",
                        ("no_such_table_a", "no_such_table_b"))
    assert svc.archive_candidates_query(db, now=NOW, grace_days=3).count() == 0
    assert svc.run_archive_pass(db, now=NOW, grace_days=3, batch_size=500) == 0

    db.refresh(t)
    assert t.is_archived is False
    assert t.archive_reason is None


def test_partial_probe_failure_still_filters(db, monkeypatch):
    """One missing table must not disable the guard built from the rest."""
    from app.models.cost_breakdown import CostBreakdown
    from app.services import tender_archive_service as svc

    worked_on = _mk(db, closing_days_ago=10)
    db.add(CostBreakdown(tender_id=worked_on.id))
    untouched = _mk(db, closing_days_ago=10)
    db.commit()

    # cost_breakdowns probes fine; the other name does not exist.
    monkeypatch.setattr(svc, "WORK_ARTIFACT_TABLES",
                        ("cost_breakdowns", "no_such_table_c"))
    ids = {x.id for x in svc.archive_candidates_query(db, now=NOW, grace_days=3).all()}

    assert worked_on.id not in ids   # guard still working off the live table
    assert untouched.id in ids       # and not shut down wholesale


def test_single_surviving_probe_still_negates(db, monkeypatch):
    """Regression: exactly ONE working table must not crash the sweep.

    `or_()` of a single element returns that element unchanged, and a bare
    TextClause has no working `_negate` — so `not_(or_(text(...)))` raised an
    internal AssertionError. Invisible with all 9 tables present.
    """
    from app.models.cost_breakdown import CostBreakdown
    from app.services import tender_archive_service as svc

    worked_on = _mk(db, closing_days_ago=10)
    db.add(CostBreakdown(tender_id=worked_on.id))
    untouched = _mk(db, closing_days_ago=10)
    db.commit()

    monkeypatch.setattr(svc, "WORK_ARTIFACT_TABLES", ("cost_breakdowns",))
    ids = {x.id for x in svc.archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert worked_on.id not in ids
    assert untouched.id in ids


# ── Scheduled sweep job (app.worker.scheduled_tasks.run_archive_sweep) ─────
#
# These monkeypatch SessionLocal to the shared test session and no-op its
# close(), so the job runs against the same DB the fixtures wrote to. There is
# no Redis in tests, so get_queue() already returns None — patched explicitly
# anyway so the test does not silently depend on that.


def _patch_sweep_session(st, db, monkeypatch):
    monkeypatch.setattr(st, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(st, "get_queue", lambda: None)


def test_sweep_is_a_noop_when_disabled(db, monkeypatch):
    """Ships off — the job must do nothing until an admin enables it."""
    import app.worker.scheduled_tasks as st
    _patch_sweep_session(st, db, monkeypatch)

    t = _mk(db, closing_days_ago=30)
    out = st.run_archive_sweep()

    assert out["archived"] == 0
    assert out["purged"] == 0
    assert out["reason"] == "disabled"
    db.refresh(t)
    assert t.is_archived in (False, None)


def test_sweep_reschedules_itself_even_when_disabled(db, monkeypatch):
    """The disabled path must NOT early-return past the reschedule.

    If it did, the self-rescheduling loop would die on the first tick while
    disabled, and flipping `archive_sweep_enabled` on in Platform Settings
    would do nothing until the next deploy.
    """
    import app.worker.scheduled_tasks as st
    _patch_sweep_session(st, db, monkeypatch)

    called = []
    monkeypatch.setattr(st, "_reschedule_archive_sweep", lambda: called.append(1))

    out = st.run_archive_sweep()
    assert out["reason"] == "disabled"
    assert out["rescheduled"] is True
    assert called == [1], "disabled tick must still re-enqueue the next run"


def test_sweep_archives_when_enabled(db, monkeypatch):
    import app.worker.scheduled_tasks as st
    from app.services.auto_scoring_settings import set_scoring_setting
    _patch_sweep_session(st, db, monkeypatch)
    set_scoring_setting(db, "archive_sweep_enabled", True)
    try:
        t = _mk(db, closing_days_ago=30)
        out = st.run_archive_sweep()
        assert out["archived"] >= 1
        assert "reason" not in out
        assert out["guard_ok"] is True
        db.refresh(t)
        assert t.is_archived is True
        assert t.archive_reason == "past_due"
    finally:
        set_scoring_setting(db, "archive_sweep_enabled", False)


def test_sweep_reports_guard_ok_false_on_total_probe_failure(db, monkeypatch):
    """A healthy zero must be distinguishable from a broken guard.

    When every work-artifact probe fails the service fails CLOSED and archives
    nothing — same `archived: 0` an idle sweep returns. The job result carries
    `guard_ok` so an operator can tell the two apart.
    """
    import app.worker.scheduled_tasks as st
    from app.services import tender_archive_service as svc
    from app.services.auto_scoring_settings import set_scoring_setting
    _patch_sweep_session(st, db, monkeypatch)
    monkeypatch.setattr(svc, "WORK_ARTIFACT_TABLES", ("table_that_does_not_exist",))
    set_scoring_setting(db, "archive_sweep_enabled", True)
    try:
        t = _mk(db, closing_days_ago=30)
        out = st.run_archive_sweep()
        assert out["archived"] == 0
        assert out["guard_ok"] is False
        db.refresh(t)
        assert t.is_archived in (False, None)
    finally:
        set_scoring_setting(db, "archive_sweep_enabled", False)


def test_sweep_uses_one_clock_for_both_passes(db, monkeypatch):
    """Both passes must receive the identical `now`, computed once per tick."""
    import app.worker.scheduled_tasks as st
    from app.services import tender_archive_service as svc
    from app.services.auto_scoring_settings import set_scoring_setting
    _patch_sweep_session(st, db, monkeypatch)

    seen = {}

    def _spy(slot):
        def fn(db_, **kw):
            seen[slot] = kw["now"]
            return 0
        return fn

    monkeypatch.setattr(svc, "run_archive_pass", _spy("a"))
    monkeypatch.setattr(svc, "run_purge_pass", _spy("p"))

    set_scoring_setting(db, "archive_sweep_enabled", True)
    try:
        st.run_archive_sweep()
    finally:
        set_scoring_setting(db, "archive_sweep_enabled", False)

    assert "a" in seen and "p" in seen
    assert seen["a"] == seen["p"]
    assert seen["a"].tzinfo is not None


def test_discard_cleanup_stamps_archive_reason(db):
    """Auto-discarded rows must be stamped so the purge never touches them."""
    from datetime import timedelta as _td
    import app.worker.scheduled_tasks as st

    old = datetime.now(timezone.utc) - _td(days=400)
    t = Tender(portal="ireps", tender_id="ARCDISCARD1", title="discard stamp",
               segment="discarded", segment_overridden=False,
               workflow_status="new", is_archived=False, created_at=old)
    db.add(t)
    db.commit()

    out = st._run_discard_cleanup(db)
    assert out["archived"] >= 1
    db.refresh(t)
    assert t.is_archived is True
    assert t.archive_reason == "auto_discard"
    assert t.archived_at is not None
    # And it is invisible to the purge, no matter how old.
    from app.services.tender_archive_service import purge_candidates_query
    far_future = datetime.now(timezone.utc) + _td(days=3650)
    ids = {x.id for x in purge_candidates_query(db, now=far_future, purge_days=7).all()}
    assert t.id not in ids


def test_sweep_reschedules_when_settings_read_raises(db, monkeypatch):
    """A settings-read failure must degrade to a no-op, not kill the heartbeat.

    This job is seeded only at startup and has no RQ retry or on_failure hook,
    so a propagated exception stops the self-rescheduling loop until the next
    deploy — the exact failure the disabled-path requirement exists to prevent.
    """
    import app.worker.scheduled_tasks as st
    _patch_sweep_session(st, db, monkeypatch)

    calls = []
    monkeypatch.setattr(st, "_reschedule_archive_sweep", lambda: calls.append(1))

    def _boom(_db):
        raise RuntimeError("settings table is on fire")

    monkeypatch.setattr(
        "app.services.auto_scoring_settings.get_scoring_settings", _boom)

    out = st.run_archive_sweep()   # must return, not propagate

    assert calls == [1], "a failed tick must still re-enqueue the next run"
    assert out == {"archived": 0, "purged": 0, "rescheduled": True,
                   "reason": "disabled"}


def test_sweep_reschedules_when_session_close_raises(db, monkeypatch):
    """An exception from db.close() in the finally must not skip the reschedule."""
    import app.worker.scheduled_tasks as st
    monkeypatch.setattr(st, "SessionLocal", lambda: db)
    monkeypatch.setattr(st, "get_queue", lambda: None)

    def _boom():
        raise RuntimeError("connection already returned to pool")

    monkeypatch.setattr(db, "close", _boom)

    calls = []
    monkeypatch.setattr(st, "_reschedule_archive_sweep", lambda: calls.append(1))

    out = st.run_archive_sweep()   # must return, not propagate

    assert calls == [1], "a failing close() must not kill the heartbeat"
    assert out["rescheduled"] is True
    assert out["archived"] == 0 and out["purged"] == 0


def test_segment_overridden_exempts_a_row_from_the_archive_sweep(db):
    """A human-touched row is never auto-archived, however past-due it is.

    The two rows are identical apart from `segment_overridden`, so this cannot
    pass vacuously — if the filter were missing, BOTH would be candidates; if
    the filter were inverted, NEITHER of the expected results would hold.
    """
    from app.services.tender_archive_service import archive_candidates_query

    touched = _mk(db, closing_days_ago=30)
    touched.segment_overridden = True
    untouched = _mk(db, closing_days_ago=30)
    untouched.segment_overridden = False
    db.commit()

    # Scope by id: the fixture DB is shared and never truncated.
    ids = {t.id for t in archive_candidates_query(db, now=NOW, grace_days=3).all()}
    assert untouched.id in ids, "an untouched past-due tender must still be archivable"
    assert touched.id not in ids, "segment_overridden must exempt the row from the sweep"


def test_segment_overridden_filter_is_null_tolerant():
    """A legacy NULL must read as "untouched", i.e. still archivable.

    `create_all` builds the column NOT NULL from the model, so the shared `db`
    fixture physically cannot hold a NULL — but real deployments got this column
    via `ALTER TABLE ... ADD COLUMN ... DEFAULT false` with no NOT NULL
    (main.py:180 and :615), where legacy rows DO hold NULL. This test therefore
    builds a throwaway engine whose column is nullable, matching production.

    Why it matters: under SQL three-valued logic `segment_overridden = 0` drops
    NULL rows, so the plain `== False` form would silently exempt tenders nobody
    ever touched. The explicit IS NULL leg is what keeps them archivable.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.core.database import Base
    from app.services.tender_archive_service import archive_candidates_query

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    ldb = sessionmaker(bind=engine)()

    # Rebuild `tenders` with a nullable segment_overridden, as production has it.
    cols = [r[1] for r in ldb.execute(text("PRAGMA table_info(tenders)"))]
    assert "segment_overridden" in cols
    ddl = ldb.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tenders'")).scalar()
    assert "segment_overridden BOOLEAN NOT NULL" in ddl, ddl
    ldb.execute(text("ALTER TABLE tenders RENAME TO tenders_old"))
    ldb.execute(text(ddl.replace("segment_overridden BOOLEAN NOT NULL",
                                 "segment_overridden BOOLEAN")))
    ldb.execute(text(f"INSERT INTO tenders ({','.join(cols)}) "
                     f"SELECT {','.join(cols)} FROM tenders_old"))
    ldb.execute(text("DROP TABLE tenders_old"))
    ldb.commit()

    legacy = Tender(portal="ireps", tender_id="LEGACY1", title="legacy",
                    closing_date=NOW - timedelta(days=30), workflow_status="new")
    ldb.add(legacy)
    ldb.commit()
    ldb.execute(text("UPDATE tenders SET segment_overridden = NULL WHERE id = :i"),
                {"i": legacy.id})
    ldb.commit()
    assert ldb.execute(text(
        "SELECT segment_overridden FROM tenders WHERE id = :i"),
        {"i": legacy.id}).scalar() is None, "precondition: the row really holds NULL"

    ids = {t.id for t in archive_candidates_query(ldb, now=NOW, grace_days=3).all()}
    assert legacy.id in ids, "a NULL segment_overridden must not silently exempt a row"
    ldb.close()


def test_purge_days_zero_means_never_purge(db):
    """archive_purge_days=0 is the "archive but never hard-delete" setting.

    Read literally, `now - timedelta(days=0)` is `now`, so 0 would mean "purge
    everything already archived" — the single most destructive possible reading
    of a number an admin might reasonably type to mean "off". It must mean the
    opposite: never.
    """
    from app.services.tender_archive_service import purge_candidates_query
    ancient = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=3650),
                  archive_reason="past_due")

    ids = {t.id for t in purge_candidates_query(db, now=NOW, purge_days=0).all()}
    assert ancient.id not in ids, "purge_days=0 must never delete anything"
    assert ids == set(), "purge_days=0 must yield an empty candidate set"


def test_run_purge_pass_deletes_nothing_when_purge_days_zero(db):
    """The pass, not just the query — nothing may reach delete_tenders_deep."""
    from app.services.tender_archive_service import run_purge_pass
    ancient = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=3650),
                  archive_reason="past_due")

    assert run_purge_pass(db, now=NOW, purge_days=0, batch_size=500) == 0
    assert db.query(Tender).filter(Tender.id == ancient.id).first() is not None


def test_negative_purge_days_also_means_never(db):
    """A negative value is nonsense input; fail safe rather than delete early."""
    from app.services.tender_archive_service import purge_candidates_query
    ancient = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=3650),
                  archive_reason="past_due")

    ids = {t.id for t in purge_candidates_query(db, now=NOW, purge_days=-5).all()}
    assert ancient.id not in ids


def test_positive_purge_days_still_purges(db):
    """The guard must not break the normal path."""
    from app.services.tender_archive_service import purge_candidates_query
    old = _mk(db, is_archived=True, archived_at=NOW - timedelta(days=8),
              archive_reason="past_due")

    ids = {t.id for t in purge_candidates_query(db, now=NOW, purge_days=7).all()}
    assert old.id in ids


def test_run_passes_are_noops_when_nothing_matches(db):
    from app.services.tender_archive_service import run_archive_pass, run_purge_pass
    # Drain whatever the earlier tests left behind, then assert idempotence.
    while run_archive_pass(db, now=NOW, grace_days=3, batch_size=500):
        pass
    while run_purge_pass(db, now=NOW, purge_days=7, batch_size=500):
        pass
    assert run_archive_pass(db, now=NOW, grace_days=3, batch_size=500) == 0
    assert run_purge_pass(db, now=NOW, purge_days=7, batch_size=500) == 0
