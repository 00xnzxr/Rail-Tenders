"""
DRPL LangChain Tool - Document Reader
Reads and extracts text from tender documents (PDF, DOCX, images with OCR).
Wraps the existing document_parser and advanced_document_parser services.
"""

import os
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class DocumentReaderInput(BaseModel):
    """Input schema for the Document Reader tool."""
    file_path: Optional[str] = Field(None, description="Direct file path to read")
    document_id: Optional[int] = Field(None, description="TenderDocument ID to load from database")
    tender_id: Optional[int] = Field(None, description="Tender ID to read all documents for")
    max_pages: int = Field(100, description="Maximum pages to extract")
    ocr_enabled: bool = Field(True, description="Enable OCR for image-based documents")


class DocumentReaderTool(BaseTool):
    """
    Reads and extracts text from uploaded tender documents.
    Supports PDF, DOCX, images (with OCR), and plain text files.
    Returns extracted text with page markers for reference.
    """
    name: str = "document_reader"
    description: str = (
        "Read and extract text from tender documents (PDF, DOCX, images with OCR). "
        "Provide either a file_path, a document_id (TenderDocument ID), or a tender_id "
        "to read all documents for that tender. Returns the extracted text content "
        "with page markers."
    )
    args_schema: Type[BaseModel] = DocumentReaderInput

    # Injected at creation time
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        file_path: Optional[str] = None,
        document_id: Optional[int] = None,
        tender_id: Optional[int] = None,
        max_pages: int = 100,
        ocr_enabled: bool = True,
    ) -> str:
        from app.services.document_parser import extract_text_from_file
        from app.services.storage_service import get_storage_service

        storage = get_storage_service()
        results = []

        # Case 1: Direct file path — tolerate BOTH local paths AND StorageService keys.
        # ReAct agents may guess a filename/key and pass it here; try local first, then
        # fall through to materializing as a storage key (handles R2 object keys).
        if file_path:
            text = None
            if os.path.exists(file_path):
                text = extract_text_from_file(file_path)
                if not text and ocr_enabled:
                    text = self._try_advanced_parse(file_path)
                basename = os.path.basename(file_path)
            else:
                try:
                    with storage.as_local_file(file_path) as local_path:
                        text = extract_text_from_file(local_path)
                        if not text and ocr_enabled:
                            text = self._try_advanced_parse(local_path)
                        basename = os.path.basename(file_path)
                except FileNotFoundError:
                    return f"Error: File not found at {file_path} (checked local and storage)"
            if text:
                results.append(f"=== File: {basename} ===\n{text}")
            else:
                results.append(f"Error: Could not extract text from {file_path}")

        # Case 2: Document ID lookup — file_path on the row is a StorageService key
        elif document_id and self.db:
            from app.models.tender import TenderDocument
            doc = self.db.query(TenderDocument).filter(TenderDocument.id == document_id).first()
            if not doc:
                return f"Error: TenderDocument with ID {document_id} not found"
            if doc.file_path:
                try:
                    with storage.as_local_file(doc.file_path) as local_path:
                        text = extract_text_from_file(local_path)
                        if text:
                            results.append(f"=== Document: {doc.file_name} ===\n{text}")
                        elif ocr_enabled:
                            text = self._try_advanced_parse(local_path)
                            if text:
                                results.append(f"=== Document: {doc.file_name} (OCR) ===\n{text}")
                except FileNotFoundError:
                    pass
            if not results:
                return f"Error: Could not extract text from document {doc.file_name}"

        # Case 3: All documents for a tender — each file_path is a StorageService key
        elif tender_id and self.db:
            from app.models.tender import TenderDocument
            docs = self.db.query(TenderDocument).filter(
                TenderDocument.tender_id == tender_id
            ).all()
            if not docs:
                return f"No documents found for tender ID {tender_id}"
            for doc in docs:
                if not doc.file_path:
                    continue
                try:
                    with storage.as_local_file(doc.file_path) as local_path:
                        text = extract_text_from_file(local_path)
                        if text:
                            results.append(f"=== Document: {doc.file_name} (type: {doc.document_type}) ===\n{text}")
                        elif ocr_enabled:
                            text = self._try_advanced_parse(local_path)
                            if text:
                                results.append(f"=== Document: {doc.file_name} (OCR) ===\n{text}")
                except FileNotFoundError:
                    continue
            if not results:
                return f"Could not extract text from any documents for tender {tender_id}"
        else:
            return "Error: Provide either file_path, document_id, or tender_id"

        combined = "\n\n".join(results)
        # Truncate if extremely large
        if len(combined) > 50000:
            combined = combined[:50000] + "\n\n... [TRUNCATED - document exceeds 50,000 characters]"
        return combined

    def _try_advanced_parse(self, file_path: str) -> Optional[str]:
        """pdfplumber → Claude Vision → Tesseract chain. Returns stitched text with page markers."""
        try:
            from app.services.advanced_document_parser import extract_text_from_pdf_advanced
            result = extract_text_from_pdf_advanced(file_path)
            if not result or not result.get("pages"):
                return None
            return "\n\n".join(
                f"--- Page {p['page_num']} ({p.get('method', '?')}) ---\n{p['text']}"
                for p in result["pages"] if p.get("text")
            )
        except Exception as e:
            logger.warning(f"Advanced parse failed for {file_path}: {e}")
            return None
