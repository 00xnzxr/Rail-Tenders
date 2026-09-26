"""
DRPL Backend - Proposal Agent
Advanced streaming chat-based AI agent for proposal creation.
Features: multi-step reasoning, structured output, context-aware responses,
runtime model configuration, enhanced system prompts.
"""

import json
import logging
import time
from typing import AsyncGenerator, Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tender import Tender, TenderDocument
from app.models.checklist import ChecklistItem
from app.models.proposal import ProposalSession, ProposalMessage
from app.models.agent_config import AgentConfig
from app.services.ai_service import _build_tender_context
from app.services.rag_service import retrieve_context
from app.services.redaction_service import redact_tender_context
from app.services.document_parser import extract_text_from_file
from app.services.template_parser import get_template_prompt_context
from app.models.proposal_template import ProposalTemplate

logger = logging.getLogger(__name__)
settings = get_settings()


# --- Agent Configuration ---

def _get_agent_runtime_config(db: Session) -> dict:
    """Get the proposal agent's runtime configuration from DB."""
    config = db.query(AgentConfig).filter(AgentConfig.agent_name == "proposal").first()
    if config:
        return {
            "model": config.ai_model or _get_platform_model(db),
            "provider": config.ai_provider or _get_platform_provider(db),
            "temperature": config.temperature or 0.8,
            "max_tokens": config.max_tokens or 4096,
            "system_prompt_override": config.system_prompt_override,
            "is_enabled": config.is_enabled,
        }
    return {
        "model": _get_platform_model(db),
        "provider": _get_platform_provider(db),
        "temperature": 0.8,
        "max_tokens": 4096,
        "system_prompt_override": None,
        "is_enabled": True,
    }


def _get_platform_model(db: Session) -> str:
    """Get AI model from platform settings, fallback to .env."""
    from app.models.platform_setting import PlatformSetting
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "ai_model").first()
    if setting:
        return setting.value
    return settings.ai_model


def _get_platform_provider(db: Session) -> str:
    """Get AI provider from platform settings, fallback to .env."""
    from app.models.platform_setting import PlatformSetting
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "ai_provider").first()
    if setting:
        return setting.value
    return settings.ai_provider


def _get_api_key(db: Session) -> str:
    """Get AI API key — check platform settings first (master admin can set it), then .env."""
    from app.models.platform_setting import PlatformSetting
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "anthropic_api_key").first()
    if setting and setting.value and setting.value != "••••••••":
        return setting.value
    # Fallback to .env
    return settings.anthropic_api_key


# --- System Prompt Construction ---

AGENT_PERSONA = """You are **DRPL Proposal Agent** — an expert AI assistant specialized in creating
winning government tender proposals for DRPL Manufacturing, a company specializing in mechanical
and electrical engineering for Indian Railways and government infrastructure.

## YOUR CAPABILITIES
- **Deep domain expertise** in Indian Railways procurement (IREPS, GeM, CPPP portals)
- **Proposal structure mastery** — you know the exact format, sections, and language government evaluators expect
- **Technical writing** — clear, precise, formal language suitable for government tender submissions
- **Compliance awareness** — you understand eligibility criteria, EMD requirements, document checklists, and tender conditions
- **Strategic thinking** — you identify competitive advantages, highlight relevant past experience, and craft persuasive narratives
- **RDSO standards knowledge** — familiar with Research Designs & Standards Organization specifications and approvals
- **CVC compliance** — aware of Central Vigilance Commission guidelines for government procurement integrity
- **Make in India / MSME** — understands Make in India certification requirements and MSME vendor preferences in government tenders
- **GeM expertise** — familiar with GeM pool, catalog purchasing, and bid/RA processes on the Government e-Marketplace
- **Railways domain** — knowledgeable about WAP/WAG locomotive parts, ICF/LHB coach components, S&T (signaling & telecom) equipment, track materials, and overhead equipment

## YOUR BEHAVIOR
1. **Be proactive**: Don't just answer — guide the user through the proposal process step by step
2. **Ask smart questions**: When requirements are unclear, ask specific clarifying questions before proceeding
3. **Structure your output**: Use proper headings, bullet points, numbered lists, and tables for readability
4. **Show your reasoning**: When making recommendations, briefly explain WHY (e.g., "I recommend emphasizing your ISO certification because evaluators weight quality systems at 15%")
5. **Be thorough but concise**: Cover all required points without unnecessary filler text
6. **Use markdown formatting**: Format responses with headers, bold, lists, tables, and code blocks for document sections
7. **Maintain context**: Remember all previous messages in the conversation and build upon them
8. **Signal section completion**: When a proposal section is complete, clearly state it and suggest the next section to work on

## PROPOSAL TEMPLATE (Standard Government Tender Format)
When creating a full proposal, follow this structure:
1. **Cover Letter** — Formal letter of intent addressed to the procuring authority
2. **Executive Summary** — 1-page overview of the offer, key strengths, and value proposition
3. **Company Profile** — History, capabilities, certifications (ISO, BIS, RDSO), manufacturing facilities
4. **Technical Approach** — Detailed technical methodology, specifications compliance, quality measures
5. **Work Methodology** — Step-by-step implementation plan, resource allocation, project management approach
6. **Past Experience** — Relevant completed projects with client names, order values, and completion dates
7. **Resource Deployment** — Team structure, key personnel qualifications, equipment and tooling
8. **Quality Assurance** — QA/QC plan, testing protocols, inspection stages, certification compliance
9. **Timeline & Milestones** — Gantt chart or milestone table, delivery schedule, critical path
10. **Commercial Terms** — Pricing structure, payment terms, warranty conditions, AMC provisions
11. **Compliance & Declarations** — Tender condition compliance matrix, integrity pact, no-deviation statement

## FORMATTING RULES
- Use `##` headers for proposal sections
- Use tables for comparison data, timelines, and compliance matrices
- Use bold for key terms and requirements
- Use bullet points for lists of features, capabilities, and requirements
- Wrap document sections in clear section markers so the user can easily copy them"""


def _build_tender_context_enhanced(db: Session, tender: Tender) -> str:
    """Build rich context from tender data."""
    ctx = redact_tender_context(_build_tender_context(tender))

    # Checklist status
    checklist_items = db.query(ChecklistItem).filter(ChecklistItem.tender_id == tender.id).all()
    if checklist_items:
        total = len(checklist_items)
        uploaded = sum(1 for i in checklist_items if i.is_uploaded)
        items_list = "\n".join(
            f"  {'✅' if i.is_uploaded else '⬜'} {i.item_name}{' (required)' if i.is_required else ''}"
            for i in checklist_items
        )
        ctx += f"\n\n## DOCUMENT CHECKLIST ({uploaded}/{total} complete)\n{items_list}"

    # Uploaded document content (expanded context)
    docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).limit(8).all()
    doc_texts = []
    for doc in docs:
        text = extract_text_from_file(doc.file_path)
        if text:
            doc_texts.append(f"### Document: {doc.file_name}\n{text[:3000]}")
    if doc_texts:
        ctx += f"\n\n## UPLOADED TENDER DOCUMENTS\n" + "\n\n".join(doc_texts)

    return ctx


def _build_template_context(db: Session, template_id: Optional[int]) -> str:
    """Build template structure context for the system prompt."""
    if not template_id:
        return ""
    try:
        template = db.query(ProposalTemplate).filter(ProposalTemplate.id == template_id).first()
        if template:
            ctx = get_template_prompt_context(template)
            if ctx:
                return f"\n\n## ACTIVE PROPOSAL TEMPLATE\n{ctx}\n\nIMPORTANT: Follow this template structure exactly when generating proposal sections."
    except Exception as e:
        logger.warning(f"Failed to load template context for template_id={template_id}: {e}")
    return ""


def _build_system_prompt(db: Session, session: ProposalSession) -> str:
    """Build the system prompt for a tender-linked proposal agent."""
    config = _get_agent_runtime_config(db)

    # If master admin has set a custom system prompt, use it
    if config.get("system_prompt_override"):
        return config["system_prompt_override"]

    tender = db.query(Tender).filter(Tender.id == session.tender_id).first()
    if not tender:
        return AGENT_PERSONA

    tender_context = _build_tender_context_enhanced(db, tender)

    # RAG context
    rag_query = f"{tender.title} {tender.department or ''} {tender.description or ''}"
    rag_context = retrieve_context(rag_query, top_k=5)

    # Template context
    template_id = int(session.template_id) if session.template_id else None
    template_context = _build_template_context(db, template_id)

    # Conversation stage detection
    msg_count = db.query(ProposalMessage).filter(
        ProposalMessage.session_id == session.id,
        ProposalMessage.role == "user",
    ).count()

    stage_hint = ""
    if msg_count == 0:
        stage_hint = """
## FIRST INTERACTION
This is the start of the conversation. Introduce yourself, summarize the tender briefly,
identify the key requirements, and suggest a plan for creating the proposal. Ask the user
which section they'd like to start with, or offer to create a full outline first."""
    elif msg_count < 5:
        stage_hint = "\n## CONVERSATION STAGE: Early — focus on understanding requirements and creating an outline."
    elif msg_count < 15:
        stage_hint = "\n## CONVERSATION STAGE: Active drafting — help write detailed sections."
    else:
        stage_hint = "\n## CONVERSATION STAGE: Advanced — focus on refinement, review, and finalization."

    system_prompt = f"""{AGENT_PERSONA}

---

## ACTIVE TENDER CONTEXT
{tender_context}

{f"## REFERENCE MATERIAL FROM PAST PROPOSALS{chr(10)}{rag_context}" if rag_context else ""}
{template_context}
{stage_hint}"""

    return system_prompt


def _build_standalone_system_prompt(db: Session, session: ProposalSession) -> str:
    """Build system prompt for a standalone proposal session."""
    config = _get_agent_runtime_config(db)
    if config.get("system_prompt_override"):
        return config["system_prompt_override"]

    title = session.title or "Untitled Proposal"
    context = session.context_data or {}
    context_text = ""
    if isinstance(context, dict):
        for k, v in context.items():
            if v:
                context_text += f"\n- **{k}**: {v}"

    rag_context = retrieve_context(f"{title} {context_text}", top_k=5)

    msg_count = db.query(ProposalMessage).filter(
        ProposalMessage.session_id == session.id,
        ProposalMessage.role == "user",
    ).count()

    stage_hint = ""
    if msg_count == 0:
        stage_hint = """
## FIRST INTERACTION
Greet the user, acknowledge the proposal topic, and ask clarifying questions about:
- Target audience / procuring authority
- Key requirements or specifications
- Budget constraints
- Timeline / deadlines
- Any specific sections they want to prioritize"""

    # Template context
    template_id = int(session.template_id) if session.template_id else None
    template_context = _build_template_context(db, template_id)

    system_prompt = f"""{AGENT_PERSONA}

---

## PROPOSAL CONTEXT
**Title:** {title}
{f"**Additional Context:**{context_text}" if context_text else "No additional context provided — ask the user for details."}

{f"## REFERENCE MATERIAL FROM PAST PROPOSALS{chr(10)}{rag_context}" if rag_context else ""}
{template_context}
{stage_hint}"""

    return system_prompt


# --- Streaming Chat ---

async def stream_chat_response(
    db: Session,
    session: ProposalSession,
    user_message: str,
) -> AsyncGenerator[str, None]:
    """Stream a chat response from the proposal agent using runtime configuration."""

    # Get runtime config
    config = _get_agent_runtime_config(db)

    if not config["is_enabled"]:
        yield "The Proposal Agent is currently disabled by the administrator. Please contact your master admin to re-enable it."
        return

    # Get API key from platform settings or .env
    api_key = _get_api_key(db)
    if not api_key:
        yield "Error: No AI API key configured. The master admin needs to add the Anthropic API key in Platform Settings → AI."
        return

    # Build system prompt based on session type
    if session.agent_type == "standalone":
        system_prompt = _build_standalone_system_prompt(db, session)
    else:
        system_prompt = _build_system_prompt(db, session)

    # Build message history with enhanced context
    messages_db = db.query(ProposalMessage).filter(
        ProposalMessage.session_id == session.id,
    ).order_by(ProposalMessage.created_at).all()

    messages = []
    for msg in messages_db:
        if msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": msg.content})

    # Add current user message
    messages.append({"role": "user", "content": user_message})

    # Keep recent messages but ensure we have enough context
    max_messages = 30  # Increased from 20 for better conversation memory
    if len(messages) > max_messages:
        # Always keep the first message pair (establishes context)
        first_pair = messages[:2] if len(messages) >= 2 else messages[:1]
        recent = messages[-(max_messages - len(first_pair)):]
        messages = first_pair + recent

    model = config["model"]
    temperature = config["temperature"]
    max_tokens = config["max_tokens"]

    start_time = time.time()

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system": system_prompt,
                    "messages": messages,
                    "stream": True,
                },
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    logger.error(f"Anthropic API error: {response.status_code} {error_body}")
                    if response.status_code == 401:
                        yield "Error: Invalid API key. The master admin should verify the Anthropic API key in Platform Settings."
                    elif response.status_code == 429:
                        yield "Error: Rate limit exceeded. Please wait a moment and try again."
                    else:
                        yield f"Error: AI service returned status {response.status_code}. Please try again."
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        if data.get("type") == "content_block_delta":
                            delta = data.get("delta", {})
                            text = delta.get("text", "")
                            if text:
                                yield text
                        # Log usage from the final message_delta event
                        elif data.get("type") == "message_delta":
                            usage = data.get("usage", {})
                            if usage:
                                _log_api_usage(
                                    db, model, usage,
                                    time.time() - start_time,
                                    session.created_by,
                                )
                    except json.JSONDecodeError:
                        continue

    except httpx.TimeoutException:
        yield "Error: Request timed out. The AI service is taking too long to respond. Please try a shorter message."
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        yield f"Error: {str(e)}"


def _log_api_usage(db: Session, model: str, usage: dict, elapsed_seconds: float, user_id: int):
    """Log API usage on a fresh SessionLocal — never poisons the caller's session.

    The `db` arg is kept for signature compatibility but is intentionally unused;
    using the request session here can fail after long LLM calls when Neon has
    dropped the connection.
    """
    from app.core.database import SessionLocal
    from app.models.api_usage import APIUsageLog
    fresh = SessionLocal()
    try:
        log = APIUsageLog(
            provider="anthropic",
            model=model,
            agent_name="proposal",
            tokens_input=usage.get("input_tokens", 0),
            tokens_output=usage.get("output_tokens", 0),
            cost_estimate=_estimate_cost(model, usage),
            user_id=user_id,
            success=True,
            response_time_ms=int(elapsed_seconds * 1000),
        )
        fresh.add(log)
        fresh.commit()
    except Exception as e:
        logger.warning(f"_log_api_usage: failed: {type(e).__name__}: {e}")
        try:
            fresh.rollback()
        except Exception:
            pass
    finally:
        try:
            fresh.close()
        except Exception:
            pass


def _estimate_cost(model: str, usage: dict) -> float:
    """Estimate API cost in USD based on model and token counts."""
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    # Use the shared table rather than a second copy. This one had drifted —
    # it still priced Opus at $15/$75 and Haiku at $0.80/$4.00 long after both
    # changed, so every cost figure it produced was wrong.
    from app.services.langchain.provider_config import PRICING

    rates = PRICING.get(model)
    if rates is None:
        # Unknown model: fall back to the current worker-tier rate rather than
        # silently reporting zero cost.
        rates = PRICING.get("claude-sonnet-5", {"input": 2.0, "output": 10.0})
    cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000
    return round(cost, 6)


# --- Structured Proposal Generation ---

async def generate_structured_proposal(
    db: Session,
    session: ProposalSession,
    template_id: int,
) -> AsyncGenerator[str, None]:
    """
    One-shot structured proposal generation using a template.
    Streams the full proposal as SSE chunks based on the template structure,
    training data, and tender context.
    """
    config = _get_agent_runtime_config(db)

    if not config["is_enabled"]:
        yield "The Proposal Agent is currently disabled by the administrator."
        return

    api_key = _get_api_key(db)
    if not api_key:
        yield "Error: No AI API key configured."
        return

    # Build context
    template_context = _build_template_context(db, template_id)

    if not template_context:
        yield "Error: Template not found or has no parsed structure."
        return

    # Build tender context if session is linked to a tender
    tender_context = ""
    if session.tender_id:
        tender = db.query(Tender).filter(Tender.id == session.tender_id).first()
        if tender:
            tender_context = f"\n\n## ACTIVE TENDER CONTEXT\n{_build_tender_context_enhanced(db, tender)}"

    # RAG context
    rag_context = ""
    if session.tender_id:
        tender = db.query(Tender).filter(Tender.id == session.tender_id).first()
        if tender:
            rag_query = f"{tender.title} {tender.department or ''} {tender.description or ''}"
            rag_text = retrieve_context(rag_query, top_k=5)
            if rag_text:
                rag_context = f"\n\n## REFERENCE MATERIAL\n{rag_text}"

    system_prompt = f"""{AGENT_PERSONA}

---
{tender_context}
{template_context}
{rag_context}

## GENERATION INSTRUCTIONS
You are generating a COMPLETE proposal document following the template structure above.
Write each section in full, with proper formatting, using all available tender context.
Use markdown formatting with ## headers for each section.
Be thorough, professional, and write in formal government tender proposal language.
If specific data is not available, use reasonable placeholders marked with [TO BE FILLED]."""

    user_message = "Generate the complete proposal document following the template structure. Write every section in full detail."

    # Include any prior conversation context
    messages_db = db.query(ProposalMessage).filter(
        ProposalMessage.session_id == session.id,
    ).order_by(ProposalMessage.created_at).all()

    messages = []
    for msg in messages_db[-10:]:  # Last 10 messages for context
        if msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": msg.content})

    messages.append({"role": "user", "content": user_message})

    model = config["model"]
    temperature = config["temperature"]
    max_tokens = min(config["max_tokens"] * 2, 8192)  # Allow more tokens for full generation

    start_time = time.time()

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system": system_prompt,
                    "messages": messages,
                    "stream": True,
                },
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    logger.error(f"Anthropic API error in generation: {response.status_code} {error_body}")
                    yield f"Error: AI service returned status {response.status_code}."
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        if data.get("type") == "content_block_delta":
                            delta = data.get("delta", {})
                            text = delta.get("text", "")
                            if text:
                                yield text
                        elif data.get("type") == "message_delta":
                            usage = data.get("usage", {})
                            if usage:
                                _log_api_usage(
                                    db, model, usage,
                                    time.time() - start_time,
                                    session.created_by,
                                )
                    except json.JSONDecodeError:
                        continue

    except httpx.TimeoutException:
        yield "Error: Request timed out during proposal generation."
    except Exception as e:
        logger.error(f"Structured generation error: {e}")
        yield f"Error: {str(e)}"
