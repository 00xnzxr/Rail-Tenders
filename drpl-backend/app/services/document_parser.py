"""
DRPL Backend - Document Parser Service
Extracts text content from PDF and other document formats
"""

import os
from typing import Optional


def extract_text_from_pdf(file_path: str) -> Optional[str]:
    """Extract text content from a PDF file using pdfplumber."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n\n".join(text_parts) if text_parts else None
    except Exception as e:
        print(f"[DRPL] PDF extraction error for {file_path}: {e}")
        return None


def extract_text_from_file(file_path: str) -> Optional[str]:
    """Extract text from a file based on its extension."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    elif ext in (".txt", ".text", ".md"):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return None
    else:
        return None
