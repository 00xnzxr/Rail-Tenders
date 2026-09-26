"""
DRPL Backend - Training Dataset Service
Business logic for managing named training datasets, their files,
and agent-dataset assignments.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.training_dataset import TrainingDataset, TrainingDatasetFile, AgentTrainingDataset

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".md", ".json", ".jsonl", ".txt"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


# ──────────────────────────────────────────────
# Dataset CRUD
# ──────────────────────────────────────────────

def create_dataset(
    db: Session,
    name: str,
    description: Optional[str] = None,
    tags: list[str] = None,
    created_by: Optional[int] = None,
) -> TrainingDataset:
    """Create a new training dataset."""
    ds = TrainingDataset(
        name=name.strip(),
        description=description,
        tags=tags or [],
        created_by=created_by,
    )
    try:
        db.add(ds)
        db.commit()
        db.refresh(ds)
    except IntegrityError:
        db.rollback()
        raise ValueError(f"A dataset named '{name}' already exists")
    logger.info(f"Created training dataset '{name}' (id={ds.id})")
    return ds


def list_datasets(db: Session, status_filter: Optional[str] = None) -> list[dict]:
    """List all datasets with file counts."""
    from sqlalchemy import func

    q = db.query(TrainingDataset)
    if status_filter:
        q = q.filter(TrainingDataset.status == status_filter)
    datasets = q.order_by(TrainingDataset.created_at.desc()).all()

    # Get file counts in one query
    file_counts = dict(
        db.query(TrainingDatasetFile.dataset_id, func.count(TrainingDatasetFile.id))
        .group_by(TrainingDatasetFile.dataset_id)
        .all()
    )

    # Get total sizes
    file_sizes = dict(
        db.query(TrainingDatasetFile.dataset_id, func.sum(TrainingDatasetFile.file_size))
        .group_by(TrainingDatasetFile.dataset_id)
        .all()
    )

    result = []
    for ds in datasets:
        result.append({
            "id": ds.id,
            "name": ds.name,
            "description": ds.description,
            "tags": ds.tags or [],
            "status": ds.status,
            "file_count": file_counts.get(ds.id, 0),
            "total_size": file_sizes.get(ds.id, 0) or 0,
            "created_by": ds.created_by,
            "created_at": ds.created_at.isoformat() if ds.created_at else None,
            "updated_at": ds.updated_at.isoformat() if ds.updated_at else None,
        })
    return result


def get_dataset(db: Session, dataset_id: int) -> Optional[TrainingDataset]:
    """Get a single dataset by ID."""
    return db.query(TrainingDataset).filter(TrainingDataset.id == dataset_id).first()


def get_dataset_with_files(db: Session, dataset_id: int) -> Optional[dict]:
    """Get a dataset with all its files (metadata only, not full content)."""
    ds = get_dataset(db, dataset_id)
    if not ds:
        return None

    files = (
        db.query(TrainingDatasetFile)
        .filter(TrainingDatasetFile.dataset_id == dataset_id)
        .order_by(TrainingDatasetFile.uploaded_at.desc())
        .all()
    )

    return {
        "id": ds.id,
        "name": ds.name,
        "description": ds.description,
        "tags": ds.tags or [],
        "status": ds.status,
        "created_by": ds.created_by,
        "created_at": ds.created_at.isoformat() if ds.created_at else None,
        "updated_at": ds.updated_at.isoformat() if ds.updated_at else None,
        "files": [
            {
                "id": f.id,
                "file_name": f.file_name,
                "file_type": f.file_type,
                "file_size": f.file_size,
                "extraction_status": f.extraction_status,
                "extraction_error": f.extraction_error,
                "uploaded_at": f.uploaded_at.isoformat() if f.uploaded_at else None,
            }
            for f in files
        ],
    }


def update_dataset(
    db: Session,
    dataset_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    status: Optional[str] = None,
) -> Optional[TrainingDataset]:
    """Update dataset metadata."""
    ds = get_dataset(db, dataset_id)
    if not ds:
        return None

    if name is not None:
        ds.name = name.strip()
    if description is not None:
        ds.description = description
    if tags is not None:
        ds.tags = tags
    if status is not None:
        ds.status = status

    try:
        db.commit()
        db.refresh(ds)
    except IntegrityError:
        db.rollback()
        raise ValueError(f"A dataset named '{name}' already exists")
    return ds


def delete_dataset(db: Session, dataset_id: int) -> bool:
    """Delete a dataset and all its files and agent associations."""
    ds = get_dataset(db, dataset_id)
    if not ds:
        return False

    # Delete agent associations
    db.query(AgentTrainingDataset).filter(AgentTrainingDataset.dataset_id == dataset_id).delete()
    # Delete files
    db.query(TrainingDatasetFile).filter(TrainingDatasetFile.dataset_id == dataset_id).delete()
    # Delete dataset
    db.delete(ds)
    db.commit()
    logger.info(f"Deleted training dataset id={dataset_id}")
    return True


# ──────────────────────────────────────────────
# File Management
# ──────────────────────────────────────────────

def upload_file_to_dataset(
    db: Session,
    dataset_id: int,
    file_name: str,
    file_content_bytes: bytes,
    uploaded_by: Optional[int] = None,
) -> TrainingDatasetFile:
    """Upload and parse a file into a dataset."""
    # Validate dataset exists
    ds = get_dataset(db, dataset_id)
    if not ds:
        raise ValueError(f"Dataset {dataset_id} not found")

    # Validate extension
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type '{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")

    # Validate size
    if len(file_content_bytes) > MAX_FILE_SIZE:
        raise ValueError(f"File too large ({len(file_content_bytes):,} bytes). Maximum: {MAX_FILE_SIZE:,} bytes")

    if len(file_content_bytes) == 0:
        raise ValueError("File is empty")

    # Decode content
    raw_content = file_content_bytes.decode("utf-8", errors="replace")

    # Map extension to file type
    file_type_map = {".md": "md", ".json": "json", ".jsonl": "jsonl", ".txt": "txt"}
    file_type = file_type_map.get(ext, "txt")

    # Parse content based on type
    parsed_content = None
    extraction_status = "completed"
    extraction_error = None

    try:
        parsed_content = _parse_file_content(raw_content, file_type, file_name)
    except Exception as e:
        extraction_status = "failed"
        extraction_error = str(e)
        parsed_content = raw_content  # Fallback to raw content
        logger.warning(f"Failed to parse {file_name}: {e}")

    # Create record
    f = TrainingDatasetFile(
        dataset_id=dataset_id,
        file_name=file_name,
        file_type=file_type,
        file_size=len(file_content_bytes),
        raw_content=raw_content,
        parsed_content=parsed_content,
        extraction_status=extraction_status,
        extraction_error=extraction_error,
        uploaded_by=uploaded_by,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    logger.info(f"Uploaded file '{file_name}' to dataset {dataset_id} (id={f.id}, status={extraction_status})")
    return f


def _parse_file_content(raw_content: str, file_type: str, file_name: str) -> str:
    """Parse file content into a clean format for context injection."""
    if file_type in ("md", "txt"):
        # Markdown and text files: use as-is
        return raw_content.strip()

    elif file_type == "json":
        # JSON: pretty-print for readability
        parsed = json.loads(raw_content)
        if isinstance(parsed, list):
            # Array of objects: format as numbered entries
            lines = []
            for i, item in enumerate(parsed, 1):
                if isinstance(item, dict):
                    parts = [f"  {k}: {v}" for k, v in item.items()]
                    lines.append(f"Entry {i}:\n" + "\n".join(parts))
                else:
                    lines.append(f"Entry {i}: {item}")
            return "\n\n".join(lines)
        elif isinstance(parsed, dict):
            return json.dumps(parsed, indent=2, ensure_ascii=False)
        else:
            return str(parsed)

    elif file_type == "jsonl":
        # JSONL: parse each line, format as numbered entries
        lines = raw_content.strip().split("\n")
        entries = []
        for i, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    parts = [f"  {k}: {v}" for k, v in obj.items()]
                    entries.append(f"Entry {i}:\n" + "\n".join(parts))
                else:
                    entries.append(f"Entry {i}: {obj}")
            except json.JSONDecodeError:
                entries.append(f"Entry {i}: {line}")
        return "\n\n".join(entries)

    return raw_content.strip()


def delete_file(db: Session, file_id: int) -> bool:
    """Delete a single file from a dataset."""
    f = db.query(TrainingDatasetFile).filter(TrainingDatasetFile.id == file_id).first()
    if not f:
        return False
    db.delete(f)
    db.commit()
    return True


def get_file(db: Session, file_id: int) -> Optional[TrainingDatasetFile]:
    """Get a single file by ID (includes content)."""
    return db.query(TrainingDatasetFile).filter(TrainingDatasetFile.id == file_id).first()


def get_dataset_files(db: Session, dataset_id: int) -> list[TrainingDatasetFile]:
    """Get all files for a dataset."""
    return (
        db.query(TrainingDatasetFile)
        .filter(TrainingDatasetFile.dataset_id == dataset_id)
        .order_by(TrainingDatasetFile.uploaded_at.desc())
        .all()
    )


# ──────────────────────────────────────────────
# Context Generation
# ──────────────────────────────────────────────

def get_dataset_context(db: Session, dataset_id: int) -> str:
    """Format all completed files in a dataset into a single context string."""
    ds = get_dataset(db, dataset_id)
    if not ds:
        return ""

    files = (
        db.query(TrainingDatasetFile)
        .filter(
            TrainingDatasetFile.dataset_id == dataset_id,
            TrainingDatasetFile.extraction_status == "completed",
        )
        .order_by(TrainingDatasetFile.uploaded_at.asc())
        .all()
    )

    if not files:
        return ""

    sections = [f"=== Training Dataset: {ds.name} ==="]
    if ds.description:
        sections.append(ds.description)
    sections.append("")

    for f in files:
        content = f.parsed_content or f.raw_content
        if content and content.strip():
            sections.append(f"--- {f.file_name} ---")
            sections.append(content.strip())
            sections.append("")

    return "\n".join(sections)


def get_agent_training_context(db: Session, agent_id: int, max_chars: int = 500_000) -> str:
    """
    Get combined training context for an agent from all its assigned datasets.
    Returns formatted string ready for system prompt injection.

    Args:
        db: Database session
        agent_id: Agent to load datasets for
        max_chars: Maximum character budget for training data context
    """
    # Get assigned datasets ordered by priority
    assignments = (
        db.query(AgentTrainingDataset)
        .filter(AgentTrainingDataset.agent_id == agent_id)
        .order_by(AgentTrainingDataset.priority.asc())
        .all()
    )

    if not assignments:
        return ""

    contexts = []
    total_chars = 0
    MAX_CONTEXT_CHARS = max_chars

    for assignment in assignments:
        ds = get_dataset(db, assignment.dataset_id)
        if not ds or ds.status != "active":
            continue

        ctx = get_dataset_context(db, assignment.dataset_id)
        if ctx:
            if total_chars + len(ctx) > MAX_CONTEXT_CHARS:
                remaining = MAX_CONTEXT_CHARS - total_chars
                if remaining > 1000:
                    ctx = ctx[:remaining] + "\n\n[... Training data truncated to fit context window ...]"
                    contexts.append(ctx)
                break
            contexts.append(ctx)
            total_chars += len(ctx)

    if not contexts:
        return ""

    header = "\n\n# ═══ TRAINING DATA ═══\nThe following training datasets have been assigned to you. Use this knowledge to inform your analysis and responses.\n\n"
    return header + "\n\n".join(contexts)


# ──────────────────────────────────────────────
# Agent-Dataset Assignment
# ──────────────────────────────────────────────

def assign_dataset_to_agent(
    db: Session,
    agent_id: int,
    dataset_id: int,
    priority: int = 0,
) -> AgentTrainingDataset:
    """Assign a training dataset to an agent."""
    # Verify both exist
    ds = get_dataset(db, dataset_id)
    if not ds:
        raise ValueError(f"Dataset {dataset_id} not found")

    from app.models.agent_builder import CustomAgent
    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        raise ValueError(f"Agent {agent_id} not found")

    link = AgentTrainingDataset(
        agent_id=agent_id,
        dataset_id=dataset_id,
        priority=priority,
    )
    try:
        db.add(link)
        db.commit()
        db.refresh(link)
    except IntegrityError:
        db.rollback()
        raise ValueError(f"Dataset '{ds.name}' is already assigned to agent '{agent.name}'")

    logger.info(f"Assigned dataset '{ds.name}' to agent '{agent.name}' (priority={priority})")
    return link


def build_training_jsonl_from_tender(db: Session, tender_id: int) -> "tuple[str, bytes]":
    """Build a JSONL rate-card from a tender's captured BOQItem rows.

    One JSON object per line item, with keys the costing agent's keyword
    retrieval matches on (description, item_code, unit, published rate, schedule,
    tender_ref). Returns (filename, utf-8 bytes). Raises ValueError when the
    tender has no captured schedule.

    The rates are the NIT's PUBLISHED (benchmark) rates — useful as cost anchors
    for similar future tenders; the firm can append actual procurement costs
    later for stronger training.
    """
    from app.models.costing_template import BOQItem
    from app.models.tender import Tender

    rows = (
        db.query(BOQItem)
        .filter(BOQItem.tender_id == tender_id)
        .order_by(BOQItem.schedule_name.asc().nullsfirst(), BOQItem.sr_no.asc())
        .all()
    )
    if not rows:
        raise ValueError(f"Tender {tender_id} has no captured BOQ schedule to ingest")

    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    # Tender.tender_id is the portal's own tender number (e.g. 232-TW-EMU-...).
    tender_ref = ""
    if tender is not None:
        tender_ref = (
            getattr(tender, "tender_id", None)
            or getattr(tender, "title", None)
            or f"tender-{tender_id}"
        )

    lines: list[str] = []
    for r in rows:
        obj = {
            "tender_ref": tender_ref,
            "schedule": r.schedule_name or "",
            "item_code": r.item_code or "",
            "description": r.description or "",
            "quantity": r.quantity,
            "unit": r.unit or "",
            "published_rate_inr": r.estimated_rate,
            "basic_value_inr": r.basic_value,
            "is_tax_line": bool(r.is_tax_line),
            "source": f"NIT benchmark rate — {tender_ref}".strip(),
        }
        lines.append(json.dumps(obj, ensure_ascii=False, default=str))

    content = "\n".join(lines)
    safe_ref = re.sub(r"[^A-Za-z0-9._-]+", "_", str(tender_ref))[:60] or f"tender_{tender_id}"
    filename = f"boq_rates_{safe_ref}.jsonl"
    return filename, content.encode("utf-8")


def promote_tender_to_training(
    db: Session,
    tender_id: int,
    *,
    created_by: Optional[int] = None,
    name: Optional[str] = None,
    tags: Optional[list[str]] = None,
    assign_priority: int = 0,
) -> dict:
    """Turn a tender's priced BOQ into a training dataset and assign it to the
    costing_researcher agent so future similar tenders can retrieve these
    benchmark rates. Reuses create_dataset / upload_file_to_dataset /
    assign_dataset_to_agent. Returns a summary dict.
    """
    filename, content_bytes = build_training_jsonl_from_tender(db, tender_id)

    ds_name = name or f"BOQ rates — tender #{tender_id}"
    description = (
        "Auto-ingested priced Bill-of-Quantities from a tender NIT. Rates are "
        "the NIT's PUBLISHED (benchmark) rates per line item — useful as cost "
        "anchors for similar tenders. Append the firm's actual procurement "
        "costs later for stronger training."
    )
    try:
        ds = create_dataset(
            db, name=ds_name, description=description,
            tags=tags or ["boq", "nit-benchmark"], created_by=created_by,
        )
    except ValueError:
        # Name collision — disambiguate with the generated filename.
        ds = create_dataset(
            db, name=f"{ds_name} ({filename})", description=description,
            tags=tags or ["boq", "nit-benchmark"], created_by=created_by,
        )

    f = upload_file_to_dataset(db, ds.id, filename, content_bytes, uploaded_by=created_by)

    assigned = False
    assign_error: Optional[str] = None
    try:
        from app.models.agent_builder import CustomAgent
        agent = (
            db.query(CustomAgent)
            .filter(CustomAgent.agent_key == "costing_researcher")
            .first()
        )
        if agent:
            try:
                assign_dataset_to_agent(db, agent.id, ds.id, priority=assign_priority)
                assigned = True
            except ValueError as ve:
                assign_error = str(ve)  # e.g. already assigned
        else:
            assign_error = "costing_researcher agent not found"
    except Exception as e:
        assign_error = str(e)

    line_count = len(content_bytes.decode("utf-8").splitlines())
    logger.info(
        f"[training] promoted tender {tender_id} → dataset id={ds.id} "
        f"({line_count} line(s); assigned={assigned})"
    )
    return {
        "dataset_id": ds.id,
        "dataset_name": ds.name,
        "file_id": f.id,
        "file_name": filename,
        "line_count": line_count,
        "assigned_to_costing_researcher": assigned,
        "assign_error": assign_error,
    }


def unassign_dataset_from_agent(db: Session, agent_id: int, dataset_id: int) -> bool:
    """Remove a dataset assignment from an agent."""
    deleted = (
        db.query(AgentTrainingDataset)
        .filter(
            AgentTrainingDataset.agent_id == agent_id,
            AgentTrainingDataset.dataset_id == dataset_id,
        )
        .delete()
    )
    db.commit()
    return deleted > 0


def get_agent_datasets(db: Session, agent_id: int) -> list[dict]:
    """Get all datasets assigned to an agent with their metadata."""
    assignments = (
        db.query(AgentTrainingDataset)
        .filter(AgentTrainingDataset.agent_id == agent_id)
        .order_by(AgentTrainingDataset.priority.asc())
        .all()
    )

    result = []
    for a in assignments:
        ds = get_dataset(db, a.dataset_id)
        if ds:
            from sqlalchemy import func
            file_count = (
                db.query(func.count(TrainingDatasetFile.id))
                .filter(TrainingDatasetFile.dataset_id == ds.id)
                .scalar()
            )
            result.append({
                "assignment_id": a.id,
                "dataset_id": ds.id,
                "name": ds.name,
                "description": ds.description,
                "tags": ds.tags or [],
                "status": ds.status,
                "priority": a.priority,
                "file_count": file_count,
                "assigned_at": a.created_at.isoformat() if a.created_at else None,
            })
    return result


def update_assignment_priority(db: Session, agent_id: int, dataset_id: int, priority: int) -> bool:
    """Update the priority of an agent-dataset assignment."""
    link = (
        db.query(AgentTrainingDataset)
        .filter(
            AgentTrainingDataset.agent_id == agent_id,
            AgentTrainingDataset.dataset_id == dataset_id,
        )
        .first()
    )
    if not link:
        return False
    link.priority = priority
    db.commit()
    return True
