"""
DRPL Backend - Workspace Service
Business logic for per-tender canvas workspace management
"""

import os
import re
import uuid
import fnmatch
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tender import Tender
from app.models.checklist import ChecklistItem
from app.models.workspace import WorkspaceConfig, DocumentWorkspace, DocumentFormatTemplate
from app.models.letterhead import GeneratedDocument, LetterheadTemplate

logger = logging.getLogger(__name__)
settings = get_settings()


def _is_valid_html(text: str) -> bool:
    """Check if content is valid HTML (not raw markdown/text wrapped in <p> tags)."""
    if not text:
        return True
    stripped = text.strip()

    # If it doesn't start with any HTML tag, it's not HTML
    if not stripped.startswith("<"):
        return False

    # Check if it's just a <p> wrapper around markdown (TipTap does this)
    inner = _strip_html_wrapper(stripped)
    if inner != stripped:
        # Content was wrapped in <p> — check if inner content is markdown
        if _looks_like_markdown(inner):
            return False

    # If it has multiple semantic HTML elements, it's valid HTML
    html_tags = ["<h1", "<h2", "<h3", "<table", "<div", "<ul", "<ol", "<blockquote"]
    tag_count = sum(1 for tag in html_tags if tag in stripped.lower())
    if tag_count >= 2:
        return True

    # Single <p> with no markdown inside is valid HTML
    if stripped.startswith("<p") and not _looks_like_markdown(inner):
        return True

    # Has multiple distinct <p> tags — likely valid HTML
    if stripped.count("<p") >= 3:
        return True

    # Default: if starts with <, treat as HTML
    return True


def migrate_markdown_content(db: Session) -> dict:
    """
    One-time migration: find all DocumentWorkspace records where draft_content_html
    contains non-HTML content (markdown/plain text) and convert to proper HTML.
    Also preserves raw markdown in draft_content_markdown.

    Safe to run multiple times — skips records that already have valid HTML.
    """
    workspaces = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.draft_content_html.isnot(None),
        DocumentWorkspace.draft_content_html != "",
    ).all()

    logger.debug(
        "[DRPL] Migration: found %d workspace documents with content", len(workspaces)
    )

    converted = 0
    skipped = 0

    for ws in workspaces:
        content = ws.draft_content_html or ""
        if not content.strip():
            skipped += 1
            continue

        # If it's already valid HTML, skip
        if _is_valid_html(content):
            # Debug, not stdout. This is the "nothing to do" branch and it fires
            # for almost every row on almost every run: at 725 rows and four
            # uvicorn workers it was ~2,900 lines of skip-notice per deploy,
            # which is how a production log ends up containing nothing else.
            logger.debug(
                "[DRPL] Migration: ws id=%s already valid HTML (starts with: %r)",
                ws.id, content.strip()[:30],
            )
            skipped += 1
            continue

        # Content is NOT HTML — convert it. This one stays at INFO: it is a
        # write, it is rare, and it is the line you want when a document comes
        # back looking different from how it was left.
        logger.info(
            "[DRPL] Migration: converting ws id=%s (starts with: %r)",
            ws.id, content.strip()[:50],
        )

        # Preserve raw content as markdown if not already saved
        if not ws.draft_content_markdown:
            ws.draft_content_markdown = content

        # Convert to HTML
        ws.draft_content_html = _convert_md_to_html(content)
        converted += 1

    if converted > 0:
        db.commit()

    logger.info(
        "[DRPL] Workspace content migration: converted=%d, skipped=%d, checked=%d",
        converted, skipped, len(workspaces),
    )
    return {"converted": converted, "skipped": skipped, "total_checked": len(workspaces)}


def _seed_checklist_items_from_artifact(db: Session, tender_id: int) -> int:
    """Seed ChecklistItem rows from the latest Command Center checklist artifact
    linked to this tender (via ProposalSession.tender_id).

    Returns the number of rows inserted. Zero if no artifact was found or parseable.
    """
    from app.models.artifact import CommandCenterArtifact
    from app.models.proposal import ProposalSession

    session_ids = [
        s.id for s in db.query(ProposalSession.id)
        .filter(ProposalSession.tender_id == tender_id)
        .all()
    ]
    if not session_ids:
        return 0

    artifact = (
        db.query(CommandCenterArtifact)
        .filter(
            CommandCenterArtifact.session_id.in_(session_ids),
            CommandCenterArtifact.artifact_type == "checklist",
        )
        .order_by(CommandCenterArtifact.created_at.desc())
        .first()
    )
    if not artifact:
        return 0

    items_data = artifact.structured_data if isinstance(artifact.structured_data, list) else None
    if not items_data:
        try:
            from app.services.langchain.graphs.chat_agent_wrappers import (
                _parse_checklist_structured_data,
            )
            items_data = _parse_checklist_structured_data(artifact.content or "")
        except Exception as e:
            logger.warning(f"Fallback checklist parse failed for tender {tender_id}: {e}")
            items_data = []

    if not items_data:
        return 0

    seeded = 0
    for i, item_data in enumerate(items_data):
        name = (
            item_data.get("name")
            or item_data.get("item_name")
            or item_data.get("title")
            or ""
        ).strip()
        if not name:
            continue
        description = (
            item_data.get("description")
            or item_data.get("item_description")
            or ""
        )
        is_required = item_data.get("is_required")
        if is_required is None:
            is_required = item_data.get("mandatory", True)

        db.add(ChecklistItem(
            tender_id=tender_id,
            item_name=name[:500],
            item_description=description,
            is_required=bool(is_required),
            display_order=i,
            item_category=item_data.get("category", "standard"),
        ))
        seeded += 1

    if seeded:
        db.commit()
        logger.info(
            f"Seeded {seeded} ChecklistItem rows for tender {tender_id} "
            f"from artifact {artifact.id}"
        )
    return seeded


def init_workspace(db: Session, tender_id: int, user_id: int) -> dict:
    """Initialize a canvas workspace for a tender.
    Creates WorkspaceConfig + DocumentWorkspace per ChecklistItem.
    Idempotent — returns existing workspace if already initialized.
    """
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise ValueError("Tender not found")

    # Fetch existing config (may be None on first-ever init). On re-init we
    # keep the same config row and top up any checklist items that don't
    # already have a DocumentWorkspace — this is what makes the post-analysis
    # pipeline idempotent when annexure_finder/checklist_generator add new
    # items on a second pass.
    existing_config = db.query(WorkspaceConfig).filter(
        WorkspaceConfig.tender_id == tender_id
    ).first()

    # Get all checklist items for this tender
    items = db.query(ChecklistItem).filter(
        ChecklistItem.tender_id == tender_id
    ).order_by(ChecklistItem.display_order).all()

    # Self-heal: try cheap artifact-based seeding first, then fall back to
    # auto-generating a checklist inline via the checklist agent.
    if not items:
        if _seed_checklist_items_from_artifact(db, tender_id) > 0:
            items = db.query(ChecklistItem).filter(
                ChecklistItem.tender_id == tender_id
            ).order_by(ChecklistItem.display_order).all()

    if not items:
        logger.info(f"init_workspace: no checklist for tender {tender_id}, auto-generating")
        import asyncio
        from app.services.checklist_service import generate_checklist
        try:
            try:
                items = asyncio.run(generate_checklist(db, tender_id))
            except RuntimeError:
                # A loop is already running in this thread — use a fresh one.
                loop = asyncio.new_event_loop()
                try:
                    items = loop.run_until_complete(generate_checklist(db, tender_id))
                finally:
                    loop.close()
        except Exception as e:
            logger.error(f"init_workspace: auto-checklist generation failed: {e}")
            raise ValueError(
                "Could not auto-generate checklist. Please generate one first."
            ) from e
        # Re-query with ordering so workspace rows follow display_order
        items = db.query(ChecklistItem).filter(
            ChecklistItem.tender_id == tender_id
        ).order_by(ChecklistItem.display_order).all()
        if not items:
            raise ValueError("Checklist generation produced no items.")

    # Load all active format templates for auto-matching
    templates = db.query(DocumentFormatTemplate).filter(
        DocumentFormatTemplate.is_active == True
    ).all()

    # Reuse existing config on re-init; otherwise create one.
    if existing_config is not None:
        config = existing_config
        config.total_items = len(items)
        config.last_activity_at = datetime.now(timezone.utc)
    else:
        config = WorkspaceConfig(
            tender_id=tender_id,
            view_mode="grid",
            total_items=len(items),
            completed_items=0,
            last_activity_at=datetime.now(timezone.utc),
        )
        db.add(config)
        db.flush()

    # Skip items that already have a DocumentWorkspace (e.g., annexure_finder
    # creates the pair itself; re-init of an existing workspace must not
    # duplicate those rows and hit the UNIQUE(checklist_item_id) constraint).
    existing_ws_item_ids = {
        row[0] for row in
        db.query(DocumentWorkspace.checklist_item_id).filter(
            DocumentWorkspace.tender_id == tender_id
        ).all()
    }

    # Create DocumentWorkspace for each checklist item that lacks one
    for item in items:
        if item.id in existing_ws_item_ids:
            continue
        # Auto-match format template
        matched_template_id = _match_format_template(item.item_name, templates)

        workspace = DocumentWorkspace(
            checklist_item_id=item.id,
            tender_id=tender_id,
            format_template_id=matched_template_id,
            conversation_session_id=str(uuid.uuid4()),
            review_status="not_started",
        )

        # If item already has a generated document, reflect that
        if item.generation_status == "generated" and item.generated_document_id:
            workspace.review_status = "approved"
            item.workspace_status = "approved"
        elif item.generation_status == "generating":
            workspace.review_status = "drafting"
            item.workspace_status = "drafting"

        db.add(workspace)

    # Flush so DocumentWorkspace rows have IDs for AI auto-extract
    db.flush()

    # AI auto-extract format instructions for unmatched items (non-fatal)
    try:
        _ai_extract_format_instructions(db, tender, items)
    except Exception as e:
        logger.warning(f"AI format auto-extract failed (non-fatal): {e}")

    # Enable workspace on tender
    tender.workspace_enabled = True
    db.commit()

    return get_workspace_overview(db, tender_id)


def get_workspace_overview(db: Session, tender_id: int) -> dict:
    """Return workspace config, all items with workspace data, and aggregate stats."""
    config = db.query(WorkspaceConfig).filter(WorkspaceConfig.tender_id == tender_id).first()
    if not config:
        return None

    # Get all checklist items with workspace data (left join)
    items = db.query(ChecklistItem).filter(
        ChecklistItem.tender_id == tender_id
    ).order_by(ChecklistItem.display_order).all()

    workspace_map = {}
    workspaces = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.tender_id == tender_id
    ).all()
    for ws in workspaces:
        workspace_map[ws.checklist_item_id] = ws

    # Build combined items
    combined_items = []
    for item in items:
        ws = workspace_map.get(item.id)
        combined = {
            "id": item.id,
            "tender_id": item.tender_id,
            "item_name": item.item_name,
            "item_description": item.item_description,
            "is_required": item.is_required,
            "is_uploaded": item.is_uploaded,
            "document_id": item.document_id,
            "display_order": item.display_order,
            "item_category": item.item_category or "standard",
            "generation_status": item.generation_status or "pending",
            "generated_document_id": item.generated_document_id,
            "ai_instructions": item.ai_instructions,
            "source_section": item.source_section,
            "is_not_required": item.is_not_required or False,
            "workspace_status": item.workspace_status or "not_started",
            "agent_key": item.agent_key,
            # Workspace fields
            "workspace_id": ws.id if ws else None,
            "draft_content_html": ws.draft_content_html if ws else None,
            "content_version": ws.content_version if ws else 0,
            "review_status": ws.review_status if ws else "not_started",
            "format_template_id": ws.format_template_id if ws else None,
            "format_instructions": ws.format_instructions if ws else None,
            "notes": ws.notes if ws else None,
            "last_edited_at": ws.last_edited_at.isoformat() if ws and ws.last_edited_at else None,
        }
        combined_items.append(combined)

    # Compute stats
    stats = _compute_stats(items, workspace_map)

    return {
        "config": {
            "id": config.id,
            "tender_id": config.tender_id,
            "view_mode": config.view_mode,
            "layout_json": config.layout_json,
            "default_letterhead_id": config.default_letterhead_id,
            "default_agent_key": config.default_agent_key,
            "total_items": config.total_items,
            "completed_items": config.completed_items,
            "last_activity_at": config.last_activity_at.isoformat() if config.last_activity_at else None,
            "created_at": config.created_at.isoformat() if config.created_at else None,
            "updated_at": config.updated_at.isoformat() if config.updated_at else None,
        },
        "items": combined_items,
        "stats": stats,
    }


def get_document_workspace(db: Session, tender_id: int, item_id: int) -> dict:
    """Get full workspace detail for a single document."""
    item = db.query(ChecklistItem).filter(
        ChecklistItem.id == item_id,
        ChecklistItem.tender_id == tender_id,
    ).first()
    if not item:
        raise ValueError("Checklist item not found")

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id
    ).first()
    if not ws:
        raise ValueError("Document workspace not found. Initialize the workspace first.")

    # Get format template details if assigned
    template = None
    if ws.format_template_id:
        t = db.query(DocumentFormatTemplate).filter(
            DocumentFormatTemplate.id == ws.format_template_id
        ).first()
        if t:
            template = {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "document_category": t.document_category,
                "content_template_markdown": t.content_template_markdown,
                "format_rules": t.format_rules,
                "required_sections": t.required_sections,
            }

    return {
        "item": {
            "id": item.id,
            "item_name": item.item_name,
            "item_description": item.item_description,
            "item_category": item.item_category or "standard",
            "is_required": item.is_required,
            "is_not_required": item.is_not_required or False,
            "generation_status": item.generation_status or "pending",
            "ai_instructions": item.ai_instructions,
            "source_section": item.source_section,
            "generated_document_id": item.generated_document_id,
        },
        "workspace": {
            "id": ws.id,
            "checklist_item_id": ws.checklist_item_id,
            "tender_id": ws.tender_id,
            "agent_key": ws.agent_key,
            "agent_config_override": ws.agent_config_override,
            "format_template_id": ws.format_template_id,
            "format_instructions": ws.format_instructions,
            "draft_content_html": ws.draft_content_html,
            "draft_content_markdown": ws.draft_content_markdown,
            "content_version": ws.content_version,
            "conversation_session_id": ws.conversation_session_id,
            "notes": ws.notes,
            "review_status": ws.review_status,
            "reviewed_by": ws.reviewed_by,
            "reviewed_at": ws.reviewed_at.isoformat() if ws.reviewed_at else None,
            "letterhead_template_id": ws.letterhead_template_id,
            "letterhead_disabled": bool(getattr(ws, "letterhead_disabled", False)),
            "signatures_json": ws.signatures_json or [],
            "depends_on": ws.depends_on or [],
            "referenced_by": ws.referenced_by or [],
            "last_edited_at": ws.last_edited_at.isoformat() if ws.last_edited_at else None,
            "last_edited_by": ws.last_edited_by,
        },
        "format_template": template,
    }


def update_workspace_config(db: Session, tender_id: int, data: dict) -> dict:
    """Update workspace configuration."""
    config = db.query(WorkspaceConfig).filter(WorkspaceConfig.tender_id == tender_id).first()
    if not config:
        raise ValueError("Workspace not found")

    for key in ["view_mode", "default_letterhead_id", "default_agent_key"]:
        if key in data and data[key] is not None:
            setattr(config, key, data[key])

    db.commit()
    db.refresh(config)
    return {"status": "updated"}


def save_workspace_layout(db: Session, tender_id: int, layout_json: dict) -> dict:
    """Save canvas layout positions."""
    config = db.query(WorkspaceConfig).filter(WorkspaceConfig.tender_id == tender_id).first()
    if not config:
        raise ValueError("Workspace not found")

    config.layout_json = layout_json
    db.commit()
    return {"status": "saved"}


def update_document_workspace(db: Session, item_id: int, data: dict) -> dict:
    """Update document workspace settings (agent, format, notes, etc.)."""
    ws = db.query(DocumentWorkspace).filter(DocumentWorkspace.checklist_item_id == item_id).first()
    if not ws:
        raise ValueError("Document workspace not found")

    # Capture old depends_on for bidirectional reference maintenance
    old_depends_on = list(ws.depends_on or [])

    updatable_fields = [
        "agent_key", "format_template_id", "format_instructions",
        "notes", "review_status", "letterhead_template_id",
        "signatures_json", "depends_on",
    ]
    for key in updatable_fields:
        if key in data and data[key] is not None:
            setattr(ws, key, data[key])

    # Sync review_status to ChecklistItem
    if "review_status" in data and data["review_status"]:
        item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
        if item:
            item.workspace_status = data["review_status"]

    # Sync agent_key to ChecklistItem
    if "agent_key" in data and data["agent_key"]:
        item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
        if item:
            item.agent_key = data["agent_key"]

    # Maintain bidirectional references when depends_on changes
    if "depends_on" in data and data["depends_on"] is not None:
        new_deps = set(data["depends_on"])
        old_deps = set(old_depends_on)

        # Add item_id to referenced_by of newly added dependencies
        for added_id in new_deps - old_deps:
            dep_ws = db.query(DocumentWorkspace).filter(
                DocumentWorkspace.checklist_item_id == added_id
            ).first()
            if dep_ws:
                refs = list(dep_ws.referenced_by or [])
                if item_id not in refs:
                    refs.append(item_id)
                    dep_ws.referenced_by = refs

        # Remove item_id from referenced_by of removed dependencies
        for removed_id in old_deps - new_deps:
            dep_ws = db.query(DocumentWorkspace).filter(
                DocumentWorkspace.checklist_item_id == removed_id
            ).first()
            if dep_ws:
                refs = list(dep_ws.referenced_by or [])
                if item_id in refs:
                    refs.remove(item_id)
                    dep_ws.referenced_by = refs

    db.commit()
    return {"status": "updated"}


def _strip_html_wrapper(text: str) -> str:
    """Strip outer HTML wrapper tags (e.g. <p>...</p>) to get inner content."""
    stripped = text.strip()
    # Strip single <p>...</p> wrapper that TipTap adds around raw text
    match = re.match(r"^<p[^>]*>(.*)</p>$", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def _looks_like_markdown(text: str) -> bool:
    """Detect if text content is markdown rather than HTML (even if wrapped in <p> tags)."""
    if not text:
        return False

    # Strip TipTap's <p> wrapper to check inner content
    inner = _strip_html_wrapper(text.strip())

    # If inner content has real HTML structure (multiple tags, semantic elements), it's HTML
    html_block_tags = ["<h1", "<h2", "<h3", "<table", "<ul", "<ol", "<div", "<blockquote"]
    html_tag_count = sum(1 for tag in html_block_tags if tag in inner.lower())
    if html_tag_count >= 2:
        return False

    # Check for markdown indicators in the inner content
    md_indicators = [
        r"#{1,6}\s",           # headings
        r"\|.*\|.*\|",         # tables
        r"\*\*.+?\*\*",        # bold
        r"^[-*+]\s",           # unordered lists
        r"^\d+\.\s",           # ordered lists
        r"^---\s*$",           # horizontal rules
    ]
    score = sum(1 for p in md_indicators if re.search(p, inner, re.MULTILINE))
    return score >= 2


def _fix_collapsed_tables(text: str) -> str:
    """Reconstruct line breaks in markdown tables that were collapsed to a single line.

    TipTap wraps content in <p> tags, collapsing newlines into spaces.
    This turns:  '| A | B | |---| | X | Y |'
    Back into:   '| A | B |\n|---|\n| X | Y |'
    """
    # Step 1: Add newline before separator rows  |---|---|
    text = re.sub(r"\s*(\|[\s:]*[-:]{2,}[\s:]*(?:\|[\s:]*[-:]{2,}[\s:]*)+\|)", r"\n\1\n", text)

    # Step 2: Add newline between consecutive table rows: '| ... | | ...' → '| ...|\n| ...'
    # Pattern: pipe + optional space + pipe (indicating end of one row, start of next)
    text = re.sub(r"\|\s+\|(?!\s*[-:])", "|\n|", text)

    # Step 3: Ensure headings get their own line
    text = re.sub(r"(\S)\s+(#{1,6}\s)", r"\1\n\n\2", text)

    # Step 4: Ensure --- horizontal rules get their own line
    text = re.sub(r"(\S)\s+---\s+", r"\1\n\n---\n\n", text)

    return text


def _pipe_tables_to_html(text: str) -> str:
    """Convert pipe-delimited tables to HTML <table> tags directly.

    Handles malformed tables that Python's markdown library rejects
    (e.g. separator column count doesn't match header).
    """
    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Detect a table: line starts and ends with | and has at least 2 |
        if line.startswith("|") and line.endswith("|") and line.count("|") >= 3:
            # Collect all consecutive pipe-delimited lines
            table_lines = []
            while i < len(lines):
                l = lines[i].strip()
                if l.startswith("|") and l.count("|") >= 3:
                    table_lines.append(l)
                    i += 1
                elif not l:
                    i += 1  # skip blank lines within table region
                    continue
                else:
                    break

            if len(table_lines) < 2:
                # Not enough rows for a table, output as-is
                result.extend(table_lines)
                continue

            # Parse cells from each row
            parsed_rows = []
            separator_idx = -1
            for idx, tl in enumerate(table_lines):
                cells = [c.strip() for c in tl.split("|")[1:-1]]
                # Check if this is a separator row (all cells are dashes/colons)
                is_sep = all(re.match(r"^[-:]+$", c) for c in cells if c)
                if is_sep and idx <= 2:
                    separator_idx = idx
                else:
                    parsed_rows.append((idx, cells))

            if not parsed_rows:
                result.extend(table_lines)
                continue

            # Build HTML table
            max_cols = max(len(cells) for _, cells in parsed_rows)
            html_parts = ['<table>']

            for row_idx, (orig_idx, cells) in enumerate(parsed_rows):
                # Pad cells to max columns
                while len(cells) < max_cols:
                    cells.append("")

                is_header = (separator_idx > 0 and orig_idx < separator_idx) or \
                            (separator_idx == -1 and row_idx == 0)
                tag = "th" if is_header else "td"

                if is_header and row_idx == 0:
                    html_parts.append("  <thead>")
                elif row_idx == 0 or (separator_idx > 0 and orig_idx == separator_idx + 1 and row_idx > 0):
                    if any(o < separator_idx for o, _ in parsed_rows[:row_idx]):
                        html_parts.append("  </thead>")
                    html_parts.append("  <tbody>")

                html_parts.append("    <tr>")
                for cell in cells:
                    # Convert basic markdown bold within cells
                    cell_html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", cell)
                    html_parts.append(f"      <{tag}>{cell_html}</{tag}>")
                html_parts.append("    </tr>")

            # Close any open tags
            if separator_idx >= 0:
                html_parts.append("  </tbody>")
            html_parts.append("</table>\n")
            result.append("\n".join(html_parts))
        else:
            result.append(lines[i])
            i += 1

    return "\n".join(result)


def _convert_md_to_html(md_text: str) -> str:
    """Convert markdown to HTML using the markdown library."""
    try:
        import markdown
        # Step 1: Fix collapsed tables (reconstruct line breaks)
        fixed = _fix_collapsed_tables(md_text)
        # Step 2: Convert pipe tables to HTML directly (handles malformed tables)
        fixed = _pipe_tables_to_html(fixed)
        # Step 3: Convert remaining markdown (headings, bold, lists, etc.)
        return markdown.markdown(fixed, extensions=["tables", "nl2br", "fenced_code"])
    except ImportError:
        return md_text


def save_document_content(db: Session, item_id: int, content_html: Optional[str],
                          content_markdown: Optional[str], user_id: int) -> dict:
    """Save draft content for a document."""
    ws = db.query(DocumentWorkspace).filter(DocumentWorkspace.checklist_item_id == item_id).first()
    if not ws:
        raise ValueError("Document workspace not found")

    # If content_html contains markdown (even wrapped in <p> by TipTap), convert to proper HTML
    if content_html is not None and _looks_like_markdown(content_html):
        # Extract raw markdown — strip TipTap's <p> wrapper if present
        raw_md = _strip_html_wrapper(content_html)
        if content_markdown is None:
            content_markdown = raw_md  # preserve raw markdown
        content_html = _convert_md_to_html(raw_md)

    if content_html is not None:
        ws.draft_content_html = content_html
    if content_markdown is not None:
        ws.draft_content_markdown = content_markdown

    ws.content_version += 1
    ws.last_edited_at = datetime.now(timezone.utc)
    ws.last_edited_by = user_id

    # Auto-transition from not_started to drafting
    if ws.review_status == "not_started":
        ws.review_status = "drafting"
        item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
        if item:
            item.workspace_status = "drafting"

    # Update workspace activity
    config = db.query(WorkspaceConfig).filter(WorkspaceConfig.tender_id == ws.tender_id).first()
    if config:
        config.last_activity_at = datetime.now(timezone.utc)

    db.commit()

    return {
        "status": "saved",
        "content_version": ws.content_version,
        "review_status": ws.review_status,
    }


# Manual annexures use the same `annexure_finder:` source_section prefix as
# extracted ones (so every existing filter / export path picks them up) but with
# a unique identifier that the extractor will never produce — so re-running
# extraction never collides with or deletes a user-added annexure.
_MANUAL_ANNEXURE_PREFIX = "annexure_finder:"


def create_manual_annexure(
    db: Session,
    tender_id: int,
    title: str,
    identifier: Optional[str] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Create a blank annexure (ChecklistItem + DocumentWorkspace) the user adds
    by hand — for forms not present in the tender documents. Returns the new
    item/workspace ids so the caller can surface it immediately.
    """
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise ValueError("Tender not found")

    title = (title or "").strip() or "Untitled Annexure"

    def _key(ident: str) -> str:
        return f"{_MANUAL_ANNEXURE_PREFIX}{ident}"

    def _exists(ident: str) -> bool:
        return db.query(ChecklistItem).filter(
            ChecklistItem.tender_id == tender_id,
            ChecklistItem.source_section == _key(ident),
        ).first() is not None

    if identifier and identifier.strip():
        identifier = identifier.strip()
        if _exists(identifier):
            raise ValueError(f"An annexure with identifier '{identifier}' already exists")
    else:
        n = 1
        while _exists(f"Custom-{n}"):
            n += 1
        identifier = f"Custom-{n}"

    # Sort manual annexures after extracted ones by default.
    max_order = (
        db.query(ChecklistItem.display_order)
        .filter(ChecklistItem.tender_id == tender_id)
        .order_by(ChecklistItem.display_order.desc())
        .first()
    )
    next_order = ((max_order[0] if max_order and max_order[0] is not None else 0) or 0) + 1

    item = ChecklistItem(
        tender_id=tender_id,
        item_name=f"{identifier} — {title}"[:500],
        item_description="Manually added annexure",
        is_required=False,
        display_order=next_order,
        item_category="generated",
        generation_status="generated",
        source_section=_key(identifier),
        agent_key="annexure_finder",
        workspace_status="drafting",
    )
    db.add(item)
    db.flush()  # assign item.id

    tmpl = db.query(DocumentFormatTemplate).filter(
        DocumentFormatTemplate.document_category == "annexure",
        DocumentFormatTemplate.is_system == True,  # noqa: E712
        DocumentFormatTemplate.is_active == True,  # noqa: E712
    ).first()

    heading = f"{identifier} — {title}"
    ws = DocumentWorkspace(
        checklist_item_id=item.id,
        tender_id=tender_id,
        agent_key="annexure_finder",
        format_template_id=(tmpl.id if tmpl else None),
        conversation_session_id=str(uuid.uuid4()),
        draft_content_markdown=f"# {heading}\n\n",
        draft_content_html=f"<h1>{heading}</h1><p></p>",
        review_status="drafting",
        page_orientation="portrait",
        content_version=1,
        last_edited_by=user_id,
        last_edited_at=datetime.now(timezone.utc),
    )
    db.add(ws)
    db.commit()

    logger.info(
        f"[workspace] manual annexure created tender={tender_id} "
        f"item={item.id} identifier={identifier!r}"
    )
    return {
        "checklist_item_id": item.id,
        "workspace_id": ws.id,
        "identifier": identifier,
        "item_name": item.item_name,
    }


def delete_workspace_item(db: Session, tender_id: int, item_id: int) -> dict:
    """Delete a workspace item — its ChecklistItem + paired DocumentWorkspace.

    Used to remove annexures that don't apply to the bidder. Idempotent-ish:
    raises ValueError only when the item doesn't belong to the tender.
    """
    item = db.query(ChecklistItem).filter(
        ChecklistItem.id == item_id,
        ChecklistItem.tender_id == tender_id,
    ).first()
    if not item:
        raise ValueError("Workspace item not found for this tender")

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id,
        DocumentWorkspace.tender_id == tender_id,
    ).first()
    if ws:
        db.delete(ws)
    db.delete(item)
    db.commit()

    logger.info(f"[workspace] deleted item tender={tender_id} item={item_id}")
    return {"deleted": item_id}


def finalize_document(db: Session, item_id: int, user_id: int) -> dict:
    """Finalize a document: lock content, create GeneratedDocument, generate PDF."""
    ws = db.query(DocumentWorkspace).filter(DocumentWorkspace.checklist_item_id == item_id).first()
    if not ws:
        raise ValueError("Document workspace not found")

    item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
    if not item:
        raise ValueError("Checklist item not found")

    if not ws.draft_content_html and not ws.draft_content_markdown:
        raise ValueError("No content to finalize. Write or generate content first.")

    # Create or update GeneratedDocument
    gen_doc = None
    if item.generated_document_id:
        gen_doc = db.query(GeneratedDocument).filter(
            GeneratedDocument.id == item.generated_document_id
        ).first()

    if not gen_doc:
        gen_doc = GeneratedDocument(
            title=item.item_name,
            document_type="workspace_document",
            tender_id=ws.tender_id,
            content_html=ws.draft_content_html,
            content_markdown=ws.draft_content_markdown,
            letterhead_template_id=ws.letterhead_template_id,
            signatures=ws.signatures_json or [],
            status="generated",
            created_by=user_id,
        )
        db.add(gen_doc)
        db.flush()
    else:
        gen_doc.content_html = ws.draft_content_html
        gen_doc.content_markdown = ws.draft_content_markdown
        gen_doc.letterhead_template_id = ws.letterhead_template_id
        gen_doc.signatures = ws.signatures_json or []
        gen_doc.status = "generated"

    # Link to checklist item
    item.generated_document_id = gen_doc.id
    item.generation_status = "generated"
    item.workspace_status = "approved"

    # Update workspace
    ws.review_status = "approved"
    ws.reviewed_by = user_id
    ws.reviewed_at = datetime.now(timezone.utc)

    # Determine output format from assigned template
    output_format = "pdf"  # default
    docx_path = None
    xlsx_path = None
    pdf_path = None

    if ws.format_template_id:
        fmt_template = db.query(DocumentFormatTemplate).filter(
            DocumentFormatTemplate.id == ws.format_template_id
        ).first()
        if fmt_template and getattr(fmt_template, "output_format", None):
            output_format = fmt_template.output_format

    output_dir = os.path.join(settings.upload_dir, "generated_docs")
    os.makedirs(output_dir, exist_ok=True)
    safe_title = re.sub(r'[^\w\s-]', '', item.item_name or "document").strip().replace(" ", "_")[:80]

    # Generate output in the template's format
    if output_format == "xlsx":
        try:
            from app.services.xlsx_generation_service import generate_xlsx_from_costing
            from app.models.costing_template import BOQItem

            boq_items = db.query(BOQItem).filter(BOQItem.checklist_item_id == item_id).all()
            if boq_items:
                items_data = [
                    {
                        "sr_no": b.sr_no,
                        "description": b.description,
                        "quantity": b.quantity or 0,
                        "unit": b.unit or "",
                        "rate": b.computed_rate or b.estimated_rate or 0,
                        "amount": (b.computed_rate or b.estimated_rate or 0) * (b.quantity or 0),
                    }
                    for b in boq_items
                ]
                xlsx_bytes = generate_xlsx_from_costing(items=items_data, title=item.item_name)
                xlsx_path = os.path.join(output_dir, f"{safe_title}.xlsx")
                with open(xlsx_path, "wb") as f:
                    f.write(xlsx_bytes)
            else:
                # Fallback: extract tables from content and generate XLSX
                content = ws.draft_content_markdown or ws.draft_content_html or ""
                if content:
                    from app.services.xlsx_generation_service import generate_xlsx_from_content_tables
                    xlsx_bytes = generate_xlsx_from_content_tables(content, title=item.item_name)
                    if xlsx_bytes:
                        xlsx_path = os.path.join(output_dir, f"{safe_title}.xlsx")
                        with open(xlsx_path, "wb") as f:
                            f.write(xlsx_bytes)
        except Exception as e:
            logger.warning(f"XLSX generation failed for document {gen_doc.id}: {e}")

    if output_format == "docx" or output_format not in ("xlsx",):
        try:
            from app.services.docx_generation_service import generate_docx_from_markdown
            # Prefer markdown (DOCX generator is designed for markdown input)
            content = ws.draft_content_markdown or ""
            if not content and ws.draft_content_html:
                # If only HTML available, still try — DOCX generator has basic HTML handling
                content = ws.draft_content_html
            if content:
                docx_bytes = generate_docx_from_markdown(
                    content,
                    title=item.item_name,
                    orientation=(ws.page_orientation or "portrait"),
                )
                docx_path = os.path.join(output_dir, f"{safe_title}.docx")
                with open(docx_path, "wb") as f:
                    f.write(docx_bytes)
        except Exception as e:
            logger.warning(f"DOCX generation failed for document {gen_doc.id}: {e}")

    # Always attempt PDF as well (for preview)
    try:
        from app.services.pdf_generation_service import generate_pdf
        pdf_path = generate_pdf(
            db, gen_doc.id,
            orientation=(ws.page_orientation or "portrait"),
        )
    except Exception as e:
        logger.warning(f"PDF generation failed for document {gen_doc.id}: {e}")

    # Update completed count
    _update_completed_count(db, ws.tender_id)

    db.commit()

    return {
        "status": "finalized",
        "generated_document_id": gen_doc.id,
        "output_format": output_format,
        "pdf_path": pdf_path,
        "docx_path": docx_path,
        "xlsx_path": xlsx_path,
    }


def generate_workspace_document_pdf(db: Session, item_id: int, user_id: int) -> dict:
    """Create or refresh a GeneratedDocument for this checklist item using the
    current draft content, letterhead, and signatures, then render the PDF.

    Unlike `finalize_document`, this does NOT lock the workspace or change the
    review status — it's the on-demand "generate PDF" used by the editor.
    """
    ws = db.query(DocumentWorkspace).filter(DocumentWorkspace.checklist_item_id == item_id).first()
    if not ws:
        raise ValueError("Document workspace not found")

    item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
    if not item:
        raise ValueError("Checklist item not found")

    if not ws.draft_content_html and not ws.draft_content_markdown:
        raise ValueError("No content to render. Write or generate content first.")

    gen_doc = None
    if item.generated_document_id:
        gen_doc = db.query(GeneratedDocument).filter(
            GeneratedDocument.id == item.generated_document_id
        ).first()

    orientation = (ws.page_orientation or "portrait")

    if not gen_doc:
        gen_doc = GeneratedDocument(
            title=item.item_name,
            document_type="workspace_document",
            tender_id=ws.tender_id,
            content_html=ws.draft_content_html,
            content_markdown=ws.draft_content_markdown,
            letterhead_template_id=ws.letterhead_template_id,
            signatures=ws.signatures_json or [],
            page_orientation=orientation,
            status="generated",
            created_by=user_id,
        )
        db.add(gen_doc)
        db.flush()
        item.generated_document_id = gen_doc.id
    else:
        gen_doc.content_html = ws.draft_content_html
        gen_doc.content_markdown = ws.draft_content_markdown
        gen_doc.letterhead_template_id = ws.letterhead_template_id
        gen_doc.signatures = ws.signatures_json or []
        gen_doc.page_orientation = orientation
        gen_doc.status = "generated"

    from app.services.pdf_generation_service import generate_pdf
    generate_pdf(db, gen_doc.id, orientation=orientation)

    db.commit()
    db.refresh(gen_doc)

    return {
        "status": "generated",
        "generated_document_id": gen_doc.id,
        "file_name": gen_doc.generated_file_name,
        "file_size": gen_doc.file_size,
    }


def toggle_not_required(db: Session, item_id: int) -> dict:
    """Toggle is_not_required on a checklist item."""
    item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()
    if not item:
        raise ValueError("Checklist item not found")

    item.is_not_required = not (item.is_not_required or False)

    # If marking as not required, count it as "completed"
    if item.is_not_required:
        item.workspace_status = "approved"
        ws = db.query(DocumentWorkspace).filter(
            DocumentWorkspace.checklist_item_id == item_id
        ).first()
        if ws:
            ws.review_status = "approved"
    else:
        # Restore previous status
        ws = db.query(DocumentWorkspace).filter(
            DocumentWorkspace.checklist_item_id == item_id
        ).first()
        if ws:
            if ws.draft_content_html:
                ws.review_status = "drafting"
                item.workspace_status = "drafting"
            else:
                ws.review_status = "not_started"
                item.workspace_status = "not_started"

    _update_completed_count(db, item.tender_id)
    db.commit()

    return {
        "status": "toggled",
        "is_not_required": item.is_not_required,
    }


def get_workspace_stats(db: Session, tender_id: int) -> dict:
    """Get aggregate workspace stats."""
    items = db.query(ChecklistItem).filter(
        ChecklistItem.tender_id == tender_id
    ).all()

    workspace_map = {}
    workspaces = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.tender_id == tender_id
    ).all()
    for ws in workspaces:
        workspace_map[ws.checklist_item_id] = ws

    return _compute_stats(items, workspace_map)


def get_dependency_graph(db: Session, item_id: int) -> dict:
    """Get the dependency graph for a document workspace item."""
    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id
    ).first()
    if not ws:
        raise ValueError("Document workspace not found")

    def _build_dep_items(item_ids: list) -> list:
        if not item_ids:
            return []
        items = db.query(ChecklistItem).filter(
            ChecklistItem.id.in_(item_ids)
        ).all()
        result = []
        for item in items:
            dep_ws = db.query(DocumentWorkspace).filter(
                DocumentWorkspace.checklist_item_id == item.id
            ).first()
            result.append({
                "id": item.id,
                "item_name": item.item_name,
                "item_category": item.item_category or "standard",
                "review_status": dep_ws.review_status if dep_ws else "not_started",
                "has_content": bool(dep_ws and dep_ws.draft_content_html),
                "content_version": dep_ws.content_version if dep_ws else 0,
            })
        return result

    return {
        "depends_on": _build_dep_items(ws.depends_on or []),
        "referenced_by": _build_dep_items(ws.referenced_by or []),
    }


# ── Internal helpers ──────────────────────────────────────────────────────────


def _match_format_template(item_name: str, templates: list) -> Optional[int]:
    """Match a checklist item name against format template patterns."""
    name_lower = item_name.lower()
    for template in templates:
        patterns = template.match_patterns or []
        for pattern in patterns:
            if fnmatch.fnmatch(name_lower, pattern.lower()):
                return template.id
    return None


def _compute_stats(items: list, workspace_map: dict) -> dict:
    """Compute workspace statistics from items and workspace data."""
    by_category = {}
    by_status = {"not_started": 0, "drafting": 0, "in_review": 0, "approved": 0, "rejected": 0}
    completed = 0

    for item in items:
        cat = item.item_category or "standard"
        if cat not in by_category:
            by_category[cat] = {"total": 0, "completed": 0, "drafting": 0, "in_review": 0, "not_required": 0}

        by_category[cat]["total"] += 1

        ws = workspace_map.get(item.id)
        status = ws.review_status if ws else (item.workspace_status or "not_started")

        if status in by_status:
            by_status[status] += 1

        if item.is_not_required:
            by_category[cat]["not_required"] += 1
            by_category[cat]["completed"] += 1
            completed += 1
        elif status == "approved":
            by_category[cat]["completed"] += 1
            completed += 1
        elif status == "drafting":
            by_category[cat]["drafting"] += 1
        elif status == "in_review":
            by_category[cat]["in_review"] += 1

    total = len(items)
    return {
        "total_items": total,
        "completed_items": completed,
        "completion_percent": round((completed / total * 100) if total > 0 else 0, 1),
        "by_category": by_category,
        "by_status": by_status,
    }


def _ai_extract_format_instructions(db: Session, tender, items: list):
    """Use AI to infer format instructions for checklist items that weren't matched to templates.
    Updates format_instructions on unmatched DocumentWorkspace rows. Non-fatal on error.
    """
    import json

    # Find unmatched items (no template assigned)
    unmatched = []
    for item in items:
        ws = db.query(DocumentWorkspace).filter(
            DocumentWorkspace.checklist_item_id == item.id
        ).first()
        if ws and not ws.format_template_id and not ws.format_instructions:
            unmatched.append({
                "id": item.id,
                "name": item.item_name,
                "description": (item.item_description or "")[:100],
                "category": item.item_category or "standard",
            })

    if not unmatched:
        return

    # Limit batch size to avoid token overflow
    unmatched = unmatched[:20]

    # Get matched template names for context
    matched_templates = db.query(DocumentFormatTemplate).filter(
        DocumentFormatTemplate.is_active == True
    ).all()
    template_names = [t.name for t in matched_templates]

    system_prompt = (
        "You are a document formatting expert for Indian government and railway tender submissions. "
        "Given a tender's context and a list of required document names, infer the expected format "
        "and structure for each document. Return ONLY valid JSON — an array of objects with 'id' (int) "
        "and 'instructions' (string). The instructions should describe: document type, expected sections, "
        "formatting conventions (tables, headers, signatory blocks), and any content requirements "
        "you can infer from the document name and tender context. Keep each instruction concise (2-4 sentences)."
    )

    items_text = "\n".join(
        f"- ID {u['id']}: \"{u['name']}\" (category: {u['category']})"
        + (f" — {u['description']}" if u['description'] else "")
        for u in unmatched
    )

    user_prompt = (
        f"Tender: {tender.title}\n"
        f"Organisation: {tender.organisation or 'N/A'}\n"
        f"Department: {tender.department or 'N/A'}\n\n"
        f"Already matched to templates: {', '.join(template_names) if template_names else 'None'}\n\n"
        f"Documents needing format instructions:\n{items_text}\n\n"
        f"Return JSON array. Example: [{{'id': 1, 'instructions': 'This is a formal declaration...'}}]"
    )

    try:
        # Through call_ai: the platform's key and model, metered into
        # api_usage_logs and counted against the user's budget. This was a
        # raw SDK call pinned to a dated claude-sonnet-4 on the env key --
        # outside the ledger, the budget and the Agent Builder model choice.
        from app.services.ai_service import call_ai
        raw = _run_sync(call_ai(
            system_prompt, user_prompt, db, "workspace_format",
            max_tokens_override=2048,
        )).strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()

        results = json.loads(raw)
        if not isinstance(results, list):
            return

        id_map = {u["id"]: True for u in unmatched}
        for entry in results:
            entry_id = entry.get("id")
            instructions = entry.get("instructions", "").strip()
            if entry_id and entry_id in id_map and instructions:
                ws = db.query(DocumentWorkspace).filter(
                    DocumentWorkspace.checklist_item_id == entry_id
                ).first()
                if ws:
                    ws.format_instructions = instructions

        db.flush()
        logger.info(f"AI auto-extracted format instructions for {len(results)} items")
    except Exception as e:
        logger.warning(f"AI format extraction API call failed: {e}")


def _run_sync(coro):
    """Run a coroutine from sync code, whether or not a loop is running here."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    import contextvars
    ctx = contextvars.copy_context()  # keep the actor and run id for metering
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, asyncio.run, coro).result()


def _update_completed_count(db: Session, tender_id: int):
    """Recalculate and update the denormalized completed_items count."""
    config = db.query(WorkspaceConfig).filter(WorkspaceConfig.tender_id == tender_id).first()
    if not config:
        return

    items = db.query(ChecklistItem).filter(ChecklistItem.tender_id == tender_id).all()
    completed = sum(
        1 for item in items
        if (item.is_not_required or False) or (item.workspace_status or "not_started") == "approved"
    )
    config.completed_items = completed
    config.last_activity_at = datetime.now(timezone.utc)
