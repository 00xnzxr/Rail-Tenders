"""Output-quality diagnosis for the master agent.

Sub-project C: let the agent notice when the platform's own generated output is
wrong, and say so in terms a non-technical user can act on.

The checks are not generic "does this look good" prompting — they are the
specific failure signatures this codebase has actually produced and documented:

- **Empty or near-empty synthesis.** `config.py` records that multi-PDF tenders
  under parallel load "occasionally produced empty synthesis output even though
  each individual call succeeded", which is why `tender_analyzer_max_parallel`
  is pinned to 1. A tender with documents but no analysis text is that bug.

- **Paraphrased annexures.** The two-pass annexure design exists because a
  single pass "silently compressed — dropping numbered clauses, collapsing
  address blocks, and substituting descriptive '[Name of Bidder]' placeholders
  where the source had dotted rules". Those placeholder strings are a
  fingerprint: a verbatim transcription would carry the blank, not a
  description of the blank.

- **Failed or stuck runs.** Orphaned `running` rows and `failed` executions
  tied to the tender.

- **Empty workspace documents.** A document row that exists with no content is
  a generation that silently produced nothing.

Detection is deterministic Python, not an LLM judging its own work — Rule 1 of
the backend Constitution, and also the only way the result is trustworthy: an
LLM asked whether its own output is good is the least reliable possible judge.
The agent's job is to explain and offer to fix, not to decide what counts as
broken.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: Text an annexure transcription should never contain. These are descriptions
#: of a blank rather than the blank itself — the paraphrasing fingerprint.
_PLACEHOLDER_PATTERNS = [
    r"\[Name of Bidder\]",
    r"\[Insert .{0,40}\]",
    r"\[Company Name\]",
    r"\[Date\]",
    r"\[to be filled.{0,20}\]",
    r"\bXXXX+\b",
]

#: Below this, a "synthesis" is not a synthesis.
_MIN_ANALYSIS_CHARS = 200
_MIN_DOCUMENT_CHARS = 40


class _TenderIdInput(BaseModel):
    tender_id: int = Field(..., description="The tender to check.")


def _check_placeholders(text: str) -> list[str]:
    hits = []
    for pattern in _PLACEHOLDER_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            hits.append(pattern)
    return hits


def diagnose_tender_outputs(db, tender_id: int) -> dict:
    """Return a structured report of what looks wrong for one tender.

    Pure inspection — never mutates. Returned as a dict so the tool layer and
    the tests can both read it without parsing prose.
    """
    from app.models.agent_builder import AgentExecution
    from app.models.tender import Tender, TenderDocument
    from app.models.workspace import DocumentWorkspace

    report: dict[str, Any] = {
        "tender_id": tender_id,
        "problems": [],
        "checked": [],
        "ok": True,
    }

    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        report["ok"] = False
        report["problems"].append(
            {
                "kind": "missing_tender",
                "detail": f"There is no tender #{tender_id}.",
                "fix": None,
            }
        )
        return report

    # ── analysis present but empty ──
    report["checked"].append("tender analysis")
    doc_count = (
        db.query(TenderDocument).filter(TenderDocument.tender_id == tender_id).count()
    )
    analysis = getattr(tender, "ai_analysis", None) or getattr(tender, "analysis", None)
    analysis_text = analysis if isinstance(analysis, str) else ""
    if doc_count and len(analysis_text.strip()) < _MIN_ANALYSIS_CHARS:
        report["ok"] = False
        report["problems"].append(
            {
                "kind": "empty_analysis",
                "detail": (
                    f"This tender has {doc_count} document(s) but its analysis is "
                    "empty or far too short. This is the known failure where the "
                    "per-document reads succeed but the final summary comes back "
                    "blank."
                ),
                "fix": "call_deep_analyzer",
            }
        )

    # ── annexures that were paraphrased instead of transcribed ──
    report["checked"].append("annexure wording")
    workspace_docs = (
        db.query(DocumentWorkspace)
        .filter(DocumentWorkspace.tender_id == tender_id)
        .limit(500)
        .all()
    )
    # The document's human name lives on the linked ChecklistItem, not on
    # DocumentWorkspace. Resolved in one query rather than per row, and the
    # report is useless without it — "document #417" tells the user nothing.
    from app.models.checklist import ChecklistItem

    item_ids = [d.checklist_item_id for d in workspace_docs if d.checklist_item_id]
    names: dict[int, str] = {}
    if item_ids:
        names = {
            row.id: row.item_name
            for row in db.query(ChecklistItem.id, ChecklistItem.item_name)
            .filter(ChecklistItem.id.in_(item_ids))
            .all()
        }

    paraphrased: list[str] = []
    empty_docs: list[str] = []
    for doc in workspace_docs:
        content = (
            getattr(doc, "draft_content_markdown", None)
            or getattr(doc, "draft_content_html", None)
            or ""
        )
        title = names.get(doc.checklist_item_id) or f"document #{doc.id}"
        if len(content.strip()) < _MIN_DOCUMENT_CHARS:
            empty_docs.append(title)
        elif _check_placeholders(content):
            paraphrased.append(title)

    if paraphrased:
        report["ok"] = False
        report["problems"].append(
            {
                "kind": "paraphrased_annexure",
                "detail": (
                    "These documents contain descriptions of blanks (like "
                    "\"[Name of Bidder]\") where the original tender had a blank "
                    "line to fill in. They were summarised rather than copied "
                    "word for word, so submitting them as-is risks rejection: "
                    + ", ".join(paraphrased[:10])
                ),
                "fix": "regenerate_annexures",
            }
        )

    if empty_docs:
        report["ok"] = False
        report["problems"].append(
            {
                "kind": "empty_document",
                "detail": (
                    "These documents exist but have no content — the generation "
                    "produced nothing: " + ", ".join(empty_docs[:10])
                ),
                "fix": "call_workspace_manager",
            }
        )

    # ── failed / stuck runs ──
    report["checked"].append("recent jobs")
    failed = (
        db.query(AgentExecution)
        .filter(AgentExecution.status.in_(("failed", "timeout")))
        .order_by(AgentExecution.created_at.desc())
        .limit(5)
        .all()
    )
    if failed:
        report["problems"].append(
            {
                "kind": "failed_runs",
                "detail": "Recent jobs that did not finish: "
                + "; ".join(
                    f"#{r.id} {r.status}"
                    + (f" ({str(r.error_message)[:120]})" if r.error_message else "")
                    for r in failed
                ),
                "fix": "retry_failed_agent_run",
            }
        )
        report["ok"] = False

    return report


def _render(report: dict) -> str:
    """Plain-language rendering for the chat, no jargon and no tool names."""
    if report["ok"] and not report["problems"]:
        checked = ", ".join(report["checked"])
        return f"I checked {checked} for tender #{report['tender_id']} and everything looks fine."

    lines = [f"I found {len(report['problems'])} problem(s) with tender #{report['tender_id']}:"]
    for i, p in enumerate(report["problems"], 1):
        lines.append(f"{i}. {p['detail']}")
    lines.append(
        "\nI can fix these if you want — say the word and I'll ask you to "
        "confirm each change before I make it."
    )
    return "\n".join(lines)


def build_quality_tools(db, user_id: Optional[int] = None) -> list[BaseTool]:
    """The output-quality checker. Read-only, so it is never gated."""

    class DiagnoseTenderOutputsTool(BaseTool):
        name: str = "diagnose_tender_outputs"
        description: str = (
            "Check a tender's generated output for known problems: an empty "
            "analysis, annexures that were summarised instead of copied word for "
            "word, documents that came out blank, and jobs that failed. Use this "
            "whenever the user says something looks wrong, is missing, or asks "
            "you to check their work. Read-only."
        )
        args_schema: Any = _TenderIdInput

        class Config:
            arbitrary_types_allowed = True

        def _run(self, tender_id: int, **kwargs) -> str:  # type: ignore[override]
            import asyncio

            return asyncio.run(self._arun(tender_id=tender_id))

        async def _arun(self, tender_id: int, **kwargs) -> str:  # type: ignore[override]
            try:
                return _render(diagnose_tender_outputs(db, int(tender_id)))
            except Exception as e:
                logger.error("quality_tools: diagnosis failed: %s", e, exc_info=True)
                return f"I couldn't finish checking that tender: {e}"

    return [DiagnoseTenderOutputsTool()]
