"""
DRPL LangChain Tool - Document Generator
Generates professional documents with AI content, applies letterhead and signatures.
Wraps the existing document_ai_service and pdf_generation_service.
"""

import json
import logging
import asyncio
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class DocumentGeneratorInput(BaseModel):
    """Input schema for the Document Generator tool."""
    document_type: str = Field(
        "custom",
        description="Type: 'proposal', 'cost_statement', 'letter', 'certificate', 'custom'",
    )
    prompt: str = Field(
        ...,
        description="Instructions for what the document should contain",
    )
    title: str = Field("Generated Document", description="Document title")
    template_variables: Optional[dict] = Field(
        None,
        description="Template variables: {date, ref_number, addressee, subject}",
    )
    letterhead_template_id: Optional[int] = Field(
        None,
        description="ID of the letterhead template to apply",
    )
    signature_ids: Optional[list[int]] = Field(
        None,
        description="List of digital signature IDs to apply to the document",
    )
    generate_pdf: bool = Field(
        False,
        description="Whether to also generate the final PDF file",
    )
    mode: str = Field(
        "generate",
        description="'generate' for new content, 'enhance' for improving existing content",
    )
    existing_content: str = Field(
        "",
        description="Existing content to enhance (used only in 'enhance' mode)",
    )


class DocumentGeneratorTool(BaseTool):
    """
    Generate professional documents (proposals, cost statements, letters, certificates)
    using AI content generation. Optionally applies DRPL letterhead templates and digital
    signatures, and generates the final PDF.
    """
    name: str = "document_generator"
    description: str = (
        "Generate professional documents with AI. Supports proposals, cost statements, "
        "letters, and certificates. Provide a prompt describing the document content. "
        "Can optionally apply a letterhead template and digital signatures, "
        "and generate a PDF file. Returns the generated markdown content and optional PDF path."
    )
    args_schema: Type[BaseModel] = DocumentGeneratorInput

    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        document_type: str = "custom",
        prompt: str = "",
        title: str = "Generated Document",
        template_variables: Optional[dict] = None,
        letterhead_template_id: Optional[int] = None,
        signature_ids: Optional[list[int]] = None,
        generate_pdf: bool = False,
        mode: str = "generate",
        existing_content: str = "",
    ) -> str:
        if not self.db:
            return "Error: Database session not available"
        if not prompt.strip():
            return "Error: A prompt is required describing what the document should contain"

        # Generate content using AI
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context — use a helper
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    content = pool.submit(
                        self._generate_content_sync,
                        prompt, document_type, existing_content, template_variables, mode,
                    ).result(timeout=120)
            else:
                content = asyncio.run(self._generate_content_async(
                    prompt, document_type, existing_content, template_variables, mode,
                ))
        except Exception as e:
            return f"Error generating content: {str(e)}"

        result = {
            "title": title,
            "document_type": document_type,
            "content_markdown": content,
            "mode": mode,
        }

        # Create GeneratedDocument record if we need to persist or generate PDF
        if generate_pdf or letterhead_template_id or signature_ids:
            try:
                from app.models.letterhead import GeneratedDocument

                # Build signatures list
                sigs = []
                if signature_ids:
                    for sid in signature_ids:
                        sigs.append({
                            "signature_id": sid,
                            "position": "bottom-right",
                            "page": "last",
                        })

                doc = GeneratedDocument(
                    title=title,
                    document_type=document_type,
                    letterhead_template_id=letterhead_template_id,
                    content_markdown=content,
                    template_variables=template_variables or {},
                    signatures=sigs,
                    status="draft",
                )
                self.db.add(doc)
                self.db.commit()
                self.db.refresh(doc)

                result["document_id"] = doc.id
                result["status"] = "draft"

                # Generate PDF if requested
                if generate_pdf:
                    try:
                        from app.services.pdf_generation_service import generate_pdf as gen_pdf
                        file_path = gen_pdf(self.db, doc.id)
                        self.db.refresh(doc)
                        result["pdf_path"] = file_path
                        result["pdf_file_name"] = doc.generated_file_name
                        result["status"] = "generated"
                    except Exception as e:
                        result["pdf_error"] = str(e)
                        result["status"] = "draft (PDF generation failed)"

            except Exception as e:
                result["persist_error"] = str(e)

        return json.dumps(result, default=str, indent=2)

    def _generate_content_sync(
        self, prompt, document_type, existing_content, template_variables, mode,
    ) -> str:
        """Synchronous wrapper for the async content generation."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._generate_content_async(prompt, document_type, existing_content, template_variables, mode)
            )
        finally:
            loop.close()

    async def _generate_content_async(
        self, prompt, document_type, existing_content, template_variables, mode,
    ) -> str:
        """Call the existing document AI service."""
        from app.services.document_ai_service import generate_document_content
        return await generate_document_content(
            db=self.db,
            prompt=prompt,
            document_type=document_type,
            existing_content=existing_content,
            template_variables=template_variables,
            mode=mode,
        )
