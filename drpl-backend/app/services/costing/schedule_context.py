"""What each schedule of a tender prices, read from its printed banner.

An IREPS NIT prints one banner per schedule, and the banner is the only place
that says what the schedule's rows are. The Liluah Mid-Life Rehabilitation NIT
lists "Web to Drg. No. LE11185" twice: in schedule A ("(Mechanical) Cost of
Material ...") at Rs 279.66, the web itself, and in schedule B ("COST OF
LABOUR ...") at Rs 2,239.21, the labour to cut out the corroded one and weld
the new one in. Both banners end "(INCLUSIVE OF ALL TAXES AND CHARGES)", so
both published rates carry GST; the electrical schedules' banners do not say.

The capture stores the banner as `BOQScheduleTotal.title`. A tender captured
before it did has no title; `schedule_contexts` then reads the banners from
the tender's IREPS-format document once and stores them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MATERIAL = "material"
LABOUR = "labour"
MATERIAL_AND_LABOUR = "material_and_labour"

_LABOUR_RE = re.compile(r"\blabou?r\b|\bworkmanship\b", re.IGNORECASE)
_MATERIAL_RE = re.compile(r"\bmaterials?\b|\bspares?\b", re.IGNORECASE)
_TAX_INCLUSIVE_RE = re.compile(
    r"\binclusive\s+of\s+(?:all\s+)?(?:taxes|gst|duties)", re.IGNORECASE,
)
_TAX_EXCLUSIVE_RE = re.compile(
    r"\bexclusive\s+of\s+(?:all\s+)?(?:taxes|gst)|\b(?:gst|taxes?)\s+(?:extra|additional"
    r"|shall\s+be\s+paid|will\s+be\s+paid)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScheduleContext:
    code: str
    title: str
    #: MATERIAL, LABOUR, MATERIAL_AND_LABOUR, or None when the banner does not say.
    work: Optional[str]
    #: True / False when the banner says whether its rates include taxes, else None.
    taxes_inclusive: Optional[bool]

    def plain_work(self) -> str:
        return {
            MATERIAL: "the cost of material: the contractor supplies the item",
            LABOUR: ("the cost of labour: the contractor's workmen do the job; the item "
                     "itself is priced elsewhere"),
            MATERIAL_AND_LABOUR: "material and labour together",
        }.get(self.work or "", "not stated by the banner; read each row")


def classify_title(title: str) -> tuple[Optional[str], Optional[bool]]:
    """(work, taxes_inclusive) as the banner states them."""
    t = title or ""
    labour = bool(_LABOUR_RE.search(t))
    material = bool(_MATERIAL_RE.search(t))
    if labour and material:
        work = MATERIAL_AND_LABOUR
    elif labour:
        work = LABOUR
    elif material:
        work = MATERIAL
    else:
        work = None
    if _TAX_INCLUSIVE_RE.search(t):
        taxes = True
    elif _TAX_EXCLUSIVE_RE.search(t):
        taxes = False
    else:
        taxes = None
    return work, taxes


def _context(code: str, title: str) -> ScheduleContext:
    work, taxes = classify_title(title)
    return ScheduleContext(code=code, title=title, work=work, taxes_inclusive=taxes)


def _titles_from_documents(db: Session, tender_id: int) -> dict[str, str]:
    """Banners read from the tender's IREPS-format document, or {}."""
    from app.models.tender import TenderDocument
    from app.services.boq_parser_service import _extract_text_pymupdf
    from app.services.costing.nit_schedule_parser import (
        is_ireps_schedule_format,
        schedule_titles,
    )
    from app.services.storage_service import get_storage_service

    try:
        docs = (
            db.query(TenderDocument.file_path)
            .filter(
                TenderDocument.tender_id == tender_id,
                TenderDocument.parent_document_id.is_(None),
            )
            .order_by(TenderDocument.id.asc())
            .all()
        )
    except Exception as e:
        logger.debug(f"[schedule context] tender {tender_id}: documents unreadable: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return {}
    storage = get_storage_service()
    for (key,) in docs:
        if not key or not str(key).lower().endswith(".pdf"):
            continue
        try:
            with storage.as_local_file(key, suffix=".pdf") as local_path:
                text = _extract_text_pymupdf(local_path)
        except Exception as e:
            logger.debug(f"[schedule context] tender {tender_id}: {key} unreadable: {e}")
            continue
        if is_ireps_schedule_format(text):
            titles = {c: t for c, t in schedule_titles(text).items() if t}
            if titles:
                return titles
    return {}


def schedule_contexts(db: Session, tender_id: int, *, read_documents: bool = True) -> dict[str, ScheduleContext]:
    """{schedule code: ScheduleContext} for the tender's captured schedules.

    Stored banners first; a tender whose capture predates them has its
    IREPS document read once (and the banners stored) when `read_documents`.
    """
    from app.models.costing_template import BOQScheduleTotal

    try:
        rows = (
            db.query(BOQScheduleTotal)
            .filter(BOQScheduleTotal.tender_id == tender_id)
            .all()
        )
    except Exception as e:
        logger.debug(f"[schedule context] tender {tender_id}: totals unreadable: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return {}
    stored = {
        (r.schedule_code or "").strip().upper(): (r.title or "").strip()
        for r in rows if (r.schedule_code or "").strip()
    }
    if any(stored.values()) or not read_documents:
        return {c: _context(c, t) for c, t in stored.items() if t}

    titles = _titles_from_documents(db, tender_id)
    if not titles:
        return {}
    try:
        by_code = {(r.schedule_code or "").strip().upper(): r for r in rows}
        for code, title in titles.items():
            row = by_code.get(code.upper())
            if row is not None and not row.title:
                row.title = title
        db.commit()
    except Exception as e:
        logger.debug(f"[schedule context] tender {tender_id}: could not store banners: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    return {c.upper(): _context(c.upper(), t) for c, t in titles.items()}
