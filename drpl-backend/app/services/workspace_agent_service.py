"""
DRPL Backend - Workspace Agent Service
Per-document agent execution with document-scoped context assembly.
Supports chat (streaming), one-shot generation, and enhancement.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, AsyncGenerator

from sqlalchemy.orm import Session

from app.models.tender import Tender
from app.models.checklist import ChecklistItem
from app.models.workspace import WorkspaceConfig, DocumentWorkspace, DocumentFormatTemplate
from app.models.agent_builder import CustomAgent
from app.models.document_analysis import DocumentExtractionResult, CriticalClauseFlag
from app.services.ai_service import call_ai, _get_effective_api_key, _get_effective_model


def _markdown_to_html(md_text: str) -> str:
    """Convert markdown to HTML for TipTap editor display."""
    try:
        import re as _re
        import markdown

        # Fix collapsed tables (newlines lost when TipTap wraps in <p>)
        fixed = _re.sub(
            r"\s*(\|[\s:]*[-:]{2,}[\s:]*(?:\|[\s:]*[-:]{2,}[\s:]*)+\|)",
            r"\n\1\n", md_text,
        )
        fixed = _re.sub(r"\|\s+\|(?!\s*[-:])", "|\n|", fixed)

        return markdown.markdown(fixed, extensions=["tables", "nl2br", "fenced_code"])
    except ImportError:
        import re
        html = md_text.replace("\n\n", "</p><p>")
        html = html.replace("\n", "<br/>")
        html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
        html = re.sub(r"^### (.+)", r"<h3>\1</h3>", html, flags=re.MULTILINE)
        html = re.sub(r"^## (.+)", r"<h2>\1</h2>", html, flags=re.MULTILINE)
        html = re.sub(r"^# (.+)", r"<h1>\1</h1>", html, flags=re.MULTILINE)
        return f"<p>{html}</p>"

logger = logging.getLogger(__name__)


def get_document_context_for_agent(db: Session, item_id: int) -> str:
    """
    Assemble the full context string for a document-scoped agent call.

    Layers:
    1. Document info: name, description, AI instructions, source section
    2. Format template: structure, rules, content skeleton
    3. Tender context: title, description, specs, eligibility
    4. Extraction results: requirements, critical clauses
    5. Cross-document: summaries of dependent documents
    """
    item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
    if not item:
        return ""

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id
    ).first()

    tender = db.query(Tender).filter(Tender.id == item.tender_id).first()

    parts = []

    # 1. Document info
    parts.append("=== DOCUMENT TO GENERATE ===")
    parts.append(f"Document Name: {item.item_name}")
    if item.item_description:
        parts.append(f"Description: {item.item_description}")
    parts.append(f"Category: {item.item_category or 'standard'}")
    if item.ai_instructions:
        parts.append(f"\nAI Instructions (from tender analysis):\n{item.ai_instructions}")
    if item.source_section:
        parts.append(f"\nSource Section in Tender: {item.source_section}")

    # 2. Format template
    if ws and ws.format_template_id:
        template = db.query(DocumentFormatTemplate).filter(
            DocumentFormatTemplate.id == ws.format_template_id
        ).first()
        if template:
            parts.append("\n=== FORMAT TEMPLATE ===")
            parts.append(f"Template: {template.name}")
            parts.append(f"Category: {template.document_category}")
            if template.content_template_markdown:
                parts.append(f"\nContent Skeleton:\n{template.content_template_markdown}")
            if template.format_rules:
                parts.append(f"\nFormat Rules:\n" + "\n".join(f"- {r}" for r in template.format_rules))
            if template.required_sections:
                parts.append(f"\nRequired Sections: {', '.join(template.required_sections)}")

    # Format instructions (free-text from user)
    if ws and ws.format_instructions:
        parts.append(f"\nUser Format Instructions:\n{ws.format_instructions}")

    # 3. Tender context
    if tender:
        parts.append("\n=== TENDER CONTEXT ===")
        parts.append(f"Tender Title: {tender.title}")
        if tender.organisation:
            parts.append(f"Organisation: {tender.organisation}")
        if tender.department:
            parts.append(f"Department: {tender.department}")
        if tender.description:
            parts.append(f"Description: {tender.description[:2000]}")
        if tender.technical_specifications:
            parts.append(f"\nTechnical Specifications:\n{tender.technical_specifications[:3000]}")
        if tender.eligibility_criteria:
            parts.append(f"\nEligibility Criteria:\n{tender.eligibility_criteria[:2000]}")

    # 4. Extraction results
    if tender:
        extractions = db.query(DocumentExtractionResult).filter(
            DocumentExtractionResult.tender_id == tender.id
        ).limit(5).all()
        if extractions:
            parts.append("\n=== EXTRACTED REQUIREMENTS ===")
            for ext in extractions:
                if ext.items:
                    items_list = ext.items if isinstance(ext.items, list) else []
                    for req in items_list[:20]:
                        text = req.get("text", "") if isinstance(req, dict) else str(req)
                        cat = req.get("category", "") if isinstance(req, dict) else ""
                        if text:
                            parts.append(f"- [{cat}] {text[:200]}")

        # Critical clauses
        flags = db.query(CriticalClauseFlag).filter(
            CriticalClauseFlag.tender_id == tender.id,
            CriticalClauseFlag.is_acknowledged == False,
        ).limit(10).all()
        if flags:
            parts.append("\n=== CRITICAL CLAUSES (must comply) ===")
            for flag in flags:
                parts.append(f"- [{flag.flag_type}] {flag.clause_text[:300]}")

    # 5. Cross-document references
    if ws and ws.depends_on:
        dep_items = db.query(ChecklistItem).filter(
            ChecklistItem.id.in_(ws.depends_on)
        ).all()
        if dep_items:
            parts.append("\n=== REFERENCED DOCUMENTS ===")
            for dep in dep_items:
                dep_ws = db.query(DocumentWorkspace).filter(
                    DocumentWorkspace.checklist_item_id == dep.id
                ).first()
                parts.append(f"\n--- {dep.item_name} ---")
                if dep_ws and dep_ws.draft_content_html:
                    # Include a truncated summary of the dependent document
                    from html import unescape
                    import re
                    text = re.sub(r'<[^>]+>', '', unescape(dep_ws.draft_content_html))
                    parts.append(text[:2000])
                elif dep.item_description:
                    parts.append(dep.item_description[:500])

    # 6. Command Center artifacts (analysis, checklist) for this tender
    if tender:
        try:
            from app.models.proposal import ProposalSession
            from app.models.artifact import CommandCenterArtifact

            # Find Command Center sessions linked to this tender
            cc_sessions = db.query(ProposalSession).filter(
                ProposalSession.tender_id == tender.id,
            ).all()
            session_ids = [s.id for s in cc_sessions]

            if session_ids:
                artifacts = db.query(CommandCenterArtifact).filter(
                    CommandCenterArtifact.session_id.in_(session_ids),
                    CommandCenterArtifact.artifact_type.in_(["analysis", "checklist"]),
                ).order_by(CommandCenterArtifact.created_at.desc()).limit(5).all()

                if artifacts:
                    parts.append("\n=== COMMAND CENTER ANALYSIS & CHECKLIST ===")
                    for art in artifacts:
                        parts.append(f"\n--- {art.title} ({art.artifact_type}) ---")
                        content = art.content or ""
                        parts.append(content[:8000])
        except Exception as e:
            logger.warning(f"Failed to load Command Center artifacts for tender {tender.id}: {e}")

    return "\n".join(parts)


def _resolve_agent(db: Session, item_id: int) -> Optional[CustomAgent]:
    """Resolve the agent for a document workspace, with fallback chain."""
    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id
    ).first()
    if not ws:
        return None

    # Priority 1: Document-specific agent
    agent_key = ws.agent_key
    # Priority 2: Checklist item agent
    if not agent_key:
        item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
        agent_key = item.agent_key if item else None
    # Priority 3: Workspace default
    if not agent_key:
        config = db.query(WorkspaceConfig).filter(
            WorkspaceConfig.tender_id == ws.tender_id
        ).first()
        agent_key = config.default_agent_key if config else None

    if agent_key:
        agent = db.query(CustomAgent).filter(
            CustomAgent.agent_key == agent_key,
            CustomAgent.is_enabled == True,
        ).first()
        if agent:
            return agent

    # Priority 4: First available document-category agent
    item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
    if item and item.item_category:
        agents = db.query(CustomAgent).filter(
            CustomAgent.is_enabled == True,
            CustomAgent.is_published == True,
        ).all()
        for a in agents:
            cats = a.document_categories or []
            if isinstance(cats, str):
                try:
                    cats = json.loads(cats)
                except:
                    cats = []
            if item.item_category in cats:
                return a

    return None


def _build_system_prompt(agent: Optional[CustomAgent], document_context: str) -> str:
    """Build the enriched system prompt combining agent base prompt + document context."""
    base = ""
    if agent and agent.system_prompt:
        base = agent.system_prompt

    if not base:
        base = (
            "You are a professional document writer specializing in Indian government tender submissions. "
            "You produce precise, formal documents that comply exactly with tender requirements. "
            "Use proper formatting, formal language, and ensure all required sections are included."
        )

    return f"""{base}

{document_context}

IMPORTANT INSTRUCTIONS:
- Generate content that strictly follows the format template if one is provided.
- Include all required sections mentioned in the format rules.
- Use formal business language appropriate for government tender submissions.
- If a content skeleton is provided, fill in every section with relevant content.
- Comply with all critical clauses and requirements extracted from the tender.
- Reference dependent documents where contextually appropriate.
"""


def _build_conversation_messages(db: Session, session_id: str, limit: int = 20) -> list[dict]:
    """Load prior conversation turns for multi-turn chat."""
    from app.services.langchain.memory_service import get_conversation_history
    turns = get_conversation_history(db, session_id, limit=limit)
    messages = []
    for turn in turns:
        messages.append({
            "role": turn.role,
            "content": turn.content,
        })
    return messages


async def execute_document_agent_chat(
    db: Session,
    item_id: int,
    user_message: str,
    user_id: int,
) -> AsyncGenerator[str, None]:
    """
    Per-document streaming chat. Yields SSE event strings.

    Events:
    - agent_start: {agent_key, agent_name}
    - token: {text} (cumulative content chunks)
    - agent_complete: {output}
    - error: {message}
    - done: {}
    """
    from app.services.langchain.memory_service import save_conversation_turn
    from app.core.config import get_settings
    import anthropic

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id
    ).first()
    if not ws:
        yield f"event: error\ndata: {json.dumps({'message': 'Document workspace not found'})}\n\n"
        return

    agent = _resolve_agent(db, item_id)
    agent_key = agent.agent_key if agent else "default-doc-writer"
    agent_name = agent.display_name if agent else "Document Writer"

    yield f"event: agent_start\ndata: {json.dumps({'agent_key': agent_key, 'agent_name': agent_name})}\n\n"

    # Save user message to conversation history
    save_conversation_turn(
        db=db,
        session_id=ws.conversation_session_id,
        agent_key=agent_key,
        role="user",
        content=user_message,
        output_type="workspace_document",
    )

    # Build context and system prompt
    document_context = get_document_context_for_agent(db, item_id)
    system_prompt = _build_system_prompt(agent, document_context)

    # Build conversation messages for multi-turn
    prior_messages = _build_conversation_messages(db, ws.conversation_session_id, limit=20)

    # Add current user message
    messages = prior_messages + [{"role": "user", "content": user_message}]

    # Get API key and model
    settings = get_settings()
    api_key = _get_effective_api_key(db) if db else settings.anthropic_api_key
    model = agent.model if agent and agent.model else "claude-sonnet-4-6"
    max_tokens = agent.max_tokens if agent and agent.max_tokens else 4096

    try:
        client = anthropic.Anthropic(api_key=api_key)

        full_response = ""
        start_time = time.time()

        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                full_response += text
                yield f"event: token\ndata: {json.dumps({'text': text})}\n\n"

        elapsed_ms = int((time.time() - start_time) * 1000)

        # Save assistant response to conversation history
        save_conversation_turn(
            db=db,
            session_id=ws.conversation_session_id,
            agent_key=agent_key,
            role="assistant",
            content=full_response,
            output_type="workspace_document",
            metadata={"latency_ms": elapsed_ms, "model": model},
        )

        yield f"event: agent_complete\ndata: {json.dumps({'output': full_response[:200]})}\n\n"
        yield f"event: done\ndata: {json.dumps({'agent_key': agent_key, 'latency_ms': elapsed_ms})}\n\n"

    except Exception as e:
        logger.error(f"Document agent chat failed for item {item_id}: {e}", exc_info=True)
        yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
        yield f"event: done\ndata: {json.dumps({'error': str(e)})}\n\n"


async def generate_document_with_agent(
    db: Session,
    item_id: int,
    user_id: int,
) -> dict:
    """One-shot full document generation via the assigned agent."""
    from app.services.langchain.memory_service import save_conversation_turn

    item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
    if not item:
        raise ValueError("Checklist item not found")

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id
    ).first()
    if not ws:
        raise ValueError("Document workspace not found")

    agent = _resolve_agent(db, item_id)
    agent_key = agent.agent_key if agent else "default-doc-writer"

    # Build context
    document_context = get_document_context_for_agent(db, item_id)
    system_prompt = _build_system_prompt(agent, document_context)

    user_prompt = (
        f"Generate the complete content for the document '{item.item_name}'. "
        f"Follow the format template exactly if one is provided. "
        f"Include all required sections. Output the content in clean **markdown** format with proper headings (#, ##, ###), "
        f"tables using pipe syntax (| col1 | col2 |), bold (**text**), and lists (- item). "
        f"Do NOT output HTML tags. Use markdown only."
    )

    # Save user turn
    save_conversation_turn(
        db=db,
        session_id=ws.conversation_session_id,
        agent_key=agent_key,
        role="user",
        content=user_prompt,
        output_type="workspace_document",
    )

    start_time = time.time()
    output_text = await call_ai(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        db=db,
        agent_name=agent_key,
    )
    elapsed_ms = int((time.time() - start_time) * 1000)

    # Save assistant turn
    save_conversation_turn(
        db=db,
        session_id=ws.conversation_session_id,
        agent_key=agent_key,
        role="assistant",
        content=output_text,
        output_type="workspace_document",
        metadata={"latency_ms": elapsed_ms, "mode": "generate"},
    )

    # Save to workspace draft — convert markdown to HTML for TipTap editor
    ws.draft_content_markdown = output_text
    ws.draft_content_html = _markdown_to_html(output_text)
    ws.content_version += 1
    ws.last_edited_at = datetime.now(timezone.utc)
    ws.last_edited_by = user_id

    if ws.review_status == "not_started":
        ws.review_status = "drafting"
        item.workspace_status = "drafting"

    # Update workspace activity
    config = db.query(WorkspaceConfig).filter(WorkspaceConfig.tender_id == ws.tender_id).first()
    if config:
        config.last_activity_at = datetime.now(timezone.utc)

    db.commit()

    return {
        "status": "generated",
        "content_html": output_text,
        "content_version": ws.content_version,
        "agent_key": agent_key,
        "latency_ms": elapsed_ms,
    }


async def enhance_document_with_agent(
    db: Session,
    item_id: int,
    enhancement_prompt: str,
    user_id: int,
) -> dict:
    """Improve existing draft based on user instructions."""
    from app.services.langchain.memory_service import save_conversation_turn

    item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
    if not item:
        raise ValueError("Checklist item not found")

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id
    ).first()
    if not ws:
        raise ValueError("Document workspace not found")

    if not ws.draft_content_html:
        raise ValueError("No existing draft to enhance. Generate content first.")

    agent = _resolve_agent(db, item_id)
    agent_key = agent.agent_key if agent else "default-doc-writer"

    # Build context with existing content
    document_context = get_document_context_for_agent(db, item_id)
    system_prompt = _build_system_prompt(agent, document_context)

    user_prompt = (
        f"Here is the current draft of '{item.item_name}':\n\n"
        f"{ws.draft_content_html}\n\n"
        f"Please enhance this document based on the following instructions:\n{enhancement_prompt}\n\n"
        f"Return the complete updated document in clean **markdown** format. Use headings (#), tables (|), bold (**), and lists (-). Do NOT use HTML tags."
    )

    # Save user turn
    save_conversation_turn(
        db=db,
        session_id=ws.conversation_session_id,
        agent_key=agent_key,
        role="user",
        content=f"[ENHANCE] {enhancement_prompt}",
        output_type="workspace_document",
    )

    start_time = time.time()
    output_text = await call_ai(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        db=db,
        agent_name=agent_key,
    )
    elapsed_ms = int((time.time() - start_time) * 1000)

    # Save assistant turn
    save_conversation_turn(
        db=db,
        session_id=ws.conversation_session_id,
        agent_key=agent_key,
        role="assistant",
        content=output_text,
        output_type="workspace_document",
        metadata={"latency_ms": elapsed_ms, "mode": "enhance"},
    )

    # Update draft — convert markdown to HTML for TipTap editor
    ws.draft_content_markdown = output_text
    ws.draft_content_html = _markdown_to_html(output_text)
    ws.content_version += 1
    ws.last_edited_at = datetime.now(timezone.utc)
    ws.last_edited_by = user_id

    config = db.query(WorkspaceConfig).filter(WorkspaceConfig.tender_id == ws.tender_id).first()
    if config:
        config.last_activity_at = datetime.now(timezone.utc)

    db.commit()

    return {
        "status": "enhanced",
        "content_html": output_text,
        "content_version": ws.content_version,
        "agent_key": agent_key,
        "latency_ms": elapsed_ms,
    }


def get_available_agents(db: Session, document_category: Optional[str] = None) -> list[dict]:
    """List agents that can handle document workspace items."""
    query = db.query(CustomAgent).filter(
        CustomAgent.is_enabled == True,
    )

    agents = query.all()
    result = []
    for a in agents:
        cats = a.document_categories or []
        if isinstance(cats, str):
            try:
                cats = json.loads(cats)
            except:
                cats = []

        # If filtering by category, only include matching agents
        if document_category and cats and document_category not in cats:
            continue

        result.append({
            "agent_key": a.agent_key,
            "display_name": a.display_name,
            "description": a.description,
            "document_categories": cats,
            "model": a.model,
            "agent_type": a.agent_type,
        })

    return result


async def auto_assign_agents(db: Session, tender_id: int) -> dict:
    """
    Use AI to auto-assign agents to unassigned workspace documents.
    Maps item names to the best matching agent based on document_categories.
    """
    workspaces = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.tender_id == tender_id,
        DocumentWorkspace.agent_key == None,
    ).all()

    if not workspaces:
        return {"assigned": 0, "message": "All documents already have agents assigned"}

    agents = db.query(CustomAgent).filter(
        CustomAgent.is_enabled == True,
    ).all()

    if not agents:
        return {"assigned": 0, "message": "No agents available"}

    assigned_count = 0
    for ws in workspaces:
        item = db.query(ChecklistItem).filter(ChecklistItem.id == ws.checklist_item_id).first()
        if not item:
            continue

        # Match by document_categories
        best_agent = None
        for a in agents:
            cats = a.document_categories or []
            if isinstance(cats, str):
                try:
                    cats = json.loads(cats)
                except:
                    cats = []
            if not cats:
                continue

            name_lower = item.item_name.lower()
            for cat in cats:
                if cat.lower() in name_lower or name_lower in cat.lower():
                    best_agent = a
                    break
            if best_agent:
                break

        if best_agent:
            ws.agent_key = best_agent.agent_key
            item.agent_key = best_agent.agent_key
            assigned_count += 1

    if assigned_count > 0:
        db.commit()

    return {"assigned": assigned_count, "total_unassigned": len(workspaces)}
