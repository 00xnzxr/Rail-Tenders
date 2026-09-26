"""
DRPL Backend - Training Dataset Models
Named, tagged collections of uploaded knowledge files (MD, JSON, JSONL)
that can be assigned to agents for context injection at execution time.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, JSON, Index, UniqueConstraint
from app.core.database import Base


class TrainingDataset(Base):
    """A named collection of training knowledge files."""
    __tablename__ = "training_datasets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    tags = Column(JSON, default=list)                                    # ["railway", "power-car", "electrical"]
    status = Column(String(20), default="active")                        # active, archived
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class TrainingDatasetFile(Base):
    """An individual file within a training dataset."""
    __tablename__ = "training_dataset_files"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, nullable=False, index=True)             # FK to training_datasets.id
    file_name = Column(String(500), nullable=False)
    file_type = Column(String(20), nullable=False)                       # md, json, jsonl, txt
    file_size = Column(Integer, default=0)
    raw_content = Column(Text, nullable=False)                           # Original file content
    parsed_content = Column(Text, nullable=True)                         # Processed version for injection
    extraction_status = Column(String(50), default="pending")            # pending, completed, failed
    extraction_error = Column(Text, nullable=True)
    uploaded_by = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_training_files_dataset_name", "dataset_id", "file_name"),
    )


class AgentTrainingDataset(Base):
    """Many-to-many link between CustomAgent and TrainingDataset."""
    __tablename__ = "agent_training_datasets"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, nullable=False, index=True)               # FK to custom_agents.id
    dataset_id = Column(Integer, nullable=False, index=True)             # FK to training_datasets.id
    priority = Column(Integer, default=0)                                # Lower = higher priority
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("agent_id", "dataset_id", name="uq_agent_dataset"),
    )
