"""
DRPL Backend - Analysis Document Generation Service
Orchestrates the generation of analysis-type checklist items (costing, BOQ, rate analysis).
Pipeline: BOQ parse → costing research → zone-specific formatting → PDF generation.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.costing_template import BOQItem
from app.models.letterhead import GeneratedDocument
from app.models.tender import Tender

logger = logging.getLogger(__name__)


async def run_analysis_document_generation(
    db: Session,
    tender_id: int,
    analysis_items: list[dict],
    analysis_result: Optional[dict] = None,
    letterhead_template_id: Optional[int] = None,
    signature_ids: Optional[list[int]] = None,
    user_id: Optional[int] = None,
) -> list[dict]:
    """
    Generate analysis documents (costing/BOQ) for the pipeline.

    Called by generate_documents_node in tender_pipeline_graph.py.

    Args:
        db: Database session
        tender_id: Tender ID
        analysis_items: List of checklist items with category="analysis"
        analysis_result: Tender analysis output for context
        letterhead_template_id: Optional letterhead template
        signature_ids: Optional digital signature IDs
        user_id: User who triggered generation

    Returns:
        List of dicts with generation results per item
    """
    results = []

    # Parse BOQ from tender if not already done
    from app.services.boq_parser_service import parse_boq_from_tender, match_costing_template
    boq_items = await parse_boq_from_tender(db, tender_id)

    # Determine railway zone from tender
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    zone = _extract_zone(tender, analysis_result) if tender else None

    # Find matching costing template
    template = match_costing_template(db, zone)

    for item in analysis_items:
        item_name = item.get("name", "Unknown")
        ai_instructions = item.get("ai_instructions", "")

        try:
            doc_id = await generate_analysis_document(
                db=db,
                tender_id=tender_id,
                item_name=item_name,
                ai_instructions=ai_instructions,
                user_id=user_id,
                boq_items=boq_items,
                template=template,
                analysis_result=analysis_result,
                letterhead_template_id=letterhead_template_id,
                signature_ids=signature_ids,
            )

            results.append({
                "item_name": item_name,
                "status": "generated",
                "generated_document_id": doc_id,
            })

        except Exception as e:
            logger.error(f"Analysis document generation failed for '{item_name}': {e}")
            results.append({
                "item_name": item_name,
                "status": "failed",
                "error": str(e),
            })

    return results


async def generate_analysis_document(
    db: Session,
    tender_id: int,
    item_name: str,
    ai_instructions: Optional[str] = None,
    user_id: Optional[int] = None,
    boq_items: Optional[list[BOQItem]] = None,
    template=None,
    analysis_result: Optional[dict] = None,
    letterhead_template_id: Optional[int] = None,
    signature_ids: Optional[list[int]] = None,
) -> int:
    """
    Generate a single analysis document (costing sheet, BOQ, rate analysis).

    Returns:
        Generated document ID
    """
    # If no BOQ items provided, try parsing
    if boq_items is None:
        from app.services.boq_parser_service import parse_boq_from_tender
        boq_items = await parse_boq_from_tender(db, tender_id)

    if boq_items:
        # We have structured BOQ data — compute rates and render formatted cost sheet
        content_result = await _generate_costing_from_boq(
            db, tender_id, boq_items, template, analysis_result, ai_instructions
        )
    else:
        # No BOQ found — generate freeform analysis document via AI
        content_result = await _generate_freeform_analysis(
            db, tender_id, item_name, ai_instructions, analysis_result
        )

    # Create GeneratedDocument record
    doc = GeneratedDocument(
        title=item_name,
        document_type="cost_statement",
        tender_id=tender_id,
        letterhead_template_id=letterhead_template_id,
        content_html=content_result.get("content_html"),
        content_markdown=content_result.get("content_markdown"),
        signatures=[{"signature_id": sid} for sid in (signature_ids or [])],
    )
    db.add(doc)
    db.flush()

    # Generate PDF
    from app.services.pdf_generation_service import generate_pdf
    generate_pdf(db=db, document_id=doc.id)

    return doc.id


async def _generate_costing_from_boq(
    db: Session,
    tender_id: int,
    boq_items: list[BOQItem],
    template,
    analysis_result: Optional[dict],
    ai_instructions: Optional[str],
) -> dict:
    """Generate costing document from structured BOQ items."""

    # Use enhanced costing agent to compute rates for BOQ items
    items_needing_rates = [item for item in boq_items if not item.computed_rate]

    if items_needing_rates:
        await _compute_rates_via_costing_agent(db, tender_id, items_needing_rates, analysis_result)
        # Refresh items from DB
        for item in items_needing_rates:
            db.refresh(item)

    # Get tender title for the document header
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    tender_title = tender.title if tender else ""

    # Render formatted cost sheet
    from app.services.costing_format_service import render_costing_document
    return render_costing_document(
        boq_items=boq_items,
        template=template,
        tender_title=tender_title,
    )


async def _compute_rates_via_costing_agent(
    db: Session,
    tender_id: int,
    boq_items: list[BOQItem],
    analysis_result: Optional[dict],
):
    """Use the costing agent to compute rates for BOQ items."""
    from app.services.ai_service import call_ai
    import json

    # Build a prompt with BOQ items that need rates
    items_text = "Compute unit rates for the following BOQ items for an Indian railway tender.\n\n"
    items_text += "For each item, provide: computed_rate (Rs. per unit), material_cost, labour_cost, rate_source.\n\n"

    for item in boq_items:
        items_text += f"- Sr.{item.sr_no}: {item.description} | Qty: {item.quantity} {item.unit or ''}"
        if item.estimated_rate:
            items_text += f" | Estimated Rate: Rs. {item.estimated_rate}"
        items_text += "\n"

    system_prompt = """You are an expert Indian railway construction cost estimator.
Given BOQ items, compute realistic unit rates based on current market rates (2024-2025).

Consider:
- Delhi Schedule of Rates (DSR) as baseline
- Material costs: current market prices for steel, cement, electrical, etc.
- Labour costs: minimum wages + skilled labour rates for the region
- Overhead: 10% for site overhead, equipment, supervision

Respond with ONLY a valid JSON array, one object per item in order:
[{"sr_no": 1, "computed_rate": 1500.0, "material_cost": 800.0, "labour_cost": 500.0, "rate_source": "dsr_2024_estimate"}]"""

    try:
        result = await call_ai(system_prompt, items_text, db, "costing")
        result = result.strip()

        # Strip code fences
        if result.startswith("```"):
            result = result.split("\n", 1)[1] if "\n" in result else result[3:]
        if result.endswith("```"):
            result = result[:-3].strip()
        if result.startswith("json"):
            result = result[4:].strip()

        rates = json.loads(result)

        # Apply rates to BOQ items
        for i, rate_data in enumerate(rates):
            if i < len(boq_items):
                boq_items[i].computed_rate = rate_data.get("computed_rate")
                boq_items[i].material_cost = rate_data.get("material_cost")
                boq_items[i].labour_cost = rate_data.get("labour_cost")
                boq_items[i].rate_source = rate_data.get("rate_source", "ai_estimate")

        db.commit()

    except Exception as e:
        logger.error(f"Rate computation failed: {e}")
        # Use estimated rates as fallback
        for item in boq_items:
            if not item.computed_rate and item.estimated_rate:
                item.computed_rate = item.estimated_rate
                item.rate_source = "estimated_from_tender"
        db.commit()


async def _generate_freeform_analysis(
    db: Session,
    tender_id: int,
    item_name: str,
    ai_instructions: Optional[str],
    analysis_result: Optional[dict],
) -> dict:
    """Generate a freeform analysis document when no structured BOQ is available."""
    from app.services.document_ai_service import generate_document_content

    prompt = ai_instructions or (
        f"Generate a detailed {item_name} for a railway tender. "
        "Include itemized cost breakdowns with quantities, rates, and amounts. "
        "Use standard Indian railway costing format with GST calculations."
    )

    if analysis_result:
        requirements = analysis_result.get("requirements", {})
        if requirements.get("financial"):
            prompt += f"\n\nFinancial requirements from tender analysis: {requirements['financial']}"
        if requirements.get("technical_specs"):
            prompt += f"\n\nTechnical specifications: {requirements['technical_specs'][:1000]}"

    content = await generate_document_content(
        db=db,
        prompt=prompt,
        document_type="cost_statement",
    )

    return {
        "content_markdown": content,
        "content_html": None,
    }


def _extract_zone(tender: Tender, analysis_result: Optional[dict]) -> Optional[str]:
    """Extract railway zone from tender data or analysis result."""
    # Common railway zone abbreviations
    zones = ["CR", "WR", "NR", "SR", "ER", "NER", "NFR", "SER", "SECR", "SWR",
             "SCR", "NWR", "NCR", "ECR", "ECoR", "WCR", "DFCCIL", "KRCL", "KONKAN"]

    # Check tender organisation/department
    for field in [tender.organisation, tender.department, tender.title]:
        if field:
            field_upper = field.upper()
            for zone in zones:
                if zone in field_upper:
                    return zone

    # Check analysis result
    if analysis_result:
        text = str(analysis_result).upper()
        for zone in zones:
            if zone in text:
                return zone

    return None
