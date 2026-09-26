"""
DRPL LangChain Tool - DOCX Generator

Renders a Word (.docx) document from a structured payload using
`python-docx`. Intended for agents that need an editable Word artifact —
proposal bodies, cover letters, compliance statements — where the
existing PDF generator is too locked-down.

Input model:
    title:    Document title (shown as the H1 heading and filename stem).
    sections: Ordered list of section dicts:
                 {"heading": "Executive Summary", "level": 1, "body": "..."}
              Heading levels 1-4 map to Word's built-in Heading styles.
              `body` is treated as plain text; blank lines separate
              paragraphs. A leading "- " on a line makes it a bullet.
    metadata: Optional dict with sender/recipient/ref_number/date. When
              present, renders a header block above the first section.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class DocxSection(BaseModel):
    heading: Optional[str] = Field(None, description="Section heading. Omit for an untitled block.")
    level: int = Field(1, description="Heading level 1-4. Defaults to 1.")
    body: str = Field("", description="Section body. Blank lines split paragraphs; lines starting '- ' become bullets.")


class DocxGeneratorInput(BaseModel):
    title: str = Field("Generated Document", description="Document title (H1 heading and filename stem).")
    sections: list[DocxSection] = Field(
        ...,
        description="Ordered list of sections with heading/level/body.",
    )
    metadata: Optional[dict] = Field(
        None,
        description=(
            "Optional header info: {sender, recipient, ref_number, date, subject}. "
            "Rendered as a block above the body when present."
        ),
    )
    tender_id: Optional[int] = Field(None, description="Tender id for filename tagging.")
    session_id: Optional[int] = Field(None, description="Command Center session to attach the artifact to.")
    artifact_type: str = Field(
        "document_docx",
        description="Artifact type tag. Default 'document_docx'; override for e.g. 'proposal_docx'.",
    )
    agent_key: Optional[str] = Field(None, description="Agent key recorded on the artifact.")


_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def _render_body(doc, body: str) -> None:
    """Write a section's body text: blank lines split paragraphs; `- ` becomes a bullet."""
    if not body:
        return
    # Split on blank-line boundaries so agent output with natural paragraph
    # breaks survives unchanged.
    for chunk in re.split(r"\n\s*\n", body.strip()):
        lines = [ln.rstrip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(_BULLET_RE.match(ln) for ln in lines):
            for ln in lines:
                text = _BULLET_RE.sub("", ln)
                doc.add_paragraph(text, style="List Bullet")
        else:
            doc.add_paragraph(" ".join(lines))


def build_docx(
    path: str,
    title: str,
    sections: list[dict],
    metadata: Optional[dict] = None,
) -> dict:
    """Render a Word document to ``path``. Returns summary counts."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()

    # Title (H1)
    doc.add_heading(title or "Document", level=0)

    # Optional header block
    if metadata:
        meta_lines: list[str] = []
        for key in ("ref_number", "date", "sender", "recipient", "subject"):
            val = metadata.get(key)
            if val:
                meta_lines.append(f"{key.replace('_', ' ').title()}: {val}")
        if meta_lines:
            p = doc.add_paragraph()
            for i, line in enumerate(meta_lines):
                if i > 0:
                    p.add_run("\n")
                run = p.add_run(line)
                run.font.size = Pt(10)

    # Sections
    para_count = 0
    for s in sections or []:
        heading = s.get("heading") if isinstance(s, dict) else getattr(s, "heading", None)
        level = s.get("level", 1) if isinstance(s, dict) else getattr(s, "level", 1)
        body = s.get("body", "") if isinstance(s, dict) else getattr(s, "body", "")
        if heading:
            level = max(1, min(int(level or 1), 4))
            doc.add_heading(heading, level=level)
        _render_body(doc, body)
        para_count += sum(1 for _ in doc.paragraphs)

    doc.save(path)
    return {
        "section_count": len(sections or []),
        "paragraph_count": len(doc.paragraphs),
    }


class DocxGeneratorTool(BaseTool):
    """Generate a Word (.docx) document and persist it as an artifact."""
    name: str = "docx_generator"
    description: str = (
        "Generate an editable Word (.docx) document from structured sections "
        "(heading/level/body). Useful for proposal bodies, cover letters, "
        "and compliance statements where the recipient wants an editable "
        "source file. Returns the file path; when session_id is provided, "
        "also creates an artifact attached to that session."
    )
    args_schema: Type[BaseModel] = DocxGeneratorInput

    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        title: str = "Generated Document",
        sections: Optional[list] = None,
        metadata: Optional[dict] = None,
        tender_id: Optional[int] = None,
        session_id: Optional[int] = None,
        artifact_type: str = "document_docx",
        agent_key: Optional[str] = None,
    ) -> str:
        sections = sections or []
        # Pydantic hands us DocxSection models when invoked via args_schema;
        # normalise to plain dicts so build_docx stays input-agnostic.
        norm_sections: list[dict] = []
        for s in sections:
            if hasattr(s, "model_dump"):
                norm_sections.append(s.model_dump())
            elif isinstance(s, dict):
                norm_sections.append(s)
            else:
                norm_sections.append({"body": str(s)})

        if not norm_sections:
            return json.dumps({"status": "error", "message": "sections is empty"})

        settings = get_settings()
        upload_dir = os.path.join(settings.upload_dir, "generated_docs")
        os.makedirs(upload_dir, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        tender_tag = f"tender_{tender_id}_" if tender_id else ""
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", title.strip()).strip("_")[:40] or "doc"
        fname = f"{safe_stem}_{tender_tag}{ts}_{uuid.uuid4().hex[:6]}.docx"
        fpath = os.path.join(upload_dir, fname)

        try:
            summary = build_docx(fpath, title, norm_sections, metadata)
        except ImportError:
            return json.dumps({"status": "error", "message": "python-docx not installed."})
        except Exception as e:
            logger.error(f"docx_generator: build failed: {e}", exc_info=True)
            return json.dumps({"status": "error", "message": f"docx build failed: {e}"})

        result: dict[str, Any] = {
            "status": "success",
            "file_path": fpath,
            "file_name": fname,
            **summary,
        }

        if session_id and self.db:
            try:
                from app.services.artifact_service import create_artifact
                artifact = create_artifact(
                    db=self.db,
                    session_id=session_id,
                    artifact_type=artifact_type,
                    title=title or "Document",
                    content=json.dumps({"sections": norm_sections, "metadata": metadata, **summary}, default=str),
                    structured_data={"sections": norm_sections, "metadata": metadata, **summary},
                    agent_key=agent_key,
                    metadata={"tender_id": tender_id, "file_name": fname, "file_path": fpath},
                )
                try:
                    artifact.file_path = fpath
                    artifact.file_name = fname
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                result["artifact_id"] = artifact.id
            except Exception as e:
                logger.error(f"docx_generator: artifact persist failed: {e}", exc_info=True)
                result["artifact_error"] = str(e)

        return json.dumps(result, default=str)
