"""
DRPL Backend - Proposal Template Model
System-provided or user-uploaded proposal templates with AI-parsed structure.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, JSON

from app.core.database import Base


class ProposalTemplate(Base):
    """Proposal templates — system-provided or user-uploaded with AI-parsed structure."""
    __tablename__ = "proposal_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    template_type = Column(String(100), nullable=False)              # works_single_packet, service_two_packet, supply, amc, custom
    source = Column(String(100), default="system")                   # system, uploaded, reverse_engineered
    structure_json = Column(JSON, default=dict)                      # Parsed template structure
    original_file_path = Column(Text, nullable=True)                 # For uploaded templates
    original_file_name = Column(String(500), nullable=True)
    railway_zone = Column(String(100), nullable=True)
    contract_type = Column(String(100), nullable=True)               # Works, Service, Supply
    bidding_system = Column(String(50), nullable=True)               # single_packet, two_packet
    is_active = Column(Boolean, default=True)
    output_format = Column(String(10), default="docx")                 # docx, pdf

    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
