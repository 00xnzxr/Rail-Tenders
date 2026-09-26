"""
DRPL Backend - Artifact Service
CRUD, versioning, and extraction for Command Center artifacts.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.artifact import CommandCenterArtifact

logger = logging.getLogger(__name__)


# ───────────────────────── Display sanitisers ──────────────────────────────
#
# The tender analyzer is markdown-only now, but prompt-cache hits from
# older versions and legacy artifacts already stored in the DB may still
# carry a trailing ```json block. These regexes match only a trailing
# block (anchored to end-of-string) so inline JSON examples in the prose
# are preserved.
_ANALYSIS_TRAILING_FENCED_JSON = re.compile(
    r"\n*\s*```(?:json|JSON)?\s*\{[\s\S]*?\}\s*```\s*\Z",
    re.MULTILINE,
)
_ANALYSIS_TRAILING_BARE_JSON = re.compile(
    r"\n*\s*\{[\s\S]*\}\s*\Z",
    re.MULTILINE,
)


def _strip_analysis_json_tail(content: str) -> str:
    """Strip a trailing JSON block from a tender-analysis report.

    Anchored to end-of-string so inline JSON examples in the report body
    are preserved. Defends against prompt-cache hits from the old
    synthesis prompt that still emit a fenced JSON tail.
    """
    if not content:
        return content
    cleaned = _ANALYSIS_TRAILING_FENCED_JSON.sub("", content)
    if cleaned == content:
        cleaned = _ANALYSIS_TRAILING_BARE_JSON.sub("", content)
    return cleaned.rstrip()

# Maps agent output_type → artifact_type + default title
ARTIFACT_TYPE_MAP = {
    "document_analysis": {"artifact_type": "analysis", "title": "Tender Analysis"},
    "checklist": {"artifact_type": "checklist", "title": "Submission Checklist"},
    "proposal_document": {"artifact_type": "document", "title": "Proposal Document"},
    "cost_breakdown": {"artifact_type": "cost_breakdown", "title": "Cost Breakdown"},
    "annexures_extracted": {"artifact_type": "annexures", "title": "Annexures Extracted"},
}


# Per-artifact-type "is the underlying data meaningful?" guards. Without these,
# an agent that emits prose but no structured data still spawns an artifact
# card that promises a row the user can't actually open — the "lying artifact
# card" bug. The check runs in extract_artifact_from_output / build_artifact_payload
# before any CommandCenterArtifact row is created.
#
# Each callable receives the agent's `structured_data` dict (may be None) and
# returns True when the artifact is safe to create, False to suppress it.
# Artifact types not in this map have no guard (legacy behaviour preserved).
def _cost_breakdown_has_line_items(structured_data: Optional[dict]) -> bool:
    """A cost_breakdown artifact is only meaningful when the agent produced at
    least one line item — even a single `needs_user_input` line is acceptable
    (the editor will surface the agent's question). Empty or missing
    line_items means persistence will silently no-op, so the artifact card
    would lie about its underlying data.
    """
    if not isinstance(structured_data, dict):
        return False
    line_items = structured_data.get("line_items")
    return isinstance(line_items, list) and len(line_items) > 0


_PAYLOAD_GUARDS: dict[str, Callable[[Optional[dict]], bool]] = {
    "cost_breakdown": _cost_breakdown_has_line_items,
}


def _payload_passes_guard(artifact_type: str, structured_data: Optional[dict]) -> bool:
    """Run the per-artifact-type guard. Returns True (allow) when no guard
    is registered for this artifact_type, so existing artifact types keep
    their current behaviour.
    """
    guard = _PAYLOAD_GUARDS.get(artifact_type)
    if guard is None:
        return True
    try:
        return bool(guard(structured_data))
    except Exception as e:
        # A buggy guard must not block legitimate artifacts — log and allow.
        logger.warning(
            f"[artifact] payload guard for {artifact_type!r} raised {e!r}; allowing"
        )
        return True

# Maps built-in agent_key → (artifact_type, default title). Used by the plan
# executor (decision_maker_agent._run_single_plan_step) so each step's output
# becomes a visible artifact regardless of how the agent self-reports its
# output_type. Anything not in this table is treated as a generic markdown
# artifact and titled from the agent's display_name.
AGENT_ARTIFACT_MAP = {
    "deep_analyzer":       {"artifact_type": "analysis",       "title": "Tender Analysis"},
    "checklist_generator": {"artifact_type": "checklist",      "title": "Submission Checklist"},
    "proposal_creator":    {"artifact_type": "document",       "title": "Proposal Document"},
    "costing_researcher":  {"artifact_type": "cost_breakdown", "title": "Cost Breakdown"},
    "annexure_finder":     {"artifact_type": "annexures",      "title": "Annexures Extracted"},
    "workspace_manager":   {"artifact_type": "workspace",      "title": "Workspace Update"},
}


def create_artifact(
    db: Session,
    session_id: int,
    artifact_type: str,
    title: str,
    content: str,
    structured_data: Optional[dict] = None,
    agent_key: Optional[str] = None,
    message_id: Optional[int] = None,
    created_by: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> CommandCenterArtifact:
    """Create a new artifact in a session."""
    artifact = CommandCenterArtifact(
        session_id=session_id,
        artifact_type=artifact_type,
        title=title,
        content=content,
        structured_data=structured_data,
        version=1,
        agent_key=agent_key,
        message_id=message_id,
        created_by=created_by,
        metadata_json=metadata,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    logger.info(f"Created artifact '{title}' (type={artifact_type}, id={artifact.id}) for session {session_id}")
    return artifact


def update_artifact(
    db: Session,
    artifact_id: int,
    content: str,
    structured_data: Optional[dict] = None,
    updated_by: Optional[int] = None,
) -> Optional[CommandCenterArtifact]:
    """
    Update an artifact by creating a new version.
    The new row points back to the original via parent_id.
    """
    original = db.query(CommandCenterArtifact).filter(CommandCenterArtifact.id == artifact_id).first()
    if not original:
        return None

    # Find the root artifact (walk parent chain to get the first one)
    root_id = _get_root_id(db, original)

    # Get the latest version number for this chain
    max_version = (
        db.query(func.max(CommandCenterArtifact.version))
        .filter(
            (CommandCenterArtifact.id == root_id) |
            (CommandCenterArtifact.parent_id == root_id)
        )
        .scalar() or 1
    )

    new_version = CommandCenterArtifact(
        session_id=original.session_id,
        artifact_type=original.artifact_type,
        title=original.title,
        content=content,
        structured_data=structured_data if structured_data is not None else original.structured_data,
        version=max_version + 1,
        parent_id=root_id,
        agent_key=original.agent_key,
        message_id=original.message_id,
        is_pinned=original.is_pinned,
        status=original.status,
        created_by=updated_by,
        metadata_json=original.metadata_json,
    )
    db.add(new_version)
    db.commit()
    db.refresh(new_version)
    logger.info(f"Updated artifact '{original.title}' to v{new_version.version} (new id={new_version.id})")
    return new_version


def list_artifacts(
    db: Session,
    session_id: int,
    artifact_type: Optional[str] = None,
) -> list[dict]:
    """
    List the latest version of each artifact chain in a session.
    Returns dicts with artifact data.
    """
    query = db.query(CommandCenterArtifact).filter(
        CommandCenterArtifact.session_id == session_id
    )
    if artifact_type:
        query = query.filter(CommandCenterArtifact.artifact_type == artifact_type)

    all_artifacts = query.order_by(CommandCenterArtifact.created_at.desc()).all()

    # Group by chain: root artifacts (parent_id is None) are chain roots
    # For each root, find the latest version
    roots = {}
    children = {}  # root_id → list of versions

    for a in all_artifacts:
        if a.parent_id is None:
            roots[a.id] = a
            if a.id not in children:
                children[a.id] = []
        else:
            if a.parent_id not in children:
                children[a.parent_id] = []
            children[a.parent_id].append(a)

    result = []
    for root_id, root in roots.items():
        versions = children.get(root_id, [])
        if versions:
            # Latest version is the one with highest version number
            latest = max(versions, key=lambda x: x.version)
        else:
            latest = root

        result.append(_artifact_to_dict(latest))

    # Sort by pinned first, then created_at desc
    result.sort(key=lambda x: (not x["is_pinned"], x["created_at"]), reverse=False)
    return result


def get_artifact(db: Session, artifact_id: int) -> Optional[CommandCenterArtifact]:
    """Get a single artifact by ID."""
    return db.query(CommandCenterArtifact).filter(CommandCenterArtifact.id == artifact_id).first()


def get_artifact_file_ref(db: Session, artifact_id: int):
    """Load only the columns the file-metadata endpoints actually read.

    `get_artifact` selects the whole row, and on a costing artifact `content`
    and `structured_data` each hold the full line-item JSON — the same payload
    stored twice, megabytes on a large schedule. `download-url` reads two
    strings out of that, and `preview` is polled every three seconds for as
    long as the preview pane is open, so dragging the payload across the wire
    costs far more than the answer it produces. Both run at the exact moment
    the user is waiting for their spreadsheet.

    Returns a row exposing `id`, `session_id`, `file_path`, `file_name` and
    `metadata_json` by attribute, which is all `assert_artifact_access` and
    those two endpoints need. `None` when no such artifact exists.
    """
    return (
        db.query(
            CommandCenterArtifact.id,
            CommandCenterArtifact.session_id,
            CommandCenterArtifact.file_path,
            CommandCenterArtifact.file_name,
            CommandCenterArtifact.metadata_json,
        )
        .filter(CommandCenterArtifact.id == artifact_id)
        .first()
    )


def get_artifact_versions(db: Session, artifact_id: int) -> list[dict]:
    """Get all versions in an artifact's chain."""
    artifact = db.query(CommandCenterArtifact).filter(CommandCenterArtifact.id == artifact_id).first()
    if not artifact:
        return []

    root_id = _get_root_id(db, artifact)

    versions = db.query(CommandCenterArtifact).filter(
        (CommandCenterArtifact.id == root_id) |
        (CommandCenterArtifact.parent_id == root_id)
    ).order_by(CommandCenterArtifact.version.desc()).all()

    return [_artifact_to_dict(v) for v in versions]


def pin_artifact(db: Session, artifact_id: int, pinned: bool = True) -> Optional[CommandCenterArtifact]:
    """Pin or unpin an artifact."""
    artifact = db.query(CommandCenterArtifact).filter(CommandCenterArtifact.id == artifact_id).first()
    if not artifact:
        return None
    artifact.is_pinned = pinned
    db.commit()
    db.refresh(artifact)
    return artifact


def extract_artifact_from_output(
    agent_key: str,
    output: str,
    output_type: str,
    structured_data: Optional[dict] = None,
) -> Optional[dict]:
    """
    Determine if an agent's output qualifies as an artifact.
    Returns artifact creation metadata if yes, None if no.

    Per-artifact-type payload guards (see `_PAYLOAD_GUARDS`) suppress
    creation when the underlying data is missing — e.g. cost_breakdown
    only spawns an artifact when structured_data carries non-empty
    line_items. This prevents the "lying artifact card" failure mode
    where the badge appears but the editor 404s.
    """
    mapping = ARTIFACT_TYPE_MAP.get(output_type)
    if not mapping:
        return None

    # Skip very short outputs that aren't meaningful artifacts
    if len(output.strip()) < 50:
        return None

    artifact_type = mapping["artifact_type"]
    if not _payload_passes_guard(artifact_type, structured_data):
        logger.info(
            f"[artifact] suppressing {artifact_type!r} artifact for agent "
            f"{agent_key!r}: payload guard returned False (structured_data "
            f"missing required content)"
        )
        return None

    # Defensive cleanup: strip any machine-readable markers from display content
    clean_output = output
    try:
        from app.services.langchain.graphs.chat_agent_wrappers import _strip_checklist_markers
        clean_output = _strip_checklist_markers(output)
    except Exception:
        pass
    if output_type == "document_analysis":
        clean_output = _strip_analysis_json_tail(clean_output)
        # Tender analysis is markdown-only now; never persist structured_data
        # for these artifacts (the artifact panel renders the markdown directly).
        structured_data = None

    return {
        "artifact_type": artifact_type,
        "title": mapping["title"],
        "content": clean_output,
        "structured_data": structured_data,
        "agent_key": agent_key,
    }


def resolve_artifact_meta_for_agent(
    agent_key: str,
    display_name: Optional[str] = None,
) -> Optional[dict]:
    """Pick artifact_type + title from an agent_key (and optional display_name).

    Built-in wrappers use AGENT_ARTIFACT_MAP. Any other key (custom Agent
    Builder agents like ``tender_doc_analyzer``) falls back to a generic
    markdown ``document`` artifact, titled from display_name → agent_key →
    ``"Agent Output"``.
    """
    if not agent_key:
        return None
    mapping = AGENT_ARTIFACT_MAP.get(agent_key)
    if mapping:
        return {"artifact_type": mapping["artifact_type"], "title": mapping["title"]}
    title = (display_name or "").strip() or agent_key.strip() or "Agent Output"
    return {"artifact_type": "document", "title": title}


def build_artifact_payload(
    agent_key: str,
    output: str,
    display_name: Optional[str] = None,
    structured_data: Optional[dict] = None,
) -> Optional[dict]:
    """Build the full artifact dict for the plan-step path.

    Mirrors ``extract_artifact_from_output`` but resolves metadata from the
    ``agent_key`` (which is always known at the plan executor) instead of the
    fragile ``output_type`` string. Returns None for empty / sub-50-char output
    or unresolvable agents.
    """
    if not output or len(output.strip()) < 50:
        return None
    meta = resolve_artifact_meta_for_agent(agent_key, display_name)
    if not meta:
        return None
    if not _payload_passes_guard(meta["artifact_type"], structured_data):
        logger.info(
            f"[artifact] suppressing {meta['artifact_type']!r} artifact for agent "
            f"{agent_key!r} (plan-step path): payload guard returned False"
        )
        return None
    clean_output = output
    try:
        from app.services.langchain.graphs.chat_agent_wrappers import _strip_checklist_markers
        clean_output = _strip_checklist_markers(output)
    except Exception:
        pass
    if meta["artifact_type"] == "analysis":
        clean_output = _strip_analysis_json_tail(clean_output)
    return {
        "artifact_type": meta["artifact_type"],
        "title": meta["title"],
        "content": clean_output,
        "structured_data": structured_data,
        "agent_key": agent_key,
    }


def _get_root_id(db: Session, artifact: CommandCenterArtifact) -> int:
    """Walk parent chain to find root artifact ID."""
    if artifact.parent_id is None:
        return artifact.id
    # One level is enough since children always point to root
    return artifact.parent_id


def _artifact_to_dict(artifact: CommandCenterArtifact) -> dict:
    """Convert artifact to dict for API responses."""
    return {
        "id": artifact.id,
        "session_id": artifact.session_id,
        "artifact_type": artifact.artifact_type,
        "title": artifact.title,
        "content": artifact.content,
        "structured_data": artifact.structured_data,
        "version": artifact.version,
        "parent_id": artifact.parent_id,
        "agent_key": artifact.agent_key,
        "message_id": artifact.message_id,
        "file_path": artifact.file_path,
        "file_name": artifact.file_name,
        "is_pinned": artifact.is_pinned,
        "status": artifact.status,
        "metadata_json": artifact.metadata_json,
        "created_by": artifact.created_by,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "updated_at": artifact.updated_at.isoformat() if artifact.updated_at else None,
    }
