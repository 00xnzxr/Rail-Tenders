"""
DRPL Backend - Document AI Service
Generates or enhances document content using AI.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.ai_service import call_ai
from app.services.document_template_definitions import get_template

logger = logging.getLogger(__name__)


async def generate_document_content(
    db: Session,
    prompt: str,
    document_type: str = "custom",
    existing_content: str = "",
    template_variables: Optional[dict] = None,
    mode: str = "generate",
) -> str:
    """Generate or enhance document content using AI.

    Args:
        db: Database session
        prompt: User's instruction for content generation
        document_type: Type of document (proposal, cost_statement, etc.)
        existing_content: Current content (used in 'enhance' mode)
        template_variables: Template variables (date, ref_number, addressee, subject)
        mode: 'generate' for new content, 'enhance' for improving existing

    Returns:
        Generated markdown content string
    """
    template = get_template(document_type)
    template_structure = ""
    if template:
        template_structure = f"\n\nUse the following document structure as a guide:\n\n{template['content_markdown_template']}"

    tv_context = ""
    if template_variables:
        parts = []
        if template_variables.get("addressee"):
            parts.append(f"Addressee: {template_variables['addressee']}")
        if template_variables.get("subject"):
            parts.append(f"Subject: {template_variables['subject']}")
        if template_variables.get("ref_number"):
            parts.append(f"Reference: {template_variables['ref_number']}")
        if parts:
            tv_context = "\n\nDocument context:\n" + "\n".join(parts)

    if mode == "enhance":
        system_prompt = f"""You are a professional document writer for an Indian engineering and construction company (DRPL).
Your task is to enhance and improve the existing document content based on the user's instructions.
Maintain the same markdown format. Keep all existing section structure unless the user asks to change it.
Output ONLY the improved markdown content — no explanations or preamble.{template_structure}{tv_context}"""

        user_prompt = f"""Here is the existing document content:

{existing_content}

User's instructions for improvement:
{prompt}

Please provide the enhanced version:"""
    else:
        system_prompt = f"""You are a professional document writer for an Indian engineering and construction company (DRPL).
Your task is to generate complete, professional document content in markdown format based on the user's instructions.
The content should be formal, detailed, and suitable for submission to government and corporate clients.
Use tables where appropriate for structured data.
Output ONLY the markdown content — no explanations or preamble.{template_structure}{tv_context}"""

        user_prompt = f"""Generate document content based on the following instructions:

{prompt}"""

    logger.info("[DRPL] AI document generation: mode=%s, type=%s, prompt_len=%d",
                mode, document_type, len(prompt))

    result = await call_ai(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        db=db,
        agent_name="document_generator",
    )

    return result
