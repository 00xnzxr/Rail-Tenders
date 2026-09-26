"""The preview render job.

A preview failure must never fail the costing run that produced the workbook --
it records status="failed" with a reason and returns.
"""
import pytest

from app.models.artifact import CommandCenterArtifact
from app.worker import preview_tasks


@pytest.fixture
def artifact(db):
    a = CommandCenterArtifact(
        session_id=1,
        artifact_type="cost_breakdown_xlsx",
        title="Cost Breakdown",
        content="{}",
        version=2,
        file_path="generated_docs/book.xlsx",
        file_name="book.xlsx",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    yield a


def test_preview_key_is_versioned(artifact):
    """A regenerated workbook must not serve the previous render."""
    assert preview_tasks.preview_key(artifact.id, 2) == f"previews/artifact_{artifact.id}/v2.pdf"
    assert preview_tasks.preview_key(artifact.id, 3) != preview_tasks.preview_key(artifact.id, 2)


def test_successful_render_writes_a_ready_manifest(monkeypatch, db, artifact):
    monkeypatch.setattr(preview_tasks, "_download_workbook", lambda k, d: f"{d}/book.xlsx")
    monkeypatch.setattr(
        preview_tasks, "apply_print_setup",
        lambda src, dest: ["1. Summary", "2. Cost Assumptions"],
    )
    monkeypatch.setattr(preview_tasks, "convert_to_pdf", lambda src, out, **kw: f"{out}/book.pdf")
    monkeypatch.setattr(
        preview_tasks, "_read_pdf_outline",
        lambda p: ([[1, "1. Summary", 1], [1, "2. Cost Assumptions", 4]], 9),
    )
    uploaded = {}
    monkeypatch.setattr(
        preview_tasks, "_upload_pdf",
        lambda key, path: uploaded.setdefault("key", key),
    )

    result = preview_tasks.render_xlsx_preview(artifact.id)

    assert result["status"] == "ready"
    db.refresh(artifact)
    preview = artifact.metadata_json["preview"]
    assert preview["status"] == "ready"
    assert preview["page_count"] == 9
    assert preview["sheets"] == [
        {"name": "1. Summary", "start_page": 1},
        {"name": "2. Cost Assumptions", "start_page": 4},
    ]
    assert preview["pdf_key"] == uploaded["key"] == f"previews/artifact_{artifact.id}/v2.pdf"
    assert preview["error"] is None


def test_render_failure_is_recorded_not_raised(monkeypatch, db, artifact):
    from app.services.xlsx_preview_service import PreviewRenderError

    monkeypatch.setattr(preview_tasks, "_download_workbook", lambda k, d: f"{d}/book.xlsx")
    monkeypatch.setattr(preview_tasks, "apply_print_setup", lambda src, dest: ["S"])

    def boom(src, out, **kw):
        raise PreviewRenderError("soffice exited 1: boom")

    monkeypatch.setattr(preview_tasks, "convert_to_pdf", boom)

    result = preview_tasks.render_xlsx_preview(artifact.id)

    assert result["status"] == "failed"
    db.refresh(artifact)
    preview = artifact.metadata_json["preview"]
    assert preview["status"] == "failed"
    assert "boom" in preview["error"]


def test_existing_metadata_is_preserved(monkeypatch, db, artifact):
    artifact.metadata_json = {"tender_id": 3808, "file_name": "book.xlsx"}
    db.commit()
    monkeypatch.setattr(preview_tasks, "_download_workbook", lambda k, d: f"{d}/book.xlsx")
    monkeypatch.setattr(preview_tasks, "apply_print_setup", lambda src, dest: ["S"])
    monkeypatch.setattr(preview_tasks, "convert_to_pdf", lambda src, out, **kw: f"{out}/book.pdf")
    monkeypatch.setattr(preview_tasks, "_read_pdf_outline", lambda p: ([[1, "S", 1]], 3))
    monkeypatch.setattr(preview_tasks, "_upload_pdf", lambda key, path: None)

    preview_tasks.render_xlsx_preview(artifact.id)

    db.refresh(artifact)
    assert artifact.metadata_json["tender_id"] == 3808
    assert artifact.metadata_json["preview"]["status"] == "ready"


def test_initial_fetch_failure_is_returned_not_raised(monkeypatch, artifact):
    """If the very first query (before the guarded try) blows up -- a
    transient DB error, or a JSON-decode failure on a malformed
    metadata_json -- render_xlsx_preview must still return a dict with a
    failure status instead of letting the exception escape the job."""
    import app.core.database as database_module

    class ExplodingQuery:
        def filter(self, *a, **kw):
            return self

        def first(self):
            raise RuntimeError("db exploded")

    class ExplodingSession:
        def query(self, *a, **kw):
            return ExplodingQuery()

        def close(self):
            pass

        def rollback(self):
            pass

        def commit(self):
            pass

    monkeypatch.setattr(database_module, "SessionLocal", lambda: ExplodingSession())

    result = preview_tasks.render_xlsx_preview(artifact.id)

    assert isinstance(result, dict)
    assert result["status"] == "failed"
    assert result["artifact_id"] == artifact.id
    assert "db exploded" in result["error"]


def test_unknown_artifact_returns_missing(db):
    assert preview_tasks.render_xlsx_preview(999999)["status"] == "missing"


def test_artifact_without_file_path_is_skipped(db):
    a = CommandCenterArtifact(
        session_id=1, artifact_type="cost_breakdown_xlsx",
        title="t", content="{}", version=1,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    assert preview_tasks.render_xlsx_preview(a.id)["status"] == "skipped"
