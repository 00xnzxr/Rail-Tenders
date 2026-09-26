"""
DRPL Backend - Workflow Execution Engine
Traverses a workflow graph and executes nodes sequentially,
handling branching, loops, user approvals, and agent dispatch.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Awaitable

from sqlalchemy.orm import Session

from app.models.workflow import (
    Workflow, WorkflowNode, WorkflowEdge,
    WorkflowExecution, WorkflowNodeExecution,
)
from app.services.langchain.llm_factory import get_chat_model

logger = logging.getLogger(__name__)


class UserApprovalRequired(Exception):
    """Raised when a user_approval node pauses execution."""
    def __init__(self, prompt: str):
        self.prompt = prompt
        super().__init__(prompt)


class WorkflowExecutionEngine:
    """Traverses a workflow graph and executes nodes."""

    def __init__(
        self,
        db: Session,
        workflow: Workflow,
        nodes: list[WorkflowNode],
        edges: list[WorkflowEdge],
        execution: WorkflowExecution,
        streaming_callback: Optional[Callable[[str, dict], Awaitable[None]]] = None,
    ):
        self.db = db
        self.workflow = workflow
        self.node_map: dict[str, WorkflowNode] = {n.node_key: n for n in nodes}
        self.edges = edges
        self.execution = execution
        self.streaming_callback = streaming_callback
        self.state: dict[str, Any] = {}
        self._loop_counters: dict[str, int] = {}

    async def run(self, input_data: dict) -> dict:
        """Main execution loop. Start at 'start' node, traverse edges."""
        start_time = time.time()

        # Initialize state
        self.state = {
            "__input": input_data,
            "__session_id": self.execution.session_id,
            **input_data,
        }
        # Apply variable defaults
        if self.workflow.variables_schema:
            for key, schema in self.workflow.variables_schema.items():
                if key not in self.state and "default" in schema:
                    self.state[key] = schema["default"]

        # Find start node
        current_node = self._find_start_node()
        if not current_node:
            return self._fail("No start node found")

        self.execution.status = "running"
        self.execution.state_snapshot = self.state
        self.db.commit()

        await self._emit("workflow_start", {
            "workflow_id": self.workflow.id,
            "execution_id": self.execution.id,
        })

        # Traverse graph
        while current_node:
            node_exec = self._create_node_execution(current_node)
            self.execution.current_node_key = current_node.node_key
            self.execution.execution_path = (self.execution.execution_path or []) + [current_node.node_key]
            self.db.commit()

            await self._emit("workflow_node_start", {
                "node_key": current_node.node_key,
                "node_type": current_node.node_type,
                "display_name": current_node.display_name,
            })

            try:
                node_start = time.time()
                result = await self._execute_node(current_node, node_exec)
                node_exec.latency_ms = int((time.time() - node_start) * 1000)
                node_exec.status = "completed"
                node_exec.output_data = self._safe_json(result)
                node_exec.completed_at = datetime.now(timezone.utc)
                self.db.commit()

                await self._emit("workflow_node_complete", {
                    "node_key": current_node.node_key,
                    "node_type": current_node.node_type,
                    "status": "completed",
                })

                # Update state from node output
                self._update_state(current_node, result)

                # Resolve next node
                next_node = self._resolve_next_node(current_node, result)
                current_node = next_node

            except UserApprovalRequired as e:
                node_exec.status = "completed"
                node_exec.output_data = {"approval_required": True, "prompt": e.prompt}
                node_exec.completed_at = datetime.now(timezone.utc)
                self.execution.status = "paused"
                self.execution.pending_approval = True
                self.execution.approval_prompt = e.prompt
                self.execution.state_snapshot = self.state
                self.db.commit()

                await self._emit("workflow_paused", {
                    "node_key": current_node.node_key,
                    "prompt": e.prompt,
                })
                return {
                    "status": "paused",
                    "state": self.state,
                    "approval_prompt": e.prompt,
                }

            except Exception as e:
                logger.error(f"Node {current_node.node_key} failed: {e}", exc_info=True)
                node_exec.status = "failed"
                node_exec.error_message = str(e)
                node_exec.completed_at = datetime.now(timezone.utc)
                self.execution.status = "failed"
                self.execution.error_message = str(e)
                self.execution.error_node_key = current_node.node_key
                self.execution.state_snapshot = self.state
                self.db.commit()

                await self._emit("workflow_error", {
                    "node_key": current_node.node_key,
                    "error": str(e),
                })

                # Check for error edge
                error_edge = self._find_edge(current_node.node_key, "error")
                if error_edge:
                    current_node = self.node_map.get(error_edge.target_node_key)
                    self.execution.status = "running"
                    self.db.commit()
                    continue

                return self._fail(str(e), current_node.node_key)

        # Execution complete
        total_ms = int((time.time() - start_time) * 1000)
        self.execution.status = "completed"
        self.execution.total_latency_ms = total_ms
        self.execution.state_snapshot = self.state
        self.execution.output_data = self.state
        self.execution.completed_at = datetime.now(timezone.utc)

        # Sum up metrics
        node_execs = (
            self.db.query(WorkflowNodeExecution)
            .filter(WorkflowNodeExecution.workflow_execution_id == self.execution.id)
            .all()
        )
        self.execution.total_tokens_input = sum(ne.tokens_input or 0 for ne in node_execs)
        self.execution.total_tokens_output = sum(ne.tokens_output or 0 for ne in node_execs)
        self.execution.total_cost = sum(ne.cost_estimate or 0 for ne in node_execs)
        self.db.commit()

        await self._emit("workflow_complete", {
            "execution_id": self.execution.id,
            "status": "completed",
            "total_latency_ms": total_ms,
        })

        return {
            "status": "completed",
            "output": self.state,
            "total_latency_ms": total_ms,
        }

    # ─── Node Executors ────────────────────────────────────────────

    async def _execute_node(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Dispatch to the correct handler by node_type."""
        handlers = {
            "start": self._exec_start,
            "end": self._exec_end,
            "agent": self._exec_agent,
            "classify": self._exec_classify,
            "if_else": self._exec_if_else,
            "while_loop": self._exec_while_loop,
            "user_approval": self._exec_user_approval,
            "transform": self._exec_transform,
            "set_state": self._exec_set_state,
            "tool": self._exec_tool,
            "note": self._exec_note,
            "for_each": self._exec_for_each,
            "parallel": self._exec_parallel,
        }
        handler = handlers.get(node.node_type)
        if not handler:
            raise ValueError(f"Unknown node type: {node.node_type}")
        return await handler(node, node_exec)

    async def _exec_start(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Pass through input data."""
        return {"output": "started"}

    async def _exec_end(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Finalize output."""
        config = node.config or {}
        output_mapping = config.get("output_mapping", {})
        output = {}
        for out_key, state_key in output_mapping.items():
            output[out_key] = self._resolve_state_path(state_key)
        return {"output": output or self.state}

    async def _exec_agent(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Execute a CustomAgent or fall back to direct LLM call."""
        config = node.config or {}
        agent_key = config.get("agent_key")
        agent_id = config.get("agent_id")

        if not agent_key and not agent_id:
            raise ValueError(f"Agent node '{node.node_key}' has no agent configured")

        # Build input from mapping
        input_data = self._apply_input_mapping(config.get("input_mapping", {}))
        if not input_data:
            input_data = {"message": self.state.get("__user_message", self.state.get("message", ""))}

        # Ensure 'message' is always a string (serialize dicts/lists)
        if "message" in input_data and not isinstance(input_data["message"], str):
            input_data["message"] = json.dumps(input_data["message"], default=str, indent=2)[:650000]

        await self._emit("agent_start", {
            "agent_key": agent_key,
            "display_name": node.display_name or agent_key,
        })

        output_text = ""
        result = {}

        # Try the agent execution service first (for CustomAgent DB records)
        try:
            from app.services.agent_execution_service import execute_agent
            result = await execute_agent(
                db=self.db,
                agent_key_or_id=agent_key or str(agent_id),
                input_data=input_data,
                user_id=self.execution.user_id,
            )
            output_text = result.get("output", "")

            if result.get("execution_id"):
                node_exec.agent_execution_id = result["execution_id"]
            node_exec.tokens_input = result.get("tokens_input", 0)
            node_exec.tokens_output = result.get("tokens_output", 0)
            node_exec.cost_estimate = result.get("cost_estimate", 0)

        except ValueError:
            # Agent not found in CustomAgent table — route to existing chat wrappers
            # for known hardcoded agents, or fall back to direct LLM call.
            logger.info(f"Agent '{agent_key}' not in DB, routing to chat wrapper or LLM fallback")

            message = str(input_data.get("message", ""))
            if not message:
                message = json.dumps(input_data, default=str)[:50000]

            # Truncate to stay within API limits
            if len(message) > 650000:
                message = message[:650000] + "\n\n[... content truncated ...]"

            file_metadata = self.state.get("__file_metadata")
            tender_id_val = self.state.get("__tender_id")
            session_id_val = self.execution.session_id

            # Route to existing chat agent wrappers for known agents
            chat_wrapper_map = {
                "deep_analyzer": "_call_chat_document_analysis",
                "checklist_generator": "_call_chat_checklist",
                "proposal_creator": "_call_chat_proposal",
                "costing_researcher": "_call_chat_costing",
                "annexure_finder": "_call_chat_annexure_finder",
                "workspace_manager": "_call_chat_workspace_manager",
            }

            wrapper_method = chat_wrapper_map.get(agent_key)
            if wrapper_method:
                try:
                    output_text = await getattr(self, wrapper_method)(
                        message, tender_id_val, session_id_val, file_metadata,
                    )
                except Exception as e:
                    logger.error(f"Chat wrapper failed for '{agent_key}': {e}")
                    output_text = f"Agent execution failed: {e}"
            else:
                # Generic LLM fallback for unknown agents
                system_prompt = (
                    f"You are the {node.display_name or agent_key} agent. "
                    f"Analyze the provided input thoroughly and produce a comprehensive response."
                )
                try:
                    llm = get_chat_model(self.db, temperature=0.3)
                    from langchain_core.messages import SystemMessage, HumanMessage
                    response = await llm.ainvoke([
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=message),
                    ])
                    output_text = response.content if hasattr(response, 'content') else str(response)
                except Exception as e:
                    logger.error(f"Direct LLM call failed for agent '{agent_key}': {e}")
                    output_text = f"Agent execution failed: {e}"

        # Stream tokens
        if output_text and self.streaming_callback:
            chunk_size = 50
            for i in range(0, len(output_text), chunk_size):
                await self._emit("token", {"content": output_text[i:i + chunk_size]})

        await self._emit("agent_complete", {
            "agent_key": agent_key,
            "output_type": result.get("output_type", "general"),
        })

        return {
            "output": output_text,
            "output_type": result.get("output_type", "general"),
            "structured_data": result.get("structured_data"),
            "agent_key": agent_key,
        }

    async def _exec_classify(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """LLM-based classification to select an output branch."""
        config = node.config or {}
        prompt = config.get("classification_prompt", "")
        model_name = config.get("model")
        branches = config.get("branches", [])

        if not branches:
            raise ValueError(f"Classify node '{node.node_key}' has no branches")

        # Build classification message
        branch_labels = [b["label"] for b in branches]
        user_message = self.state.get("__user_message", self.state.get("message", ""))

        classification_input = f"""{prompt}

Available categories: {', '.join(branch_labels)}

User message: {user_message}

Respond with ONLY a JSON object: {{"category": "<one of the categories>", "reasoning": "<brief reasoning>"}}"""

        # Call LLM
        llm = get_chat_model(self.db, temperature=0.1)
        from langchain_core.messages import HumanMessage
        response = await llm.ainvoke([HumanMessage(content=classification_input)])
        response_text = response.content if hasattr(response, 'content') else str(response)

        # Parse classification
        selected_branch = branch_labels[0]  # default
        try:
            parsed = json.loads(response_text.strip().strip('`').strip())
            if parsed.get("category") in branch_labels:
                selected_branch = parsed["category"]
        except (json.JSONDecodeError, KeyError):
            # Try to find a branch label in the response
            for label in branch_labels:
                if label.lower() in response_text.lower():
                    selected_branch = label
                    break

        # Emit routing event for frontend compatibility
        await self._emit("routing", {
            "intent": selected_branch,
            "agents": [selected_branch],
            "agent_names": [selected_branch],
        })

        return {
            "selected_branch": selected_branch,
            "reasoning": response_text,
            "__branch": selected_branch,
        }

    async def _exec_if_else(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Evaluate condition and return branch direction."""
        config = node.config or {}
        expression = config.get("condition_expression", "False")
        result = self._evaluate_condition(expression)
        return {"__branch": "true" if result else "false", "condition_result": result}

    async def _exec_while_loop(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Evaluate loop condition. Returns 'body' or 'done' branch."""
        config = node.config or {}
        expression = config.get("condition_expression", "False")
        max_iterations = config.get("max_iterations", 10)

        counter_key = f"__loop_{node.node_key}"
        current_count = self._loop_counters.get(counter_key, 0)

        if current_count >= max_iterations:
            logger.info(f"While loop '{node.node_key}' hit max iterations ({max_iterations})")
            self._loop_counters[counter_key] = 0
            return {"__branch": "done", "iterations": current_count}

        result = self._evaluate_condition(expression)
        if result:
            self._loop_counters[counter_key] = current_count + 1
            self.state[f"__iteration_{node.node_key}"] = current_count + 1
            return {"__branch": "body", "iteration": current_count + 1}
        else:
            self._loop_counters[counter_key] = 0
            return {"__branch": "done", "iterations": current_count}

    async def _exec_user_approval(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Pause execution for user approval."""
        config = node.config or {}
        prompt = config.get("prompt_template", "Please approve this step to continue.")

        # Substitute state variables in prompt
        for key, value in self.state.items():
            if isinstance(value, str):
                prompt = prompt.replace(f"{{{{state.{key}}}}}", value)

        raise UserApprovalRequired(prompt)

    async def _exec_transform(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Transform data using template or expression."""
        config = node.config or {}
        transform_type = config.get("transform_type", "expression")
        template_str = config.get("template", "")

        # Special transform: flatten_documents — extracts and merges document lists
        # from parallel download results into a single flat list
        if transform_type == "flatten_documents":
            source_path = config.get("source_path", template_str)
            source_data = self._resolve_state_path(source_path) if source_path else self.state
            documents = []
            if isinstance(source_data, dict):
                for branch_key, branch_result in source_data.items():
                    # Navigate into branch output → output → documents
                    if isinstance(branch_result, dict):
                        output = branch_result.get("output", branch_result)
                        if isinstance(output, dict):
                            docs = output.get("documents", [])
                            if isinstance(docs, list):
                                # Only include successfully downloaded documents
                                for doc in docs:
                                    if isinstance(doc, dict) and doc.get("success") and doc.get("file_path"):
                                        documents.append(doc)
            elif isinstance(source_data, list):
                documents = source_data
            return {"output": documents}

        if transform_type == "jinja2":
            # Use Python string.Template as fallback, Jinja2 if available
            try:
                from jinja2 import Template
                t = Template(template_str)
                result = t.render(state=self.state)
            except ImportError:
                # Fallback: simple {{state.key}} replacement
                result = template_str
                import re
                for match in re.findall(r'\{\{\s*state\.([a-zA-Z0-9_.]+)\s*\}\}', template_str):
                    value = self._resolve_state_path(f"state.{match}")
                    result = result.replace(f"{{{{state.{match}}}}}", str(value) if value is not None else "")
                    # Also handle with spaces
                    result = result.replace(f"{{{{ state.{match} }}}}", str(value) if value is not None else "")
            except Exception as e:
                result = f"Transform error: {e}"
        else:
            # Simple expression evaluation
            result = self._evaluate_expression(template_str)

        return {"output": result}

    async def _exec_set_state(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Assign variables to workflow state."""
        config = node.config or {}
        assignments = config.get("assignments", [])
        results = {}
        for assignment in assignments:
            key = assignment.get("key", "")
            value_expr = assignment.get("value_expression", "")
            if key:
                value = self._evaluate_expression(value_expr)
                self.state[key] = value
                results[key] = value
        return {"assignments": results}

    async def _exec_tool(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Execute a registered tool handler."""
        from app.services.workflow_tool_handlers import TOOL_HANDLER_REGISTRY

        config = node.config or {}
        tool_key = config.get("tool_key", "")
        if not tool_key:
            raise ValueError(f"Tool node '{node.node_key}' has no tool_key")

        input_data = self._apply_input_mapping(config.get("input_mapping", {}))

        handler = TOOL_HANDLER_REGISTRY.get(tool_key)
        if handler:
            logger.info(f"Tool '{tool_key}' executing with keys: {list(input_data.keys())}")
            result = await handler(self, input_data, config)
            return {"output": result, "tool_key": tool_key}

        logger.warning(f"No handler for tool '{tool_key}' — returning stub")
        return {"output": f"Tool '{tool_key}' has no handler", "tool_key": tool_key}

    async def _exec_for_each(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Iterate over a collection, executing a subgraph body per element."""
        import asyncio

        config = node.config or {}
        collection_path = config.get("collection_expression", "")
        item_var = config.get("item_variable", "__item")
        index_var = config.get("index_variable", "__index")
        output_key = config.get("output_key", f"__{node.node_key}_results")
        max_iter = config.get("max_iterations", 20)
        parallel = config.get("parallel", False)
        concurrency = config.get("concurrency_limit", 5)
        continue_on_error = config.get("continue_on_error", True)

        collection = self._resolve_state_path(collection_path)
        # Auto-convert dict to list of values (e.g., parallel node output)
        if isinstance(collection, dict):
            collection = list(collection.values())
        if not isinstance(collection, list):
            logger.warning(f"for_each '{node.node_key}': collection is not a list (got {type(collection).__name__})")
            self.state[output_key] = []
            return {"__branch": "done", "results": [], "count": 0}

        items = collection[:max_iter]
        if not items:
            self.state[output_key] = []
            return {"__branch": "done", "results": [], "count": 0}

        # Find body entry edge
        body_edge = self._find_edge(node.node_key, "body")
        if not body_edge:
            raise ValueError(f"for_each '{node.node_key}' has no 'body' edge")
        body_start_key = body_edge.target_node_key

        results: list = []

        if parallel and len(items) > 1:
            sem = asyncio.Semaphore(concurrency)

            async def run_one(idx: int, item: Any):
                async with sem:
                    await self._emit("for_each_iteration_start", {
                        "parent_node": node.node_key,
                        "index": idx,
                        "total": len(items),
                        "item_name": item.get("name", str(idx)) if isinstance(item, dict) else str(idx),
                    })
                    r = await self._run_subgraph(body_start_key, {item_var: item, index_var: idx}, node.node_key)
                    await self._emit("for_each_iteration_complete", {
                        "parent_node": node.node_key, "index": idx, "status": "completed",
                    })
                    return r

            tasks = [run_one(i, item) for i, item in enumerate(items)]
            raw = await asyncio.gather(*tasks, return_exceptions=continue_on_error)
            for i, r in enumerate(raw):
                if isinstance(r, Exception):
                    results.append({"index": i, "status": "failed", "error": str(r)})
                else:
                    results.append(r)
        else:
            # Sequential
            for idx, item in enumerate(items):
                await self._emit("for_each_iteration_start", {
                    "parent_node": node.node_key, "index": idx, "total": len(items),
                    "item_name": item.get("name", str(idx)) if isinstance(item, dict) else str(idx),
                })
                try:
                    r = await self._run_subgraph(body_start_key, {item_var: item, index_var: idx}, node.node_key)
                    results.append(r)
                except Exception as e:
                    if continue_on_error:
                        results.append({"index": idx, "status": "failed", "error": str(e)})
                    else:
                        raise
                await self._emit("for_each_iteration_complete", {
                    "parent_node": node.node_key, "index": idx, "status": "completed",
                })

        self.state[output_key] = results
        return {"__branch": "done", "results": results, "count": len(results)}

    async def _exec_parallel(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """Execute multiple branches concurrently via asyncio.gather."""
        import asyncio

        config = node.config or {}
        branches = config.get("branches", [])
        merge_strategy = config.get("merge_strategy", "dict")
        output_key = config.get("output_key", f"__{node.node_key}_results")
        continue_on_error = config.get("continue_on_error", True)
        timeout = config.get("timeout_seconds", 300)

        if not branches:
            self.state[output_key] = {}
            return {"__branch": "done", "output": {}}

        async def run_branch(branch: dict):
            target_key = branch.get("target_node_key", "")
            label = branch.get("label", target_key)
            if not target_key or target_key not in self.node_map:
                return label, {"error": f"Target node '{target_key}' not found"}
            await self._emit("parallel_branch_start", {"parent_node": node.node_key, "branch": label})
            result = await self._run_subgraph(target_key, {}, node.node_key)
            await self._emit("parallel_branch_complete", {"parent_node": node.node_key, "branch": label, "status": "completed"})
            return label, result

        tasks = [run_branch(b) for b in branches]
        try:
            raw = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=continue_on_error),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"Parallel node '{node.node_key}' timed out after {timeout}s")
            self.state[output_key] = {"error": "timeout"}
            return {"__branch": "done", "output": {"error": "timeout"}}

        if merge_strategy == "dict":
            merged = {}
            for r in raw:
                if isinstance(r, Exception):
                    continue
                label, result = r
                merged[label] = result
        else:
            merged = []
            for r in raw:
                if isinstance(r, Exception):
                    merged.append({"error": str(r)})
                else:
                    merged.append(r[1])

        self.state[output_key] = merged
        return {"__branch": "done", "output": merged}

    async def _exec_note(self, node: WorkflowNode, node_exec: WorkflowNodeExecution) -> dict:
        """No-op documentation node."""
        node_exec.status = "skipped"
        return {"skipped": True}

    # ─── Graph Navigation ──────────────────────────────────────────

    # ─── Chat Wrapper Calls (for hardcoded agents) ────────────────

    async def _call_chat_document_analysis(
        self, message: str, tender_id: Any, session_id: str, file_metadata: Any,
    ) -> str:
        from app.services.langchain.graphs.chat_agent_wrappers import chat_document_analysis
        result = await chat_document_analysis(
            db=self.db,
            message=message,
            tender_id=tender_id,
            session_id=session_id,
            file_metadata=file_metadata,
        )
        return result.get("output", "") if isinstance(result, dict) else str(result)

    async def _call_chat_checklist(
        self, message: str, tender_id: Any, session_id: str, file_metadata: Any,
    ) -> str:
        from app.services.langchain.graphs.chat_agent_wrappers import chat_checklist_generation
        result = await chat_checklist_generation(
            db=self.db,
            message=message,
            tender_id=tender_id,
            session_id=session_id,
        )
        return result.get("output", "") if isinstance(result, dict) else str(result)

    async def _call_chat_proposal(
        self, message: str, tender_id: Any, session_id: str, file_metadata: Any,
    ) -> str:
        from app.services.langchain.graphs.chat_agent_wrappers import chat_proposal_writing
        result = await chat_proposal_writing(
            db=self.db,
            message=message,
            tender_id=tender_id,
            session_id=session_id,
        )
        return result.get("output", "") if isinstance(result, dict) else str(result)

    async def _call_chat_costing(
        self, message: str, tender_id: Any, session_id: str, file_metadata: Any,
    ) -> str:
        from app.services.langchain.graphs.chat_agent_wrappers import chat_costing_research
        result = await chat_costing_research(
            db=self.db,
            message=message,
            tender_id=tender_id,
            session_id=session_id,
        )
        return result.get("output", "") if isinstance(result, dict) else str(result)

    async def _call_chat_annexure_finder(
        self, message: str, tender_id: Any, session_id: str, file_metadata: Any,
    ) -> str:
        from app.services.langchain.graphs.chat_agent_wrappers import chat_annexure_finder
        proposal_session_id = None
        try:
            exec_row = self.db.query(type(self.execution)).filter(
                type(self.execution).id == self.execution.id
            ).first()
            if exec_row and exec_row.session_id:
                from app.models.proposal import ProposalSession
                ps = self.db.query(ProposalSession).filter(
                    ProposalSession.router_session_id == exec_row.session_id
                ).first()
                if ps:
                    proposal_session_id = ps.id
        except Exception:
            pass
        result = await chat_annexure_finder(
            db=self.db,
            message=message,
            tender_id=tender_id,
            session_id=session_id,
            file_metadata=file_metadata,
            proposal_session_id=proposal_session_id,
        )
        return result.get("output", "") if isinstance(result, dict) else str(result)

    async def _call_chat_workspace_manager(
        self, message: str, tender_id: Any, session_id: str, file_metadata: Any,
    ) -> str:
        from app.services.langchain.graphs.chat_agent_wrappers import chat_workspace_operations
        result = await chat_workspace_operations(
            db=self.db,
            message=message,
            tender_id=tender_id,
            session_id=session_id,
        )
        return result.get("output", "") if isinstance(result, dict) else str(result)

    # ─── Graph Navigation ──────────────────────────────────────────

    def _find_start_node(self) -> Optional[WorkflowNode]:
        for node in self.node_map.values():
            if node.node_type == "start":
                return node
        return None

    def _resolve_next_node(self, current: WorkflowNode, result: dict) -> Optional[WorkflowNode]:
        """Find the next node to execute based on edges and result."""
        branch = result.get("__branch")

        # For branching nodes, find edge matching the branch handle
        if branch and current.node_type in ("classify", "if_else", "while_loop", "user_approval", "for_each", "parallel"):
            edge = self._find_edge(current.node_key, branch)
            if edge:
                return self.node_map.get(edge.target_node_key)
            # Fallback: try without handle
            edge = self._find_edge(current.node_key, None)
            if edge:
                return self.node_map.get(edge.target_node_key)
            return None

        # For non-branching nodes, find the single outgoing edge
        edge = self._find_edge(current.node_key, None)
        if edge:
            return self.node_map.get(edge.target_node_key)

        # End node or no outgoing edges
        return None

    def _find_edge(self, source_key: str, handle: Optional[str]) -> Optional[WorkflowEdge]:
        """Find an edge from source with optional handle matching."""
        if handle:
            for edge in self.edges:
                if edge.source_node_key == source_key and edge.source_handle == handle:
                    return edge
        # Fallback: any edge from this source
        for edge in self.edges:
            if edge.source_node_key == source_key and not edge.source_handle:
                return edge
        # Last resort: first edge from source
        for edge in self.edges:
            if edge.source_node_key == source_key:
                return edge
        return None

    # ─── Subgraph Execution ──────────────────────────────────────────

    async def _run_subgraph(
        self, start_node_key: str, extra_state: dict, parent_node_key: str,
    ) -> dict:
        """Execute a subgraph for for_each/parallel nodes.

        Walks nodes from start_node_key until a node's outgoing edge leads
        back to parent_node_key or there are no more edges.
        """
        # Save state keys we're about to overwrite
        saved = {k: self.state[k] for k in extra_state if k in self.state}
        self.state.update(extra_state)

        current = self.node_map.get(start_node_key)
        last_result: dict = {}

        while current:
            node_exec = self._create_node_execution(current)
            await self._emit("workflow_node_start", {
                "node_key": current.node_key,
                "node_type": current.node_type,
                "display_name": current.display_name,
                "subgraph_parent": parent_node_key,
            })

            try:
                result = await self._execute_node(current, node_exec)
                node_exec.status = "completed"
                node_exec.output_data = self._safe_json(result)
                node_exec.completed_at = datetime.now(timezone.utc)
                self.db.commit()

                await self._emit("workflow_node_complete", {
                    "node_key": current.node_key, "status": "completed",
                    "subgraph_parent": parent_node_key,
                })

                self._update_state(current, result)
                last_result = result

                # Find next node — stop if it loops back to parent
                next_node = self._resolve_next_node(current, result)
                if next_node and next_node.node_key == parent_node_key:
                    break
                # Also stop at end nodes within subgraphs
                if not next_node or current.node_type == "end":
                    break
                current = next_node

            except Exception as e:
                node_exec.status = "failed"
                node_exec.error_message = str(e)
                node_exec.completed_at = datetime.now(timezone.utc)
                self.db.commit()
                # Restore state and re-raise
                for k, v in saved.items():
                    self.state[k] = v
                for k in extra_state:
                    if k not in saved:
                        self.state.pop(k, None)
                raise

        # Restore overwritten state keys
        for k, v in saved.items():
            self.state[k] = v
        for k in extra_state:
            if k not in saved:
                self.state.pop(k, None)

        return last_result

    # ─── State Management ──────────────────────────────────────────

    def _update_state(self, node: WorkflowNode, result: dict):
        """Update workflow state from node output."""
        config = node.config or {}
        output_key = config.get("output_key")
        if output_key and "output" in result:
            self.state[output_key] = result["output"]
        # Always store the last result
        self.state[f"__{node.node_key}_result"] = result
        self.execution.state_snapshot = self.state

    def _apply_input_mapping(self, mapping: dict) -> dict:
        """Map state variables to input parameters."""
        result = {}
        for param_name, state_path in mapping.items():
            result[param_name] = self._resolve_state_path(state_path)
        return result

    def _resolve_state_path(self, path: str) -> Any:
        """Resolve a dotted path like 'state.analysis_result.output'."""
        if not path:
            return None
        parts = path.replace("state.", "", 1).split(".") if path.startswith("state.") else path.split(".")
        current = self.state
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current

    def _evaluate_condition(self, expression: str) -> bool:
        """Evaluate a condition expression against the state safely using AST."""
        import ast
        import operator

        try:
            # Resolve state references first
            expr = expression.strip()

            # Build a flat namespace of resolved values
            namespace: dict[str, Any] = {
                "True": True, "False": False, "None": None,
                "true": True, "false": False, "null": None,
            }

            # Resolve all state.X references to their values
            import re
            state_refs = re.findall(r'state\.([a-zA-Z_][a-zA-Z0-9_.]*)', expr)
            for ref in state_refs:
                value = self._resolve_state_path(f"state.{ref}")
                safe_name = f"__ref_{ref.replace('.', '_')}"
                namespace[safe_name] = value
                expr = expr.replace(f"state.{ref}", safe_name)

            # Safe AST-based evaluation using a restricted set of operations
            tree = ast.parse(expr, mode='eval')

            # Whitelist of allowed AST node types
            allowed_nodes = (
                ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp,
                ast.Compare, ast.Constant, ast.Name, ast.Load,
                ast.And, ast.Or, ast.Not,
                ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
                ast.Is, ast.IsNot, ast.In, ast.NotIn,
                ast.Add, ast.Sub, ast.Mult,
                ast.Call,  # Only for len/str/int/float/bool
            )

            for node in ast.walk(tree):
                if not isinstance(node, allowed_nodes):
                    raise ValueError(f"Disallowed expression element: {type(node).__name__}")
                # Restrict function calls to safe builtins
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id not in ('len', 'str', 'int', 'float', 'bool'):
                        raise ValueError(f"Disallowed function call: {node.func.id}")

            # Add safe builtins to namespace
            namespace.update({"len": len, "str": str, "int": int, "float": float, "bool": bool})

            code = compile(tree, '<condition>', 'eval')
            result = eval(code, {"__builtins__": {}}, namespace)  # noqa: S307
            return bool(result)

        except Exception as e:
            logger.warning(f"Condition evaluation failed for '{expression}': {e}")
            return False

    def _evaluate_expression(self, expression: str) -> Any:
        """Evaluate a simple expression."""
        try:
            # Direct state reference
            if expression.startswith("state."):
                return self._resolve_state_path(expression)
            # Literal values
            if expression in ("true", "True"):
                return True
            if expression in ("false", "False"):
                return False
            if expression in ("null", "None"):
                return None
            try:
                return int(expression)
            except ValueError:
                pass
            try:
                return float(expression)
            except ValueError:
                pass
            # String literal
            if expression.startswith('"') and expression.endswith('"'):
                return expression[1:-1]
            if expression.startswith("'") and expression.endswith("'"):
                return expression[1:-1]
            return expression
        except Exception:
            return expression

    # ─── Utilities ─────────────────────────────────────────────────

    def _create_node_execution(self, node: WorkflowNode) -> WorkflowNodeExecution:
        node_exec = WorkflowNodeExecution(
            workflow_execution_id=self.execution.id,
            node_key=node.node_key,
            node_type=node.node_type,
            status="running",
            input_data=self._safe_json({"state_keys": list(self.state.keys())}),
        )
        self.db.add(node_exec)
        self.db.flush()
        return node_exec

    def _fail(self, message: str, node_key: str = None) -> dict:
        self.execution.status = "failed"
        self.execution.error_message = message
        self.execution.error_node_key = node_key
        self.execution.state_snapshot = self.state
        self.execution.completed_at = datetime.now(timezone.utc)
        self.db.commit()
        return {"status": "failed", "error": message}

    async def _emit(self, event_type: str, data: dict):
        if self.streaming_callback:
            await self.streaming_callback(event_type, data)

    @staticmethod
    def _safe_json(data: Any) -> Any:
        """Ensure data is JSON-serializable."""
        try:
            json.dumps(data)
            return data
        except (TypeError, ValueError):
            return {"__raw": str(data)}
