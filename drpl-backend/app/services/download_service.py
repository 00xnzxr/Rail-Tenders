"""
DRPL Backend - Download Service
Assembles all tender documents + approved proposal into a ZIP file,
reading object data from the storage backend (local or Cloudflare R2).
"""

import io
import logging
import zipfile
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.tender import Tender, TenderDocument
from app.models.proposal import ProposalSession, ProposalDocument
from app.services.storage_service import get_storage_service

logger = logging.getLogger(__name__)


def build_tender_zip(db: Session, tender_id: int) -> tuple[io.BytesIO, str]:
    """
    Build a ZIP file containing all tender documents and the approved proposal.
    Returns (bytes_buffer, filename).
    """
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise ValueError("Tender not found")

    if tender.workflow_status != "approved":
        raise ValueError("Tender must be approved before downloading")

    # Get all tender documents
    documents = db.query(TenderDocument).filter(
        TenderDocument.tender_id == tender_id,
    ).all()

    # Get approved proposal document
    session = db.query(ProposalSession).filter(
        ProposalSession.tender_id == tender_id,
        ProposalSession.status == "approved",
    ).first()

    proposal_doc = None
    if session:
        proposal_doc = db.query(ProposalDocument).filter(
            ProposalDocument.session_id == session.id,
        ).order_by(ProposalDocument.generated_at.desc()).first()

    # Build ZIP in memory by streaming bytes from the storage backend
    storage = get_storage_service()
    buffer = io.BytesIO()
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in tender.title[:50])
    zip_filename = f"DRPL_{tender.portal.upper()}_{safe_title}.zip"

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add tender info
        tender_info = f"""Tender: {tender.title}
Portal: {tender.portal.upper()}
Tender ID: {tender.tender_id}
Department: {tender.department or 'N/A'}
Organisation: {tender.organisation or 'N/A'}
Closing Date: {tender.closing_date or 'N/A'}
Status: {tender.workflow_status}
Generated: {datetime.now(timezone.utc).isoformat()}
"""
        zf.writestr("tender_info.txt", tender_info)

        # Add checklist/uploaded documents
        for doc in documents:
            if not doc.file_path:
                continue
            try:
                data = storage.download_file_sync(doc.file_path)
            except Exception as e:
                logger.warning(f"[ZIP] Skipped missing doc {doc.id} ({doc.file_path}): {e}")
                continue
            doc_type = doc.document_type or "other"
            folder = {
                "checklist_upload": "checklist_documents",
                "tender_notice": "tender_info",
                "proposal": "proposal",
            }.get(doc_type, "other_documents")
            arcname = f"{folder}/{doc.file_name}"
            zf.writestr(arcname, data)

        # Add proposal document
        if proposal_doc and proposal_doc.file_path:
            try:
                data = storage.download_file_sync(proposal_doc.file_path)
                zf.writestr(f"proposal/{proposal_doc.file_name}", data)
            except Exception as e:
                logger.warning(
                    f"[ZIP] Skipped missing proposal doc {proposal_doc.id} "
                    f"({proposal_doc.file_path}): {e}"
                )

    buffer.seek(0)
    return buffer, zip_filename
