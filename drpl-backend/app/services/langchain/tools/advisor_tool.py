"""
DRPL LangChain Tool - Advisor (Extended-Thinking Second Opinion)

Lets an agent "consult an adviser" for a reasoned second opinion on a hard
sub-problem without burning its own context window or model budget on
extended thinking. Internally this calls Claude with the `thinking` block
enabled at a configurable budget and returns the adviser's final answer
(plus, when available, the thinking summary).

Typical uses:
- Costing agent: "Given these two quote structures, which is the fairer
  comparison and why?"
- Proposal Creator: "Is this executive summary over-promising for a PSU
  tender? Push back hard if so."
- Checklist Generator: "Which of these three interpretations of Clause 5.2
  is most defensible? Cite the clause wording."

Sync-only. Uses `httpx.Client` to talk to the Anthropic messages API
directly, matching the pattern established in `pdf_vision_service.py`.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional, Type

import httpx
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)


_DEFAULT_SYSTEM = (
    "You are a senior adviser giving a second opinion to another AI agent "
    "working on a tender/proposal task. Be concise, specific, and opinionated. "
    "If the calling agent is wrong or missing something, say so directly. "
    "Prefer plain structured prose over bullet-point walls. Do not pad. "
    "Do not repeat the question back."
)


class AdvisorInput(BaseModel):
    question: str = Field(
        ...,
        description=(
            "The specific question or dilemma you want a second opinion on. "
            "Include enough context that the adviser can answer without "
            "re-reading the original brief."
        ),
    )
    context: Optional[str] = Field(
        None,
        description=(
            "Optional extra background (quote data, clause text, prior "
            "reasoning). Keep under ~4000 characters."
        ),
    )
    thinking_budget: int = Field(
        8000,
        description=(
            "Extended-thinking token budget for the adviser. Higher = deeper "
            "reasoning and higher cost. Default 8000; cap 16000."
        ),
    )
    return_thinking: bool = Field(
        False,
        description=(
            "When true, also return the adviser's thinking summary alongside "
            "the final answer. Off by default to keep the calling agent's "
            "context clean."
        ),
    )


def _get_api_key(db: Optional[Session]) -> Optional[str]:
    if db is not None:
        try:
            from app.models.platform_setting import PlatformSetting
            row = db.query(PlatformSetting).filter(PlatformSetting.key == "anthropic_api_key").first()
            if row and row.value:
                return row.value
        except Exception:
            try: db.rollback()
            except Exception: pass
    return get_settings().anthropic_api_key


def _resolve_advisor_model(db: Optional[Session]) -> str:
    """Platform setting `advisor_model` wins; else sensible default (Opus)."""
    default = "claude-opus-4-7"
    if db is None:
        return default
    try:
        from app.services.settings_service import get_setting_value
        return str(get_setting_value(db, "advisor_model", default)) or default
    except Exception:
        return default


def consult_advisor_sync(
    question: str,
    *,
    context: Optional[str] = None,
    thinking_budget: int = 8000,
    return_thinking: bool = False,
    db: Optional[Session] = None,
    timeout_s: float = 180.0,
) -> dict:
    """Synchronous Claude extended-thinking call. Returns a result dict."""
    api_key = _get_api_key(db)
    if not api_key:
        return {"status": "error", "message": "No anthropic_api_key configured."}

    model = _resolve_advisor_model(db)
    budget = max(1024, min(int(thinking_budget or 8000), 16000))
    # `max_tokens` must exceed `thinking.budget_tokens` per the API contract.
    max_tokens = budget + 4000

    user_text_parts = [question.strip()]
    if context:
        user_text_parts.append("\n\n---\nAdditional context:\n" + context.strip())
    user_text = "\n".join(user_text_parts)

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": _DEFAULT_SYSTEM,
        "thinking": {"type": "enabled", "budget_tokens": budget},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
        ],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout_s) as client:
            t0 = time.time()
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=body,
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            if resp.status_code != 200:
                logger.warning(
                    f"advisor: HTTP {resp.status_code} ({elapsed_ms}ms): {resp.text[:300]}"
                )
                return {
                    "status": "error",
                    "message": f"Anthropic HTTP {resp.status_code}",
                    "detail": resp.text[:500],
                }
            data = resp.json()
    except httpx.TimeoutException:
        return {"status": "error", "message": f"Timeout after {timeout_s}s"}
    except Exception as e:
        logger.warning(f"advisor: HTTP call failed: {e}")
        return {"status": "error", "message": f"HTTP call failed: {e}"}

    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in data.get("content", []):
        btype = block.get("type")
        if btype == "text":
            answer_parts.append(block.get("text", ""))
        elif btype == "thinking":
            thinking_parts.append(block.get("thinking", ""))
    answer = "\n".join(p for p in answer_parts if p).strip()
    thinking = "\n".join(p for p in thinking_parts if p).strip()

    if not answer:
        return {"status": "error", "message": "Adviser returned no content."}

    usage = data.get("usage", {}) or {}
    result = {
        "status": "success",
        "model": model,
        "answer": answer,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thinking_budget": budget,
        "elapsed_ms": elapsed_ms,
    }
    if return_thinking and thinking:
        result["thinking"] = thinking
    return result


class AdvisorTool(BaseTool):
    """Consult an extended-thinking adviser for a reasoned second opinion."""
    name: str = "advisor"
    description: str = (
        "Consult a senior adviser (Claude with extended thinking) for a "
        "reasoned second opinion on a tough sub-problem: ambiguous clauses, "
        "quote comparisons, proposal framing dilemmas. Returns {status, "
        "answer, model, input_tokens, output_tokens}. Use sparingly — this "
        "is more expensive than a normal model call. Do NOT call in a loop."
    )
    args_schema: Type[BaseModel] = AdvisorInput

    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        question: str,
        context: Optional[str] = None,
        thinking_budget: int = 8000,
        return_thinking: bool = False,
    ) -> str:
        result = consult_advisor_sync(
            question,
            context=context,
            thinking_budget=thinking_budget,
            return_thinking=return_thinking,
            db=self.db,
        )
        return json.dumps(result, default=str)
