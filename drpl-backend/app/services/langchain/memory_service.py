"""
DRPL Backend - Agent Memory Service
Provides long-term memory storage and retrieval for LangChain agents.
Uses keyword-based scoring (extending the RAG service pattern) over SQLite.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_memory import AgentMemory, AgentConversationHistory

logger = logging.getLogger(__name__)


# --- Visibility ---
#
# Memory used to be one pool: every row for an agent plus every row with no
# agent was retrieved for whoever asked, and learning extraction, exemplar
# capture and the agent's own `memory_store` wrote user work into it with no
# owner. One user's rates, margins and costing narratives were injected into
# the next user's system prompt -- and the costing settler accepts a memory
# with a reference as market evidence. Memory now follows the per-user
# firewall in `app.core.ownership`:
#
# * a row a user's work produced is that user's (`created_by`);
# * curated knowledge -- written by a master_admin through the memory page or
#   a knowledge-file upload, not captured from a run -- is shared, because
#   that is what it is for;
# * a row with no owner predates this and stays with master_admin, the same
#   rule every owned table follows (guessing an owner could hand one user
#   another's work);
# * background work with no actor reads curated knowledge only.

#: Context prefixes marking a row captured from a run rather than curated.
AUTO_CAPTURE_CONTEXT_PREFIXES = ("Auto-", "[agent] ")


def _master_admin_ids(db: Session) -> list[int]:
    from app.core.roles import MASTER_ADMIN, normalize
    from app.models.user import User

    try:
        return [uid for uid, role in db.query(User.id, User.role).all()
                if normalize(role) == MASTER_ADMIN]
    except Exception as e:
        logger.warning(f"memory visibility: could not read roles ({e})")
        return []


def _is_curated_clause(db: Session):
    from sqlalchemy import and_, not_, or_

    admins = _master_admin_ids(db)
    if not admins:
        return AgentMemory.id.is_(None)  # matches nothing
    not_auto = and_(*[
        not_(AgentMemory.context.startswith(prefix))
        for prefix in AUTO_CAPTURE_CONTEXT_PREFIXES
    ])
    return and_(
        AgentMemory.created_by.in_(admins),
        or_(AgentMemory.context.is_(None), not_auto),
    )


def _is_master_admin(actor) -> bool:
    from app.core.roles import MASTER_ADMIN, normalize

    return bool(actor is not None and actor.role and normalize(actor.role) == MASTER_ADMIN)


def visible_memories_query(db: Session, actor=None):
    """``AgentMemory`` rows the actor may read (ambient actor by default)."""
    from sqlalchemy import or_
    from app.core.actor_context import current_actor

    actor = actor if actor is not None else current_actor()
    q = db.query(AgentMemory)
    if _is_master_admin(actor):
        return q
    curated = _is_curated_clause(db)
    if actor is None or actor.user_id is None:
        return q.filter(curated)
    return q.filter(or_(AgentMemory.created_by == actor.user_id, curated))


def _ambient_user_id() -> Optional[int]:
    from app.core.actor_context import current_actor

    actor = current_actor()
    return actor.user_id if actor is not None else None


# --- Memory CRUD ---

def store_memory(
    db: Session,
    agent_key: Optional[str],
    memory_type: str,
    content: str,
    keywords: list[str] = None,
    importance: float = 0.5,
    context: Optional[str] = None,
    tender_id: Optional[int] = None,
    created_by: Optional[int] = None,
) -> AgentMemory:
    """Store a new memory entry, owned by the ambient actor unless given."""
    if created_by is None:
        created_by = _ambient_user_id()
    memory = AgentMemory(
        agent_key=agent_key,
        memory_type=memory_type,
        content=content,
        context=context,
        keywords=keywords or [],
        importance=max(0.0, min(1.0, importance)),
        tender_id=tender_id,
        created_by=created_by,
    )
    db.add(memory)
    try:
        db.commit()
        db.refresh(memory)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    logger.info(f"Stored memory {memory.id}: type={memory_type}, agent={agent_key}")
    return memory


def retrieve_memories(
    db: Session,
    agent_key: Optional[str],
    query: str,
    top_k: int = 10,
    memory_type: Optional[str] = None,
    tender_id: Optional[int] = None,
    min_score: float = 0.05,
) -> list[AgentMemory]:
    """
    Retrieve relevant memories using keyword-based scoring.

    Scoring formula:
      score = keyword_overlap * 0.4 + importance * 0.3 + recency_bonus * 0.2 + access_frequency * 0.1

    Returns top_k memories sorted by relevance score.
    """
    # Build base query — include both agent-specific and global memories,
    # limited to what the acting user may read.
    try:
        q = visible_memories_query(db)

        if agent_key:
            q = q.filter(
                (AgentMemory.agent_key == agent_key) |
                (AgentMemory.agent_key == None)
            )

        if memory_type:
            q = q.filter(AgentMemory.memory_type == memory_type)

        if tender_id:
            q = q.filter(
                (AgentMemory.tender_id == tender_id) |
                (AgentMemory.tender_id == None)
            )

        # Filter out expired memories
        now = datetime.now(timezone.utc)
        q = q.filter(
            (AgentMemory.expires_at == None) |
            (AgentMemory.expires_at > now)
        )

        all_memories = q.all()
    except Exception as e:
        logger.warning(f"retrieve_memories: DB query failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return []

    if not all_memories:
        return []

    # Tokenize query
    query_words = set(w.lower() for w in query.split() if len(w) > 2)

    # Score each memory
    scored = []
    for mem in all_memories:
        # Keyword overlap score (0-1)
        mem_keywords = set(k.lower() for k in (mem.keywords or []))
        mem_content_words = set(w.lower() for w in mem.content.split() if len(w) > 2)
        all_mem_words = mem_keywords | mem_content_words

        if query_words and all_mem_words:
            overlap = len(query_words & all_mem_words) / max(len(query_words), 1)
        else:
            overlap = 0.0

        # Importance score (already 0-1)
        importance = mem.importance or 0.5

        # Recency bonus (0-1, higher for more recent memories)
        if mem.created_at:
            age_days = (now - mem.created_at.replace(tzinfo=timezone.utc if mem.created_at.tzinfo is None else mem.created_at.tzinfo)).days
            recency = max(0.0, 1.0 - (age_days / 365))  # Decays over a year
        else:
            recency = 0.5

        # Access frequency bonus (0-1, capped)
        access_freq = min(1.0, (mem.access_count or 0) / 50)

        # Combined score
        score = (overlap * 0.4) + (importance * 0.3) + (recency * 0.2) + (access_freq * 0.1)

        if score > min_score:
            scored.append((score, mem))

    # Sort by score descending and take top_k
    scored.sort(key=lambda x: x[0], reverse=True)
    top_memories = [mem for _, mem in scored[:top_k]]

    # Update access counts and timestamps
    for mem in top_memories:
        mem.access_count = (mem.access_count or 0) + 1
        mem.last_accessed_at = now

    try:
        db.commit()
    except Exception:
        db.rollback()

    return top_memories


def delete_memory(db: Session, memory_id: int, actor=None) -> bool:
    """Delete a memory by ID -- only one the actor owns (master_admin: any)."""
    mem = db.query(AgentMemory).filter(AgentMemory.id == memory_id).first()
    if not mem:
        return False
    if actor is not None and not _is_master_admin(actor):
        if mem.created_by is None or mem.created_by != actor.user_id:
            return False  # reported as not found: a 403 confirms the row exists
    db.delete(mem)
    db.commit()
    return True


def list_memories(
    db: Session,
    agent_key: Optional[str] = None,
    memory_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    actor=None,
) -> list[AgentMemory]:
    """List memories the actor may read, with optional filters."""
    q = visible_memories_query(db, actor)
    if agent_key:
        q = q.filter(AgentMemory.agent_key == agent_key)
    if memory_type:
        q = q.filter(AgentMemory.memory_type == memory_type)
    return q.order_by(AgentMemory.created_at.desc()).offset(offset).limit(limit).all()


def get_memory_stats(db: Session, actor=None) -> dict:
    """Aggregate statistics over the memories the actor may read."""
    from collections import Counter

    rows = visible_memories_query(db, actor).with_entities(
        AgentMemory.memory_type, AgentMemory.agent_key
    ).all()
    by_type = Counter(t for t, _k in rows)
    by_agent = Counter(k for _t, k in rows)
    return {"total": len(rows), "by_type": dict(by_type), "by_agent": dict(by_agent)}


# --- Markdown File Parsing & Bulk Import ---

VALID_MEMORY_TYPES = {"fact", "preference", "learning", "decision", "exemplar"}


def parse_md_to_memories(md_content: str) -> list[dict]:
    """
    Parse a structured Markdown file into memory entries.

    Expected format:
        ## FACT
        Some knowledge content here.
        Keywords: keyword1, keyword2
        Importance: 0.8
        Context: Where this came from

        ## LEARNING
        Another piece of knowledge...
    """
    import re

    entries = []
    # Split on ## headings
    sections = re.split(r'^## ', md_content, flags=re.MULTILINE)

    for section in sections:
        section = section.strip()
        if not section:
            continue

        lines = section.split('\n')
        heading = lines[0].strip().lower()

        # Determine memory type from heading
        memory_type = None
        for mt in VALID_MEMORY_TYPES:
            if mt in heading:
                memory_type = mt
                break

        # Skip sections that don't match any valid memory type (e.g., title headers)
        if memory_type is None:
            continue

        content_lines = []
        keywords = []
        importance = 0.5
        context = None

        for line in lines[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()

            if lower.startswith("keywords:"):
                kw_text = stripped[len("keywords:"):].strip()
                keywords = [k.strip().lower() for k in kw_text.split(",") if k.strip()]
            elif lower.startswith("importance:"):
                try:
                    importance = float(stripped[len("importance:"):].strip())
                    importance = max(0.0, min(1.0, importance))
                except ValueError:
                    pass
            elif lower.startswith("context:"):
                context = stripped[len("context:"):].strip()
            else:
                content_lines.append(stripped)

        content = "\n".join(content_lines).strip()
        if content:
            entries.append({
                "memory_type": memory_type,
                "content": content,
                "keywords": keywords,
                "importance": importance,
                "context": context,
            })

    # If no headings were found, treat entire content as a single fact
    if not entries and md_content.strip():
        entries.append({
            "memory_type": "fact",
            "content": md_content.strip(),
            "keywords": [],
            "importance": 0.5,
            "context": None,
        })

    return entries


async def ai_parse_md_to_memories(md_content: str, db: Session) -> list[dict]:
    """
    Use Claude Haiku (cheapest model) to parse any freeform Markdown into
    structured memory entries. Calls the Anthropic API directly to avoid
    agent config routing issues.
    """
    import json as _json
    import re as _re
    import httpx

    from app.core.config import get_settings
    from app.services.ai_service import _get_effective_api_key

    api_key = _get_effective_api_key(db) if db else get_settings().anthropic_api_key
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    system_prompt = """You are a knowledge extraction engine. Your job is to read ANY Markdown document — regardless of its format, structure, or style — and break it into discrete knowledge entries.

The document may use any format: headings, bullet points, numbered lists, tables, paragraphs, code blocks, or a mix of all. Handle ALL of them.

For each distinct piece of knowledge you identify, output a JSON object:
{
  "memory_type": "fact" | "preference" | "learning" | "decision",
  "content": "A self-contained knowledge statement (1-3 sentences)",
  "keywords": ["keyword1", "keyword2"],
  "importance": 0.5,
  "context": "optional source/situation or null"
}

Types:
- fact: Objective information, data, specifications, certifications, company details
- preference: How things should be done, preferred approaches, formatting rules
- learning: Insights from experience, patterns, pitfalls, best practices
- decision: Past choices and their outcomes, what was decided and why

Rules:
1. Extract EVERY piece of knowledge — do not skip anything
2. Each entry must be self-contained (understandable without the original document)
3. Keywords should be 2-5 lowercase words useful for search/retrieval
4. Importance: 0.9+ critical, 0.7-0.8 important, 0.4-0.6 general knowledge
5. If a section contains multiple facts, split them into separate entries
6. Output ONLY a valid JSON array — no explanations, no markdown fences"""

    # Truncate very large files to fit in Haiku's context
    max_chars = 180_000  # ~45k tokens, well within Haiku's 200k context
    if len(md_content) > max_chars:
        md_content = md_content[:max_chars] + "\n\n[... file truncated ...]"

    # Estimate needed output tokens: ~250 tokens per entry, rough estimate from content size
    estimated_entries = md_content.count('## ') or max(1, len(md_content) // 500)
    needed_tokens = max(8192, min(16384, estimated_entries * 300))
    logger.info(f"[DRPL] AI memory parser: input={len(md_content)} chars, estimated_entries={estimated_entries}, max_tokens={needed_tokens}")

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": needed_tokens,
                    "temperature": 0.1,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": md_content}],
                },
            )

        if response.status_code != 200:
            error_msg = "Unknown API error"
            try:
                error_body = response.json()
                error_msg = error_body.get("error", {}).get("message", error_msg)
            except Exception:
                error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
            logger.error(f"[DRPL] Haiku memory parser API error: {response.status_code} - {error_msg}")
            raise ValueError(f"AI parsing failed: {error_msg}")

        data = response.json()
        stop_reason = data.get("stop_reason", "")
        logger.info(f"[DRPL] Haiku response: stop_reason={stop_reason}, usage={data.get('usage', {})}")

        # Extract text from response content blocks
        result_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                result_text += block.get("text", "")

        if not result_text.strip():
            raise ValueError(f"AI returned empty response (stop_reason={stop_reason})")

        # Robust JSON extraction: find the JSON array in the response
        cleaned = result_text.strip()
        # Strip markdown code fences if present
        cleaned = _re.sub(r'^```(?:json)?\s*\n?', '', cleaned)
        cleaned = _re.sub(r'\n?```\s*$', '', cleaned)
        cleaned = cleaned.strip()

        # Try to find JSON array in the response
        if not cleaned.startswith('['):
            bracket_idx = cleaned.find('[')
            if bracket_idx >= 0:
                cleaned = cleaned[bracket_idx:]
            else:
                logger.error(f"[DRPL] No JSON array found. Response starts with: {cleaned[:200]}")
                raise ValueError("No JSON array found in AI response")

        # If response was truncated (max_tokens hit), try to salvage partial JSON
        if stop_reason == "max_tokens" or not cleaned.endswith(']'):
            logger.warning("[DRPL] AI response appears truncated, attempting to salvage partial JSON")
            # Find the last complete JSON object (ends with })
            last_brace = cleaned.rfind('}')
            if last_brace > 0:
                cleaned = cleaned[:last_brace + 1] + ']'
            else:
                raise ValueError("AI response was truncated and could not be salvaged")

        parsed = _json.loads(cleaned)
        if not isinstance(parsed, list):
            parsed = [parsed]

        # Validate and sanitize entries
        entries = []
        for item in parsed:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            mt = str(item.get("memory_type", "fact")).lower().strip()
            if mt not in VALID_MEMORY_TYPES:
                mt = "fact"
            try:
                imp = float(item.get("importance", 0.5))
            except (ValueError, TypeError):
                imp = 0.5
            entries.append({
                "memory_type": mt,
                "content": str(item["content"]).strip(),
                "keywords": [str(k).strip().lower() for k in item.get("keywords", []) if k],
                "importance": max(0.0, min(1.0, imp)),
                "context": str(item["context"]).strip() if item.get("context") and item["context"] != "null" else None,
            })

        if not entries:
            raise ValueError("AI could not extract any knowledge entries from this file")

        if stop_reason == "max_tokens":
            logger.warning(f"[DRPL] AI memory parser: response was truncated but salvaged {len(entries)} entries")
        logger.info(f"[DRPL] AI memory parser extracted {len(entries)} entries using Haiku")
        return entries

    except _json.JSONDecodeError as e:
        logger.error(f"[DRPL] AI memory parser returned invalid JSON: {e}\nResponse preview: {result_text[:500] if 'result_text' in dir() else 'N/A'}")
        raise ValueError(f"AI returned malformed JSON. Try using the structured format with ## headings instead. (Detail: {str(e)[:100]})")
    except ValueError:
        raise  # Re-raise our own ValueErrors
    except Exception as e:
        logger.error(f"[DRPL] AI memory parsing failed: {type(e).__name__}: {e}")
        raise ValueError(f"AI parsing failed: {str(e)}")


def bulk_store_memories(
    db: Session,
    memories: list[dict],
    agent_key: Optional[str] = None,
    created_by: Optional[int] = None,
) -> list[int]:
    """Store multiple memory entries at once. Returns list of created IDs."""
    created_ids = []
    for entry in memories:
        mem = store_memory(
            db=db,
            agent_key=agent_key,
            memory_type=entry.get("memory_type", "fact"),
            content=entry["content"],
            keywords=entry.get("keywords", []),
            importance=entry.get("importance", 0.5),
            context=entry.get("context"),
            created_by=created_by,
        )
        created_ids.append(mem.id)
    return created_ids


# --- Conversation History ---

def save_conversation_turn(
    db: Session,
    session_id: str,
    agent_key: str,
    role: str,
    content: str,
    tool_calls: Optional[list[dict]] = None,
    metadata: Optional[dict] = None,
    output_type: Optional[str] = None,
    routed_from: Optional[str] = None,
) -> Optional[AgentConversationHistory]:
    """Save a single conversation turn.

    Tries the caller's session first (fast, atomic with surrounding work). If
    that commit fails — typically because a long-running LLM call left the
    request connection dead or the session expired/poisoned — falls back to a
    fresh SessionLocal so the turn still lands in history. Returns None only
    when both paths fail.
    """
    turn_kwargs = dict(
        session_id=session_id,
        agent_key=agent_key,
        role=role,
        content=content,
        tool_calls=tool_calls,
        metadata_json=metadata,
        output_type=output_type,
        routed_from=routed_from,
    )

    turn = AgentConversationHistory(**turn_kwargs)
    try:
        db.add(turn)
        db.commit()
        db.refresh(turn)
        return turn
    except Exception as e:
        logger.warning(
            f"save_conversation_turn: request-session commit failed "
            f"({type(e).__name__}: {e}) — retrying on fresh SessionLocal"
        )
        try:
            db.rollback()
        except Exception:
            pass

    # Fallback: fresh session with its own pool connection.
    from app.core.database import SessionLocal
    fresh = SessionLocal()
    try:
        fresh_turn = AgentConversationHistory(**turn_kwargs)
        fresh.add(fresh_turn)
        fresh.commit()
        fresh.refresh(fresh_turn)
        return fresh_turn
    except Exception as e:
        logger.warning(
            f"save_conversation_turn: fresh-session commit also failed "
            f"({type(e).__name__}: {e}) — turn dropped"
        )
        try:
            fresh.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            fresh.close()
        except Exception:
            pass


def get_conversation_history(
    db: Session,
    session_id: str,
    limit: int = 50,
) -> list[AgentConversationHistory]:
    """The most recent ``limit`` turns of a session, oldest first.

    This ordered ASC and then applied the LIMIT, which returns the *oldest*
    ``limit`` turns — the opposite of what every caller wants. Past turn 20 the
    agent's window stopped advancing: it re-read the opening of the
    conversation on every message and could not see what the user had just been
    talking about, which is what "the agent cannot access the chat history"
    looks like from the outside. On the Command Center's own history endpoint
    (limit 50) the same bug hid the user's most recent messages from them on
    reload.

    ``id`` breaks ties because turns saved in one request share a timestamp —
    ``created_at`` alone can sort an answer ahead of the question that produced
    it.
    """
    rows = (
        db.query(AgentConversationHistory)
        .filter(AgentConversationHistory.session_id == session_id)
        .order_by(
            AgentConversationHistory.created_at.desc(),
            AgentConversationHistory.id.desc(),
        )
        .limit(limit)
        .all()
    )
    return list(reversed(rows))
