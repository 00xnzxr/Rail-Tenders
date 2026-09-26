"""
DRPL Backend - PDF page-level Claude vision cache.

Caches the text extracted for a single PDF page via Claude's native
document-vision API so we don't re-pay for the same scanned page on
repeated reads. Keyed by (source_key, page_index, page_sha1) — source_key
is a stable fingerprint of the source file, page_sha1 is the SHA-1 of
that single page's raw PDF bytes. If the page bytes change, the sha1
changes and we re-extract.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, UniqueConstraint
from app.core.database import Base


class DocumentPageVisionCache(Base):
    __tablename__ = "document_page_vision_cache"

    id = Column(Integer, primary_key=True, index=True)
    source_key = Column(String(64), nullable=False, index=True)    # SHA-1 of source file fingerprint
    page_index = Column(Integer, nullable=False)                    # 0-based
    page_sha1 = Column(String(40), nullable=False)                  # SHA-1 of single-page PDF bytes
    text = Column(Text, nullable=False)
    method = Column(String(32), nullable=False)                     # claude_vision | ocr
    model = Column(String(100), nullable=True)                      # model ID used for vision
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cost_usd = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("source_key", "page_index", "page_sha1", name="uq_page_vision_cache_key"),
    )
