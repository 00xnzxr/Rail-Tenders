"""
DRPL Backend - GEM File ID Extraction & Download Service
Parses GEM file identifiers from analysis text, creates pending document
records, and attempts to download files from GEM portal URLs.
"""

import os
import re
import logging
import tempfile
from typing import Optional

from sqlalchemy.orm import Session

from app.services.storage_service import get_storage_service, key_gem_file

logger = logging.getLogger(__name__)

# Regex patterns to extract GEM file IDs from analysis text.
# Matches patterns like: (file: 1653305920), (file_id: 1653305920), (file id: 1653305920)
# Also matches: file: 1653305920, fileId: 1653305920
_FILE_ID_PATTERNS = [
    # Pattern with parentheses: "document name (file: 1234567890)"
    re.compile(
        r'([^\n(]{3,80}?)\s*\(file[_\s]?(?:id)?[:\s=]+(\d{8,12})\)',
        re.IGNORECASE,
    ),
    # Pattern with colon: "file: 1234567890" or "file_id: 1234567890"
    re.compile(
        r'(?:^|[\n•\-*])\s*([^\n(]{3,80}?)\s+file[_\s]?(?:id)?[:\s=]+(\d{8,12})',
        re.IGNORECASE,
    ),
    # GEM tender format: "Document Name:1234567890.pdf" or "Document Name: 1234567890.pdf"
    re.compile(
        r'([^\n:]{3,80}?)[:\s]+(\d{8,12})\.pdf',
        re.IGNORECASE,
    ),
    # Standalone GEM file IDs in URLs or text: "1234567890" (10-digit numbers)
    re.compile(
        r'(?:download|bidfiles|showbiddoc)[/=]+(\d{10,12})',
        re.IGNORECASE,
    ),
]

# GEM URL templates to try for downloading files.
# Order matters — most likely pattern first.
GEM_URL_TEMPLATES = [
    "https://bidplus.gem.gov.in/bidfiles/download/{file_id}",
    "https://mkp.gem.gov.in/download/file/{file_id}",
    "https://bidplus.gem.gov.in/showbiddoc/{file_id}",
]

MAX_FILES_PER_ANALYSIS = 20


def extract_gem_file_ids(text: str) -> list[dict]:
    """
    Parse analysis text for GEM file ID references.

    Returns list of {"name": str, "gem_file_id": str} dicts, capped at 20.
    """
    if not text:
        return []

    results = []
    seen_ids = set()

    for pattern in _FILE_ID_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()
            if len(groups) >= 2:
                name = groups[0].strip().rstrip(":-–—")
                file_id = groups[1]
            else:
                name = ""
                file_id = groups[0]

            if file_id in seen_ids:
                continue
            seen_ids.add(file_id)

            # Clean up the name
            name = re.sub(r'^[\d.)\]\s]+', '', name).strip()
            if not name:
                name = f"GEM Document {file_id}"

            results.append({
                "name": name,
                "gem_file_id": file_id,
            })

            if len(results) >= MAX_FILES_PER_ANALYSIS:
                break

        if len(results) >= MAX_FILES_PER_ANALYSIS:
            break

    logger.info(f"[GEM] Extracted {len(results)} file IDs from analysis text")
    return results


def create_pending_documents(
    db: Session,
    tender_id: int,
    file_refs: list[dict],
    user_id: Optional[int] = None,
) -> int:
    """
    Create TenderDocument records for GEM file references that don't already exist.

    Returns count of newly created records.
    """
    from app.models.tender import TenderDocument

    created = 0
    for ref in file_refs:
        gem_fid = ref["gem_file_id"]

        # Check for existing record
        existing = db.query(TenderDocument).filter(
            TenderDocument.tender_id == tender_id,
            TenderDocument.gem_file_id == gem_fid,
        ).first()
        if existing:
            continue

        doc = TenderDocument(
            tender_id=tender_id,
            file_name=ref["name"] + ".pdf",
            file_path="",  # Set after download
            file_size=0,
            mime_type="application/pdf",
            document_type="gem_referenced",
            uploaded_by=user_id,
            extraction_status="pending",
            gem_file_id=gem_fid,
        )
        db.add(doc)
        created += 1

    if created:
        db.commit()
        logger.info(f"[GEM] Created {created} pending document records for tender {tender_id}")

    return created


def download_gem_file(gem_file_id: str, tender_id: int) -> dict:
    """
    Attempt to download a file from GEM using known URL templates and stage
    it in object storage. Returns a result dict whose `file_path` is the
    storage key (not a local path).
    """
    from app.services.document_link_service import download_document

    last_result = {"success": False, "error": "no_urls_tried"}
    storage = get_storage_service()

    with tempfile.TemporaryDirectory(prefix="gem_dl_") as tmpdir:
        for template in GEM_URL_TEMPLATES:
            url = template.format(file_id=gem_file_id)
            result = download_document(url, tmpdir)

            if result["success"]:
                local_path = result["file_path"]
                file_name = result.get("file_name") or os.path.basename(local_path)
                with open(local_path, "rb") as f:
                    data = f.read()
                key = key_gem_file(tender_id, file_name)
                storage.upload_file_sync(
                    key, data,
                    content_type=result.get("content_type") or "application/pdf",
                )
                logger.info(f"[GEM] Downloaded file {gem_file_id} from {url} -> {key}")
                result["file_path"] = key
                result["file_name"] = file_name
                result["source_url"] = url
                return result

            last_result = result
            # Try next URL on any failure (auth page, not_document, timeouts, etc.)
            continue

    logger.warning(f"[GEM] All download attempts failed for file {gem_file_id}: {last_result.get('error')}")
    return last_result


def process_gem_file_ids_background(
    tender_id: int,
    analysis_text: str,
    user_id: Optional[int] = None,
):
    """
    Background task: extract GEM file IDs from analysis text,
    create pending records, and attempt downloads.
    """
    from app.core.database import SessionLocal
    from app.core.config import get_settings
    from app.models.tender import TenderDocument

    settings = get_settings()
    db = SessionLocal()

    try:
        file_refs = extract_gem_file_ids(analysis_text)
        if not file_refs:
            return

        create_pending_documents(db, tender_id, file_refs, user_id)

        for ref in file_refs:
            gem_fid = ref["gem_file_id"]
            doc = db.query(TenderDocument).filter(
                TenderDocument.tender_id == tender_id,
                TenderDocument.gem_file_id == gem_fid,
            ).first()

            if not doc or doc.extraction_status not in ("pending", "failed"):
                continue

            doc.extraction_status = "processing"
            db.commit()

            result = download_gem_file(gem_fid, tender_id)

            if result["success"]:
                doc.file_path = result["file_path"]
                doc.file_name = result.get("file_name", doc.file_name)
                doc.file_size = result.get("file_size", 0)
                doc.source_url = result.get("source_url", "")
                doc.extraction_status = "completed"
            elif result.get("error") in ("auth_required", "not_document"):
                doc.extraction_status = "failed_auth"
            else:
                doc.extraction_status = "failed"

            db.commit()

        logger.info(f"[GEM] Background file processing complete for tender {tender_id}")

    except Exception as e:
        logger.error(f"[GEM] Background processing failed for tender {tender_id}: {e}", exc_info=True)
    finally:
        db.close()
