"""
DRPL Backend - Context Assembly Service
Unified context assembly for agent execution.
Combines Training Datasets, Agent Memory (auto-retrieval), and memory usage instructions
into a single enriched system prompt before agent execution.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_builder import CustomAgent, AgentTool

logger = logging.getLogger(__name__)

# Context budget allocation (chars)
DEFAULT_CONTEXT_BUDGET = 500_000
TRAINING_DATA_RATIO = 0.60      # 60% for training datasets
MEMORY_RATIO = 0.20             # 20% for auto-retrieved memories
# Remaining 20% reserved for conversation history + user input


@dataclass
class AssembledContext:
    """Result of context assembly."""
    enriched_system_prompt: str
    training_chars: int
    memory_chars: int
    memories_injected: int


def assemble_agent_context(
    db: Session,
    agent: CustomAgent,
    user_input: str,
    tender_id: Optional[int] = None,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
) -> AssembledContext:
    """
    Assemble context from Training Datasets and Agent Memory into the system prompt.

    This is the single entry point for all pre-execution context enrichment.
    Both simple and LangChain agent execution paths call this.

    Args:
        db: Database session
        agent: The CustomAgent being executed
        user_input: The user's input text (used for memory retrieval relevance)
        tender_id: Optional tender ID for scoped memory retrieval
        context_budget: Total character budget for all injected context

    Returns:
        AssembledContext with enriched system prompt and metrics
    """
    system_prompt = agent.system_prompt or ""
    training_chars = 0
    memory_chars = 0
    memories_injected = 0

    # 1. Inject training dataset context
    training_budget = int(context_budget * TRAINING_DATA_RATIO)
    training_ctx = _load_training_datasets(db, agent.id, training_budget)
    if training_ctx:
        system_prompt += training_ctx
        training_chars = len(training_ctx)

    # 2. Auto-retrieve relevant memories based on user input
    memory_budget = int(context_budget * MEMORY_RATIO)
    if user_input and user_input.strip():
        memory_ctx, memories_injected = _retrieve_and_format_memories(
            db, agent.agent_key, user_input, tender_id, memory_budget
        )
        if memory_ctx:
            system_prompt += memory_ctx
            memory_chars = len(memory_ctx)

    # 3. Append memory usage instructions (if agent has memory tools)
    try:
        has_memory_tools = _agent_has_memory_tools(db, agent)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        has_memory_tools = False
    if has_memory_tools:
        system_prompt += _get_memory_instructions()

    return AssembledContext(
        enriched_system_prompt=system_prompt,
        training_chars=training_chars,
        memory_chars=memory_chars,
        memories_injected=memories_injected,
    )


def _load_training_datasets(db: Session, agent_id: int, max_chars: int) -> str:
    """Load training dataset context for the agent, respecting char budget."""
    try:
        from app.services.training_dataset_service import get_agent_training_context
        ctx = get_agent_training_context(db, agent_id, max_chars=max_chars)
        return ctx or ""
    except Exception as e:
        logger.warning(f"Failed to load training datasets for agent_id={agent_id}: {e}")
        return ""


def _retrieve_and_format_memories(
    db: Session,
    agent_key: str,
    user_input: str,
    tender_id: Optional[int],
    max_chars: int,
    top_k: int = 15,
) -> tuple[str, int]:
    """
    Auto-retrieve relevant memories and format them for system prompt injection.

    Returns:
        Tuple of (formatted_context_string, number_of_memories_injected)
    """
    try:
        from app.services.langchain.memory_service import retrieve_memories

        memories = retrieve_memories(
            db=db,
            agent_key=agent_key,
            query=user_input,
            top_k=top_k,
            tender_id=tender_id,
            min_score=0.15,  # Higher threshold for auto-retrieval to avoid noise
        )

        if not memories:
            return "", 0

        # Separate exemplars from other memory types
        regular_memories = [m for m in memories if (m.memory_type or "fact") != "exemplar"]
        exemplar_memories = [m for m in memories if (m.memory_type or "fact") == "exemplar"]

        lines = []
        total_chars = 0
        included = 0

        # Format regular memories
        if regular_memories:
            header_lines = [
                "\n\n# ═══ INSTITUTIONAL MEMORY ═══",
                "The following are relevant memories from past interactions and learnings.",
                "Use these as context but verify when critical.\n",
            ]
            lines.extend(header_lines)
            total_chars += sum(len(line) for line in header_lines)

            for mem in regular_memories:
                mem_type = (mem.memory_type or "fact").upper()
                importance = mem.importance or 0.5
                keywords = ", ".join(mem.keywords[:5]) if mem.keywords else ""

                entry = f"[{mem_type}] {mem.content}"
                if keywords:
                    entry += f"\n  Keywords: {keywords} | Importance: {importance:.1f}"

                entry_len = len(entry) + 2  # +2 for newlines
                if total_chars + entry_len > max_chars:
                    break

                lines.append(entry)
                total_chars += entry_len
                included += 1

        # Format exemplar memories in a distinct section
        if exemplar_memories and total_chars < max_chars:
            exemplar_header = [
                "\n\n# ═══ PAST HIGH-QUALITY OUTPUTS ═══",
                "The following are examples of previously generated high-quality outputs.",
                "Reference these for format, depth, and style guidance.\n",
            ]
            lines.extend(exemplar_header)
            total_chars += sum(len(line) for line in exemplar_header)

            for mem in exemplar_memories:
                agent_key = mem.agent_key or "unknown"
                entry = f"[EXEMPLAR - {agent_key}] {mem.content}"

                entry_len = len(entry) + 2
                if total_chars + entry_len > max_chars:
                    break

                lines.append(entry)
                total_chars += entry_len
                included += 1

        if included == 0:
            return "", 0

        return "\n".join(lines), included

    except Exception as e:
        logger.warning(f"Failed to auto-retrieve memories for agent '{agent_key}': {e}")
        return "", 0


def _agent_has_memory_tools(db: Session, agent: CustomAgent) -> bool:
    """Check if the agent has memory_store or memory_retrieve tools configured."""
    tools_config = agent.tools or []
    if not tools_config:
        return False

    tool_ids = []
    for entry in tools_config:
        if isinstance(entry, dict):
            tid = entry.get("tool_id")
            if tid:
                tool_ids.append(tid)
        elif isinstance(entry, int):
            tool_ids.append(entry)

    if not tool_ids:
        return False

    memory_tool_count = (
        db.query(AgentTool)
        .filter(
            AgentTool.id.in_(tool_ids),
            AgentTool.tool_key.in_(["memory_store", "memory_retrieve"]),
            AgentTool.is_active == True,
        )
        .count()
    )
    return memory_tool_count > 0


def _get_memory_instructions() -> str:
    """Return memory usage instructions for agents with memory tools."""
    return """

# ═══ MEMORY USAGE GUIDELINES ═══
You have access to long-term memory tools. Use them proactively:

## WHEN TO STORE (memory_store):
- User corrections or preferences ("I prefer...", "Don't do X", "Always use Y format")
- Discovered facts: rates, specifications, contacts, deadlines, organization preferences
- Successful approaches: patterns, formats, or methods that worked well
- Domain knowledge: industry standards, regulatory requirements, calculation methods

## WHEN TO RETRIEVE (memory_retrieve):
- Before writing proposals or cost estimates — check for past patterns and rates
- When a user mentions an organization, person, or tender type you may have encountered
- When making calculations — check for previously discovered rates or formulas
- At the start of complex tasks — retrieve relevant past learnings

## HOW TO STORE EFFECTIVELY:
- Include specific numbers, names, dates, and values (not vague summaries)
- Use descriptive keywords that will help future retrieval
- Set importance: 0.8-1.0 for critical facts, 0.5-0.7 for useful patterns, 0.3-0.4 for minor preferences
- Store as you go during the interaction, not just at the end"""
