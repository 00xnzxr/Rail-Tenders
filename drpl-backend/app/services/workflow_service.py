"""
DRPL Backend - Workflow CRUD Service
Create, update, delete, publish, rollback, clone, and validate workflows.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models.workflow import (
    Workflow, WorkflowVersion, WorkflowNode, WorkflowEdge,
    WorkflowExecution, WorkflowNodeExecution,
)

logger = logging.getLogger(__name__)

VALID_NODE_TYPES = {
    "start", "end", "agent", "classify", "if_else",
    "while_loop", "user_approval", "transform", "set_state", "tool", "note",
    "for_each", "parallel",
}


def _slugify(name: str) -> str:
    """Convert display name to a URL-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:100]


# ─── CRUD ───────────────────────────────────────────────────────────────────


def create_workflow(
    db: Session,
    display_name: str,
    description: str = None,
    category: str = None,
    trigger_type: str = "manual",
    user_id: int = None,
) -> Workflow:
    """Create a new workflow with default start and end nodes."""
    workflow_key = _slugify(display_name)

    # Ensure unique key
    existing = db.query(Workflow).filter(Workflow.workflow_key == workflow_key).first()
    if existing:
        import time
        workflow_key = f"{workflow_key}_{int(time.time())}"

    workflow = Workflow(
        workflow_key=workflow_key,
        display_name=display_name,
        description=description,
        category=category,
        trigger_type=trigger_type,
        created_by=user_id,
    )
    db.add(workflow)
    db.flush()

    # Create default start and end nodes
    start_node = WorkflowNode(
        workflow_id=workflow.id,
        node_key="start",
        node_type="start",
        display_name="Start",
        position_x=100,
        position_y=300,
        config={},
        sort_order=0,
    )
    end_node = WorkflowNode(
        workflow_id=workflow.id,
        node_key="end",
        node_type="end",
        display_name="End",
        position_x=800,
        position_y=300,
        config={},
        sort_order=999,
    )
    db.add_all([start_node, end_node])
    db.commit()
    db.refresh(workflow)
    return workflow


def get_workflow(db: Session, workflow_id: int) -> Optional[dict]:
    """Get workflow with all nodes and edges."""
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not workflow:
        return None

    nodes = (
        db.query(WorkflowNode)
        .filter(WorkflowNode.workflow_id == workflow_id)
        .order_by(WorkflowNode.sort_order)
        .all()
    )
    edges = (
        db.query(WorkflowEdge)
        .filter(WorkflowEdge.workflow_id == workflow_id)
        .order_by(WorkflowEdge.sort_order)
        .all()
    )

    return {
        "id": workflow.id,
        "workflow_key": workflow.workflow_key,
        "display_name": workflow.display_name,
        "description": workflow.description,
        "category": workflow.category,
        "tags": workflow.tags or [],
        "current_version": workflow.current_version,
        "status": workflow.status,
        "is_default_router": workflow.is_default_router,
        "trigger_type": workflow.trigger_type,
        "input_schema": workflow.input_schema,
        "output_schema": workflow.output_schema,
        "variables_schema": workflow.variables_schema,
        "canvas_state": workflow.canvas_state,
        "created_by": workflow.created_by,
        "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
        "updated_at": workflow.updated_at.isoformat() if workflow.updated_at else None,
        "nodes": [_serialize_node(n) for n in nodes],
        "edges": [_serialize_edge(e) for e in edges],
    }


def list_workflows(
    db: Session,
    status: str = None,
    category: str = None,
) -> list[dict]:
    """List all workflows with summary info."""
    query = db.query(Workflow).order_by(Workflow.updated_at.desc())
    if status:
        query = query.filter(Workflow.status == status)
    if category:
        query = query.filter(Workflow.category == category)

    workflows = query.all()
    result = []
    for wf in workflows:
        node_count = db.query(WorkflowNode).filter(WorkflowNode.workflow_id == wf.id).count()
        edge_count = db.query(WorkflowEdge).filter(WorkflowEdge.workflow_id == wf.id).count()
        exec_count = db.query(WorkflowExecution).filter(WorkflowExecution.workflow_id == wf.id).count()
        result.append({
            "id": wf.id,
            "workflow_key": wf.workflow_key,
            "display_name": wf.display_name,
            "description": wf.description,
            "category": wf.category,
            "status": wf.status,
            "current_version": wf.current_version,
            "is_default_router": wf.is_default_router,
            "trigger_type": wf.trigger_type,
            "node_count": node_count,
            "edge_count": edge_count,
            "execution_count": exec_count,
            "created_at": wf.created_at.isoformat() if wf.created_at else None,
            "updated_at": wf.updated_at.isoformat() if wf.updated_at else None,
        })
    return result


def update_workflow(
    db: Session,
    workflow_id: int,
    data: dict,
) -> Optional[dict]:
    """Update workflow metadata (not nodes/edges)."""
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not workflow:
        return None

    allowed = {
        "display_name", "description", "category", "tags",
        "trigger_type", "input_schema", "output_schema", "variables_schema",
        "canvas_state", "is_default_router",
    }
    for key, value in data.items():
        if key in allowed:
            setattr(workflow, key, value)

    # If setting as default router, unset others
    if data.get("is_default_router"):
        db.query(Workflow).filter(
            and_(Workflow.id != workflow_id, Workflow.is_default_router == True)
        ).update({"is_default_router": False})

    db.commit()
    return get_workflow(db, workflow_id)


def delete_workflow(db: Session, workflow_id: int) -> bool:
    """Delete a workflow and all related records."""
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not workflow:
        return False

    # Delete node executions via workflow executions
    exec_ids = [
        e.id for e in
        db.query(WorkflowExecution.id).filter(WorkflowExecution.workflow_id == workflow_id).all()
    ]
    if exec_ids:
        db.query(WorkflowNodeExecution).filter(
            WorkflowNodeExecution.workflow_execution_id.in_(exec_ids)
        ).delete(synchronize_session=False)

    db.query(WorkflowExecution).filter(WorkflowExecution.workflow_id == workflow_id).delete()
    db.query(WorkflowEdge).filter(WorkflowEdge.workflow_id == workflow_id).delete()
    db.query(WorkflowNode).filter(WorkflowNode.workflow_id == workflow_id).delete()
    db.query(WorkflowVersion).filter(WorkflowVersion.workflow_id == workflow_id).delete()
    db.delete(workflow)
    db.commit()
    return True


# ─── Graph Operations ──────────────────────────────────────────────────────


def save_workflow_graph(
    db: Session,
    workflow_id: int,
    nodes: list[dict],
    edges: list[dict],
    canvas_state: dict = None,
) -> Optional[dict]:
    """Bulk upsert nodes and edges from the frontend canvas."""
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not workflow:
        return None

    # Delete existing nodes/edges and recreate
    db.query(WorkflowEdge).filter(WorkflowEdge.workflow_id == workflow_id).delete()
    db.query(WorkflowNode).filter(WorkflowNode.workflow_id == workflow_id).delete()

    # Create nodes
    for i, node_data in enumerate(nodes):
        node = WorkflowNode(
            workflow_id=workflow_id,
            node_key=node_data["node_key"],
            node_type=node_data["node_type"],
            display_name=node_data.get("display_name"),
            description=node_data.get("description"),
            position_x=node_data.get("position_x", node_data.get("position", {}).get("x", 0)),
            position_y=node_data.get("position_y", node_data.get("position", {}).get("y", 0)),
            config=node_data.get("config", {}),
            sort_order=i,
        )
        db.add(node)

    # Create edges
    for i, edge_data in enumerate(edges):
        edge = WorkflowEdge(
            workflow_id=workflow_id,
            source_node_key=edge_data["source"],
            target_node_key=edge_data["target"],
            source_handle=edge_data.get("sourceHandle"),
            label=edge_data.get("label"),
            condition=edge_data.get("condition"),
            sort_order=i,
        )
        db.add(edge)

    # Update canvas state if provided
    if canvas_state is not None:
        workflow.canvas_state = canvas_state

    db.commit()
    return get_workflow(db, workflow_id)


# ─── Publishing & Versioning ──────────────────────────────────────────────


def publish_workflow(
    db: Session,
    workflow_id: int,
    user_id: int = None,
    change_description: str = None,
) -> Optional[dict]:
    """Publish workflow: validate, create version snapshot, set status=published."""
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not workflow:
        return None

    # Validate before publishing
    validation = validate_workflow(db, workflow_id)
    if not validation["valid"]:
        raise ValueError(f"Cannot publish invalid workflow: {'; '.join(validation['errors'])}")

    # Get current graph
    nodes = db.query(WorkflowNode).filter(WorkflowNode.workflow_id == workflow_id).all()
    edges = db.query(WorkflowEdge).filter(WorkflowEdge.workflow_id == workflow_id).all()

    # Create version snapshot
    version = WorkflowVersion(
        workflow_id=workflow_id,
        version_number=workflow.current_version,
        nodes_snapshot=[_serialize_node(n) for n in nodes],
        edges_snapshot=[_serialize_edge(e) for e in edges],
        variables_snapshot=workflow.variables_schema,
        change_description=change_description,
        created_by=user_id,
    )
    db.add(version)

    workflow.status = "published"
    workflow.current_version += 1
    db.commit()

    return get_workflow(db, workflow_id)


def get_workflow_versions(db: Session, workflow_id: int) -> list[dict]:
    """List all versions of a workflow."""
    versions = (
        db.query(WorkflowVersion)
        .filter(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version_number.desc())
        .all()
    )
    return [
        {
            "id": v.id,
            "version_number": v.version_number,
            "change_description": v.change_description,
            "created_by": v.created_by,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "node_count": len(v.nodes_snapshot) if v.nodes_snapshot else 0,
            "edge_count": len(v.edges_snapshot) if v.edges_snapshot else 0,
        }
        for v in versions
    ]


def rollback_workflow(
    db: Session,
    workflow_id: int,
    version_number: int,
) -> Optional[dict]:
    """Restore workflow graph from a version snapshot."""
    version = (
        db.query(WorkflowVersion)
        .filter(and_(
            WorkflowVersion.workflow_id == workflow_id,
            WorkflowVersion.version_number == version_number,
        ))
        .first()
    )
    if not version:
        return None

    # Restore graph from snapshot
    save_workflow_graph(
        db, workflow_id,
        nodes=version.nodes_snapshot,
        edges=version.edges_snapshot,
    )

    # Restore variables
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if version.variables_snapshot:
        workflow.variables_schema = version.variables_snapshot
    db.commit()

    return get_workflow(db, workflow_id)


def clone_workflow(
    db: Session,
    workflow_id: int,
    new_name: str = None,
    user_id: int = None,
) -> Optional[dict]:
    """Clone a workflow with all nodes and edges."""
    source = get_workflow(db, workflow_id)
    if not source:
        return None

    new_display_name = new_name or f"{source['display_name']} (Copy)"
    new_wf = create_workflow(db, new_display_name, source.get("description"), source.get("category"), user_id=user_id)

    # Save the cloned graph (overwriting the default start/end nodes)
    save_workflow_graph(db, new_wf.id, source["nodes"], source["edges"])

    # Copy schemas
    wf = db.query(Workflow).filter(Workflow.id == new_wf.id).first()
    wf.input_schema = source.get("input_schema")
    wf.output_schema = source.get("output_schema")
    wf.variables_schema = source.get("variables_schema")
    wf.canvas_state = source.get("canvas_state")
    db.commit()

    return get_workflow(db, new_wf.id)


# ─── Validation ────────────────────────────────────────────────────────────


def validate_workflow(db: Session, workflow_id: int) -> dict:
    """Validate workflow graph integrity."""
    workflow_data = get_workflow(db, workflow_id)
    if not workflow_data:
        return {"valid": False, "errors": ["Workflow not found"]}

    errors = []
    warnings = []
    nodes = workflow_data["nodes"]
    edges = workflow_data["edges"]

    node_keys = {n["node_key"] for n in nodes}
    node_map = {n["node_key"]: n for n in nodes}

    # Check for start node
    start_nodes = [n for n in nodes if n["node_type"] == "start"]
    if len(start_nodes) == 0:
        errors.append("Workflow must have a Start node")
    elif len(start_nodes) > 1:
        errors.append("Workflow must have exactly one Start node")

    # Check for end node
    end_nodes = [n for n in nodes if n["node_type"] == "end"]
    if len(end_nodes) == 0:
        errors.append("Workflow must have at least one End node")

    # Check node types
    for node in nodes:
        if node["node_type"] not in VALID_NODE_TYPES:
            errors.append(f"Node '{node['node_key']}' has invalid type '{node['node_type']}'")

    # Check edge references
    for edge in edges:
        if edge["source"] not in node_keys:
            errors.append(f"Edge source '{edge['source']}' references non-existent node")
        if edge["target"] not in node_keys:
            errors.append(f"Edge target '{edge['target']}' references non-existent node")

    # Check for orphan nodes (no incoming or outgoing edges, excluding start/end/note)
    connected = set()
    for edge in edges:
        connected.add(edge["source"])
        connected.add(edge["target"])
    # Parallel nodes reference branch targets via config — count those as connected
    for node in nodes:
        if node["node_type"] == "parallel":
            config = node.get("config", {})
            for branch in config.get("branches", []):
                target = branch.get("target_node_key")
                if target:
                    connected.add(target)
            connected.add(node["node_key"])
    for node in nodes:
        if node["node_type"] not in ("start", "end", "note") and node["node_key"] not in connected:
            warnings.append(f"Node '{node['node_key']}' ({node['display_name']}) is disconnected")

    # Check agent nodes have agent_id or agent_key
    for node in nodes:
        if node["node_type"] == "agent":
            config = node.get("config", {})
            if not config.get("agent_id") and not config.get("agent_key"):
                errors.append(f"Agent node '{node['node_key']}' must specify an agent")

    # Check classify nodes have branches
    for node in nodes:
        if node["node_type"] == "classify":
            config = node.get("config", {})
            if not config.get("branches") or len(config["branches"]) == 0:
                errors.append(f"Classify node '{node['node_key']}' must have at least one branch")

    # Check while_loop has max_iterations
    for node in nodes:
        if node["node_type"] == "while_loop":
            config = node.get("config", {})
            if not config.get("max_iterations"):
                warnings.append(f"While loop '{node['node_key']}' has no max_iterations — risk of infinite loop")

    # Check for_each has collection_expression and body edge
    for node in nodes:
        if node["node_type"] == "for_each":
            config = node.get("config", {})
            if not config.get("collection_expression"):
                errors.append(f"For Each node '{node['node_key']}' must specify a collection_expression")
            has_body = any(e["source"] == node["node_key"] and e.get("sourceHandle") == "body" for e in edges)
            has_done = any(e["source"] == node["node_key"] and e.get("sourceHandle") == "done" for e in edges)
            if not has_body:
                errors.append(f"For Each node '{node['node_key']}' must have a 'body' edge")
            if not has_done:
                warnings.append(f"For Each node '{node['node_key']}' has no 'done' edge — workflow will stop here")

    # Check parallel has branches
    for node in nodes:
        if node["node_type"] == "parallel":
            config = node.get("config", {})
            if not config.get("branches") or len(config["branches"]) == 0:
                errors.append(f"Parallel node '{node['node_key']}' must have at least one branch")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ─── Execution Queries ─────────────────────────────────────────────────────


def list_workflow_executions(db: Session, workflow_id: int, limit: int = 50) -> list[dict]:
    """List executions for a workflow."""
    executions = (
        db.query(WorkflowExecution)
        .filter(WorkflowExecution.workflow_id == workflow_id)
        .order_by(WorkflowExecution.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_serialize_execution(e) for e in executions]


def get_workflow_execution(db: Session, execution_id: int) -> Optional[dict]:
    """Get execution detail with node traces."""
    execution = db.query(WorkflowExecution).filter(WorkflowExecution.id == execution_id).first()
    if not execution:
        return None

    node_execs = (
        db.query(WorkflowNodeExecution)
        .filter(WorkflowNodeExecution.workflow_execution_id == execution_id)
        .order_by(WorkflowNodeExecution.started_at)
        .all()
    )

    result = _serialize_execution(execution)
    result["node_executions"] = [
        {
            "id": ne.id,
            "node_key": ne.node_key,
            "node_type": ne.node_type,
            "status": ne.status,
            "input_data": ne.input_data,
            "output_data": ne.output_data,
            "error_message": ne.error_message,
            "latency_ms": ne.latency_ms,
            "tokens_input": ne.tokens_input,
            "tokens_output": ne.tokens_output,
            "cost_estimate": ne.cost_estimate,
            "agent_execution_id": ne.agent_execution_id,
            "started_at": ne.started_at.isoformat() if ne.started_at else None,
            "completed_at": ne.completed_at.isoformat() if ne.completed_at else None,
        }
        for ne in node_execs
    ]
    return result


# ─── Helpers ───────────────────────────────────────────────────────────────


def _serialize_node(n: WorkflowNode) -> dict:
    return {
        "id": n.id,
        "node_key": n.node_key,
        "node_type": n.node_type,
        "display_name": n.display_name,
        "description": n.description,
        "position": {"x": n.position_x, "y": n.position_y},
        "position_x": n.position_x,
        "position_y": n.position_y,
        "config": n.config or {},
        "sort_order": n.sort_order,
    }


def _serialize_edge(e: WorkflowEdge) -> dict:
    return {
        "id": e.id,
        "source": e.source_node_key,
        "target": e.target_node_key,
        "sourceHandle": e.source_handle,
        "label": e.label,
        "condition": e.condition,
        "sort_order": e.sort_order,
    }


def _serialize_execution(e: WorkflowExecution) -> dict:
    return {
        "id": e.id,
        "workflow_id": e.workflow_id,
        "workflow_version": e.workflow_version,
        "session_id": e.session_id,
        "trigger": e.trigger,
        "status": e.status,
        "current_node_key": e.current_node_key,
        "state_snapshot": e.state_snapshot,
        "execution_path": e.execution_path or [],
        "total_latency_ms": e.total_latency_ms,
        "total_tokens_input": e.total_tokens_input,
        "total_tokens_output": e.total_tokens_output,
        "total_cost": e.total_cost,
        "error_message": e.error_message,
        "error_node_key": e.error_node_key,
        "pending_approval": e.pending_approval,
        "approval_prompt": e.approval_prompt,
        "input_data": e.input_data,
        "output_data": e.output_data,
        "user_id": e.user_id,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "completed_at": e.completed_at.isoformat() if e.completed_at else None,
    }
