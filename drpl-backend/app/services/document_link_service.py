"""
DRPL Backend - Document Link Extraction & Download Service
Extracts hyperlinks from master PDFs (e.g. GEM documents), downloads linked
documents, and saves them on the platform with parent-child tracking.
"""

import os
import re
import hashlib
import logging
from typing import Optional
from urllib.parse import urlparse, unquote

logger = logging.getLogger(__name__)

# --- Constants ---

MAX_LINKS_PER_DOCUMENT = 50
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
MIN_FILE_SIZE = 100  # bytes
DOWNLOAD_TIMEOUT = 60.0  # seconds

DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv",
    ".zip", ".rar", ".txt", ".rtf", ".odt", ".ods",
}

# URL patterns for text-scan extraction
URL_PATTERN = re.compile(
    r'https?://[^\s<>"\')\]\},]+',
    re.IGNORECASE,
)

# Content types that indicate a downloadable document
DOCUMENT_CONTENT_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/zip",
    "application/x-rar-compressed",
    "application/octet-stream",
    "text/csv",
    "text/plain",
    "application/rtf",
}

# Known government/tender portal domains (always allow document downloads from these)
KNOWN_DOMAINS = {
    "gem.gov.in",
    "mkp.gem.gov.in",
    "bidplus.gem.gov.in",
    "eprocure.gov.in",
    "ireps.gov.in",
    "tendertiger.com",
    "bidassist.com",
    "etenders.gov.in",
}


# --- Link Extraction ---

def extract_links_from_pdf(file_path: str) -> list[dict]:
    """
    Extract all hyperlinks from a PDF using two strategies:
    A) PDF annotation extraction (PyPDF2) — clickable hyperlinks
    B) Text URL regex scan (pdfplumber) — URLs printed as plain text

    Returns deduplicated list of {"url": str, "page_num": int, "source": str}.
    """
    links = []
    seen_urls = set()

    # Strategy A: PDF annotations via PyPDF2
    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(file_path)
        for page_num, page in enumerate(reader.pages, start=1):
            annotations = page.get("/Annots")
            if not annotations:
                continue
            annotations = annotations.get_object()
            for annot in annotations:
                try:
                    obj = annot.get_object()
                    if obj.get("/Subtype") == "/Link" and "/A" in obj:
                        uri = obj["/A"].get("/URI")
                        if uri and uri not in seen_urls:
                            seen_urls.add(uri)
                            links.append({
                                "url": uri,
                                "page_num": page_num,
                                "source": "annotation",
                            })
                except Exception:
                    continue
    except ImportError:
        logger.warning("[DRPL] PyPDF2 not installed, skipping annotation extraction")
    except Exception as e:
        logger.warning(f"[DRPL] PDF annotation extraction error for {file_path}: {e}")

    # Strategy B: Text URL regex scan via pdfplumber
    try:
        import pdfplumber

        with pdfplumber.open(file_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text()
                if not text:
                    continue
                for match in URL_PATTERN.finditer(text):
                    url = match.group(0).rstrip(".,;:")
                    if url not in seen_urls:
                        seen_urls.add(url)
                        links.append({
                            "url": url,
                            "page_num": page_num,
                            "source": "text_scan",
                        })
    except Exception as e:
        logger.warning(f"[DRPL] PDF text scan error for {file_path}: {e}")

    logger.info(f"[DRPL] Extracted {len(links)} links from {os.path.basename(file_path)}")
    return links


# --- Link Filtering ---

def _looks_like_document_url(url: str) -> bool:
    """Check if a URL likely points to a downloadable document."""
    parsed = urlparse(url)
    path_lower = parsed.path.lower()
    query_lower = parsed.query.lower()

    # Check file extension in path
    for ext in DOCUMENT_EXTENSIONS:
        if path_lower.endswith(ext):
            return True

    # Check query params that suggest a file download
    download_hints = ["download", "file", "attachment", "getfile", "docid", "fileid"]
    for hint in download_hints:
        if hint in query_lower or hint in path_lower:
            return True

    return False


def _is_known_domain(url: str) -> bool:
    """Check if URL is from a known government/tender portal."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    for domain in KNOWN_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def filter_downloadable_links(links: list[dict]) -> list[dict]:
    """
    Filter extracted links to only those likely pointing to downloadable documents.
    Deduplicates and caps at MAX_LINKS_PER_DOCUMENT.
    """
    filtered = []
    seen = set()

    for link in links:
        url = link["url"]
        parsed = urlparse(url)

        # Must be http/https
        if parsed.scheme not in ("http", "https"):
            continue

        # Skip obvious non-documents
        path_lower = parsed.path.lower()
        skip_extensions = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".css", ".js", ".woff", ".woff2"}
        if any(path_lower.endswith(ext) for ext in skip_extensions):
            continue

        # Normalize for dedup
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            normalized += f"?{parsed.query}"
        if normalized in seen:
            continue

        # Accept if it looks like a document URL or is from a known domain
        if _looks_like_document_url(url) or _is_known_domain(url):
            seen.add(normalized)
            filtered.append(link)

        if len(filtered) >= MAX_LINKS_PER_DOCUMENT:
            break

    logger.info(f"[DRPL] Filtered to {len(filtered)} downloadable links from {len(links)} total")
    return filtered


# --- Document Download ---

def _derive_filename(url: str, content_disposition: Optional[str], content_type: Optional[str]) -> str:
    """Derive a safe filename from response headers or URL."""
    # Try Content-Disposition header first
    if content_disposition:
        # Parse filename from header: attachment; filename="document.pdf"
        match = re.search(r'filename[*]?=["\']?([^"\';\r\n]+)', content_disposition)
        if match:
            name = unquote(match.group(1).strip())
            if name:
                return _sanitize_filename(name)

    # Fallback: extract from URL path
    parsed = urlparse(url)
    path = unquote(parsed.path)
    basename = os.path.basename(path)
    if basename and "." in basename:
        return _sanitize_filename(basename)

    # Last resort: hash-based name with guessed extension
    ext = ".pdf"  # default
    if content_type:
        ext_map = {
            "application/pdf": ".pdf",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "text/csv": ".csv",
            "application/zip": ".zip",
            "text/plain": ".txt",
        }
        ext = ext_map.get(content_type.split(";")[0].strip(), ".pdf")

    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    return f"linked_{url_hash}{ext}"


def _sanitize_filename(name: str) -> str:
    """Remove unsafe characters from filename."""
    # Remove path separators and null bytes
    name = name.replace("/", "_").replace("\\", "_").replace("\0", "")
    # Remove other problematic characters
    name = re.sub(r'[<>:"|?*]', "_", name)
    # Limit length
    if len(name) > 200:
        base, ext = os.path.splitext(name)
        name = base[:200 - len(ext)] + ext
    return name


def download_document(url: str, dest_dir: str, timeout: float = DOWNLOAD_TIMEOUT) -> dict:
    """
    Download a document from a URL and save to dest_dir.
    Uses synchronous httpx (called from BackgroundTasks thread pool).

    Returns: {"success": bool, "file_path": str, "file_name": str,
              "file_size": int, "content_type": str, "error": str|None}
    """
    import httpx

    from app.core.url_safety import BlockedURLError, public_only_hook

    os.makedirs(dest_dir, exist_ok=True)

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=5,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/pdf,application/octet-stream,*/*",
            },
            verify=False,  # Some government sites have certificate issues
            # The URL came out of an uploaded PDF: never follow it (or its
            # redirects) into the metadata endpoint or the private network.
            event_hooks={"request": [public_only_hook]},
        ) as client:
            response = client.get(url)

            # Check HTTP status
            if response.status_code in (401, 403):
                return {"success": False, "file_path": "", "file_name": "", "file_size": 0,
                        "content_type": "", "error": "auth_required"}
            if response.status_code == 404:
                return {"success": False, "file_path": "", "file_name": "", "file_size": 0,
                        "content_type": "", "error": "not_found"}
            if response.status_code >= 400:
                return {"success": False, "file_path": "", "file_name": "", "file_size": 0,
                        "content_type": "", "error": f"http_{response.status_code}"}

            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            content_disposition = response.headers.get("content-disposition")
            content_length = len(response.content)

            # Reject HTML responses (likely an error page or login page)
            if content_type in ("text/html", "application/xhtml+xml"):
                return {"success": False, "file_path": "", "file_name": "", "file_size": 0,
                        "content_type": content_type, "error": "not_document"}

            # Reject oversized files
            if content_length > MAX_DOWNLOAD_SIZE:
                return {"success": False, "file_path": "", "file_name": "", "file_size": content_length,
                        "content_type": content_type, "error": "too_large"}

            # Reject empty/tiny files
            if content_length < MIN_FILE_SIZE:
                return {"success": False, "file_path": "", "file_name": "", "file_size": content_length,
                        "content_type": content_type, "error": "empty"}

            # Derive filename and save
            file_name = _derive_filename(url, content_disposition, content_type)
            file_path = os.path.join(dest_dir, file_name)

            # Avoid overwriting existing files
            if os.path.exists(file_path):
                base, ext = os.path.splitext(file_name)
                url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                file_name = f"{base}_{url_hash}{ext}"
                file_path = os.path.join(dest_dir, file_name)

            with open(file_path, "wb") as f:
                f.write(response.content)

            return {
                "success": True,
                "file_path": file_path,
                "file_name": file_name,
                "file_size": content_length,
                "content_type": content_type,
                "error": None,
            }

    except BlockedURLError as e:
        logger.warning(f"[DRPL] Refused linked document {url}: {e}")
        return {"success": False, "file_path": "", "file_name": "", "file_size": 0,
                "content_type": "", "error": "blocked_address"}
    except httpx.TimeoutException:
        return {"success": False, "file_path": "", "file_name": "", "file_size": 0,
                "content_type": "", "error": "timeout"}
    except Exception as e:
        error_type = "ssl_error" if "ssl" in str(e).lower() or "certificate" in str(e).lower() else "download_error"
        logger.warning(f"[DRPL] Download failed for {url}: {e}")
        return {"success": False, "file_path": "", "file_name": "", "file_size": 0,
                "content_type": "", "error": error_type}


# --- Background Orchestrator ---

def process_document_links_background(
    source_type: str,
    source_id: int,
    file_path: str,
    tender_id: Optional[int],
    session_id: Optional[int],
    user_id: Optional[int],
):
    """
    Background task: extract links from a master PDF, download linked documents,
    and save them on the platform with parent-child tracking.

    Args:
        source_type: "tender_document" or "chat_attachment"
        source_id: ID of the parent TenderDocument or ChatAttachment
        file_path: Path to the master PDF on disk
        tender_id: Associated tender ID (for TenderDocument creation)
        session_id: Associated session ID (for ChatAttachment creation)
        user_id: User who uploaded the master document
    """
    from app.core.database import SessionLocal
    from app.services.storage_service import get_storage_service
    import tempfile

    db = SessionLocal()
    storage = get_storage_service()
    tmp_dir_ctx = None

    try:
        # Update extraction status to processing
        _update_extraction_status(db, source_type, source_id, "processing")

        # Step 1: Extract links (resolve storage key to a local PDF)
        with storage.as_local_file(file_path, suffix=".pdf") as local_pdf:
            links = extract_links_from_pdf(local_pdf)
        if not links:
            logger.info(f"[DRPL] No links found in {os.path.basename(file_path)}")
            _update_extraction_status(db, source_type, source_id, "completed")
            return

        # Step 2: Filter to downloadable links
        downloadable = filter_downloadable_links(links)
        if not downloadable:
            logger.info(f"[DRPL] No downloadable links after filtering for {os.path.basename(file_path)}")
            _update_extraction_status(db, source_type, source_id, "completed")
            return

        # Step 3: Determine storage key prefix for uploaded linked docs
        if source_type == "tender_document" and tender_id:
            key_prefix = f"tenders/{tender_id}/linked/{source_id}"
        elif source_type == "chat_attachment" and session_id:
            key_prefix = f"command_center/{session_id}/linked/{source_id}"
        else:
            logger.warning(f"[DRPL] Cannot determine key prefix for {source_type} (tender={tender_id}, session={session_id})")
            _update_extraction_status(db, source_type, source_id, "failed")
            return

        # Step 4: Download each linked document into a temp dir, then upload to storage
        downloaded = 0
        failed = 0
        skipped = 0
        tmp_dir_ctx = tempfile.TemporaryDirectory(prefix="linked_dl_")
        tmp_dir = tmp_dir_ctx.__enter__()

        for link in downloadable:
            url = link["url"]
            result = download_document(url, tmp_dir)

            if result["success"]:
                # Stage the downloaded file into storage and rewrite the file_path
                # in the result so downstream code sees a storage key, not a local path.
                with open(result["file_path"], "rb") as _fh:
                    _data = _fh.read()
                _storage_key = f"{key_prefix}/{result['file_name']}"
                storage.upload_file_sync(
                    _storage_key, _data,
                    content_type=result.get("content_type") or "application/pdf",
                )
                result["file_path"] = _storage_key

                # Create DB record for downloaded document
                child_id = _create_child_record(
                    db=db,
                    source_type=source_type,
                    parent_id=source_id,
                    tender_id=tender_id,
                    session_id=session_id,
                    user_id=user_id,
                    url=url,
                    download_result=result,
                )

                # Trigger embedding for tender documents
                if source_type == "tender_document" and tender_id and child_id:
                    _embed_child_document(child_id, result["file_path"], tender_id, result["file_name"])

                # Dual-write: also create TenderDocument if this is a chat attachment on a tender-linked session
                if source_type == "chat_attachment" and tender_id and child_id:
                    td_id = _create_tender_document_from_attachment(
                        db=db,
                        tender_id=tender_id,
                        user_id=user_id,
                        url=url,
                        download_result=result,
                    )
                    if td_id:
                        _embed_child_document(td_id, result["file_path"], tender_id, result["file_name"])

                downloaded += 1
                logger.info(f"[DRPL] Downloaded: {result['file_name']} from {url}")
            else:
                if result["error"] in ("auth_required", "not_document"):
                    skipped += 1
                else:
                    failed += 1
                logger.info(f"[DRPL] Skip/fail {url}: {result['error']}")

        # Step 5: Update status
        _update_extraction_status(db, source_type, source_id, "completed")
        logger.info(
            f"[DRPL] Link extraction complete for {os.path.basename(file_path)}: "
            f"{len(downloadable)} links, {downloaded} downloaded, {failed} failed, {skipped} skipped"
        )

    except Exception as e:
        logger.error(f"[DRPL] Link extraction background task failed: {e}", exc_info=True)
        try:
            _update_extraction_status(db, source_type, source_id, "failed")
        except Exception:
            pass
    finally:
        if tmp_dir_ctx is not None:
            try:
                tmp_dir_ctx.__exit__(None, None, None)
            except Exception:
                pass
        db.close()


# --- Internal Helpers ---

def _update_extraction_status(db, source_type: str, source_id: int, status: str):
    """Update the extraction_status column on the source document."""
    from sqlalchemy import text

    if source_type == "tender_document":
        table = "tender_documents"
    else:
        table = "chat_attachments"

    db.execute(
        text(f"UPDATE {table} SET extraction_status = :status WHERE id = :id"),
        {"status": status, "id": source_id},
    )
    db.commit()


def _create_child_record(
    db, source_type: str, parent_id: int,
    tender_id: Optional[int], session_id: Optional[int],
    user_id: Optional[int], url: str, download_result: dict,
) -> Optional[int]:
    """Create a TenderDocument or ChatAttachment record for a downloaded child document."""
    from app.models.tender import TenderDocument
    from app.models.chat_attachment import ChatAttachment

    try:
        if source_type == "tender_document":
            child = TenderDocument(
                tender_id=tender_id or 0,
                file_name=download_result["file_name"],
                file_path=download_result["file_path"],
                file_size=download_result["file_size"],
                mime_type=download_result["content_type"] or "application/pdf",
                document_type="linked_document",
                uploaded_by=user_id,
                parent_document_id=parent_id,
                source_url=url,
                extraction_status="skipped",  # Don't recurse into child docs
            )
            db.add(child)
            db.flush()
            child_id = child.id
        else:
            child = ChatAttachment(
                session_id=session_id or 0,
                file_name=download_result["file_name"],
                file_path=download_result["file_path"],
                file_type=download_result["content_type"] or "application/pdf",
                file_size=download_result["file_size"],
                uploaded_by=user_id or 0,
                parent_attachment_id=parent_id,
                source_url=url,
                extraction_status="skipped",
            )
            db.add(child)
            db.flush()
            child_id = child.id

        db.commit()
        return child_id
    except Exception as e:
        logger.error(f"[DRPL] Failed to create child record for {url}: {e}")
        db.rollback()
        return None


def _create_tender_document_from_attachment(
    db, tender_id: int, user_id: Optional[int],
    url: str, download_result: dict,
) -> Optional[int]:
    """Dual-write: create a TenderDocument when a chat attachment is linked to a tender."""
    from app.models.tender import TenderDocument

    try:
        td = TenderDocument(
            tender_id=tender_id,
            file_name=download_result["file_name"],
            file_path=download_result["file_path"],
            file_size=download_result["file_size"],
            mime_type=download_result["content_type"] or "application/pdf",
            document_type="linked_document",
            uploaded_by=user_id,
            source_url=url,
            extraction_status="skipped",
        )
        db.add(td)
        db.commit()
        db.refresh(td)
        return td.id
    except Exception as e:
        logger.error(f"[DRPL] Dual-write TenderDocument failed for {url}: {e}")
        db.rollback()
        return None


def _embed_child_document(document_id: int, file_path: str, tender_id: int, file_name: str):
    """Trigger text extraction + embedding for a downloaded child document.

    `file_path` is a storage key (R2 object key or path under upload_dir).
    """
    try:
        from app.services.advanced_document_parser import extract_text_from_file_advanced
        from app.services.embedding_service import embed_document_pages
        from app.services.storage_service import get_storage_service
        from app.core.database import SessionLocal

        db = SessionLocal()
        try:
            with get_storage_service().as_local_file(file_path) as local_path:
                result = extract_text_from_file_advanced(local_path)
            if result and result.get("pages"):
                count = embed_document_pages(
                    db=db,
                    document_id=document_id,
                    pages=result["pages"],
                    tender_id=tender_id,
                    source_name=file_name,
                )
                logger.info(f"[DRPL] Embedded linked doc {file_name}: {count} chunks")
            else:
                logger.info(f"[DRPL] No text extracted from linked doc {file_name}")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[DRPL] Embedding failed for linked doc {file_name}: {e}")
