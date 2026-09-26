"""
DRPL Backend - Context Budget & Pre-Flight Trim Policy

Single source of truth for "will this LLM request fit?" decisions. Estimates
input token cost across system prompt, user message text, and native PDF
vision blocks, then applies a deterministic trim sequence to keep the request
under Claude's 200K context window.

Designed to solve the systemic context-overflow problem in the costing agent:
the platform was sending ~210K+ tokens (200K training data + multi-PDF vision
blocks + 30K text extracts + structured BIDDING SCHEDULE block) and only
finding out it was too large when Anthropic returned a 400. The friendly
"input is too long" error fired AFTER the user had already incurred the
wait and the run was lost.

This module estimates pre-flight, drops the least-valuable signals first
(annexed PDFs when a structured schedule is captured, training data,
PDF text extracts), and records every trim so the streaming layer can show
the user a one-line notice explaining what was dropped.

Plan: ~/.claude/plans/now-i-need-to-synchronous-taco.md (Phase 2)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.services.settings_service import get_effective_setting

logger = logging.getLogger(__name__)


# Conservative chars→tokens ratio for Anthropic's BPE tokenizer. Real ratio
# is ~3.5–4.0 for English; we use 3.5 to err on the side of over-estimating
# input size (better to trim a bit too much than to overflow).
_CHARS_PER_TOKEN = 3.5

# Anthropic's published guidance for native PDF vision blocks: each page is
# roughly 1500–2000 tokens depending on density. Use 1800 as a defensible
# middle estimate. This is the dominant cost for tenders with large annexed
# documents — a 100-page PDF alone is ~180K tokens.
_TOKENS_PER_PDF_PAGE = 1800

# NIT-class doc_type tags. When the captured bidding schedule is present,
# only these doc types are useful to attach as native vision blocks; the
# rest (annexures, drawings, T&C) are redundant context.
_NIT_CLASS_DOC_TYPES = frozenset({"BOQ", "schedule_of_rates", "RFP"})


def estimate_text_tokens(text: Optional[str]) -> int:
    """Char-count divided by the conservative chars/token ratio."""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN)


def estimate_pdf_block_tokens(blocks: Optional[list]) -> int:
    """Total tokens for a list of native PDF document blocks.

    Each block is expected to carry a `page_count` field added by
    `_build_tender_pdf_content_blocks` in the costing agent. If a block
    lacks page_count (e.g. third-party blocks), we conservatively assume
    50 pages.
    """
    if not blocks:
        return 0
    total = 0
    for blk in blocks:
        pages = blk.get("page_count") if isinstance(blk, dict) else None
        if not isinstance(pages, int) or pages <= 0:
            pages = 50
        total += pages * _TOKENS_PER_PDF_PAGE
    return total


@dataclass
class BudgetConfig:
    """PlatformSetting-backed knobs. Read once at the start of a run.

    target_tokens — the safe ceiling we aim for after pre-flight trim.
        Set well below the model's hard 200K so output tokens have room.
    hard_floor_tokens — if estimated input exceeds this even after trim,
        fail fast rather than letting Anthropic reject.
    training_data_ceiling — start of the trim ladder for the training
        data injection. The full training data is loaded with a 200K
        char cap at load time; this ceiling further bounds how much
        gets stitched into the system prompt for any one run.
    pdf_pages_high_water — if a single PDF exceeds this, count it as
        "heavy" and prefer to drop it first when over budget.
    """

    target_tokens: int = 180_000
    hard_floor_tokens: int = 195_000
    training_data_ceiling: int = 100_000
    pdf_pages_high_water: int = 80

    @classmethod
    def load(cls, db: Optional[Session]) -> "BudgetConfig":
        """Resolve from PlatformSetting overrides, falling back to defaults."""
        if db is None:
            return cls()
        try:
            return cls(
                target_tokens=int(
                    get_effective_setting(db, "context_budget_target_tokens", 180_000)
                    or 180_000
                ),
                hard_floor_tokens=int(
                    get_effective_setting(db, "context_budget_hard_floor", 195_000)
                    or 195_000
                ),
                training_data_ceiling=int(
                    get_effective_setting(
                        db, "context_budget_training_data_ceiling", 100_000
                    )
                    or 100_000
                ),
                pdf_pages_high_water=int(
                    get_effective_setting(
                        db, "context_budget_pdf_pages_high_water", 80
                    )
                    or 80
                ),
            )
        except Exception as e:
            # Settings lookup failures must not block costing. Use defaults.
            logger.warning(f"[context_budget] settings load failed, using defaults: {e}")
            return cls()


@dataclass
class TrimContext:
    """Mutable bag of trimmable signals passed into the trim policy.

    Each field corresponds to one accumulator from the costing agent's
    request assembly. The trim policy may modify any of them. After the
    policy runs, the caller pulls the updated values and rebuilds its
    user_message / pdf_blocks accordingly.

    Fields that are not size-relevant (system_prompt_base, user_message_other)
    still contribute to the budget but are NOT trimmable here — they're
    the irreducible floor.
    """

    # Fixed cost (not trimmed by this policy).
    system_prompt_base_chars: int = 0  # the costing system prompt itself
    user_message_other_chars: int = 0  # USER REQUEST, framing, etc.

    # Trimmable signals.
    training_data_text: str = ""
    pdf_blocks: list = field(default_factory=list)
    pdf_extracts_text: str = ""
    bidding_schedule_block_text: str = ""
    bidding_schedule_sidecar_text: str = ""
    tender_analysis_text: str = ""
    section_7_text: str = ""

    # Signals derived from the run state.
    has_bidding_schedule: bool = False  # BIDDING SCHEDULE block was captured
    bidding_schedule_row_count: int = 0


def _total_input_tokens(ctx: TrimContext) -> int:
    """Sum the estimated input tokens across every accumulator in ctx."""
    return (
        ctx.system_prompt_base_chars // int(_CHARS_PER_TOKEN)
        + ctx.user_message_other_chars // int(_CHARS_PER_TOKEN)
        + estimate_text_tokens(ctx.training_data_text)
        + estimate_pdf_block_tokens(ctx.pdf_blocks)
        + estimate_text_tokens(ctx.pdf_extracts_text)
        + estimate_text_tokens(ctx.bidding_schedule_block_text)
        + estimate_text_tokens(ctx.bidding_schedule_sidecar_text)
        + estimate_text_tokens(ctx.tender_analysis_text)
        + estimate_text_tokens(ctx.section_7_text)
    )


def apply_trim_policy(
    ctx: TrimContext,
    cfg: BudgetConfig,
) -> tuple[TrimContext, list[str], int, int]:
    """Run the deterministic trim sequence to bring `ctx` under `cfg.target_tokens`.

    Order — drop the cheapest-to-lose signals first:

      1. **Drop annexed-class PDFs** when a BIDDING SCHEDULE is captured.
         The structured schedule + analysis summary already cover what the
         annexed doc redundantly provided.
      2. **Cap training-data injection** at `cfg.training_data_ceiling`,
         falling to 50K → 25K if still over budget.
      3. **Drop PDF text extracts** when a BIDDING SCHEDULE is captured.
         The schedule is the structured source of truth; the text dump
         becomes redundant in that case.
      4. **Cap BOQ JSON sidecar / markdown table** at 30K chars; truncate
         the human-readable table to 50 rows if still over.
      5. **Hard floor** — if still over, give up and the caller fails fast.

    Returns the mutated ctx, a list of trim notes (each a one-line string
    suitable for the chat warning bubble), and (before_tokens, after_tokens)
    for logging.
    """
    before_tokens = _total_input_tokens(ctx)
    trim_notes: list[str] = []

    if before_tokens <= cfg.target_tokens:
        return ctx, trim_notes, before_tokens, before_tokens

    # --- Step 1: drop annexed-class PDFs (only when we have a captured schedule) ---
    if ctx.has_bidding_schedule and ctx.pdf_blocks:
        before_blocks = len(ctx.pdf_blocks)
        before_pages = sum(
            (b.get("page_count") or 50) for b in ctx.pdf_blocks if isinstance(b, dict)
        )
        kept: list = []
        dropped_pages = 0
        for blk in ctx.pdf_blocks:
            doc_type = blk.get("doc_type") if isinstance(blk, dict) else None
            if doc_type in _NIT_CLASS_DOC_TYPES:
                kept.append(blk)
            else:
                dropped_pages += blk.get("page_count") or 50 if isinstance(blk, dict) else 50
        if len(kept) < before_blocks:
            ctx.pdf_blocks = kept
            kept_pages = before_pages - dropped_pages
            trim_notes.append(
                f"Dropped {before_blocks - len(kept)} annexed/non-NIT PDF(s) "
                f"(~{dropped_pages} pages) — the captured bidding schedule already "
                f"covers the schedule content."
            )
            logger.info(
                f"[context_budget] step1 dropped {before_blocks - len(kept)} non-NIT PDFs "
                f"({dropped_pages} pages); kept {len(kept)} PDFs ({kept_pages} pages)"
            )

    if _total_input_tokens(ctx) <= cfg.target_tokens:
        after_tokens = _total_input_tokens(ctx)
        return ctx, trim_notes, before_tokens, after_tokens

    # --- Step 2: cap training data (ladder: ceiling → 50K → 25K) ---
    for ceiling in (cfg.training_data_ceiling, 50_000, 25_000):
        if len(ctx.training_data_text) > ceiling:
            before_len = len(ctx.training_data_text)
            ctx.training_data_text = (
                ctx.training_data_text[:ceiling]
                + f"\n\n…(training data truncated to {ceiling // 1000}K chars to fit context budget)"
            )
            trim_notes.append(
                f"Capped training data at {ceiling // 1000}K chars "
                f"(was {before_len // 1000}K) — kept the highest-priority rate cards."
            )
            logger.info(
                f"[context_budget] step2 capped training data {before_len} → {ceiling}"
            )
        if _total_input_tokens(ctx) <= cfg.target_tokens:
            after_tokens = _total_input_tokens(ctx)
            return ctx, trim_notes, before_tokens, after_tokens

    # --- Step 3: drop PDF text extracts when bidding schedule is present ---
    if ctx.has_bidding_schedule and ctx.pdf_extracts_text:
        dropped_chars = len(ctx.pdf_extracts_text)
        ctx.pdf_extracts_text = ""
        trim_notes.append(
            f"Dropped {dropped_chars // 1000}K chars of PDF text extracts — "
            f"the structured bidding schedule is the source of truth."
        )
        logger.info(f"[context_budget] step3 dropped {dropped_chars} chars of pdf extracts")

    if _total_input_tokens(ctx) <= cfg.target_tokens:
        after_tokens = _total_input_tokens(ctx)
        return ctx, trim_notes, before_tokens, after_tokens

    # --- Step 4: cap BOQ sidecar / table ---
    if len(ctx.bidding_schedule_sidecar_text) > 30_000:
        before_len = len(ctx.bidding_schedule_sidecar_text)
        ctx.bidding_schedule_sidecar_text = (
            ctx.bidding_schedule_sidecar_text[:30_000]
            + "/* sidecar truncated to 30K to fit context budget */"
        )
        trim_notes.append(
            f"Truncated BIDDING SCHEDULE JSON sidecar at 30K chars "
            f"(was {before_len // 1000}K) — agent will work with the first ~30K of rows."
        )

    if _total_input_tokens(ctx) <= cfg.target_tokens:
        after_tokens = _total_input_tokens(ctx)
        return ctx, trim_notes, before_tokens, after_tokens

    # If still over budget here, fall further: drop PDF extracts even
    # WITHOUT bidding schedule (last resort), and aggressively cap training.
    if ctx.pdf_extracts_text:
        dropped_chars = len(ctx.pdf_extracts_text)
        ctx.pdf_extracts_text = ""
        trim_notes.append(
            f"Dropped remaining {dropped_chars // 1000}K chars of PDF extracts "
            f"(no captured schedule available — accuracy may suffer)."
        )

    if len(ctx.training_data_text) > 10_000:
        before_len = len(ctx.training_data_text)
        ctx.training_data_text = (
            ctx.training_data_text[:10_000] + "\n\n…(further truncated)"
        )
        trim_notes.append(
            f"Aggressively capped training data at 10K chars (was {before_len // 1000}K)."
        )

    after_tokens = _total_input_tokens(ctx)
    if after_tokens > cfg.hard_floor_tokens:
        logger.error(
            f"[context_budget] HARD FLOOR EXCEEDED — after all trims, "
            f"estimated input is still {after_tokens} tokens "
            f"(hard_floor={cfg.hard_floor_tokens}). Caller should fail fast."
        )
    return ctx, trim_notes, before_tokens, after_tokens


def log_budget_event(
    tender_id: Optional[int],
    agent_key: str,
    before_tokens: int,
    after_tokens: int,
    trims_applied: list[str],
) -> None:
    """Structured info log so /health/costing (Phase 4) can count overflows."""
    logger.info(
        f"[context_budget] tender_id={tender_id} agent={agent_key} "
        f"before_tokens={before_tokens} after_tokens={after_tokens} "
        f"trims_applied={trims_applied!r}"
    )
    # Increment the per-minute Redis counter so /health/costing can surface
    # "how many trims fired in the last hour?". Fire-and-forget.
    try:
        from app.core.costing_metrics import record_trim
        if trims_applied:
            record_trim()
    except Exception as e:
        logger.debug(f"[context_budget] record_trim failed (non-fatal): {e}")
