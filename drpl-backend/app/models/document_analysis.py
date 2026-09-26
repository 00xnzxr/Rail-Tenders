"""
DRPL Backend - Document Analysis Models
Stores structured extraction results, critical clause flags, feedback, and analysis summaries
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, JSON, Index, Numeric
from app.core.database import Base


class DocumentExtractionResult(Base):
    __tablename__ = "document_extraction_results"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, index=True)              # FK to tenders.id
    document_id = Column(Integer, nullable=True, index=True)             # FK to tender_documents.id
    document_name = Column(String(500), nullable=True)

    # Extraction type: requirements, eligibility, terms_conditions, technical_specs,
    # financial, experience, compliance
    extraction_type = Column(String(50), nullable=False)

    # Structured extraction: array of {text, category, page_number, document_name, confidence, is_critical}
    items = Column(JSON, default=list)
    raw_text = Column(Text, nullable=True)                               # Full extracted text used
    extraction_model = Column(String(255), nullable=True)                # AI model used
    completeness_score = Column(Float, nullable=True)                    # 0-1 confidence
    token_count = Column(Integer, nullable=True)                         # Tokens used for this extraction

    # v2: compact per-doc summary blob produced by Haiku vision pass.
    # When present, the synthesis step reads this instead of re-reading the PDF.
    # Schema: see _analyze_single_document_native in document_analysis_agent.py.
    summary_json = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class CriticalClauseFlag(Base):
    __tablename__ = "critical_clause_flags"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, index=True)              # FK to tenders.id
    document_id = Column(Integer, nullable=True, index=True)             # FK to tender_documents.id
    document_name = Column(String(500), nullable=True)

    clause_text = Column(Text, nullable=False)                           # The flagged clause
    surrounding_context = Column(Text, nullable=True)                    # Paragraph around the clause
    flag_type = Column(String(50), nullable=False)                       # disqualification, mandatory, non_negotiable, penalty, rejection
    severity = Column(String(20), nullable=False, default="high")        # critical, high, medium
    keyword_matched = Column(String(255), nullable=True)                 # Which trigger keyword was found
    page_number = Column(Integer, nullable=True)
    ai_explanation = Column(Text, nullable=True)                         # AI's explanation of the clause impact

    is_acknowledged = Column(Boolean, default=False)
    acknowledged_by = Column(Integer, nullable=True)                     # FK to users.id
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ExtractionFeedback(Base):
    __tablename__ = "extraction_feedback"

    id = Column(Integer, primary_key=True, index=True)
    extraction_result_id = Column(Integer, nullable=False, index=True)   # FK to document_extraction_results.id
    item_index = Column(Integer, nullable=False)                         # Which item in the JSON array
    feedback_type = Column(String(50), nullable=False)                   # correct, incorrect, missing, duplicate
    corrected_text = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    user_id = Column(Integer, nullable=False)                            # FK to users.id
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class TenderAnalysisSummary(Base):
    __tablename__ = "tender_analysis_summaries"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, unique=True, index=True) # FK to tenders.id

    total_requirements = Column(Integer, default=0)
    total_critical_flags = Column(Integer, default=0)
    completeness_score = Column(Float, nullable=True)                    # Overall confidence 0-1
    documents_analyzed = Column(Integer, default=0)

    # Cross-document analysis results
    cross_document_conflicts = Column(JSON, default=list)                # [{doc_a, doc_b, conflict_description}]
    category_counts = Column(JSON, default=dict)                         # {"eligibility": 5, "technical": 12, ...}
    requirement_summary = Column(Text, nullable=True)                    # AI-generated summary of all requirements

    analysis_status = Column(String(50), default="pending")              # pending, in_progress, completed, failed
    error_message = Column(Text, nullable=True)
    last_analyzed_at = Column(DateTime(timezone=True), nullable=True)

    # v2: cost / version tracking
    total_input_tokens = Column(Integer, nullable=True)
    total_output_tokens = Column(Integer, nullable=True)
    total_cost_usd = Column(Numeric(10, 4), nullable=True)
    analysis_version = Column(String(8), nullable=True)                  # "v1" | "v2"
    per_doc_unreadable_count = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
