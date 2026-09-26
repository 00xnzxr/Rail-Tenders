"""Creating a cost_breakdown_xlsx artifact always queues a preview render.

Both producers go through one helper so a new call site cannot silently skip it.
"""
from app.services import cost_breakdown_service as cbs


def test_persist_creates_artifact_and_enqueues(monkeypatch, db):
    monkeypatch.setattr(cbs, "_upload_xlsx", lambda key, data: None)
    queued = []
    monkeypatch.setattr(cbs, "enqueue_preview_render", lambda aid: queued.append(aid))

    info = cbs.persist_xlsx_artifact(
        db,
        session_id=1,
        title="Cost Breakdown",
        fname="cost_tender_1.xlsx",
        data=b"fake",
        rows=[{"description": "x"}],
        summary={"total_amount": 100},
        metadata_extra={"tender_id": 3808},
    )

    assert info["artifact_type"] == "cost_breakdown_xlsx"
    assert info["file_path"] == "generated_docs/cost_tender_1.xlsx"
    assert queued == [info["artifact_id"]]


def test_enqueue_failure_does_not_break_artifact_creation(monkeypatch, db):
    """Redis being down must not fail a costing run."""
    monkeypatch.setattr(cbs, "_upload_xlsx", lambda key, data: None)

    def boom(aid):
        raise RuntimeError("redis down")

    monkeypatch.setattr(cbs, "enqueue_preview_render", boom)

    info = cbs.persist_xlsx_artifact(
        db, session_id=1, title="t", fname="f.xlsx",
        data=b"x", rows=[{"description": "x"}], summary={},
    )
    assert info["artifact_id"] is not None
