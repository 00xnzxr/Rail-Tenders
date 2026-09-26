"""
DRPL Backend - Learning Extraction Service
Automatically extracts learnable insights from agent interactions
and stores them as long-term agent memories using a background Haiku call.
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """You are a knowledge extraction engine. Analyze this agent-user interaction and extract REUSABLE learnings that would be valuable in future interactions.

Extract ONLY genuinely new, non-obvious knowledge. Do NOT extract:
- Information that was just restated from the user's question
- Generic facts anyone would know
- Conversation-specific details with no future value

For each learning, output a JSON object:
{
  "memory_type": "fact" | "preference" | "learning" | "decision",
  "content": "A self-contained knowledge statement (1-3 sentences)",
  "keywords": ["keyword1", "keyword2", "keyword3"],
  "importance": 0.3 to 0.7
}

Types:
- fact: Concrete data points — rates, specs, contacts, organization details
- preference: User preferences — formatting, approach, communication style
- learning: Insights from the interaction — what worked, patterns discovered
- decision: Choices made and their rationale

Rules:
1. Maximum 5 entries per interaction
2. Each entry must be self-contained and useful without the original conversation
3. Keywords should be 2-5 lowercase words useful for search/retrieval
4. Importance capped at 0.7 (can grow via access frequency over time)
5. Focus on domain knowledge, not conversation mechanics
6. Output ONLY a valid JSON array — no explanations, no markdown fences"""


async def extract_and_store_learnings(
    agent_key: str,
    user_input: str,
    agent_output: str,
    tender_id: Optional[int] = None,
    execution_id: Optional[int] = None,
) -> list[int]:
    """
    Extract learnable insights from an agent interaction and store as memories.
    Designed to run as a fire-and-forget background task.

    Creates its own DB session since the caller's session may be closed.

    Args:
        agent_key: The agent's key for memory scoping
        user_input: The user's input text
        agent_output: The agent's output text
        tender_id: Optional tender context
        execution_id: Optional execution ID for provenance tracking

    Returns:
        List of created memory IDs
    """
    # Skip very short interactions
    if not agent_output or len(agent_output) < 100:
        return []

    from app.core.database import SessionLocal
    db = SessionLocal()

    try:
        # Build interaction text for extraction
        interaction = (
            f"USER INPUT:\n{user_input[:3000]}\n\n"
            f"AGENT RESPONSE:\n{agent_output[:5000]}"
        )

        # Route through call_ai so this respects the platform's force_provider_override
        # and configured ai_provider — previously this hardcoded Anthropic Haiku and
        # would crash the whole pipeline if Claude credits were depleted, even when
        # the rest of the system was failed-over to OpenAI.
        from app.services.ai_service import call_ai
        try:
            result_text = await call_ai(
                system_prompt=EXTRACTION_SYSTEM_PROMPT,
                user_prompt=interaction,
                db=db,
                agent_name="learning_extractor",
                max_tokens_override=2048,
            )
        except Exception as call_err:
            logger.warning(f"Learning extraction skipped — AI call failed: {call_err}")
            return []

        entries = _parse_extraction_response(result_text)
        if not entries:
            return []

        # Deduplicate against existing memories and store
        from app.services.langchain.memory_service import retrieve_memories, store_memory
        created_ids = []

        for entry in entries[:5]:  # Hard cap at 5
            content = entry.get("content", "").strip()
            if not content or len(content) < 10:
                continue

            # Check for duplicates
            existing = retrieve_memories(
                db=db,
                agent_key=agent_key,
                query=content,
                top_k=3,
                min_score=0.40,  # High threshold = likely duplicate
            )

            if existing:
                # Check keyword overlap
                new_keywords = set(k.lower() for k in entry.get("keywords", []))
                for mem in existing:
                    mem_keywords = set(k.lower() for k in (mem.keywords or []))
                    if new_keywords and mem_keywords:
                        overlap = len(new_keywords & mem_keywords) / max(len(new_keywords), 1)
                        if overlap > 0.6:
                            logger.debug(f"Skipping duplicate learning: '{content[:50]}...'")
                            break
                else:
                    # No duplicate found among existing
                    pass
                if existing and any(
                    len(set(k.lower() for k in entry.get("keywords", [])) &
                        set(k.lower() for k in (m.keywords or []))) /
                    max(len(entry.get("keywords", [])), 1) > 0.6
                    for m in existing
                    if entry.get("keywords")
                ):
                    continue

            # Store the learning
            importance = min(0.7, entry.get("importance", 0.5))
            context = f"Auto-extracted from execution #{execution_id}" if execution_id else "Auto-extracted from interaction"

            mem = store_memory(
                db=db,
                agent_key=agent_key,
                content=content,
                memory_type=entry.get("memory_type", "learning"),
                keywords=entry.get("keywords", []),
                importance=importance,
                context=context,
                tender_id=tender_id,
            )
            created_ids.append(mem.id)

        if created_ids:
            logger.info(
                f"Learning extraction for agent '{agent_key}': stored {len(created_ids)} memories "
                f"from execution #{execution_id}"
            )

        return created_ids

    except Exception as e:
        logger.error(f"Learning extraction failed for agent '{agent_key}': {e}")
        return []
    finally:
        db.close()


def _parse_extraction_response(result_text: str) -> list[dict]:
    """Parse the JSON array of learnings from the LLM response, tolerating code fences."""
    if not result_text:
        return []
    try:
        text = result_text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        entries = json.loads(text)
        if not isinstance(entries, list):
            entries = [entries]
        return entries
    except json.JSONDecodeError:
        logger.warning("Learning extraction: could not parse LLM response as JSON")
        return []


async def capture_quality_output(
    agent_key: str,
    output: str,
    output_type: str,
    user_input: str = "",
    tender_id: Optional[int] = None,
    status: str = "completed",
) -> Optional[int]:
    """
    Capture high-quality agent outputs as exemplar memories for self-training.
    Designed to run as a fire-and-forget background task.

    Only captures outputs that:
    - Are from specialized agents (not general_query)
    - Have completed status
    - Exceed minimum length threshold
    """
    # Quality gates
    if status != "completed":
        return None
    if output_type in ("general", "clarification_needed", ""):
        return None
    if not output or len(output) < 500:
        return None

    from app.core.database import SessionLocal
    db = SessionLocal()

    try:
        from app.services.langchain.memory_service import retrieve_memories, store_memory

        # Create a summarized version (first 3000 chars)
        summary = output[:3000]
        if len(output) > 3000:
            summary += "\n[... truncated for storage ...]"

        # Build keywords from output_type, agent_key, and user input
        keywords = [output_type, agent_key]
        # Extract key terms from user input (words > 4 chars, skip common words)
        stop_words = {"please", "could", "would", "should", "about", "which", "their", "there", "these", "those", "based"}
        if user_input:
            input_words = [
                w.lower().strip(".,!?()[]{}\"'")
                for w in user_input.split()
                if len(w) > 4 and w.lower() not in stop_words
            ]
            keywords.extend(input_words[:5])

        # Deduplicate: check existing exemplars with high keyword overlap
        existing = retrieve_memories(
            db=db,
            agent_key=agent_key,
            query=summary[:500],
            top_k=3,
            memory_type="exemplar",
            min_score=0.35,
        )
        if existing:
            new_kw = set(k.lower() for k in keywords)
            for mem in existing:
                mem_kw = set(k.lower() for k in (mem.keywords or []))
                if new_kw and mem_kw:
                    overlap = len(new_kw & mem_kw) / max(len(new_kw), 1)
                    if overlap > 0.5:
                        logger.debug(f"Skipping duplicate exemplar for agent '{agent_key}'")
                        return None

        # Store as exemplar memory
        mem = store_memory(
            db=db,
            agent_key=agent_key,
            memory_type="exemplar",
            content=summary,
            keywords=keywords,
            importance=0.5,
            context=f"Auto-captured {output_type} output",
            tender_id=tender_id,
        )

        logger.info(f"Captured quality output as exemplar memory {mem.id} for agent '{agent_key}'")
        return mem.id

    except Exception as e:
        logger.error(f"Quality output capture failed for agent '{agent_key}': {e}")
        return None
    finally:
        db.close()
