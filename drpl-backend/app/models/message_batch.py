"""
DRPL Backend - Message Batch Model
Tracks Claude Message Batches API batch jobs for async processing.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, JSON
from app.core.database import Base


class MessageBatch(Base):
    __tablename__ = "message_batches"

    id = Column(Integer, primary_key=True, index=True)
    # Anthropic batch ID (e.g., msgbatch_01HkcTjaV5...)
    batch_id = Column(String(100), unique=True, nullable=False, index=True)
    # Type of batch job: tender_analysis, eligibility, classification, custom
    batch_type = Column(String(50), nullable=False, default="tender_analysis")
    # Processing status: created, in_progress, ended, canceling, canceled, expired, failed
    status = Column(String(30), nullable=False, default="created", index=True)
    # Model used for the batch
    model = Column(String(255), nullable=True)
    # Request counts
    total_requests = Column(Integer, default=0)
    succeeded_count = Column(Integer, default=0)
    errored_count = Column(Integer, default=0)
    expired_count = Column(Integer, default=0)
    canceled_count = Column(Integer, default=0)
    processing_count = Column(Integer, default=0)
    # Cost tracking (50% discount applied)
    total_input_tokens = Column(Integer, default=0)
    total_output_tokens = Column(Integer, default=0)
    estimated_cost = Column(Float, default=0.0)
    # Results
    results_url = Column(Text, nullable=True)
    results_processed = Column(Boolean, default=False)
    # Metadata: stores tender_ids, agent mapping, etc.
    metadata_json = Column(Text, nullable=True)  # JSON string
    # Error tracking
    error_message = Column(Text, nullable=True)
    # User who created the batch
    created_by = Column(Integer, nullable=True)
    # Timestamps
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)


class MessageBatchItem(Base):
    """Individual request items within a batch, tracking per-request results."""
    __tablename__ = "message_batch_items"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(String(100), nullable=False, index=True)
    # custom_id used in the Anthropic batch request
    custom_id = Column(String(255), nullable=False)
    # What this request is for: e.g., "tender_123_classifier", "tender_123_relevance"
    item_type = Column(String(50), nullable=False)  # classifier, relevance, risk, summary, eligibility
    # Reference IDs
    tender_id = Column(Integer, nullable=True, index=True)
    # Result status: pending, succeeded, errored, canceled, expired
    result_status = Column(String(30), default="pending")
    # Result data (the AI response text)
    result_text = Column(Text, nullable=True)
    # Token usage for this item
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    # Error info
    error_type = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
