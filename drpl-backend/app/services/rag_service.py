"""
DRPL Backend - RAG Service
Simple retrieval-augmented generation using text chunking and keyword matching.
Stores past proposals/company profile for context injection.
"""

import logging
import os
import re
from typing import Optional
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.document_parser import extract_text_from_file

logger = logging.getLogger(__name__)

settings = get_settings()
RAG_DIR = os.path.join(settings.upload_dir, "rag_corpus")


def _get_chunks(text: str, chunk_size: int = 500, overlap: int = 100) -> list[str]:
    """Split text into overlapping chunks."""
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
    return chunks


def _score_chunk(chunk: str, query_words: set[str]) -> float:
    """Simple keyword-based relevance scoring."""
    chunk_lower = chunk.lower()
    matches = sum(1 for w in query_words if w in chunk_lower)
    return matches / max(len(query_words), 1)


def retrieve_context(query: str, top_k: int = 5) -> str:
    """Retrieve relevant context from the RAG corpus."""
    os.makedirs(RAG_DIR, exist_ok=True)

    all_chunks = []

    for filename in os.listdir(RAG_DIR):
        filepath = os.path.join(RAG_DIR, filename)
        if os.path.isfile(filepath):
            text = extract_text_from_file(filepath)
            if text:
                chunks = _get_chunks(text)
                for chunk in chunks:
                    all_chunks.append((chunk, filename))

    if not all_chunks:
        return ""

    # Score and rank chunks
    query_words = set(re.findall(r'\w+', query.lower()))
    scored = [(chunk, fname, _score_chunk(chunk, query_words))
              for chunk, fname in all_chunks]
    scored.sort(key=lambda x: x[2], reverse=True)

    # Return top-k
    results = []
    for chunk, fname, score in scored[:top_k]:
        if score > 0:
            results.append(f"[Source: {fname}]\n{chunk}")

    return "\n\n---\n\n".join(results)


def list_corpus_documents() -> list[dict]:
    """List all documents in the RAG corpus."""
    os.makedirs(RAG_DIR, exist_ok=True)
    docs = []
    for filename in os.listdir(RAG_DIR):
        filepath = os.path.join(RAG_DIR, filename)
        if os.path.isfile(filepath):
            stat = os.stat(filepath)
            docs.append({
                "filename": filename,
                "size": stat.st_size,
                "uploaded_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return docs


def add_corpus_document(filename: str, file_data: bytes, db: Session = None) -> dict:
    """Add a document to the RAG corpus and optionally embed it."""
    os.makedirs(RAG_DIR, exist_ok=True)
    filepath = os.path.join(RAG_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(file_data)

    # Embed the document if a DB session is available
    if db:
        try:
            text = extract_text_from_file(filepath)
            if text:
                from app.services.embedding_service import embed_document
                # Use negative hash as pseudo document_id for corpus files
                doc_id = abs(hash(filename)) % (10**9)
                embed_document(
                    db=db,
                    document_id=doc_id,
                    text=text,
                    source_type="rag_corpus",
                    source_name=filename,
                )
                logger.info(f"Embedded RAG corpus document: {filename}")
        except Exception as e:
            logger.warning(f"Failed to embed RAG corpus document '{filename}': {e}")

    return {"filename": filename, "size": len(file_data)}


def delete_corpus_document(filename: str) -> bool:
    """Delete a document from the RAG corpus."""
    filepath = os.path.join(RAG_DIR, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
        return True
    return False


def retrieve_context_hybrid(query: str, db: Session = None, top_k: int = 5) -> str:
    """
    Retrieve context using semantic search (Voyage AI), falling back to keyword search.

    This is the recommended entry point for RAG retrieval — it automatically
    uses vector search when available and degrades gracefully to keyword matching.

    Args:
        query: The search query.
        db: Optional DB session (required for semantic search).
        top_k: Number of results to return.

    Returns:
        Formatted context string with source attributions.
    """
    if db:
        try:
            from app.services.embedding_service import semantic_search
            results = semantic_search(db, query, top_k=top_k, source_type="rag_corpus")
            if results:
                formatted = []
                for r in results:
                    source = r.get("source_name", "unknown")
                    score = r.get("score", 0)
                    text = r.get("chunk_text", "")
                    formatted.append(f"[Source: {source}] [Relevance: {score}]\n{text}")
                return "\n\n---\n\n".join(formatted)
        except Exception as e:
            logger.info(f"Semantic search unavailable, using keyword fallback: {e}")

    # Fall back to keyword-based retrieval
    return retrieve_context(query, top_k=top_k)
