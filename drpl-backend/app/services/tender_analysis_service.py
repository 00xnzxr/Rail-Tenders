"""
DRPL Backend - Tender Analysis Service
AI-driven deep extraction of requirements, eligibility, terms from tender documents.
Detects critical/disqualification clauses. Cross-document correlation and completeness scoring.
"""

import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.tender import Tender, TenderDocument
from app.models.document_analysis import (
    DocumentExtractionResult,
    CriticalClauseFlag,
    ExtractionFeedback,
    TenderAnalysisSummary,
)
from app.core.config import get_settings
from app.services.advanced_document_parser import extract_text_from_file_advanced, get_full_text
from app.services.ai_service import call_ai

logger = logging.getLogger(__name__)
_settings = get_settings()

# --- Negative Keyword Patterns ---
# These patterns indicate clauses that could lead to disqualification or rejection

CRITICAL_KEYWORD_PATTERNS = [
    # Disqualification triggers
    (r"(?i)\b(?:will be|shall be|is|are)\s+(?:summarily\s+)?(?:disqualified|rejected|excluded|eliminated)", "disqualification"),
    (r"(?i)\b(?:liable\s+(?:for|to)\s+(?:rejection|disqualification))", "disqualification"),
    (r"(?i)\b(?:bid|tender|proposal|offer)\s+(?:will|shall)\s+(?:not\s+be\s+(?:considered|accepted|entertained))", "rejection"),
    (r"(?i)\b(?:summarily\s+rejected)", "disqualification"),
    (r"(?i)\b(?:outright\s+rejection)", "disqualification"),

    # Mandatory requirements
    (r"(?i)\b(?:mandatory|compulsory|obligatory)\s+(?:requirement|condition|criteria|document)", "mandatory"),
    (r"(?i)\b(?:must\s+(?:comply|submit|provide|furnish|produce|enclose|attach))", "mandatory"),
    (r"(?i)\b(?:failing\s+which|failure\s+to\s+(?:comply|submit|provide|furnish))", "mandatory"),
    (r"(?i)\b(?:non[\-\s]?submission\s+(?:will|shall|may)\s+(?:lead|result))", "mandatory"),

    # Non-negotiable terms
    (r"(?i)\b(?:non[\-\s]?negotiable)", "non_negotiable"),
    (r"(?i)\b(?:strictly\s+(?:required|mandatory|enforced))", "non_negotiable"),
    (r"(?i)\b(?:no\s+(?:deviation|exception|relaxation)\s+(?:will|shall)\s+be\s+(?:allowed|permitted|entertained))", "non_negotiable"),

    # Penalty clauses
    (r"(?i)\b(?:penalty|liquidated\s+damages|LD)\s+(?:of|@|at\s+the\s+rate)", "penalty"),
    (r"(?i)\b(?:forfeiture\s+of\s+(?:EMD|earnest\s+money|security\s+deposit|bid\s+security))", "penalty"),

    # Rejection conditions
    (r"(?i)\b(?:incomplete\s+(?:bid|tender|proposal)s?\s+(?:will|shall)\s+(?:not\s+be|be)\s+(?:considered|rejected))", "rejection"),
    (r"(?i)\b(?:conditional\s+(?:bid|tender|offer)s?\s+(?:will|shall)\s+(?:not\s+be|be)\s+(?:accepted|rejected))", "rejection"),
    (r"(?i)\b(?:late\s+(?:bid|tender|submission)s?\s+(?:will|shall)\s+(?:not\s+be\s+(?:accepted|considered|entertained)))", "rejection"),
]

# Extraction categories
EXTRACTION_CATEGORIES = [
    "requirements",
    "eligibility",
    "terms_conditions",
    "technical_specs",
    "financial",
    "experience",
    "compliance",
]


# --- Core Analysis Functions ---

async def analyze_document(db: Session, tender_id: int, document_id: int) -> list[DocumentExtractionResult]:
    """
    Extract structured requirements from a single document using AI.
    Processes document text page by page for accuracy, extracting into 7 categories.
    """
    document = db.query(TenderDocument).filter(TenderDocument.id == document_id).first()
    if not document:
        raise ValueError(f"Document {document_id} not found")

    # Extract text from document
    extraction = extract_text_from_file_advanced(document.file_path)
    full_text = get_full_text(extraction)

    if not full_text or not full_text.strip():
        logger.warning(f"[DRPL] No text extracted from document {document_id}")
        return []

    # Get tender context for better understanding
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    tender_context = f"Tender: {tender.title}\nPortal: {tender.portal}\nDepartment: {tender.department or 'N/A'}" if tender else ""

    results = []

    # Process in chunks if document is very long (>15000 chars)
    text_chunks = _chunk_text_for_analysis(full_text, max_chars=15000)

    for chunk_idx, chunk in enumerate(text_chunks):
        system_prompt = """You are an expert government tender document analyst for India. Your job is to extract
EVERY single requirement, criterion, and condition from tender documents with 100% accuracy.
Missing even ONE requirement can lead to bid disqualification.

Extract ALL items into these exact categories:
1. requirements - General submission requirements (documents, forms, formats, copies needed)
2. eligibility - Who can bid (turnover, experience, certifications, registrations required)
3. terms_conditions - Legal terms, contract conditions, warranties, liabilities
4. technical_specs - Technical specifications, standards, quality requirements
5. financial - EMD, security deposit, pricing format, payment terms, financial criteria
6. experience - Past work experience, similar project requirements, references needed
7. compliance - Regulatory compliance, licenses, approvals, environmental/safety requirements

For EACH extracted item, provide:
- text: The exact requirement text (concise but complete)
- category: One of the 7 categories above
- page_number: Page number where found (from the "--- Page N ---" markers)
- confidence: 0.0-1.0 how confident you are this is a real requirement
- is_critical: true if missing this could cause disqualification

Respond with ONLY a valid JSON array. Be exhaustive - extract EVERYTHING, even seemingly minor requirements.
Do NOT miss any requirement. Do NOT summarize - capture each individual requirement separately."""

        user_prompt = f"{tender_context}\n\nDocument: {document.file_name}\n\n{chunk}"

        try:
            result_text = await call_ai(system_prompt, user_prompt, db, "document_analyzer")
            items = _parse_json_response(result_text)

            if not isinstance(items, list):
                items = []

            # Group items by category and store
            for category in EXTRACTION_CATEGORIES:
                category_items = [item for item in items if item.get("category") == category]
                if category_items:
                    extraction_result = DocumentExtractionResult(
                        tender_id=tender_id,
                        document_id=document_id,
                        document_name=document.file_name,
                        extraction_type=category,
                        items=category_items,
                        raw_text=chunk[:5000],  # Store first 5000 chars for reference
                        extraction_model="document_analyzer",
                        completeness_score=None,  # Set later in completeness scoring
                    )
                    db.add(extraction_result)
                    results.append(extraction_result)

        except Exception as e:
            logger.error(f"[DRPL] AI extraction failed for doc {document_id}, chunk {chunk_idx}: {e}")

    db.commit()
    for r in results:
        db.refresh(r)

    return results


async def detect_critical_clauses(
    db: Session, tender_id: int, document_id: int, extraction: dict, document_name: str
) -> list[CriticalClauseFlag]:
    """
    Two-pass detection of critical/disqualification clauses.
    Pass 1: Regex scan for known negative keywords.
    Pass 2: AI analysis of surrounding context for severity classification.
    """
    flags = []
    full_text = get_full_text(extraction)
    pages = extraction.get("pages", [])

    # --- Pass 1: Regex keyword scan ---
    regex_matches = []
    for page in pages:
        page_text = page.get("text", "")
        page_num = page.get("page_num")

        for pattern, flag_type in CRITICAL_KEYWORD_PATTERNS:
            for match in re.finditer(pattern, page_text):
                # Get surrounding context (200 chars before and after)
                start = max(0, match.start() - 200)
                end = min(len(page_text), match.end() + 200)
                context = page_text[start:end]

                regex_matches.append({
                    "clause_text": match.group(),
                    "surrounding_context": context,
                    "flag_type": flag_type,
                    "keyword_matched": match.group(),
                    "page_number": page_num,
                })

    # --- Pass 2: AI severity classification ---
    if regex_matches:
        ai_classifications = await _ai_classify_critical_clauses(regex_matches, db)
    else:
        ai_classifications = []

    # Also do a full AI scan for critical clauses that regex might miss
    ai_additional = await _ai_scan_for_critical_clauses(full_text, document_name, db)

    # Combine and deduplicate
    all_clauses = ai_classifications + ai_additional
    seen_texts = set()

    for clause in all_clauses:
        clause_key = clause.get("clause_text", "")[:100]
        if clause_key in seen_texts:
            continue
        seen_texts.add(clause_key)

        flag = CriticalClauseFlag(
            tender_id=tender_id,
            document_id=document_id,
            document_name=document_name,
            clause_text=clause.get("clause_text", ""),
            surrounding_context=clause.get("surrounding_context", ""),
            flag_type=clause.get("flag_type", "mandatory"),
            severity=clause.get("severity", "high"),
            keyword_matched=clause.get("keyword_matched", ""),
            page_number=clause.get("page_number"),
            ai_explanation=clause.get("ai_explanation", ""),
        )
        db.add(flag)
        flags.append(flag)

    db.commit()
    for f in flags:
        db.refresh(f)

    return flags


async def _ai_classify_critical_clauses(matches: list[dict], db: Session) -> list[dict]:
    """AI classifies severity and provides explanation for regex-matched clauses."""
    system_prompt = """You are a legal/compliance expert analyzing government tender clauses in India.
For each flagged clause, assess:
1. severity: "critical" (instant disqualification), "high" (very likely rejection), "medium" (potential issue)
2. ai_explanation: Brief explanation of the consequence if this is not addressed

Respond with ONLY a valid JSON array matching the input structure with added severity and ai_explanation fields."""

    # Batch up to 20 matches
    batch = matches[:20]
    user_prompt = json.dumps(batch, indent=2)

    try:
        result_text = await call_ai(system_prompt, user_prompt, db, "document_analyzer")
        classified = _parse_json_response(result_text)
        if isinstance(classified, list):
            return classified
    except Exception as e:
        logger.error(f"[DRPL] AI clause classification failed: {e}")

    # Fallback: return with default severity
    for m in batch:
        m["severity"] = "high"
        m["ai_explanation"] = "Keyword-based detection; review manually."
    return batch


async def _ai_scan_for_critical_clauses(full_text: str, document_name: str, db: Session) -> list[dict]:
    """AI scans entire document for critical clauses that regex might miss."""
    if not full_text or len(full_text) < 100:
        return []

    # Truncate to fit context window
    text_for_analysis = full_text[:20000]

    system_prompt = """You are a compliance expert analyzing Indian government tender documents.
Identify ALL clauses that could lead to:
1. Disqualification of the bidder
2. Summary rejection of the bid
3. Forfeiture of EMD or security deposit
4. Non-consideration of the bid
5. Penalties or liquidated damages
6. Mandatory compliance requirements that are non-negotiable

For each clause found, provide:
- clause_text: The exact critical text
- surrounding_context: Brief surrounding context
- flag_type: "disqualification", "mandatory", "non_negotiable", "penalty", or "rejection"
- severity: "critical", "high", or "medium"
- keyword_matched: Key phrase that makes this critical
- page_number: Page number if identifiable (from "--- Page N ---" markers)
- ai_explanation: Why this is critical and what bidder must do

Respond with ONLY a valid JSON array. Be thorough - missing a critical clause can disqualify the bid."""

    user_prompt = f"Document: {document_name}\n\n{text_for_analysis}"

    try:
        result_text = await call_ai(system_prompt, user_prompt, db, "document_analyzer")
        clauses = _parse_json_response(result_text)
        if isinstance(clauses, list):
            return clauses
    except Exception as e:
        logger.error(f"[DRPL] AI critical clause scan failed: {e}")

    return []


async def analyze_all_documents(
    db: Session, tender_id: int, force_refresh: bool = False
) -> TenderAnalysisSummary:
    """
    Orchestrator: Run full analysis on ALL documents for a tender.
    Creates/updates TenderAnalysisSummary.

    Per-document reads are reused for documents whose bytes, model and prompt
    are unchanged (see analysis_reuse); ``force_refresh`` re-reads them all.
    """
    # Get or create summary
    summary = db.query(TenderAnalysisSummary).filter(
        TenderAnalysisSummary.tender_id == tender_id
    ).first()

    if not summary:
        summary = TenderAnalysisSummary(tender_id=tender_id)
        db.add(summary)

    summary.analysis_status = "in_progress"
    db.commit()

    try:
        # Clear previous synthesized results for this tender. Per-document
        # reads are kept: they carry the content hash that lets an unchanged
        # document be reused instead of re-read, and the v2 pass rewrites
        # them for every document it reads again.
        db.query(DocumentExtractionResult).filter(
            DocumentExtractionResult.tender_id == tender_id,
            DocumentExtractionResult.extraction_type != "per_doc_summary",
        ).delete(synchronize_session=False)
        db.query(CriticalClauseFlag).filter(
            CriticalClauseFlag.tender_id == tender_id
        ).delete()
        db.commit()

        # ── v2 path: vision-first, parallel per-doc + synthesis ────────────
        # Bypasses the legacy text-extraction loop entirely. See
        # _run_v2_analysis in document_analysis_agent.py for the orchestrator.
        if _settings.tender_analyzer_v2_enabled:
            from app.services.langchain.graphs.document_analysis_agent import _run_v2_analysis

            v2_result = await _run_v2_analysis(db, tender_id, force_refresh=force_refresh)
            v2_metrics = v2_result.get("metrics", {})

            # Compute aggregates from the persisted rows v2 just wrote.
            # Only the synthesized category rows count toward totals — per_doc_summary
            # rows are auxiliary detail (and would double-count if included).
            extractions = db.query(DocumentExtractionResult).filter(
                DocumentExtractionResult.tender_id == tender_id,
                DocumentExtractionResult.extraction_type != "per_doc_summary",
            ).all()
            total_requirements = sum(len(e.items or []) for e in extractions)
            category_counts = {}
            for e in extractions:
                category_counts[e.extraction_type] = (
                    category_counts.get(e.extraction_type, 0) + len(e.items or [])
                )
            total_flags = db.query(CriticalClauseFlag).filter(
                CriticalClauseFlag.tender_id == tender_id
            ).count()
            documents_analyzed = (
                v2_metrics.get("documents_count", 0)
                - v2_metrics.get("documents_unreadable", 0)
            )
            completeness = await score_completeness(db, tender_id)

            summary.total_requirements = total_requirements
            summary.total_critical_flags = total_flags
            summary.completeness_score = completeness
            summary.documents_analyzed = documents_analyzed
            summary.cross_document_conflicts = []  # synthesis already reconciled
            summary.category_counts = category_counts
            summary.requirement_summary = (
                v2_result.get("analysis", {}).get("report_markdown", "")
            )
            summary.analysis_status = "completed"
            summary.error_message = None
            summary.last_analyzed_at = datetime.now(timezone.utc)
            # cost columns + analysis_version are written inside _run_v2_analysis
            db.commit()
            db.refresh(summary)
            return summary
        # ── end v2 path ────────────────────────────────────────────────────

        # Get all documents for this tender
        documents = db.query(TenderDocument).filter(
            TenderDocument.tender_id == tender_id
        ).all()

        total_requirements = 0
        total_flags = 0
        category_counts = {}
        documents_analyzed = 0

        for doc in documents:
            try:
                # Extract and analyze each document
                extraction = extract_text_from_file_advanced(doc.file_path)

                # AI extraction of requirements
                extraction_results = await analyze_document(db, tender_id, doc.id)
                for result in extraction_results:
                    item_count = len(result.items) if result.items else 0
                    total_requirements += item_count
                    cat = result.extraction_type
                    category_counts[cat] = category_counts.get(cat, 0) + item_count

                # Critical clause detection
                critical_flags = await detect_critical_clauses(
                    db, tender_id, doc.id, extraction, doc.file_name
                )
                total_flags += len(critical_flags)
                documents_analyzed += 1

            except Exception as e:
                logger.error(f"[DRPL] Analysis failed for document {doc.id}: {e}")

        # Also analyze deep scrape text from the tender itself
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if tender:
            deep_scrape_results = await _analyze_deep_scrape_text(db, tender)
            for result in deep_scrape_results:
                item_count = len(result.items) if result.items else 0
                total_requirements += item_count
                cat = result.extraction_type
                category_counts[cat] = category_counts.get(cat, 0) + item_count

        # Cross-document correlation
        conflicts = []
        if documents_analyzed > 1:
            conflicts = await cross_document_correlate(db, tender_id)

        # Completeness scoring
        completeness = await score_completeness(db, tender_id)

        # Update summary
        summary.total_requirements = total_requirements
        summary.total_critical_flags = total_flags
        summary.completeness_score = completeness
        summary.documents_analyzed = documents_analyzed
        summary.cross_document_conflicts = conflicts
        summary.category_counts = category_counts
        summary.analysis_status = "completed"
        summary.error_message = None
        summary.last_analyzed_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(summary)
        return summary

    except Exception as e:
        logger.error(f"[DRPL] Full analysis failed for tender {tender_id}: {e}")
        summary.analysis_status = "failed"
        summary.error_message = str(e)
        db.commit()
        db.refresh(summary)
        return summary


async def _analyze_deep_scrape_text(db: Session, tender: Tender) -> list[DocumentExtractionResult]:
    """Analyze text from deep scrape fields (eligibility_criteria, technical_specifications, etc.)."""
    results = []
    deep_scrape_texts = []

    if tender.full_description:
        deep_scrape_texts.append(("Full Description", tender.full_description))
    if tender.eligibility_criteria:
        deep_scrape_texts.append(("Eligibility Criteria", tender.eligibility_criteria))
    if tender.technical_specifications:
        deep_scrape_texts.append(("Technical Specifications", tender.technical_specifications))
    if tender.evaluation_criteria:
        deep_scrape_texts.append(("Evaluation Criteria", tender.evaluation_criteria))

    if not deep_scrape_texts:
        return results

    combined_text = "\n\n".join(f"--- {name} ---\n{text}" for name, text in deep_scrape_texts)

    system_prompt = """You are an expert government tender document analyst for India. Extract ALL requirements,
criteria, and conditions from the following tender information. Categorize each into:
requirements, eligibility, terms_conditions, technical_specs, financial, experience, compliance.

For each item provide: text, category, confidence (0-1), is_critical (boolean).
Respond with ONLY a valid JSON array."""

    tender_context = f"Tender: {tender.title}\nPortal: {tender.portal}"
    user_prompt = f"{tender_context}\n\n{combined_text}"

    try:
        result_text = await call_ai(system_prompt, user_prompt, db, "document_analyzer")
        items = _parse_json_response(result_text)

        if isinstance(items, list):
            for category in EXTRACTION_CATEGORIES:
                category_items = [item for item in items if item.get("category") == category]
                if category_items:
                    extraction_result = DocumentExtractionResult(
                        tender_id=tender.id,
                        document_id=None,
                        document_name="Deep Scrape Data",
                        extraction_type=category,
                        items=category_items,
                        raw_text=combined_text[:5000],
                        extraction_model="document_analyzer",
                    )
                    db.add(extraction_result)
                    results.append(extraction_result)
            db.commit()
            for r in results:
                db.refresh(r)
    except Exception as e:
        logger.error(f"[DRPL] Deep scrape analysis failed for tender {tender.id}: {e}")

    return results


async def cross_document_correlate(db: Session, tender_id: int) -> list[dict]:
    """
    AI analyzes extractions across multiple documents to find conflicts,
    amendments overriding earlier requirements, and inconsistencies.
    """
    extractions = db.query(DocumentExtractionResult).filter(
        DocumentExtractionResult.tender_id == tender_id
    ).all()

    if len(extractions) < 2:
        return []

    # Build summary of extractions per document
    doc_summaries = {}
    for ext in extractions:
        doc_name = ext.document_name or f"Document {ext.document_id}"
        if doc_name not in doc_summaries:
            doc_summaries[doc_name] = []
        for item in (ext.items or []):
            doc_summaries[doc_name].append({
                "category": ext.extraction_type,
                "text": item.get("text", "")[:200],
            })

    # Truncate for API context
    summary_text = json.dumps(doc_summaries, indent=2)[:15000]

    system_prompt = """You are a tender compliance expert. Analyze requirements extracted from multiple tender
documents and identify:
1. Conflicts: Where two documents say contradictory things
2. Amendments: Where a later document (corrigendum/amendment) changes an earlier requirement
3. Duplicates: Same requirement stated differently across documents
4. Gaps: Requirements in one document that should have corresponding details in another

Respond with ONLY a valid JSON array of objects:
[{"type": "conflict|amendment|duplicate|gap", "doc_a": "name", "doc_b": "name", "description": "explanation"}]"""

    try:
        result_text = await call_ai(system_prompt, summary_text, db, "document_analyzer")
        conflicts = _parse_json_response(result_text)
        if isinstance(conflicts, list):
            return conflicts
    except Exception as e:
        logger.error(f"[DRPL] Cross-document correlation failed for tender {tender_id}: {e}")

    return []


#: The categories the old completeness prompt called "major".
_MAJOR_CATEGORIES = ("eligibility", "requirements", "financial")


def completeness_from_counts(category_counts: dict) -> float:
    """How completely the extraction covered the tender, from item counts.

    This was an LLM call whose entire input was two numbers' worth of JSON --
    the total and a per-category count -- and whose prompt was a list of
    rules: fewer than five items is incomplete, a missing major category
    scores lower, more items scores higher, most categories should be
    present. Those rules are arithmetic, so they are written as arithmetic:
    the same answer every time, no call, and a fallback formula that was
    already here is now the whole of it plus the two rules it lacked.
    """
    total = sum(v for v in category_counts.values() if isinstance(v, (int, float)))
    if total <= 0:
        return 0.0
    filled = sum(1 for c in EXTRACTION_CATEGORIES if category_counts.get(c, 0) > 0)
    score = (filled / len(EXTRACTION_CATEGORIES)) * 0.5 + min(total / 50, 0.5)
    missing_major = sum(1 for c in _MAJOR_CATEGORIES if category_counts.get(c, 0) <= 0)
    score -= 0.15 * missing_major
    if total < 5:
        score = min(score, 0.3)
    return round(max(0.0, min(1.0, score)), 3)


async def score_completeness(db: Session, tender_id: int) -> float:
    """Completeness of the extraction for a tender (see completeness_from_counts)."""
    extractions = db.query(DocumentExtractionResult).filter(
        DocumentExtractionResult.tender_id == tender_id
    ).all()

    if not extractions:
        return 0.0

    category_counts: dict = {}
    for ext in extractions:
        cat = ext.extraction_type
        category_counts[cat] = category_counts.get(cat, 0) + len(ext.items or [])
    return completeness_from_counts(category_counts)


# --- Feedback and Accuracy ---

def submit_feedback(
    db: Session,
    extraction_result_id: int,
    item_index: int,
    feedback_type: str,
    user_id: int,
    corrected_text: Optional[str] = None,
    notes: Optional[str] = None,
) -> ExtractionFeedback:
    """Store human feedback on extraction accuracy."""
    feedback = ExtractionFeedback(
        extraction_result_id=extraction_result_id,
        item_index=item_index,
        feedback_type=feedback_type,
        corrected_text=corrected_text,
        notes=notes,
        user_id=user_id,
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return feedback


def get_accuracy_stats(db: Session, tender_id: Optional[int] = None) -> dict:
    """Aggregate feedback to compute accuracy rates."""
    query = db.query(ExtractionFeedback)
    if tender_id:
        extraction_ids = [
            r.id for r in db.query(DocumentExtractionResult.id).filter(
                DocumentExtractionResult.tender_id == tender_id
            ).all()
        ]
        query = query.filter(ExtractionFeedback.extraction_result_id.in_(extraction_ids))

    total_feedback = query.count()
    if total_feedback == 0:
        return {"total_feedback": 0, "accuracy_rate": None, "breakdown": {}}

    correct = query.filter(ExtractionFeedback.feedback_type == "correct").count()
    incorrect = query.filter(ExtractionFeedback.feedback_type == "incorrect").count()
    missing = query.filter(ExtractionFeedback.feedback_type == "missing").count()
    duplicate = query.filter(ExtractionFeedback.feedback_type == "duplicate").count()

    accuracy_rate = correct / total_feedback if total_feedback > 0 else None

    return {
        "total_feedback": total_feedback,
        "accuracy_rate": round(accuracy_rate, 3) if accuracy_rate else None,
        "breakdown": {
            "correct": correct,
            "incorrect": incorrect,
            "missing": missing,
            "duplicate": duplicate,
        },
    }


# --- Helpers ---

def _parse_json_response(text: str) -> list | dict:
    """Parse JSON from AI response, handling markdown code blocks."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array or object in the text
        for start_char, end_char in [("[", "]"), ("{", "}")]:
            start = text.find(start_char)
            end = text.rfind(end_char)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
        logger.warning(f"[DRPL] Could not parse JSON from AI response: {text[:200]}")
        return []


def _chunk_text_for_analysis(text: str, max_chars: int = 15000) -> list[str]:
    """Split text into chunks for analysis, respecting page boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current_chunk = ""
    pages = text.split("--- Page ")

    for page in pages:
        page_with_marker = f"--- Page {page}" if page != pages[0] else page
        if len(current_chunk) + len(page_with_marker) > max_chars and current_chunk:
            chunks.append(current_chunk)
            current_chunk = page_with_marker
        else:
            current_chunk += page_with_marker

    if current_chunk:
        chunks.append(current_chunk)

    return chunks if chunks else [text[:max_chars]]
