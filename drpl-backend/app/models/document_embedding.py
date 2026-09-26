"""
DRPL Backend - Document Embedding Model
Stores vector embeddings for document chunks (Voyage AI).
Uses JSON-serialized float arrays for SQLite compatibility.
For production PostgreSQL, can be upgraded to pgvector column.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Text, DateTime, Index, JSON
from app.core.database import Base


class DocumentEmbedding(Base):
    __tablename__ = "document_embeddings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(Integer, nullable=True, index=True)       # FK concept to tender_documents.id
    tender_id = Column(Integer, nullable=True, index=True)         # FK concept to tenders.id
    source_type = Column(String(50), nullable=False)               # "tender_document" or "rag_corpus"
    source_name = Column(String(500), nullable=True)               # filename for attribution
    chunk_index = Column(Integer, nullable=False)                  # position within document
    chunk_text = Column(Text, nullable=False)                      # actual text chunk
    chunk_metadata = Column(JSON, default=dict)                    # {page_num, method, section_header}
    embedding = Column(Text, nullable=False)                       # JSON-serialized float array
    embedding_model = Column(String(100), nullable=False)          # e.g. "voyage-3.5"
    embedding_dim = Column(Integer, nullable=False)                # 1024 or 512
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_doc_embedding_doc_chunk", "document_id", "chunk_index"),
    )
