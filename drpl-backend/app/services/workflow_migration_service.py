"""
DRPL Backend - Workflow Migration Service
Creates a "Default Tender Router" workflow that matches the hardcoded
agent_router_graph.py logic, allowing gradual migration.
"""

import logging
from sqlalchemy.orm import Session

from app.models.workflow import Workflow, WorkflowNode, WorkflowEdge
from app.services.langchain.graphs.agent_router_graph import INTENT_CLASSIFIER_PROMPT

logger = logging.getLogger(__name__)

# The hardcoded agent definitions from agent_router_graph.py
AGENT_DEFINITIONS = [
    {
        "key": "deep_analyzer",
        "label": "Tender Document Analyzer",
        "intent": "analyze_documents",
        "output_type": "document_analysis",
    },
    {
        "key": "checklist_generator",
        "label": "Checklist Generator",
        "intent": "generate_checklist",
        "output_type": "checklist",
    },
    {
        "key": "proposal_creator",
        "label": "Proposal Creator",
        "intent": "write_proposal",
        "output_type": "proposal_document",
    },
    {
        "key": "costing_researcher",
        "label": "Costing Researcher",
        "intent": "estimate_costing",
        "output_type": "cost_breakdown",
    },
    {
        "key": "workspace_manager",
        "label": "Workspace Manager",
        "intent": "workspace_operations",
        "output_type": "workspace_operations",
    },
]


def create_default_tender_router(db: Session, user_id: int = None) -> Workflow:
    """
    Create a 'Default Tender Router' workflow that replicates the
    hardcoded agent_router_graph.py logic.

    Graph structure:
    [Start] → [Classify Intent] ─┬─ analyze_documents ──→ [Deep Analyzer] ──→ [End]
                                  ├─ generate_checklist ─→ [Checklist Gen]  ──→ [End]
                                  ├─ write_proposal ─────→ [Proposal Creator]→ [End]
                                  ├─ estimate_costing ───→ [Costing Research]→ [End]
                                  ├─ workspace_ops ──────→ [Workspace Mgr]  ──→ [End]
                                  └─ general_query ──────→ [General Response]→ [End]
    """
    # Check if already exists
    existing = db.query(Workflow).filter(Workflow.workflow_key == "default_tender_router").first()
    if existing:
        logger.info("Default Tender Router workflow already exists (id=%d)", existing.id)
        return existing

    # Create workflow
    workflow = Workflow(
        workflow_key="default_tender_router",
        display_name="Default Tender Router",
        description=(
            "Auto-migrated from hardcoded agent_router_graph.py. "
            "Classifies user intent and routes to specialized agents."
        ),
        category="tender_processing",
        trigger_type="command_center",
        is_default_router=False,  # Not set as default until explicitly published
        status="draft",
        created_by=user_id,
    )
    db.add(workflow)
    db.flush()
    wf_id = workflow.id

    # ─── Create Nodes ──────────────────────────────────────────

    nodes = []

    # Start node
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="start", node_type="start",
        display_name="Start", position_x=50, position_y=350, config={}, sort_order=0,
    ))

    # Classify node
    branches = [{"label": a["intent"]} for a in AGENT_DEFINITIONS]
    branches.append({"label": "general_query"})
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="classify_intent", node_type="classify",
        display_name="Classify Intent",
        position_x=300, position_y=350,
        config={
            "classification_prompt": INTENT_CLASSIFIER_PROMPT,
            "model": "claude-sonnet-4-6",
            "branches": branches,
        },
        sort_order=1,
    ))

    # Agent nodes — vertically distributed
    y_start = 50
    y_step = 120
    for i, agent in enumerate(AGENT_DEFINITIONS):
        nodes.append(WorkflowNode(
            workflow_id=wf_id,
            node_key=agent["key"],
            node_type="agent",
            display_name=agent["label"],
            position_x=600,
            position_y=y_start + i * y_step,
            config={
                "agent_key": agent["key"],
                "output_key": f"{agent['key']}_result",
            },
            sort_order=i + 2,
        ))

    # General response agent node
    general_y = y_start + len(AGENT_DEFINITIONS) * y_step
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="general_response", node_type="agent",
        display_name="General Response",
        position_x=600, position_y=general_y,
        config={"agent_key": "general_query", "output_key": "general_result"},
        sort_order=len(AGENT_DEFINITIONS) + 2,
    ))

    # End node
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="end", node_type="end",
        display_name="End", position_x=900, position_y=350, config={},
        sort_order=len(AGENT_DEFINITIONS) + 3,
    ))

    db.add_all(nodes)

    # ─── Create Edges ──────────────────────────────────────────

    edges = []

    # Start → Classify
    edges.append(WorkflowEdge(
        workflow_id=wf_id, source_node_key="start",
        target_node_key="classify_intent", sort_order=0,
    ))

    # Classify → each agent (by intent branch)
    for i, agent in enumerate(AGENT_DEFINITIONS):
        edges.append(WorkflowEdge(
            workflow_id=wf_id, source_node_key="classify_intent",
            target_node_key=agent["key"],
            source_handle=agent["intent"],
            label=agent["intent"],
            sort_order=i + 1,
        ))

    # Classify → general response
    edges.append(WorkflowEdge(
        workflow_id=wf_id, source_node_key="classify_intent",
        target_node_key="general_response",
        source_handle="general_query",
        label="general_query",
        sort_order=len(AGENT_DEFINITIONS) + 1,
    ))

    # Each agent → End
    for i, agent in enumerate(AGENT_DEFINITIONS):
        edges.append(WorkflowEdge(
            workflow_id=wf_id, source_node_key=agent["key"],
            target_node_key="end",
            sort_order=len(AGENT_DEFINITIONS) + 2 + i,
        ))

    # General → End
    edges.append(WorkflowEdge(
        workflow_id=wf_id, source_node_key="general_response",
        target_node_key="end",
        sort_order=len(AGENT_DEFINITIONS) * 2 + 2,
    ))

    db.add_all(edges)
    db.commit()
    db.refresh(workflow)

    logger.info(
        "Created Default Tender Router workflow (id=%d) with %d nodes and %d edges",
        workflow.id, len(nodes), len(edges),
    )
    return workflow


# ─── GEM Tender Processing Workflow ────────────────────────────────────────


def create_gem_tender_processing_workflow(db: Session, user_id: int = None) -> Workflow:
    """
    Create a GEM Tender Processing workflow that autonomously:
    1. Analyzes the main tender document
    2. Downloads all linked GEM documents + PDF hyperlinks (in parallel)
    3. Extracts text and deep-analyzes each document (parallel for_each)
    4. Aggregates into a comprehensive summary
    5. Pauses for human review

    Graph:
    [Start] → [Initial Analysis] → [Parallel Download] → [Merge Docs]
    → [If Has Docs?] → true: [For Each Analyze] → [Aggregation] → [Review] → [End]
                      → false: [Aggregation] → [Review] → [End]
    """
    existing = db.query(Workflow).filter(Workflow.workflow_key == "gem_tender_processing").first()
    if existing:
        logger.info("GEM Tender Processing workflow already exists (id=%d)", existing.id)
        return existing

    workflow = Workflow(
        workflow_key="gem_tender_processing",
        display_name="GEM Tender Processing Pipeline",
        description=(
            "Autonomous end-to-end GEM tender analysis: analyzes the main document, "
            "downloads all linked child documents, deep-analyzes each in parallel, "
            "and produces a comprehensive summary with risk matrix and submission checklist."
        ),
        category="tender_processing",
        trigger_type="command_center",
        is_default_router=False,
        status="draft",
        created_by=user_id,
    )
    db.add(workflow)
    db.flush()
    wf_id = workflow.id

    # ─── Nodes ─────────────────────────────────────────────────

    nodes = []

    # 1. Start
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="start", node_type="start",
        display_name="Start", position_x=50, position_y=300, config={}, sort_order=0,
    ))

    # 2. Initial Analysis
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="initial_analysis", node_type="agent",
        display_name="Initial Tender Analysis",
        position_x=250, position_y=300,
        config={
            "agent_key": "deep_analyzer",
            "input_mapping": {
                "message": "state.__user_message",
                "tender_id": "state.__tender_id",
            },
            "output_key": "initial_analysis_result",
        },
        sort_order=1,
    ))

    # 3. Parallel Download (downloads GEM files + PDF links concurrently)
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="parallel_download", node_type="parallel",
        display_name="Download Documents",
        position_x=500, position_y=300,
        config={
            "branches": [
                {"label": "gem_download", "target_node_key": "extract_gem_ids"},
                {"label": "pdf_links", "target_node_key": "extract_pdf_links"},
            ],
            "merge_strategy": "dict",
            "output_key": "download_results",
            "continue_on_error": True,
            "timeout_seconds": 120,
        },
        sort_order=2,
    ))

    # 3a. GEM Download tool (branch 1 of parallel)
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="extract_gem_ids", node_type="tool",
        display_name="GEM File Download",
        position_x=500, position_y=150,
        config={
            "tool_key": "gem_download",
            "input_mapping": {
                "analysis_text": "state.initial_analysis_result.output",
                "tender_id": "state.__tender_id",
            },
            "output_key": "gem_docs",
        },
        sort_order=3,
    ))

    # 3b. PDF Link Extract tool (branch 2 of parallel)
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="extract_pdf_links", node_type="tool",
        display_name="PDF Link Download",
        position_x=500, position_y=450,
        config={
            "tool_key": "pdf_link_extract",
            "input_mapping": {
                "tender_id": "state.__tender_id",
            },
            "output_key": "link_docs",
        },
        sort_order=4,
    ))

    # 4. Merge downloaded documents — flatten parallel results into a single doc list
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="merge_documents", node_type="transform",
        display_name="Merge Documents",
        position_x=750, position_y=300,
        config={
            "transform_type": "flatten_documents",
            "source_path": "state.download_results",
            "output_key": "all_documents",
        },
        sort_order=5,
    ))

    # 5. Check if we have documents
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="has_documents", node_type="if_else",
        display_name="Has Documents?",
        position_x=950, position_y=300,
        config={
            "condition_expression": "len(state.all_documents) > 0" if False else "True",
            "true_label": "Yes",
            "false_label": "No",
        },
        sort_order=6,
    ))

    # 6. For Each: Analyze each document (parallel)
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="analyze_each_doc", node_type="for_each",
        display_name="Analyze Each Document",
        position_x=1150, position_y=200,
        config={
            "collection_expression": "state.all_documents",
            "item_variable": "current_document",
            "index_variable": "current_doc_index",
            "output_key": "document_analyses",
            "parallel": True,
            "concurrency_limit": 3,
            "continue_on_error": True,
            "max_iterations": 20,
        },
        sort_order=7,
    ))

    # 6a. Extract text from current document (for_each body)
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="extract_text", node_type="tool",
        display_name="Extract Text",
        position_x=1150, position_y=50,
        config={
            "tool_key": "text_extract",
            "input_mapping": {
                "file_path": "state.current_document.file_path",
            },
            "output_key": "extracted_text",
        },
        sort_order=8,
    ))

    # 6b. Deep analyze the extracted text (for_each body)
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="deep_analyze_doc", node_type="agent",
        display_name="Deep Document Analysis",
        position_x=1350, position_y=50,
        config={
            "agent_key": "deep_analyzer",
            "input_mapping": {
                "message": "state.extracted_text.text",
                "tender_id": "state.__tender_id",
            },
            "output_key": "individual_analysis",
        },
        sort_order=9,
    ))

    # 7. Aggregation agent
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="aggregate_analysis", node_type="agent",
        display_name="Aggregate & Summarize",
        position_x=1400, position_y=300,
        config={
            "agent_key": "deep_analyzer",
            "input_mapping": {
                "message": "state.document_analyses",
                "tender_id": "state.__tender_id",
            },
            "output_key": "final_analysis",
        },
        sort_order=10,
    ))

    # 8. Human review
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="human_review", node_type="user_approval",
        display_name="Review Analysis",
        position_x=1600, position_y=300,
        config={
            "prompt_template": "Tender analysis is complete. Please review the comprehensive analysis and approve to finalize.",
            "timeout_seconds": 7200,
        },
        sort_order=11,
    ))

    # 9. End nodes
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="end_complete", node_type="end",
        display_name="Complete", position_x=1800, position_y=250, config={}, sort_order=12,
    ))
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="end_rejected", node_type="end",
        display_name="Rejected", position_x=1800, position_y=400, config={}, sort_order=13,
    ))

    db.add_all(nodes)

    # ─── Edges ─────────────────────────────────────────────────

    edges = []
    s = 0

    def edge(src, tgt, handle=None, label=None):
        nonlocal s
        edges.append(WorkflowEdge(
            workflow_id=wf_id, source_node_key=src, target_node_key=tgt,
            source_handle=handle, label=label, sort_order=s,
        ))
        s += 1

    edge("start", "initial_analysis")
    edge("initial_analysis", "parallel_download")
    # Visual edges: parallel → branches → next step (shows clear data flow)
    edge("parallel_download", "extract_gem_ids", "gem_download", "GEM Download")
    edge("parallel_download", "extract_pdf_links", "pdf_links", "PDF Links")
    edge("extract_gem_ids", "parallel_download", None, "GEM Done")
    edge("extract_pdf_links", "parallel_download", None, "Links Done")
    edge("parallel_download", "merge_documents", "done", "Done")
    edge("merge_documents", "has_documents")
    edge("has_documents", "analyze_each_doc", "true", "Yes")
    edge("has_documents", "aggregate_analysis", "false", "No")
    edge("analyze_each_doc", "extract_text", "body", "Body")
    edge("extract_text", "deep_analyze_doc")
    edge("deep_analyze_doc", "analyze_each_doc")  # return to for_each
    edge("analyze_each_doc", "aggregate_analysis", "done", "Done")
    edge("aggregate_analysis", "human_review")
    edge("human_review", "end_complete", "approved", "Approved")
    edge("human_review", "end_rejected", "rejected", "Rejected")

    db.add_all(edges)
    db.commit()
    db.refresh(workflow)

    logger.info(
        "Created GEM Tender Processing workflow (id=%d) with %d nodes and %d edges",
        workflow.id, len(nodes), len(edges),
    )
    return workflow


# ─── Tender Checklist Pipeline ─────────────────────────────────────────


def create_checklist_workflow(db: Session, user_id: int = None) -> Workflow:
    """
    Create a Tender Checklist Pipeline workflow.

    [Start] → [Analyze Tender] → [Generate Checklist] → [Review] → [End]
    """
    existing = db.query(Workflow).filter(Workflow.workflow_key == "tender_checklist_pipeline").first()
    if existing:
        logger.info("Tender Checklist Pipeline already exists (id=%d)", existing.id)
        return existing

    workflow = Workflow(
        workflow_key="tender_checklist_pipeline",
        display_name="Tender Checklist Pipeline",
        description=(
            "Analyzes a tender document and generates a structured submission "
            "checklist with all required documents, formats, and deadlines."
        ),
        category="tender_processing",
        trigger_type="command_center",
        status="draft",
        created_by=user_id,
    )
    db.add(workflow)
    db.flush()
    wf_id = workflow.id

    nodes = []

    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="start", node_type="start",
        display_name="Start", position_x=50, position_y=250, config={}, sort_order=0,
    ))

    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="analyze_tender", node_type="agent",
        display_name="Analyze Tender",
        position_x=250, position_y=250,
        config={
            "agent_key": "deep_analyzer",
            "input_mapping": {"message": "state.__user_message"},
            "output_key": "tender_analysis",
        },
        sort_order=1,
    ))

    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="generate_checklist", node_type="agent",
        display_name="Generate Checklist",
        position_x=500, position_y=250,
        config={
            "agent_key": "checklist_generator",
            "input_mapping": {
                "message": "state.tender_analysis.output",
            },
            "output_key": "checklist_result",
        },
        sort_order=2,
    ))

    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="review_checklist", node_type="user_approval",
        display_name="Review Checklist",
        position_x=750, position_y=250,
        config={
            "prompt_template": "Tender checklist has been generated. Please review before finalizing.",
            "timeout_seconds": 7200,
        },
        sort_order=3,
    ))

    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="end_approved", node_type="end",
        display_name="Complete", position_x=1000, position_y=200, config={}, sort_order=4,
    ))
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="end_rejected", node_type="end",
        display_name="Rejected", position_x=1000, position_y=350, config={}, sort_order=5,
    ))

    db.add_all(nodes)

    edges = []
    s = 0

    def edge(src, tgt, handle=None, label=None):
        nonlocal s
        edges.append(WorkflowEdge(
            workflow_id=wf_id, source_node_key=src, target_node_key=tgt,
            source_handle=handle, label=label, sort_order=s,
        ))
        s += 1

    edge("start", "analyze_tender")
    edge("analyze_tender", "generate_checklist")
    edge("generate_checklist", "review_checklist")
    edge("review_checklist", "end_approved", "approved", "Approved")
    edge("review_checklist", "end_rejected", "rejected", "Rejected")

    db.add_all(edges)
    db.commit()
    db.refresh(workflow)

    logger.info("Created Tender Checklist Pipeline (id=%d) with %d nodes, %d edges",
                workflow.id, len(nodes), len(edges))
    return workflow


# ─── Costing Analysis Pipeline ─────────────────────────────────────────


def create_costing_workflow(db: Session, user_id: int = None) -> Workflow:
    """
    Create a Costing Analysis Pipeline workflow.

    [Start] → [Analyze Tender] → [Parallel: Research]
                                    ├── [Web Search Rates]
                                    └── [Training Data Lookup]
                                  → [Generate Cost Breakdown] → [Review] → [End]
    """
    existing = db.query(Workflow).filter(Workflow.workflow_key == "costing_analysis_pipeline").first()
    if existing:
        logger.info("Costing Analysis Pipeline already exists (id=%d)", existing.id)
        return existing

    workflow = Workflow(
        workflow_key="costing_analysis_pipeline",
        display_name="Costing Analysis Pipeline",
        description=(
            "Analyzes tender requirements, researches current market rates via "
            "web search and training data (in parallel), then generates a "
            "detailed cost breakdown with GST calculations."
        ),
        category="tender_processing",
        trigger_type="command_center",
        status="draft",
        created_by=user_id,
    )
    db.add(workflow)
    db.flush()
    wf_id = workflow.id

    nodes = []

    # Start
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="start", node_type="start",
        display_name="Start", position_x=50, position_y=300, config={}, sort_order=0,
    ))

    # Analyze tender for requirements
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="analyze_requirements", node_type="agent",
        display_name="Analyze Requirements",
        position_x=250, position_y=300,
        config={
            "agent_key": "deep_analyzer",
            "input_mapping": {"message": "state.__user_message"},
            "output_key": "requirements_analysis",
        },
        sort_order=1,
    ))

    # Parallel cost research
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="parallel_research", node_type="parallel",
        display_name="Research Costs",
        position_x=500, position_y=300,
        config={
            "branches": [
                {"label": "web_search", "target_node_key": "web_search_rates"},
                {"label": "training_data", "target_node_key": "training_lookup"},
            ],
            "merge_strategy": "dict",
            "output_key": "cost_research",
            "continue_on_error": True,
            "timeout_seconds": 120,
        },
        sort_order=2,
    ))

    # Branch 1: Web search for current market rates
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="web_search_rates", node_type="agent",
        display_name="Web Search Rates",
        position_x=500, position_y=120,
        config={
            "agent_key": "costing_researcher",
            "input_mapping": {
                "message": "state.requirements_analysis.output",
            },
            "output_key": "web_rates",
        },
        sort_order=3,
    ))

    # Branch 2: Training data / historical rate lookup
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="training_lookup", node_type="agent",
        display_name="Historical Rate Lookup",
        position_x=500, position_y=480,
        config={
            "agent_key": "costing_researcher",
            "input_mapping": {
                "message": "state.requirements_analysis.output",
            },
            "output_key": "historical_rates",
        },
        sort_order=4,
    ))

    # Generate cost breakdown
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="generate_costing", node_type="agent",
        display_name="Generate Cost Breakdown",
        position_x=800, position_y=300,
        config={
            "agent_key": "costing_researcher",
            "input_mapping": {
                "message": "state.cost_research",
            },
            "output_key": "cost_breakdown",
        },
        sort_order=5,
    ))

    # Review
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="review_costing", node_type="user_approval",
        display_name="Review Costing",
        position_x=1050, position_y=300,
        config={
            "prompt_template": "Cost breakdown is ready. Review the estimates before finalizing.",
            "timeout_seconds": 7200,
        },
        sort_order=6,
    ))

    # End nodes
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="end_approved", node_type="end",
        display_name="Complete", position_x=1300, position_y=250, config={}, sort_order=7,
    ))
    nodes.append(WorkflowNode(
        workflow_id=wf_id, node_key="end_rejected", node_type="end",
        display_name="Rejected", position_x=1300, position_y=400, config={}, sort_order=8,
    ))

    db.add_all(nodes)

    edges = []
    s = 0

    def edge(src, tgt, handle=None, label=None):
        nonlocal s
        edges.append(WorkflowEdge(
            workflow_id=wf_id, source_node_key=src, target_node_key=tgt,
            source_handle=handle, label=label, sort_order=s,
        ))
        s += 1

    edge("start", "analyze_requirements")
    edge("analyze_requirements", "parallel_research")
    edge("parallel_research", "web_search_rates", "web_search", "Web Search")
    edge("parallel_research", "training_lookup", "training_data", "Training Data")
    edge("web_search_rates", "parallel_research", None, "Rates Done")
    edge("training_lookup", "parallel_research", None, "Data Done")
    edge("parallel_research", "generate_costing", "done", "Done")
    edge("generate_costing", "review_costing")
    edge("review_costing", "end_approved", "approved", "Approved")
    edge("review_costing", "end_rejected", "rejected", "Rejected")

    db.add_all(edges)
    db.commit()
    db.refresh(workflow)

    logger.info("Created Costing Analysis Pipeline (id=%d) with %d nodes, %d edges",
                workflow.id, len(nodes), len(edges))
    return workflow
