"""
DRPL LangChain Agent - Proposal Document Generator
Step 3 of the Tender Pipeline: Generates documents for each checklist item,
applies letterhead and signatures, and produces PDFs.
"""

import json
import logging
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage
from sqlalchemy.orm import Session

from app.services.langchain.llm_factory import get_chat_model
from app.services.langchain.callback_handler import DRPLCallbackHandler
from app.services.langchain.tools.tool_loader import load_tools_by_keys

logger = logging.getLogger(__name__)

PROPOSAL_AGENT_SYSTEM_PROMPT = """You are a professional proposal writer for DRPL Manufacturing, an Indian engineering company specializing in mechanical and electrical work for Indian Railways.

Your job is to generate HIGH-QUALITY, SUBMISSION-READY documents for tender proposals. Each document must be:

1. **Professional and Formal**: Suitable for submission to government and corporate clients
2. **Accurate**: Based on available tender context and web research
3. **Complete**: Cover all requirements specified in the tender analysis
4. **Properly Formatted**: Use markdown with appropriate tables, headers, and sections

## Document Types You Generate:
- **Proposals**: Technical proposals with methodology, team composition, timeline, past experience
- **Cost Statements**: Detailed cost breakdowns with material, labour, equipment, overheads, GST
- **Letters**: Cover letters, undertakings, declarations, compliance letters
- **Certificates**: Experience certificates, turnover certificates, compliance certificates

## Process for Each Document:
1. Use web_search and memory_retrieve to gather relevant context
2. Use the document_generator tool to create the document content
3. Apply appropriate letterhead and signatures if specified
4. Store learnings in memory for future reference

## Important Guidelines:
- Include all required sections as specified in the tender requirements
- Use formal Indian business English appropriate for government submissions
- Include proper tables for structured data (team composition, past works, cost breakdowns)
- The web_search tool may return a `grounded_summary` with inline citations — prioritize this as your primary research source and cite the sources in your output

**No preambles. No narration.** Do NOT describe what you are about to do, do NOT announce which tools you are calling, and do NOT restate the user's request. Start your final answer directly with the document heading or salutation. Phrases like "I'll draft the proposal", "Let me now compile", "Now I will prepare" are forbidden in your final output."""


async def run_proposal_generation(
    db: Session,
    tender_id: int,
    checklist_items: list[dict],
    analysis_result: dict,
    letterhead_template_id: Optional[int] = None,
    signature_ids: Optional[list[int]] = None,
    user_id: Optional[int] = None,
) -> list[dict]:
    """
    Generate documents for each checklist item using a ReAct agent.

    For each item in the checklist, the agent:
    1. Determines the appropriate document type
    2. Reads company profile for real data
    3. Generates content using AI
    4. Optionally applies letterhead + signatures and generates PDF

    Args:
        db: Database session
        tender_id: The tender to generate documents for
        checklist_items: List of checklist items from Step 2
        analysis_result: Analysis output from Step 1 (provides context)
        letterhead_template_id: Optional letterhead to apply to all docs
        signature_ids: Optional signatures to apply
        user_id: User triggering the generation

    Returns:
        List of generated document records
    """
    # Phase 3d — resolve tools + prompt through canonical_registry.
    from app.services.langchain.canonical_registry import (
        resolve_system_prompt,
        resolve_tool_keys,
    )
    # No local default_keys — the canonical registry's default_tools decides.
    tool_keys, _tool_src = resolve_tool_keys(db, "proposal_creator")
    tools = load_tools_by_keys(db, tool_keys, agent_key="proposal_creator")

    llm = get_chat_model(db, agent_name="proposal_creator")
    callback = DRPLCallbackHandler(db, agent_name="proposal_creator")

    base_prompt, prompt_src = resolve_system_prompt(
        db, "proposal_creator",
        canonical_builder=lambda: PROPOSAL_AGENT_SYSTEM_PROMPT,
    )
    logger.info(
        f"[proposal_creator] system prompt source={prompt_src}, tools_source={_tool_src}"
    )

    from langgraph.prebuilt import create_react_agent

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=SystemMessage(content=base_prompt),
    )

    generated_documents = []

    # Build context from analysis
    analysis_summary = json.dumps({
        "tender_summary": analysis_result.get("summary", ""),
        "key_requirements": analysis_result.get("requirements", {}),
        "key_risks": analysis_result.get("key_risks", []),
    }, default=str)[:3000]

    for item in checklist_items:
        item_name = item.get("name", "Unknown Document")
        item_desc = item.get("description", "")
        is_required = item.get("is_required", True)

        logger.info(f"Generating document for checklist item: {item_name}")

        # Determine document type from item name
        doc_type = _infer_document_type(item_name)

        # Build the generation prompt
        user_message = (
            f"Generate the following document for tender ID {tender_id}:\n\n"
            f"Document: {item_name}\n"
            f"Description: {item_desc}\n"
            f"Required: {'Yes - MANDATORY' if is_required else 'Optional'}\n"
            f"Document Type: {doc_type}\n\n"
            f"Tender Analysis Context:\n{analysis_summary}\n\n"
            f"Instructions:\n"
            f"1. Use document_generator tool with:\n"
            f"   - document_type='{doc_type}'\n"
            f"   - A detailed prompt describing what this document should contain\n"
            f"   - title='{item_name}'\n"
        )

        if letterhead_template_id:
            user_message += f"   - letterhead_template_id={letterhead_template_id}\n"
        if signature_ids:
            user_message += f"   - signature_ids={signature_ids}\n"
            user_message += f"   - generate_pdf=true\n"

        user_message += (
            f"3. If you learn anything useful for future proposals, store it in memory\n"
            f"4. Return the result of the document_generator tool"
        )

        try:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=user_message)]},
                config={"callbacks": [callback]},
            )

            messages = result.get("messages", [])
            final_content = messages[-1].content if messages else ""

            generated_documents.append({
                "checklist_item": item_name,
                "document_type": doc_type,
                "is_required": is_required,
                "status": "generated",
                "content_preview": final_content[:500],
            })

        except Exception as e:
            logger.error(f"Failed to generate document '{item_name}': {e}")
            generated_documents.append({
                "checklist_item": item_name,
                "document_type": doc_type,
                "is_required": is_required,
                "status": "failed",
                "error": str(e),
            })

    return generated_documents


def _infer_document_type(item_name: str) -> str:
    """Infer the document type from the checklist item name."""
    name_lower = item_name.lower()

    if any(w in name_lower for w in ["proposal", "technical bid", "methodology", "approach"]):
        return "proposal"
    elif any(w in name_lower for w in ["cost", "price", "financial", "rate", "bor", "schedule of rates"]):
        return "cost_statement"
    elif any(w in name_lower for w in ["certificate", "certification", "experience cert", "turnover cert"]):
        return "certificate"
    elif any(w in name_lower for w in ["letter", "declaration", "undertaking", "affidavit", "cover"]):
        return "letter"
    else:
        return "custom"
