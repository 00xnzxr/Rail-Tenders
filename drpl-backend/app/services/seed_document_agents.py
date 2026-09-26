"""
DRPL Backend - Seed Document Agents
Creates 4 specialized agents for workspace document generation.
"""

import json
import logging
from sqlalchemy.orm import Session
from app.models.agent_builder import CustomAgent

logger = logging.getLogger(__name__)

DOCUMENT_AGENTS = [
    {
        "agent_key": "doc-letter-writer",
        "display_name": "Letter & Declaration Writer",
        "description": "Specializes in formal business letters, undertakings, declarations, and authorization documents for Indian government tender submissions.",
        "agent_type": "chain_of_thought",
        "system_prompt": (
            "You are an expert document writer specializing in formal Indian government tender submission documents. "
            "You write letters, undertakings, declarations, and authorization documents with:\n"
            "- Proper formal salutations and subject lines\n"
            "- Reference numbers and date formatting (DD/MM/YYYY)\n"
            "- Compliance with railway/government terminology\n"
            "- Appropriate signatory blocks with designation\n"
            "- Clear, precise legal language suitable for binding declarations\n"
            "Output clean HTML with proper formatting. Use tables where required."
        ),
        "model": "claude-haiku-4-5",
        "temperature": 0.4,
        "max_tokens": 4096,
        "document_categories": ["letter", "declaration"],
        "category": "document",
        "tags": ["workspace", "letter", "declaration", "undertaking"],
    },
    {
        "agent_key": "doc-technical-writer",
        "display_name": "Technical Document Writer",
        "description": "Produces technical proposals, methodology statements, work plans, and engineering certificates for tender submissions.",
        "agent_type": "chain_of_thought",
        "system_prompt": (
            "You are a technical document writer specializing in Indian infrastructure and railway tender submissions. "
            "You produce technical proposals, methodology statements, work plans, and certificates with:\n"
            "- Engineering precision and IS standards references\n"
            "- Structured methodology sections (approach, resources, timeline)\n"
            "- Technical specifications matching tender requirements\n"
            "- Quality assurance and safety plan references\n"
            "- Professional formatting with numbered sections and sub-sections\n"
            "Output clean HTML with proper headings, tables, and structured content."
        ),
        "model": "gpt-5.6-terra",
        "provider": "openai",
        "temperature": 0.5,
        "max_tokens": 8192,
        "document_categories": ["proposal", "certificate"],
        "category": "document",
        "tags": ["workspace", "proposal", "technical", "methodology"],
    },
    {
        "agent_key": "doc-costing-analyst",
        "display_name": "BOQ & Costing Analyst",
        "description": "Generates Bill of Quantities, cost breakdowns, rate analysis, and financial statements for tender submissions.",
        "agent_type": "chain_of_thought",
        "system_prompt": (
            "You are a costing and financial document specialist for Indian government tender submissions. "
            "You produce BOQs, cost breakdowns, rate analysis, and financial statements with:\n"
            "- Detailed line items with quantities, units, rates, and amounts\n"
            "- DSR (Delhi Schedule of Rates) and market rate awareness\n"
            "- Proper Indian number formatting (lakhs, crores)\n"
            "- GST, taxes, and overhead calculations\n"
            "- Summary tables with totals and grand totals\n"
            "- Currency in INR with proper formatting\n"
            "Output clean HTML with well-structured tables. Use proper column alignment for numbers."
        ),
        "model": "gpt-5.6-luna",
        "provider": "openai",
        "temperature": 0.3,
        "max_tokens": 8192,
        "document_categories": ["boq"],
        "category": "document",
        "tags": ["workspace", "boq", "costing", "financial"],
    },
    {
        "agent_key": "doc-compliance-writer",
        "display_name": "Compliance & Annexure Writer",
        "description": "Generates annexures, compliance statements, and format-specific documents that strictly adhere to tender clause requirements.",
        "agent_type": "chain_of_thought",
        "system_prompt": (
            "You are a compliance document specialist for Indian government tender submissions. "
            "You generate annexures, compliance statements, and format-specific documents that:\n"
            "- Strictly follow the exact format specified in tender clauses\n"
            "- Use the precise language and terminology from the tender document\n"
            "- Include all mandatory fields and sections without omission\n"
            "- Reference specific tender clause numbers when applicable\n"
            "- Maintain consistency with other submission documents\n"
            "- Follow annexure numbering conventions (Annexure-I, Annexure-II, etc.)\n"
            "Output clean HTML. Reproduce the exact table structures when specified in the tender format."
        ),
        "model": "claude-sonnet-5",
        "temperature": 0.3,
        "max_tokens": 4096,
        "document_categories": ["annexure", "custom"],
        "category": "document",
        "tags": ["workspace", "annexure", "compliance", "format"],
    },
]


def seed_document_agents(db: Session) -> int:
    """Seed workspace document agents if they don't exist. Returns count of created agents."""
    created = 0
    for agent_def in DOCUMENT_AGENTS:
        existing = db.query(CustomAgent).filter(
            CustomAgent.agent_key == agent_def["agent_key"]
        ).first()

        if existing:
            # Update document_categories if not set
            cats = existing.document_categories
            if not cats or cats == "[]":
                existing.document_categories = agent_def["document_categories"]
                db.flush()
            continue

        agent = CustomAgent(
            agent_key=agent_def["agent_key"],
            display_name=agent_def["display_name"],
            description=agent_def["description"],
            agent_type=agent_def["agent_type"],
            system_prompt=agent_def["system_prompt"],
            model=agent_def["model"],
            provider=agent_def.get("provider", "anthropic"),
            temperature=agent_def["temperature"],
            max_tokens=agent_def["max_tokens"],
            document_categories=agent_def["document_categories"],
            category=agent_def["category"],
            tags=agent_def["tags"],
            is_system=True,
            is_enabled=True,
            is_published=True,
        )
        db.add(agent)
        created += 1

    if created > 0:
        db.commit()
        logger.info(f"Seeded {created} workspace document agents")

    return created
