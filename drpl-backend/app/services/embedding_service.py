"""
DRPL Backend - Embedding Service
Voyage AI document embedding, chunking, and semantic search.
Provides vector-based RAG to replace/augment keyword-based retrieval.
"""

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.document_embedding import DocumentEmbedding
from app.services.settings_service import get_effective_setting

logger = logging.getLogger(__name__)


def _get_voyage_client(db: Session):
    """Get a Voyage AI client using the configured API key. Returns None if not configured."""
    api_key = get_effective_setting(db, "voyage_api_key", "")
    if not api_key:
        return None
    try:
        import voyageai
        return voyageai.Client(api_key=api_key)
    except ImportError:
        logger.warning("voyageai package not installed. Run: pip install voyageai")
        return None


def chunk_text(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    metadata: Optional[dict] = None,
) -> list[dict]:
    """
    Split text into overlapping chunks using LangChain's RecursiveCharacterTextSplitter.

    Args:
        text: The text to chunk.
        chunk_size: Approximate token count per chunk (multiplied by 4 for char estimate).
        chunk_overlap: Token overlap between chunks.
        metadata: Optional metadata dict to attach to each chunk.

    Returns:
        List of dicts with chunk_index, text, and metadata.
    """
    if not text or not text.strip():
        return []

    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size * 4,       # ~4 chars per token
            chunk_overlap=chunk_overlap * 4,
            separators=["\n\n", "\n", ". ", " ", ""],
            length_function=len,
        )
        chunks = splitter.split_text(text)
    except ImportError:
        # Fallback: simple word-based chunking
        logger.warning("langchain-text-splitters not installed, using word-based chunking")
        words = text.split()
        word_chunk = chunk_size * 3  # approximate words per chunk
        word_overlap = chunk_overlap * 3
        chunks = []
        for i in range(0, len(words), max(1, word_chunk - word_overlap)):
            chunk = " ".join(words[i:i + word_chunk])
            if chunk.strip():
                chunks.append(chunk)

    return [
        {
            "chunk_index": i,
            "text": chunk,
            "metadata": metadata or {},
        }
        for i, chunk in enumerate(chunks)
    ]


def embed_texts(
    db: Session,
    texts: list[str],
    input_type: str = "document",
) -> list[list[float]]:
    """
    Embed a list of texts using Voyage AI.

    Args:
        db: Database session for config lookup.
        texts: List of text strings to embed.
        input_type: "document" for stored content, "query" for search queries.

    Returns:
        List of embedding vectors (list of floats).

    Raises:
        ValueError: If Voyage API key is not configured.
    """
    client = _get_voyage_client(db)
    if not client:
        raise ValueError("Voyage AI API key not configured. Set it in Platform Settings or .env")

    model = get_effective_setting(db, "voyage_embedding_model", "voyage-3.5") or "voyage-3.5"

    all_embeddings = []
    batch_size = 128  # Voyage API max batch size

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            result = client.embed(
                batch,
                model=model,
                input_type=input_type,
                truncation=True,
            )
            all_embeddings.extend(result.embeddings)
        except Exception as e:
            logger.error(f"Voyage AI embedding failed for batch {i // batch_size}: {e}")
            raise

    return all_embeddings


def embed_document(
    db: Session,
    document_id: int,
    text: str,
    tender_id: Optional[int] = None,
    source_type: str = "tender_document",
    source_name: Optional[str] = None,
) -> int:
    """
    Embed a full document: chunk it, embed all chunks, store in DB.

    Args:
        db: Database session.
        document_id: ID of the source document.
        text: Full document text.
        tender_id: Optional tender association.
        source_type: "tender_document" or "rag_corpus".
        source_name: Filename for attribution.

    Returns:
        Number of chunks embedded and stored.
    """
    if not text or not text.strip():
        return 0

    # Read chunking settings
    chunk_size = int(get_effective_setting(db, "embedding_chunk_size", 512) or 512)
    chunk_overlap = int(get_effective_setting(db, "embedding_chunk_overlap", 64) or 64)

    # Delete existing embeddings for this document (supports re-embedding)
    delete_document_embeddings(db, document_id)

    # Chunk the text
    chunks = chunk_text(text, chunk_size, chunk_overlap)
    if not chunks:
        return 0

    # Embed all chunks
    texts = [c["text"] for c in chunks]
    embeddings = embed_texts(db, texts, input_type="document")

    # Get model info
    model = get_effective_setting(db, "voyage_embedding_model", "voyage-3.5") or "voyage-3.5"
    dim = len(embeddings[0]) if embeddings else 1024

    # Store in database
    for chunk, embedding in zip(chunks, embeddings):
        row = DocumentEmbedding(
            document_id=document_id,
            tender_id=tender_id,
            source_type=source_type,
            source_name=source_name,
            chunk_index=chunk["chunk_index"],
            chunk_text=chunk["text"],
            chunk_metadata=chunk.get("metadata", {}),
            embedding=json.dumps(embedding),
            embedding_model=model,
            embedding_dim=dim,
        )
        db.add(row)

    db.commit()
    logger.info(f"Embedded document {document_id}: {len(chunks)} chunks, model={model}, dim={dim}")
    return len(chunks)


def embed_document_pages(
    db: Session,
    document_id: int,
    pages: list[dict],
    tender_id: Optional[int] = None,
    source_name: Optional[str] = None,
) -> int:
    """
    Embed a document from its page-structured output (from advanced_document_parser).

    Args:
        db: Database session.
        document_id: ID of the source document.
        pages: List of dicts with page_num, text, method (from advanced_document_parser).
        tender_id: Optional tender association.
        source_name: Filename for attribution.

    Returns:
        Number of chunks embedded and stored.
    """
    if not pages:
        return 0

    chunk_size = int(get_effective_setting(db, "embedding_chunk_size", 512) or 512)
    chunk_overlap = int(get_effective_setting(db, "embedding_chunk_overlap", 64) or 64)

    # Delete existing embeddings for this document
    delete_document_embeddings(db, document_id)

    # Chunk each page, preserving page_num in metadata
    all_chunks = []
    for page in pages:
        page_text = page.get("text", "")
        if not page_text or not page_text.strip():
            continue
        page_metadata = {
            "page_num": page.get("page_num"),
            "method": page.get("method", "text"),
        }
        page_chunks = chunk_text(page_text, chunk_size, chunk_overlap, metadata=page_metadata)
        # Adjust chunk indices to be globally sequential
        for c in page_chunks:
            c["chunk_index"] = len(all_chunks)
            all_chunks.append(c)

    if not all_chunks:
        return 0

    # Embed all chunks in bulk
    texts = [c["text"] for c in all_chunks]
    embeddings = embed_texts(db, texts, input_type="document")

    model = get_effective_setting(db, "voyage_embedding_model", "voyage-3.5") or "voyage-3.5"
    dim = len(embeddings[0]) if embeddings else 1024

    for chunk, embedding in zip(all_chunks, embeddings):
        row = DocumentEmbedding(
            document_id=document_id,
            tender_id=tender_id,
            source_type="tender_document",
            source_name=source_name,
            chunk_index=chunk["chunk_index"],
            chunk_text=chunk["text"],
            chunk_metadata=chunk.get("metadata", {}),
            embedding=json.dumps(embedding),
            embedding_model=model,
            embedding_dim=dim,
        )
        db.add(row)

    db.commit()
    logger.info(f"Embedded document {document_id} pages: {len(all_chunks)} chunks from {len(pages)} pages")
    return len(all_chunks)


def semantic_search(
    db: Session,
    query: str,
    top_k: int = 5,
    tender_id: Optional[int] = None,
    source_type: Optional[str] = None,
) -> list[dict]:
    """
    Semantic similarity search across embedded documents.

    Args:
        db: Database session.
        query: Search query text.
        top_k: Number of top results to return.
        tender_id: Filter by tender ID (optional).
        source_type: Filter by source type (optional).

    Returns:
        List of dicts with chunk_text, score, document_id, source_name, metadata.
    """
    import numpy as np

    # Embed the query
    query_embedding = embed_texts(db, [query], input_type="query")[0]
    query_vec = np.array(query_embedding, dtype=np.float32)

    # Build query with optional filters
    q = db.query(DocumentEmbedding)
    if tender_id is not None:
        q = q.filter(DocumentEmbedding.tender_id == tender_id)
    if source_type is not None:
        q = q.filter(DocumentEmbedding.source_type == source_type)

    rows = q.all()
    if not rows:
        return []

    # Compute cosine similarity (Voyage embeddings are pre-normalized, so dot product = cosine)
    scored = []
    for row in rows:
        try:
            doc_vec = np.array(json.loads(row.embedding), dtype=np.float32)
            score = float(np.dot(query_vec, doc_vec))
            scored.append({
                "chunk_text": row.chunk_text,
                "score": round(score, 4),
                "document_id": row.document_id,
                "tender_id": row.tender_id,
                "source_name": row.source_name,
                "chunk_index": row.chunk_index,
                "metadata": row.chunk_metadata or {},
            })
        except Exception as e:
            logger.warning(f"Failed to deserialize embedding for row {row.id}: {e}")

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def delete_document_embeddings(db: Session, document_id: int) -> int:
    """Delete all embeddings for a given document ID."""
    count = db.query(DocumentEmbedding).filter(
        DocumentEmbedding.document_id == document_id
    ).delete()
    db.flush()
    return count


def get_embedding_stats(db: Session) -> dict:
    """Get aggregate embedding statistics."""
    from sqlalchemy import func

    total_chunks = db.query(func.count(DocumentEmbedding.id)).scalar() or 0
    total_documents = db.query(
        func.count(func.distinct(DocumentEmbedding.document_id))
    ).scalar() or 0

    model_row = db.query(DocumentEmbedding.embedding_model).limit(1).first()
    current_model = model_row[0] if model_row else None

    return {
        "total_chunks": total_chunks,
        "total_documents": total_documents,
        "embedding_model": current_model,
    }
