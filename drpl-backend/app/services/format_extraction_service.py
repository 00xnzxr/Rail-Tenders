"""
DRPL Backend - Format Extraction Service
Extracts annexure, proforma, BOQ, and costing format references from
tender analysis output and creates DocumentFormatTemplate + workspace records.

Called as a post-analysis hook in the streaming handler.
"""

import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models.workspace import DocumentFormatTemplate, DocumentWorkspace, WorkspaceConfig
from app.models.checklist import ChecklistItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category mapping for known document types
# ---------------------------------------------------------------------------
_CATEGORY_KEYWORDS = {
    "boq": ["boq", "bill of quantities", "bill of quantity", "schedule of rates",
            "price schedule", "rate schedule", "price bid", "financial bid format"],
    "annexure": ["annexure", "annex", "appendix", "proforma", "format-", "form-",
                 "prescribed format", "specimen"],
    "declaration": ["declaration", "undertaking", "affidavit", "self-declaration",
                    "no deviation", "non-blacklisting"],
    "certificate": ["certificate", "certification", "compliance certificate",
                    "experience certificate", "iso certificate"],
    "letter": ["covering letter", "cover letter", "forwarding letter",
               "authorization letter", "bid letter", "letter of intent"],
}


def _classify_category(name: str) -> str:
    """Classify a document name into a category."""
    lower = name.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return "custom"


# ---------------------------------------------------------------------------
# Regex patterns for format references in analysis text
# ---------------------------------------------------------------------------
_FORMAT_PATTERNS = [
    # Annexure-A, Annexure 1, Annexure-IV etc.
    re.compile(r"(Annexure[\s\-]?[A-Z0-9]+(?:\s*[-:]\s*[A-Za-z\s&,]+)?)", re.IGNORECASE),
    # Format-A, Format 1 etc.
    re.compile(r"(Format[\s\-]?[A-Z0-9]+(?:\s*[-:]\s*[A-Za-z\s&,]+)?)", re.IGNORECASE),
    # Proforma for X, Proforma-A
    re.compile(r"(Proforma[\s\-]?[A-Z0-9]*(?:\s+(?:for|of)\s+[A-Za-z\s&,]+)?)", re.IGNORECASE),
    # Schedule of Rates, BOQ, Bill of Quantities
    re.compile(r"((?:Schedule\s+of\s+Rates|Bill\s+of\s+Quantit(?:y|ies)|BOQ)(?:\s*[-:]\s*[A-Za-z\s&,]+)?)", re.IGNORECASE),
    # Price Schedule / Price Bid Format
    re.compile(r"((?:Price|Financial)\s+(?:Schedule|Bid\s+Format|Bid))", re.IGNORECASE),
]


def extract_formats_from_analysis(
    output: str,
    structured_data: Optional[dict] = None,
) -> list[dict]:
    """
    Extract annexure, proforma, and format template references from
    analysis output text and structured data.

    Returns a list of dicts: [{"name": str, "category": str, "format_hints": str}]
    """
    found = {}  # name_lower -> {"name": str, "category": str, "format_hints": str}

    # --- Pass 1: Regex extraction from markdown output ---
    if output:
        for pattern in _FORMAT_PATTERNS:
            for match in pattern.finditer(output):
                raw_name = match.group(1).strip().rstrip(".,;:")
                # Clean up trailing whitespace and common noise
                raw_name = re.sub(r"\s+", " ", raw_name).strip()
                if len(raw_name) < 4 or len(raw_name) > 120:
                    continue
                key = raw_name.lower()
                if key not in found:
                    category = _classify_category(raw_name)
                    # Extract surrounding context as format hints (±200 chars)
                    start = max(0, match.start() - 200)
                    end = min(len(output), match.end() + 200)
                    context = output[start:end].strip()
                    found[key] = {
                        "name": raw_name,
                        "category": category,
                        "format_hints": context[:500],
                    }

    # --- Pass 2: Structured data parsing ---
    if structured_data and isinstance(structured_data, dict):
        # Check common keys where required documents are listed
        for doc_key in ("required_documents", "documents", "submission_documents",
                        "missing_items", "document_requirements"):
            docs = structured_data.get(doc_key)
            if not docs or not isinstance(docs, list):
                continue
            for doc in docs:
                if isinstance(doc, str):
                    doc_name = doc.strip()
                elif isinstance(doc, dict):
                    doc_name = (doc.get("name") or doc.get("document") or
                                doc.get("title") or doc.get("item") or "").strip()
                else:
                    continue
                if not doc_name or len(doc_name) < 4:
                    continue
                key = doc_name.lower()
                if key not in found:
                    category = _classify_category(doc_name)
                    hints = ""
                    if isinstance(doc, dict):
                        hints = doc.get("format", doc.get("description", ""))
                    found[key] = {
                        "name": doc_name,
                        "category": category,
                        "format_hints": str(hints)[:500],
                    }

    return list(found.values())


def create_format_templates_and_workspace_items(
    db: Session,
    tender_id: int,
    formats: list[dict],
    user_id: Optional[int] = None,
) -> dict:
    """
    Create DocumentFormatTemplate records for extracted formats and
    optionally link them to existing workspace items.

    Returns {"templates_created": int, "workspace_items_updated": int}
    """
    templates_created = 0
    workspace_items_updated = 0

    for fmt in formats:
        name = fmt["name"]
        category = fmt.get("category", "custom")
        hints = fmt.get("format_hints", "")

        # Check if template already exists (by name, case-insensitive)
        existing = db.query(DocumentFormatTemplate).filter(
            DocumentFormatTemplate.name.ilike(name),
            DocumentFormatTemplate.is_active == True,
        ).first()
        if existing:
            template = existing
            template_id = existing.id
        else:
            # Build match patterns from name
            name_lower = name.lower().strip()
            patterns = [f"*{name_lower}*"]
            # Also add a pattern without separators (e.g., "annexure a" matches "annexure-a")
            simplified = re.sub(r"[\s\-]+", "*", name_lower)
            if simplified != name_lower:
                patterns.append(f"*{simplified}*")

            template = DocumentFormatTemplate(
                name=name,
                description=f"Auto-extracted from tender analysis: {hints[:200]}" if hints else None,
                document_category=category,
                match_patterns=patterns,
                output_format="xlsx" if category == "boq" else "docx",
                format_rules=[hints] if hints else [],
                is_system=False,
                is_active=True,
                created_by=user_id,
            )
            db.add(template)
            db.flush()
            template_id = template.id
            templates_created += 1

        # Link to workspace items if workspace exists for this tender
        workspace_config = db.query(WorkspaceConfig).filter(
            WorkspaceConfig.tender_id == tender_id,
        ).first()
        if not workspace_config:
            continue

        # Find checklist items that might match this format
        checklist_items = db.query(ChecklistItem).filter(
            ChecklistItem.tender_id == tender_id,
        ).all()

        import fnmatch
        for ci in checklist_items:
            ci_name_lower = (ci.item_name or "").lower()
            if not ci_name_lower:
                continue
            # Check if any of the template's match patterns match this checklist item
            matched = any(fnmatch.fnmatch(ci_name_lower, p) for p in (template.match_patterns or []))
            if not matched:
                # Also try simple substring match
                matched = name.lower() in ci_name_lower or ci_name_lower in name.lower()
            if not matched:
                continue

            # Check if this checklist item has a workspace entry without a template
            ws = db.query(DocumentWorkspace).filter(
                DocumentWorkspace.checklist_item_id == ci.id,
            ).first()
            if ws and not ws.format_template_id:
                ws.format_template_id = template_id
                workspace_items_updated += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("Failed to commit format templates/workspace updates")

    if templates_created > 0 or workspace_items_updated > 0:
        logger.info(
            f"Format extraction for tender {tender_id}: "
            f"{templates_created} templates created, {workspace_items_updated} workspace items updated"
        )

    return {
        "templates_created": templates_created,
        "workspace_items_updated": workspace_items_updated,
    }
