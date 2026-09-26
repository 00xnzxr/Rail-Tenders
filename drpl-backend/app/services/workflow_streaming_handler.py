"""
DRPL Backend - Workflow Streaming Handler
SSE streaming adapter for workflow execution within the Command Center.
Emits events compatible with the existing frontend event handler.

Uses asyncio.Queue to stream events in real-time as the workflow engine
executes, rather than buffering all events until completion.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.workflow import (
    Workflow, WorkflowNode, WorkflowEdge, WorkflowExecution,
)
from app.models.agent_memory import AgentConversationHistory
from app.services.workflow_execution_engine import WorkflowExecutionEngine

logger = logging.getLogger(__name__)

# Sentinel object to signal the queue consumer that the engine is done
_DONE = object()


def _sse(event: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def stream_workflow_response(
    db: Session,
    workflow_id: int,
    message: str,
    session_id: str,
    tender_id: Optional[int],
    user_id: int,
    file_metadata: Optional[dict] = None,
    display_message: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    Execute a workflow and stream SSE events in real-time.
    Drop-in replacement for stream_router_response() when a workflow is active.
    """
    start_time = time.time()

    # Use a fresh DB session to avoid transaction issues
    fresh_db = SessionLocal()

    try:
        # Load workflow
        workflow = fresh_db.query(Workflow).filter(Workflow.id == workflow_id).first()
        if not workflow:
            yield _sse("error", {"message": "Workflow not found"})
            return

        nodes = (
            fresh_db.query(WorkflowNode)
            .filter(WorkflowNode.workflow_id == workflow_id)
            .order_by(WorkflowNode.sort_order)
            .all()
        )
        edges = (
            fresh_db.query(WorkflowEdge)
            .filter(WorkflowEdge.workflow_id == workflow_id)
            .all()
        )

        # Create execution record
        execution = WorkflowExecution(
            workflow_id=workflow_id,
            workflow_version=workflow.current_version,
            session_id=session_id,
            trigger="command_center",
            status="running",
            user_id=user_id,
            input_data={
                "message": message,
                "tender_id": tender_id,
                "file_metadata": file_metadata,
            },
        )
        fresh_db.add(execution)
        fresh_db.flush()

        # Emit session event
        yield _sse("session", {"session_id": session_id})

        # Queue-based real-time streaming: the callback pushes events,
        # the generator yields them as they arrive.
        event_queue: asyncio.Queue = asyncio.Queue()
        full_response = ""

        async def streaming_callback(event_type: str, data: dict):
            nonlocal full_response
            if event_type == "token":
                full_response += data.get("content", "")
            await event_queue.put((event_type, data))

        # Create engine
        engine = WorkflowExecutionEngine(
            db=fresh_db,
            workflow=workflow,
            nodes=nodes,
            edges=edges,
            execution=execution,
            streaming_callback=streaming_callback,
        )

        # Build input data
        input_data = {
            "message": message,
            "__user_message": message,
            "__session_id": session_id,
            "__tender_id": tender_id,
        }
        if file_metadata:
            input_data["__file_metadata"] = file_metadata

        # Load conversation history for context
        history = (
            fresh_db.query(AgentConversationHistory)
            .filter(AgentConversationHistory.session_id == session_id)
            .order_by(AgentConversationHistory.created_at.desc())
            .limit(20)
            .all()
        )
        if history:
            input_data["__conversation_history"] = [
                {"role": h.role, "content": h.content[:500], "agent_key": h.agent_key}
                for h in reversed(history)
            ]

        # Run the workflow engine in a background task so we can yield
        # events from the queue as they arrive.
        engine_result = {}
        engine_error = None

        async def run_engine():
            nonlocal engine_result, engine_error
            try:
                engine_result = await engine.run(input_data)
            except Exception as e:
                engine_error = e
                logger.error(f"Workflow engine failed: {e}", exc_info=True)
            finally:
                await event_queue.put(_DONE)

        engine_task = asyncio.create_task(run_engine())

        # Yield events in real-time as they arrive from the engine
        while True:
            item = await event_queue.get()
            if item is _DONE:
                break
            event_type, data = item
            yield _sse(event_type, data)

        # Ensure the engine task is fully done
        await engine_task

        # Handle engine error
        if engine_error:
            from app.services.langchain.error_utils import format_user_error
            yield _sse("error", {"message": format_user_error(engine_error)})
            return

        result = engine_result

        # Save conversation history
        try:
            # Save user turn
            user_turn = AgentConversationHistory(
                session_id=session_id,
                agent_key="workflow",
                role="user",
                content=display_message or message,
            )
            fresh_db.add(user_turn)

            # Save assistant turn
            output_type = "general"
            agent_key = "workflow"
            # Extract from result state
            state = result.get("output", result.get("state", {}))
            for key in ("output_type", "__output_type"):
                if key in state:
                    output_type = state[key]
                    break

            assistant_turn = AgentConversationHistory(
                session_id=session_id,
                agent_key=agent_key,
                role="assistant",
                content=full_response[:10000] if full_response else json.dumps(state)[:10000],
                output_type=output_type,
                routed_from="workflow",
                metadata_json={
                    "workflow_id": workflow_id,
                    "execution_id": execution.id,
                    "total_latency_ms": result.get("total_latency_ms"),
                    "status": result.get("status"),
                },
            )
            fresh_db.add(assistant_turn)
            fresh_db.commit()
        except Exception as e:
            logger.error(f"Failed to save conversation history: {e}")
            fresh_db.rollback()

        # Emit done event
        latency = int((time.time() - start_time) * 1000)
        yield _sse("done", {
            "session_id": session_id,
            "status": result.get("status", "completed"),
            "output_type": output_type,
            "agents_used": ["workflow"],
            "latency_ms": latency,
            "workflow_execution_id": execution.id,
        })

    except Exception as e:
        logger.error(f"Workflow streaming failed: {e}", exc_info=True)
        from app.services.langchain.error_utils import format_user_error
        yield _sse("error", {"message": format_user_error(e)})
    finally:
        try:
            fresh_db.close()
        except Exception:
            pass  # Connection may have been dropped by Neon idle timeout


async def stream_workflow_test(
    db: Session,
    workflow_id: int,
    input_data: dict,
    user_id: int,
) -> AsyncGenerator[str, None]:
    """
    Lightweight workflow test execution with real-time SSE streaming.
    Runs the workflow engine without creating Command Center sessions
    or saving conversation history. Used by the editor test runner.
    """
    start_time = time.time()
    fresh_db = SessionLocal()

    try:
        workflow = fresh_db.query(Workflow).filter(Workflow.id == workflow_id).first()
        if not workflow:
            yield _sse("error", {"message": "Workflow not found"})
            return

        nodes = (
            fresh_db.query(WorkflowNode)
            .filter(WorkflowNode.workflow_id == workflow_id)
            .order_by(WorkflowNode.sort_order)
            .all()
        )
        edges = (
            fresh_db.query(WorkflowEdge)
            .filter(WorkflowEdge.workflow_id == workflow_id)
            .all()
        )

        execution = WorkflowExecution(
            workflow_id=workflow_id,
            workflow_version=workflow.current_version,
            session_id=f"test_{uuid.uuid4().hex[:8]}",
            trigger="test",
            status="running",
            user_id=user_id,
            input_data=input_data,
        )
        fresh_db.add(execution)
        fresh_db.flush()

        yield _sse("workflow_start", {
            "workflow_id": workflow_id,
            "execution_id": execution.id,
        })

        # Queue-based real-time streaming
        event_queue: asyncio.Queue = asyncio.Queue()

        async def streaming_callback(event_type: str, data: dict):
            await event_queue.put((event_type, data))

        engine = WorkflowExecutionEngine(
            db=fresh_db,
            workflow=workflow,
            nodes=nodes,
            edges=edges,
            execution=execution,
            streaming_callback=streaming_callback,
        )

        engine_input = {
            "__user_message": input_data.get("message", ""),
            "__tender_id": input_data.get("tender_id"),
            **input_data,
        }

        # Run engine in background, yield events in real-time
        engine_result = {}
        engine_error = None

        async def run_engine():
            nonlocal engine_result, engine_error
            try:
                engine_result = await engine.run(engine_input)
            except Exception as e:
                engine_error = e
                logger.error(f"Workflow test engine failed: {e}", exc_info=True)
            finally:
                await event_queue.put(_DONE)

        engine_task = asyncio.create_task(run_engine())

        while True:
            item = await event_queue.get()
            if item is _DONE:
                break
            event_type, data = item
            yield _sse(event_type, data)

        await engine_task

        if engine_error:
            yield _sse("error", {"message": str(engine_error)})
            return

        result = engine_result

        # Emit done
        latency = int((time.time() - start_time) * 1000)
        yield _sse("done", {
            "execution_id": execution.id,
            "status": result.get("status", "completed"),
            "latency_ms": latency,
            "total_tokens_input": execution.total_tokens_input or 0,
            "total_tokens_output": execution.total_tokens_output or 0,
            "total_cost": execution.total_cost or 0,
            "error": result.get("error"),
        })

    except Exception as e:
        logger.error(f"Workflow test failed: {e}", exc_info=True)
        yield _sse("error", {"message": str(e)})
    finally:
        try:
            fresh_db.close()
        except Exception:
            pass  # Connection may have been dropped by Neon idle timeout
