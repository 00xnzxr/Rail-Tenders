"""
DRPL Backend - Workflow Tool Handlers
Service function wrappers for tool nodes in the workflow execution engine.
Each handler wraps existing platform services for use as workflow tool nodes.
"""

import os
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_gem_download(engine: Any, input_data: dict, config: dict) -> dict:
    """Download GEM-referenced documents from analysis text."""
    from app.services.gem_file_service import extract_gem_file_ids, download_gem_file
    from app.core.config import get_settings

    analysis_text = input_data.get("analysis_text", "")
    tender_id = input_data.get("tender_id") or engine.state.get("__tender_id")

    if not analysis_text:
        return {"documents": [], "count": 0, "message": "No analysis text provided"}

    file_refs = extract_gem_file_ids(analysis_text)
    if not file_refs:
        return {"documents": [], "count": 0, "message": "No GEM file IDs found"}

    settings = get_settings()
    dest_dir = os.path.join(
        getattr(settings, "upload_dir", "uploads"),
        "tender_docs", str(tender_id or "unknown"), "gem_files",
    )
    os.makedirs(dest_dir, exist_ok=True)

    results = []
    for ref in file_refs:
        try:
            result = await asyncio.to_thread(download_gem_file, ref["gem_file_id"], dest_dir)
            results.append({
                "name": ref.get("name", f"file_{ref['gem_file_id']}"),
                "gem_file_id": ref["gem_file_id"],
                "success": result.get("success", False),
                "file_path": result.get("file_path", ""),
                "error": result.get("error"),
            })
        except Exception as e:
            logger.warning(f"GEM download failed for {ref['gem_file_id']}: {e}")
            results.append({
                "name": ref.get("name", f"file_{ref['gem_file_id']}"),
                "gem_file_id": ref["gem_file_id"],
                "success": False,
                "file_path": "",
                "error": str(e),
            })

    successful = [r for r in results if r["success"]]
    return {
        "documents": results,
        "count": len(results),
        "successful": len(successful),
        "message": f"Downloaded {len(successful)} of {len(results)} GEM files",
    }


async def handle_pdf_link_extract(engine: Any, input_data: dict, config: dict) -> dict:
    """Extract hyperlinks from a PDF and download linked documents."""
    from app.services.document_link_service import (
        extract_links_from_pdf, filter_downloadable_links, download_document,
    )
    from app.core.config import get_settings

    file_path = input_data.get("file_path", "")
    tender_id = input_data.get("tender_id") or engine.state.get("__tender_id")

    if not file_path or not os.path.exists(file_path):
        return {"documents": [], "count": 0, "message": f"File not found: {file_path}"}

    # Extract links
    try:
        all_links = await asyncio.to_thread(extract_links_from_pdf, file_path)
    except Exception as e:
        logger.warning(f"Link extraction failed for {file_path}: {e}")
        return {"documents": [], "count": 0, "message": f"Link extraction failed: {e}"}

    downloadable = filter_downloadable_links(all_links)
    if not downloadable:
        return {"documents": [], "count": 0, "message": "No downloadable links found"}

    # Download linked docs
    settings = get_settings()
    dest_dir = os.path.join(
        getattr(settings, "upload_dir", "uploads"),
        "tender_docs", str(tender_id or "unknown"), "linked_docs",
    )
    os.makedirs(dest_dir, exist_ok=True)

    results = []
    for link in downloadable[:20]:  # Cap at 20
        try:
            result = await asyncio.to_thread(download_document, link["url"], dest_dir)
            results.append({
                "name": result.get("file_name", os.path.basename(link["url"])),
                "url": link["url"],
                "page_num": link.get("page_num"),
                "success": result.get("success", False),
                "file_path": result.get("file_path", ""),
                "error": result.get("error"),
            })
        except Exception as e:
            results.append({
                "name": os.path.basename(link["url"]),
                "url": link["url"],
                "success": False,
                "file_path": "",
                "error": str(e),
            })

    successful = [r for r in results if r["success"]]
    return {
        "documents": results,
        "count": len(results),
        "successful": len(successful),
        "total_links_found": len(all_links),
        "downloadable_links": len(downloadable),
        "message": f"Downloaded {len(successful)} of {len(results)} linked documents",
    }


async def handle_text_extract(engine: Any, input_data: dict, config: dict) -> dict:
    """Extract text content from a document file (PDF, DOCX, images)."""
    from app.services.advanced_document_parser import extract_text_from_file_advanced, get_full_text

    file_path = input_data.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        return {"text": "", "pages": 0, "error": f"File not found: {file_path}"}

    try:
        result = await asyncio.to_thread(extract_text_from_file_advanced, file_path)
        full_text = get_full_text(result)
        return {
            "text": full_text,
            "pages": result.get("total_pages", len(result.get("pages", []))),
            "file_path": file_path,
            "file_name": os.path.basename(file_path),
            "char_count": len(full_text),
        }
    except Exception as e:
        logger.warning(f"Text extraction failed for {file_path}: {e}")
        return {"text": "", "pages": 0, "error": str(e)}


# ─── Tool Handler Registry ────────────────────────────────────────────

TOOL_HANDLER_REGISTRY: dict[str, Any] = {
    "gem_download": handle_gem_download,
    "pdf_link_extract": handle_pdf_link_extract,
    "text_extract": handle_text_extract,
}
