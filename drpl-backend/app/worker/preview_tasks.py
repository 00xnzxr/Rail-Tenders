"""RQ job: render a cost-breakdown workbook to a previewable PDF.

Enqueued when a `cost_breakdown_xlsx` artifact is created. A failure here is
recorded on the artifact and never propagated — the costing run that produced
the workbook has already succeeded, and the UI degrades to its table view.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone

from app.core.run_context import run_id_scope
from app.services.xlsx_preview_service import (
    build_manifest,
    convert_to_pdf,
)
from app.services.xlsx_print_setup import apply_print_setup

logger = logging.getLogger(__name__)

PREVIEW_MIME = "application/pdf"


def preview_key(artifact_id: int, version: int) -> str:
    """Storage key for a render. Versioned so a regenerated workbook (which
    bumps the artifact version) never serves the previous render."""
    return f"previews/artifact_{artifact_id}/v{version}.pdf"


def _download_workbook(key: str, dest_dir: str) -> str:
    from app.services.storage_service import get_storage_service

    data = get_storage_service().download_file_sync(key)
    path = os.path.join(dest_dir, "book.xlsx")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _upload_pdf(key: str, path: str) -> None:
    from app.services.storage_service import get_storage_service

    with open(path, "rb") as f:
        get_storage_service().upload_file_sync(key, f.read(), content_type=PREVIEW_MIME)


def _read_pdf_outline(pdf_path: str) -> tuple[list, int]:
    """Return ``(toc, page_count)`` for a PDF."""
    import fitz

    doc = fitz.open(pdf_path)
    try:
        return doc.get_toc(), doc.page_count
    finally:
        doc.close()


def _save_preview(db, artifact, **fields) -> None:
    """Merge a preview manifest into metadata_json without dropping siblings."""
    meta = dict(artifact.metadata_json or {})
    meta["preview"] = {
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
        **fields,
    }
    artifact.metadata_json = meta
    # A new dict is built above and reassigned to metadata_json (not mutated
    # in place) — that reassignment is what makes SQLAlchemy detect the
    # change and durable on commit. flag_modified is belt-and-braces in case
    # this ever becomes an in-place mutation instead.
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(artifact, "metadata_json")
    db.commit()


def render_xlsx_preview(artifact_id: int) -> dict:
    """RQ entrypoint. Never raises — records failure on the artifact."""
    from app.core.database import SessionLocal
    from app.models.artifact import CommandCenterArtifact

    with run_id_scope(f"preview-{artifact_id}"):
        db = SessionLocal()
        try:
            try:
                artifact = (
                    db.query(CommandCenterArtifact)
                    .filter(CommandCenterArtifact.id == artifact_id)
                    .first()
                )
            except Exception as e:  # noqa: BLE001
                reason = f"{type(e).__name__}: {e}"[:300]
                logger.exception(
                    "[preview] artifact %s initial fetch failed", artifact_id
                )
                # No artifact object to record the failure on (fetch itself
                # raised) -- don't attempt a DB write that would likely raise
                # too. Just log and return the failure status.
                return {"status": "failed", "artifact_id": artifact_id, "error": reason}

            if artifact is None:
                logger.warning("[preview] artifact %s not found", artifact_id)
                return {"status": "missing", "artifact_id": artifact_id}
            if not artifact.file_path:
                logger.warning("[preview] artifact %s has no file_path", artifact_id)
                return {"status": "skipped", "artifact_id": artifact_id}

            key = preview_key(artifact.id, artifact.version or 1)
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    src = _download_workbook(artifact.file_path, tmp)
                    fitted = os.path.join(tmp, "fitted.xlsx")
                    sheet_names = apply_print_setup(src, fitted)
                    pdf_path = convert_to_pdf(fitted, tmp)
                    toc, page_count = _read_pdf_outline(pdf_path)
                    _upload_pdf(key, pdf_path)

                manifest = build_manifest(sheet_names, toc, page_count)
                _save_preview(
                    db, artifact,
                    status="ready",
                    pdf_key=key,
                    page_count=manifest["page_count"],
                    sheets=manifest["sheets"],
                )
                logger.info(
                    "[preview] artifact %s rendered: %d sheet(s), %d page(s)",
                    artifact_id, len(manifest["sheets"]), manifest["page_count"],
                )
                return {"status": "ready", "artifact_id": artifact_id}

            except Exception as e:  # noqa: BLE001  (includes PreviewRenderError)
                reason = f"{type(e).__name__}: {e}"[:300]
                logger.exception("[preview] artifact %s render failed", artifact_id)
                try:
                    db.rollback()
                    artifact = (
                        db.query(CommandCenterArtifact)
                        .filter(CommandCenterArtifact.id == artifact_id)
                        .first()
                    )
                    meta = dict(artifact.metadata_json or {})
                    meta["preview"] = {
                        "status": "failed",
                        "error": reason,
                        "pdf_key": None,
                        "page_count": 0,
                        "sheets": [],
                        "rendered_at": datetime.now(timezone.utc).isoformat(),
                    }
                    artifact.metadata_json = meta
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(artifact, "metadata_json")
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
                return {"status": "failed", "artifact_id": artifact_id, "error": reason}
        finally:
            db.close()


def enqueue_preview_render(artifact_id: int) -> bool:
    """Queue a render. Returns False when Redis is off (local dev) — never raises."""
    from app.core.redis_client import get_queue

    q = get_queue()
    if q is None:
        logger.info("[preview] queue unavailable; skipping render for %s", artifact_id)
        return False
    try:
        q.enqueue(
            "app.worker.preview_tasks.render_xlsx_preview",
            artifact_id,
            job_id=f"preview-render-{artifact_id}",
            result_ttl=86400,
            # Must exceed convert_to_pdf's own timeout_s=180 so THAT timeout is
            # the one that fires (and raises a proper PreviewRenderError)
            # instead of RQ's default 180s (Queue.DEFAULT_TIMEOUT) killing the
            # job mid-conversion with no chance to record a failure reason.
            job_timeout=300,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[preview] enqueue failed for artifact %s: %s", artifact_id, e)
        return False
