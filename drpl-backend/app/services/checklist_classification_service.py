"""
DRPL Backend - Checklist Classification Service
Classifies checklist items into standard/generated/analysis categories
and generates AI instructions for items that need generation.
"""

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.ai_service import call_ai

logger = logging.getLogger(__name__)

CLASSIFICATION_SYSTEM_PROMPT = """You are a document classification specialist for Indian railway tender submissions.

Given a list of checklist items (required documents) extracted from a tender, classify each item into one of three categories:

1. **standard** — Pre-existing company documents that do NOT need to be generated. The company already has these.
   Examples: PAN Card, GST Registration Certificate, Company Registration, Trade License, Bank Account Details, EMD Receipt, Tender Fee Receipt, MSME Certificate, ISO Certificate, Digital Signature Certificate, Power of Attorney (existing), Annual Reports, Balance Sheets, ITR copies, Audited Financial Statements.

2. **generated** — Documents that need to be drafted/written by AI and exported on company letterhead.
   Examples: Cover Letter, Undertaking Letters, Self-Declarations, Compliance Statements, Authorization Letters, No-Deviation Statements, Joint Venture Agreements (drafts), Experience Certificates (narrative), Bid Security Declarations, Integrity Pact declarations, Technical Methodology write-ups, Work Plan narratives.

3. **analysis** — Documents requiring computation, data analysis, or structured financial calculations.
   Examples: Bill of Quantities (BOQ), Cost Estimates, Rate Analysis, Price Bid/Schedule, Financial Bid, Abstract of Cost, Detailed Cost Breakdown, Material/Labour Cost Statements, Escalation Calculations, Schedule of Rates.

For each item, provide:
- category: "standard", "generated", or "analysis"
- ai_instructions: For "generated" items, a specific prompt describing what the AI should write. For "analysis" items, what needs to be computed. For "standard" items, leave as null.
- source_section: Which part of the tender this requirement likely comes from (e.g., "Section 2 - Eligibility", "NIT Clause 4", "Technical Bid Requirements"). Use your best guess based on the item name and description.

Respond with ONLY a valid JSON array matching the input order. Each element:
{"category": "standard|generated|analysis", "ai_instructions": "string or null", "source_section": "string or null"}"""


async def classify_checklist_items(
    db: Session,
    checklist_items: list[dict],
    analysis_result: Optional[dict] = None,
    tender_id: Optional[int] = None,
) -> list[dict]:
    """
    Classify each checklist item as standard/generated/analysis.

    Args:
        db: Database session
        checklist_items: List of dicts with 'name', 'description', 'is_required'
        analysis_result: Optional tender analysis result for context
        tender_id: Optional tender ID for logging

    Returns:
        List of dicts with 'category', 'ai_instructions', 'source_section' per item
    """
    if not checklist_items:
        return []

    # Build the user prompt with item list
    items_text = "## Checklist Items to Classify:\n\n"
    for i, item in enumerate(checklist_items):
        name = item.get("name", item.get("item_name", "Unknown"))
        desc = item.get("description", item.get("item_description", ""))
        items_text += f"{i + 1}. **{name}**"
        if desc:
            items_text += f" — {desc}"
        items_text += "\n"

    # Add analysis context if available
    if analysis_result:
        context_parts = []
        if analysis_result.get("requirements"):
            context_parts.append(f"Tender Requirements: {json.dumps(analysis_result['requirements'], default=str)[:2000]}")
        if analysis_result.get("tender_type"):
            context_parts.append(f"Tender Type: {analysis_result['tender_type']}")
        if context_parts:
            items_text += "\n## Tender Context:\n" + "\n".join(context_parts)

    try:
        result = await call_ai(CLASSIFICATION_SYSTEM_PROMPT, items_text, db, "checklist")
        result = result.strip()

        # Strip markdown code fences if present
        if result.startswith("```"):
            result = result.split("\n", 1)[1] if "\n" in result else result[3:]
        if result.endswith("```"):
            result = result[:-3].strip()
        if result.startswith("json"):
            result = result[4:].strip()

        classifications = json.loads(result)

        # Validate length matches
        if len(classifications) != len(checklist_items):
            logger.warning(
                f"Classification count mismatch: got {len(classifications)}, expected {len(checklist_items)}. "
                f"Padding with defaults."
            )
            while len(classifications) < len(checklist_items):
                classifications.append({"category": "standard", "ai_instructions": None, "source_section": None})
            classifications = classifications[:len(checklist_items)]

        # Validate categories
        valid_categories = {"standard", "generated", "analysis"}
        for cls in classifications:
            if cls.get("category") not in valid_categories:
                cls["category"] = "standard"

        return classifications

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse classification response as JSON: {e}")
        return [{"category": "standard", "ai_instructions": None, "source_section": None}] * len(checklist_items)
    except Exception as e:
        logger.error(f"Checklist classification failed for tender {tender_id}: {e}")
        return [{"category": "standard", "ai_instructions": None, "source_section": None}] * len(checklist_items)
