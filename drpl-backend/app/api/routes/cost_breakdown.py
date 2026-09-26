"""
DRPL Backend - Cost Breakdown Routes
GET / PUT / regenerate-xlsx for the editable per-tender cost breakdown.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_master_admin
from app.core.config import get_settings
from app.core.database import get_db
from app.models.user import User
from app.services import cost_breakdown_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tenders", tags=["cost-breakdown"])


class CostBreakdownLinePayload(BaseModel):
    """One editable row sent from the frontend editor.

    `rate` / `amount` are the expected band; `rate_low`/`rate_high` and
    `amount_low`/`amount_high` carry the agent's range when supplied.
    `profit_pct` lets the user vary margin per line (falls back to the
    breakdown-level margin_percent on the server if null).

    Phase 3b margin-analysis fields:
      - `tender_rate` / `tender_amount` — rate stated in the tender BOQ
      - `margin_amount_*` / `margin_pct` — tender_amount - amount per band
      - `schedule_section` — grouping label for multi-schedule tenders
      - `cost_buildup_note` — short build-up explanation
    """
    id: Optional[int] = None
    sr_no: Optional[int] = None
    description: str
    category: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    rate: Optional[float] = None
    amount: Optional[float] = None

    # Range fields
    rate_low: Optional[float] = None
    rate_high: Optional[float] = None
    amount_low: Optional[float] = None
    amount_high: Optional[float] = None

    # Per-line profit
    profit_pct: Optional[float] = None
    profit_amount: Optional[float] = None
    profit_amount_low: Optional[float] = None
    profit_amount_high: Optional[float] = None

    # Phase 3b — margin-analysis fields
    tender_rate: Optional[float] = None
    tender_amount: Optional[float] = None
    margin_amount_low: Optional[float] = None
    margin_amount: Optional[float] = None
    margin_amount_high: Optional[float] = None
    margin_pct: Optional[float] = None
    schedule_section: Optional[str] = None
    cost_buildup_note: Optional[str] = None

    rate_source: Optional[str] = None
    source_ref: Optional[str] = None
    confidence: Optional[str] = None
    needs_input: bool = False
    notes: Optional[str] = None

    # NIT-mirror identity and provenance. The editor sends every field it was
    # given; this model used to declare none of these, so Pydantic dropped
    # them on the way in and a single save stripped every line of its link to
    # the captured schedule (boq_item_id, schedule_name, item_code ...), its
    # tax flag and its web source. All optional: an older client that omits
    # them still saves.
    boq_item_id: Optional[int] = None
    item_code: Optional[str] = None
    schedule_name: Optional[str] = None
    bidding_unit: Optional[str] = None
    basic_value: Optional[float] = None
    escalation_pct: Optional[float] = None
    is_tax_line: bool = False
    annexure: Optional[str] = None
    oem_manufacturer: Optional[str] = None
    source_url: Optional[str] = None
    # Annexure component link (see CostBreakdownLine.parent_boq_item_id).
    parent_boq_item_id: Optional[int] = None
    annexure_ref: Optional[str] = None


class CostBreakdownUpdatePayload(BaseModel):
    """PUT body — entire line set is replaced atomically."""
    lines: list[CostBreakdownLinePayload] = Field(default_factory=list)
    overhead_percent: Optional[float] = None
    margin_percent: Optional[float] = None
    gst_percent: Optional[float] = None


class RegenerateXlsxPayload(BaseModel):
    """Optional session id for the regenerated artifact.

    `single_sheet` controls the NIT-mirror XLSX layout: True → all schedules
    stacked into one sheet/tab (exact NIT representation); False → one sheet per
    schedule + Summary; None → use the `costing.nit_single_sheet` default.
    (Ignored by the PDF route.)
    """
    session_id: Optional[int] = None
    single_sheet: Optional[bool] = None
    # Output layout: "nit_mirror" | "margin_analysis" | "client_annexure" | None.
    # When given, overrides the layout stored on the breakdown and is persisted
    # so future regenerations remember the choice. None = use stored / auto.
    cost_sheet_template: Optional[str] = None


@router.get("/{tender_id}/cost-breakdown")
def get_cost_breakdown(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the latest CostBreakdown for a tender, or 404 if none exists yet."""
    breakdown = cost_breakdown_service.get_latest_for_tender(db, tender_id)
    if not breakdown:
        raise HTTPException(status_code=404, detail="No cost breakdown for this tender yet")
    return cost_breakdown_service.to_dict(breakdown)


@router.put("/{tender_id}/cost-breakdown")
def update_cost_breakdown(
    tender_id: int,
    payload: CostBreakdownUpdatePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Replace all lines of the latest CostBreakdown. Recomputes totals."""
    breakdown = cost_breakdown_service.get_latest_for_tender(db, tender_id)
    if not breakdown:
        raise HTTPException(status_code=404, detail="No cost breakdown for this tender yet")

    updated = cost_breakdown_service.replace_lines(
        db=db,
        breakdown_id=breakdown.id,
        lines=[ln.model_dump() for ln in payload.lines],
        edited_by=current_user.id,
        overhead_percent=payload.overhead_percent,
        margin_percent=payload.margin_percent,
        gst_percent=payload.gst_percent,
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update cost breakdown")
    return cost_breakdown_service.to_dict(updated)


@router.post("/{tender_id}/cost-breakdown/regenerate-xlsx")
def regenerate_xlsx(
    tender_id: int,
    payload: RegenerateXlsxPayload = RegenerateXlsxPayload(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Build a fresh XLSX from the current breakdown state and create a new
    cost_breakdown_xlsx artifact in the given (or breakdown's recorded) session.

    Layout is auto-selected (NIT-mirror when the costing carries the bidding
    schedule columns; margin-analysis / single-sheet otherwise). See plan:
    ~/.claude/plans/now-i-need-to-synchronous-taco.md
    """
    breakdown = cost_breakdown_service.get_latest_for_tender(db, tender_id)
    if not breakdown:
        raise HTTPException(status_code=404, detail="No cost breakdown for this tender yet")

    # Persist an explicit layout choice so future regenerations remember it.
    if payload.cost_sheet_template:
        breakdown.cost_sheet_template = payload.cost_sheet_template.strip()
        db.commit()

    info = cost_breakdown_service.regenerate_xlsx_artifact(
        db=db,
        breakdown=breakdown,
        session_id=payload.session_id,
        single_sheet=payload.single_sheet,
        cost_sheet_template=payload.cost_sheet_template,
    )
    if not info:
        raise HTTPException(
            status_code=400,
            detail="Cannot regenerate XLSX — no session is associated with this breakdown",
        )

    # Hand back a link the browser can follow straight to R2. This request has
    # just built the workbook, held every byte of it in memory and uploaded it;
    # without a URL here the client's only way to obtain those bytes is a second
    # request that makes the backend download them back out of R2 and re-stream
    # them — the whole file across the network three times to deliver it once.
    # `None` on the local backend, where the client falls back to /download.
    try:
        from app.services.storage_service import get_storage_service

        info["download_url"] = get_storage_service().get_presigned_url_sync(
            info["file_path"],
            expires_in=get_settings().presigned_download_ttl_seconds,
            response_content_disposition=(
                f'attachment; filename="{info["file_name"]}"'
            ),
        )
    except Exception as e:  # noqa: BLE001 — a link is an optimisation, not the deliverable
        logger.warning(f"[regenerate-xlsx] presign failed for {info.get('file_path')!r}: {e}")
        info["download_url"] = None
    return info


@router.get("/{tender_id}/bidding-schedule")
def get_bidding_schedule(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the captured NIT bidding schedule (BOQItem rows) for a tender.

    Used by the frontend's Tender Schedule side panel to render the verbatim
    NIT layout next to the editable cost breakdown so the user can visually
    confirm the costing 1:1-mirrors the tender.
    """
    from app.models.costing_template import BOQItem

    rows = (
        db.query(BOQItem)
        .filter(BOQItem.tender_id == tender_id)
        .order_by(BOQItem.schedule_name.asc().nullsfirst(), BOQItem.sr_no.asc())
        .all()
    )
    return {
        "rows": [
            {
                "id": r.id,
                "sr_no": r.sr_no,
                "item_code": r.item_code,
                "description": r.description,
                "quantity": r.quantity,
                "unit": r.unit,
                "estimated_rate": r.estimated_rate,
                "basic_value": r.basic_value,
                "escalation_pct": r.escalation_pct,
                "bidding_unit": r.bidding_unit,
                "schedule_name": r.schedule_name,
                "is_tax_line": bool(r.is_tax_line),
            }
            for r in rows
        ]
    }


class PromoteToTrainingPayload(BaseModel):
    """Optional overrides when promoting a tender's BOQ into a training dataset."""
    name: Optional[str] = None
    tags: Optional[list[str]] = None


@router.post("/{tender_id}/promote-to-training")
def promote_to_training(
    tender_id: int,
    payload: PromoteToTrainingPayload = PromoteToTrainingPayload(),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Ingest this tender's priced BOQ as a costing training dataset and assign
    it to the costing_researcher agent.

    Builds a JSONL rate-card from the captured BOQItem rows (NIT benchmark
    rates per line item), creates a TrainingDataset, uploads the file, and
    assigns it to costing_researcher so future similar tenders can retrieve
    these rates via `costing_training_retrieval`. Master-admin only.
    """
    from app.services import training_dataset_service

    try:
        info = training_dataset_service.promote_tender_to_training(
            db,
            tender_id,
            created_by=admin.id,
            name=payload.name,
            tags=payload.tags,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return info


@router.post("/{tender_id}/cost-breakdown/regenerate-pdf")
def regenerate_pdf(
    tender_id: int,
    payload: RegenerateXlsxPayload = RegenerateXlsxPayload(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Render the NIT-mirror cost-breakdown as a PDF and create a new
    `cost_breakdown_pdf` artifact in the given (or breakdown's recorded)
    session. The PDF mirrors the tender's NIT Schedule of Items layout
    exactly — one section per Schedule (A, B, ...), identical columns —
    so it can sit alongside the source NIT for procurement / signing.
    """
    from app.services.cost_breakdown_pdf_service import regenerate_pdf_artifact

    breakdown = cost_breakdown_service.get_latest_for_tender(db, tender_id)
    if not breakdown:
        raise HTTPException(status_code=404, detail="No cost breakdown for this tender yet")

    info = regenerate_pdf_artifact(
        db=db,
        breakdown=breakdown,
        session_id=payload.session_id,
    )
    if not info:
        raise HTTPException(
            status_code=400,
            detail="Cannot regenerate PDF — no session is associated with this breakdown",
        )
    return info
