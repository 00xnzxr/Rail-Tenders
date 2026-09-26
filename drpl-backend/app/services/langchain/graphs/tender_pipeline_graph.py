"""
DRPL LangChain Pipeline - Tender Processing Graph
Main 4-step pipeline orchestration using LangGraph:
  Step 1: Document Analysis → Step 2: Checklist Generation →
  Step 3: Document Generation → Step 4: Costing Research → END
"""

import json
import logging
from typing import Optional, TypedDict, Annotated
from operator import add

from langgraph.graph import StateGraph, END
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# --- Pipeline State ---

class TenderPipelineState(TypedDict):
    """Shared state across all pipeline steps."""
    tender_id: int
    documents_text: dict                # doc_name → extracted text
    analysis_result: dict               # deep analysis output
    negative_keywords: list             # detected rejection triggers
    checklist_items: list               # required documents
    generated_documents: list           # per-item generated content + PDF paths
    costing_data: dict                  # cost calculations
    execution_id: Optional[int]         # parent execution tracking
    letterhead_template_id: Optional[int]
    signature_ids: Optional[list]
    user_id: Optional[int]
    current_step: str                   # tracks which step we're on
    errors: Annotated[list, add]        # accumulated errors across steps
    completed_steps: Annotated[list, add]


# --- Pipeline Node Functions ---

async def analyze_documents_node(state: TenderPipelineState, db: Session) -> dict:
    """Step 1: Deep document analysis."""
    tender_id = state["tender_id"]
    logger.info(f"Pipeline Step 1: Analyzing documents for tender {tender_id}")

    try:
        from app.services.langchain.graphs.document_analysis_agent import run_document_analysis

        result = await run_document_analysis(
            db=db,
            tender_id=tender_id,
            user_id=state.get("user_id"),
        )

        analysis = result.get("analysis", {})

        return {
            "analysis_result": analysis,
            "negative_keywords": analysis.get("negative_keywords", []),
            "current_step": "analyze_documents",
            "completed_steps": ["analyze_documents"],
        }

    except Exception as e:
        logger.error(f"Document analysis failed: {e}")
        return {
            "analysis_result": {},
            "negative_keywords": [],
            "current_step": "analyze_documents",
            "errors": [f"Document analysis failed: {str(e)}"],
            "completed_steps": ["analyze_documents (failed)"],
        }


async def generate_checklist_node(state: TenderPipelineState, db: Session) -> dict:
    """Step 2: Generate document checklist from analysis, then classify items."""
    tender_id = state["tender_id"]
    analysis = state.get("analysis_result", {})
    logger.info(f"Pipeline Step 2: Generating checklist for tender {tender_id}")

    try:
        # First, try to extract checklist from the analysis result
        required_docs = analysis.get("required_documents", [])

        if required_docs:
            # Use the analysis-extracted checklist
            checklist_items = []
            for doc in required_docs:
                if isinstance(doc, dict):
                    checklist_items.append({
                        "name": doc.get("name", doc.get("document", "Unknown")),
                        "description": doc.get("description", doc.get("details", "")),
                        "is_required": doc.get("mandatory", doc.get("is_required", True)),
                    })
                elif isinstance(doc, str):
                    checklist_items.append({
                        "name": doc,
                        "description": "",
                        "is_required": True,
                    })
        else:
            # Fallback: use existing checklist service (which now classifies internally)
            from app.services.checklist_service import generate_checklist
            items = await generate_checklist(db, tender_id)
            checklist_items = [
                {
                    "name": item.item_name,
                    "description": item.item_description or "",
                    "is_required": item.is_required,
                    "category": item.item_category or "standard",
                    "ai_instructions": item.ai_instructions,
                    "source_section": item.source_section,
                }
                for item in items
            ]
            # Already persisted by generate_checklist, return directly
            return {
                "checklist_items": checklist_items,
                "current_step": "generate_checklist",
                "completed_steps": ["generate_checklist"],
            }

        # Classify items extracted from analysis (not already classified)
        from app.services.checklist_classification_service import classify_checklist_items
        classifications = await classify_checklist_items(
            db=db,
            checklist_items=checklist_items,
            analysis_result=analysis,
            tender_id=tender_id,
        )

        # Merge classification data into checklist items
        for i, item in enumerate(checklist_items):
            cls = classifications[i] if i < len(classifications) else {}
            item["category"] = cls.get("category", "standard")
            item["ai_instructions"] = cls.get("ai_instructions")
            item["source_section"] = cls.get("source_section")

        # Persist checklist items to the database
        _persist_checklist(db, tender_id, checklist_items)

        return {
            "checklist_items": checklist_items,
            "current_step": "generate_checklist",
            "completed_steps": ["generate_checklist"],
        }

    except Exception as e:
        logger.error(f"Checklist generation failed: {e}")
        return {
            "checklist_items": [],
            "current_step": "generate_checklist",
            "errors": [f"Checklist generation failed: {str(e)}"],
            "completed_steps": ["generate_checklist (failed)"],
        }


async def generate_documents_node(state: TenderPipelineState, db: Session) -> dict:
    """Step 3: Generate documents for each checklist item, branching by category."""
    tender_id = state["tender_id"]
    checklist_items = state.get("checklist_items", [])
    analysis_result = state.get("analysis_result", {})
    logger.info(f"Pipeline Step 3: Generating {len(checklist_items)} documents for tender {tender_id}")

    if not checklist_items:
        return {
            "generated_documents": [],
            "current_step": "generate_documents",
            "errors": ["No checklist items to generate documents for"],
            "completed_steps": ["generate_documents (skipped)"],
        }

    # Split items by category
    standard_items = [i for i in checklist_items if i.get("category", "standard") == "standard"]
    generated_items = [i for i in checklist_items if i.get("category") == "generated"]
    analysis_items = [i for i in checklist_items if i.get("category") == "analysis"]

    all_docs = []
    errors = []

    # Standard items: mark as needs_attachment, no AI generation
    for item in standard_items:
        all_docs.append({
            "item_name": item.get("name", "Unknown"),
            "category": "standard",
            "status": "needs_attachment",
            "content": None,
            "pdf_path": None,
        })

    # Generated items: send to proposal agent for AI content + letterhead + PDF
    if generated_items:
        try:
            from app.services.langchain.graphs.proposal_agent import run_proposal_generation

            _update_checklist_generation_status(db, tender_id, generated_items, "generating")

            docs = await run_proposal_generation(
                db=db,
                tender_id=tender_id,
                checklist_items=generated_items,
                analysis_result=analysis_result,
                letterhead_template_id=state.get("letterhead_template_id"),
                signature_ids=state.get("signature_ids"),
                user_id=state.get("user_id"),
            )

            for doc in docs:
                doc["category"] = "generated"
            all_docs.extend(docs)

            _update_checklist_generation_status(db, tender_id, generated_items, "generated")
        except Exception as e:
            logger.error(f"Generated document creation failed: {e}")
            errors.append(f"Generated documents failed: {str(e)}")
            _update_checklist_generation_status(db, tender_id, generated_items, "failed", str(e))

    # Analysis items: send to analysis document generation service
    if analysis_items:
        try:
            from app.services.analysis_document_service import run_analysis_document_generation

            _update_checklist_generation_status(db, tender_id, analysis_items, "generating")

            docs = await run_analysis_document_generation(
                db=db,
                tender_id=tender_id,
                analysis_items=analysis_items,
                analysis_result=analysis_result,
                letterhead_template_id=state.get("letterhead_template_id"),
                signature_ids=state.get("signature_ids"),
                user_id=state.get("user_id"),
            )

            for doc in docs:
                doc["category"] = "analysis"
            all_docs.extend(docs)

            _update_checklist_generation_status(db, tender_id, analysis_items, "generated")
        except Exception as e:
            logger.error(f"Analysis document generation failed: {e}")
            errors.append(f"Analysis documents failed: {str(e)}")
            _update_checklist_generation_status(db, tender_id, analysis_items, "failed", str(e))

    result = {
        "generated_documents": all_docs,
        "current_step": "generate_documents",
        "completed_steps": ["generate_documents"],
    }
    if errors:
        result["errors"] = errors
    return result


async def research_costing_node(state: TenderPipelineState, db: Session) -> dict:
    """Step 4: Research and calculate costing."""
    tender_id = state["tender_id"]
    analysis_result = state.get("analysis_result", {})
    logger.info(f"Pipeline Step 4: Researching costing for tender {tender_id}")

    # Check if there are financial/costing requirements
    requirements = analysis_result.get("requirements", {})
    financial = requirements.get("financial", [])
    technical = requirements.get("technical_specs", [])

    if not financial and not technical:
        return {
            "costing_data": {"note": "No costing requirements identified in tender analysis"},
            "current_step": "research_costing",
            "completed_steps": ["research_costing (skipped - no requirements)"],
        }

    try:
        from app.services.langchain.graphs.costing_agent import run_costing_research

        result = await run_costing_research(
            db=db,
            tender_id=tender_id,
            analysis_result=analysis_result,
            user_id=state.get("user_id"),
        )

        return {
            "costing_data": result.get("costing", {}),
            "current_step": "research_costing",
            "completed_steps": ["research_costing"],
        }

    except Exception as e:
        logger.error(f"Costing research failed: {e}")
        return {
            "costing_data": {},
            "current_step": "research_costing",
            "errors": [f"Costing research failed: {str(e)}"],
            "completed_steps": ["research_costing (failed)"],
        }


# --- Pipeline Builder ---

def build_tender_pipeline(db: Session):
    """
    Build the 4-step tender processing pipeline using LangGraph.

    Flow: analyze → checklist → documents → costing → END
    """

    # Create node wrappers that inject db
    async def _analyze(state):
        return await analyze_documents_node(state, db)

    async def _checklist(state):
        return await generate_checklist_node(state, db)

    async def _documents(state):
        return await generate_documents_node(state, db)

    async def _costing(state):
        return await research_costing_node(state, db)

    # Conditional edge: skip document generation if no checklist
    def should_generate_docs(state):
        if state.get("checklist_items"):
            return "generate_documents"
        return "research_costing"

    # Conditional edge: skip costing if no financial requirements
    def should_do_costing(state):
        analysis = state.get("analysis_result", {})
        reqs = analysis.get("requirements", {})
        if reqs.get("financial") or reqs.get("technical_specs"):
            return "research_costing"
        return END

    graph = StateGraph(TenderPipelineState)

    # Add nodes
    graph.add_node("analyze_documents", _analyze)
    graph.add_node("generate_checklist", _checklist)
    graph.add_node("generate_documents", _documents)
    graph.add_node("research_costing", _costing)

    # Set entry point
    graph.set_entry_point("analyze_documents")

    # Add edges
    graph.add_edge("analyze_documents", "generate_checklist")
    graph.add_conditional_edges("generate_checklist", should_generate_docs, {
        "generate_documents": "generate_documents",
        "research_costing": "research_costing",
    })
    graph.add_conditional_edges("generate_documents", should_do_costing, {
        "research_costing": "research_costing",
        END: END,
    })
    graph.add_edge("research_costing", END)

    return graph.compile()


# --- Helpers ---

def _update_checklist_generation_status(
    db: Session,
    tender_id: int,
    items: list[dict],
    status: str,
    error: str = None,
):
    """Update generation_status on checklist items matching by name."""
    try:
        from app.models.checklist import ChecklistItem

        item_names = {item.get("name", "").lower() for item in items}
        db_items = db.query(ChecklistItem).filter(
            ChecklistItem.tender_id == tender_id,
        ).all()

        for db_item in db_items:
            if db_item.item_name.lower() in item_names:
                db_item.generation_status = status
                if error:
                    db_item.generation_error = error
                elif status != "failed":
                    db_item.generation_error = None

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update checklist generation status: {e}")


def _persist_checklist(db: Session, tender_id: int, items: list[dict]):
    """Save checklist items to the database with classification data."""
    try:
        from app.models.checklist import ChecklistItem

        # Don't overwrite existing uploaded items
        existing_uploaded = db.query(ChecklistItem).filter(
            ChecklistItem.tender_id == tender_id,
            ChecklistItem.is_uploaded == True,
        ).all()
        uploaded_names = {item.item_name.lower() for item in existing_uploaded}

        for i, item in enumerate(items):
            name = item.get("name", "Unknown")
            if name.lower() not in uploaded_names:
                checklist_item = ChecklistItem(
                    tender_id=tender_id,
                    item_name=name,
                    item_description=item.get("description", ""),
                    is_required=item.get("is_required", True),
                    display_order=i,
                    item_category=item.get("category", "standard"),
                    ai_instructions=item.get("ai_instructions"),
                    source_section=item.get("source_section"),
                )
                db.add(checklist_item)

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to persist checklist: {e}")
